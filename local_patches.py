#!/usr/bin/env python3
"""Preflight the entire pinned patch set before changing a live checkout.

No network, subprocess shell, index writes, resets, or automatic retirement.
A lock serializes our writers; byte/mode comparisons fence unrelated edits.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re

import stat
import subprocess
import tempfile
import time
import uuid


class UnsafePatch(RuntimeError):
    pass


def git(root, *args, required=True, input_data=None):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            timeout=60, input=input_data)
    if required and result.returncode:
        raise UnsafePatch(result.stderr.decode(errors='replace').strip())
    return result


def snapshot(root, name):
    path = root / name
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise UnsafePatch(f'Symlink in patch target: {name}')
    if not path.exists():
        return None
    if not path.is_file():
        raise UnsafePatch(f'Non-file patch target: {name}')
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.stat().st_nlink > 1:
        raise UnsafePatch(f'Hardlinked patch target: {name}')
    return (path.read_bytes(), mode)


def fingerprint(value):
    return None if value is None else [hashlib.sha256(value[0]).hexdigest(), value[1]]


def put(root, name, value):
    path = root / name
    if value is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.local-patch-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(value[0])
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, value[1])
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def safe_target(target):
    parts = PurePosixPath(target).parts
    if (not parts or target.startswith('/') or '\\' in target or '"' in target
            or any(p in ('.', '..', '.git') for p in parts)
            or PurePosixPath(target).as_posix() != target):
        raise UnsafePatch(f'Unsafe patch path: {target}')
    return target


def load_patches(root, directory):
    names = [line.strip() for line in (directory / 'ACTIVE').read_text().splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    if len(names) != len(set(names)):
        raise UnsafePatch('Duplicate ACTIVE entries')
    pins = json.loads((directory / 'pins.json').read_text())
    patches, paths = [], set()
    for name in names:
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', name):
            raise UnsafePatch(f'Invalid ACTIVE name: {name!r}')
        path = directory / (name + '.patch')
        if path.is_symlink():
            raise UnsafePatch(f'Symlinked patch: {name}')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if pins.get(name) != digest:
            raise UnsafePatch(f'Unreviewed or changed patch: {name}')
        # Ask Git to parse both Git diffs and ordinary unified diffs. Never
        # trust only diff headers when deciding which files to preserve.
        rows = git(root, 'apply', '--numstat', '-z', input_data=data).stdout.split(b'\0')
        if not rows[0] or re.search(rb'^(?:rename |copy |GIT binary patch|Binary files)', data, re.M):
            raise UnsafePatch(f'Unsupported patch format: {name}')
        modes = re.findall(rb'^(?:new file mode|deleted file mode|old mode|new mode) (\d+)$', data, re.M)
        if any(mode not in (b'100644', b'100755') for mode in modes):
            raise UnsafePatch(f'Unsafe file mode: {name}')
        for row in filter(None, rows):
            added, removed, path_bytes = row.split(b'\t', 2)
            if not added.isdigit() or not removed.isdigit():
                raise UnsafePatch(f'Unsupported binary patch: {name}')
            paths.add(safe_target(path_bytes.decode('utf-8')))
        patches.append((name, data, digest))
    return patches, sorted(paths)


def preflight(agent, directory, scratch):
    patches, paths = load_patches(agent, directory)
    original = {name: snapshot(agent, name) for name in paths}
    git(scratch, 'init', '-q')
    for name, value in original.items():
        if value is not None:
            put(scratch, name, value)
    pending = []
    states = []
    for i, (name, data, digest) in enumerate(patches):
        patch = scratch.parent / f'{i}.patch'
        patch.write_bytes(data)
        if git(scratch, 'apply', '--check', '-R', str(patch), required=False).returncode == 0:
            states.append({'name': name, 'sha256': digest, 'state': 'applied'})
        else:
            git(scratch, 'apply', '--check', str(patch))
            git(scratch, 'apply', str(patch))
            pending.append(name)
            states.append({'name': name, 'sha256': digest, 'state': 'pending'})
    for i, (name, _, _) in enumerate(patches):
        patch = scratch.parent / f'{i}.patch'
        if git(scratch, 'apply', '--check', '-R', str(patch), required=False).returncode:
            raise UnsafePatch(f'Patch set invalidates {name}; bundle overlapping patches')
    final = {name: snapshot(scratch, name) for name in paths}
    return original, final, states, pending


def ensure(agent, directory, state, check=False):
    state.parent.mkdir(parents=True, exist_ok=True)
    # Lock beside the checkout so callers cannot bypass serialization by choosing
    # a different status file. Updates themselves must not run concurrently.
    with open(agent.parent / (agent.name + '.local-patches.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        head = git(agent, 'rev-parse', '--verify', 'HEAD', required=False).stdout.decode().strip()
        with tempfile.TemporaryDirectory(prefix='patch-preflight-', dir=state.parent) as temp:
            scratch = Path(temp) / 'agent'
            scratch.mkdir()
            original, final, states, pending = preflight(agent, directory, scratch)
        report = {'head': head, 'agent': str(agent), 'checked_at': int(time.time()), 'patches': states,
                  'pending': pending, 'mode': 'check' if check else 'apply', 'ok': True}
        if check:
            return report
        if head != git(agent, 'rev-parse', '--verify', 'HEAD', required=False).stdout.decode().strip():
            raise UnsafePatch('Checkout changed during preflight')
        if any(snapshot(agent, name) != value for name, value in original.items()):
            raise UnsafePatch('Target changed during preflight; retry after writers finish')
        changes = [name for name in original if original[name] != final[name]]
        journal = None
        if changes:
            journal = state.parent / ('patch-backup-' + uuid.uuid4().hex)
            journal.mkdir(mode=0o700)
            for name in changes:
                if original[name] is not None:
                    put(journal / 'before', name, original[name])
                if final[name] is not None:
                    put(journal / 'after', name, final[name])
            put(journal, 'receipt.json', (json.dumps({**report, 'changes': changes,
                'before_hashes': {n: fingerprint(original[n]) for n in changes},
                'after_hashes': {n: fingerprint(final[n]) for n in changes},
                'absent_before': [n for n in changes if original[n] is None],
                'absent_after': [n for n in changes if final[n] is None]}, indent=2).encode(), 0o600))
            written = []
            try:
                for name in changes:
                    if snapshot(agent, name) != original[name]:
                        raise UnsafePatch(f'Concurrent edit: {name}')
                    put(agent, name, final[name])
                    written.append(name)
                if any(snapshot(agent, name) != final[name] for name in original):
                    raise UnsafePatch('Verification changed during installation')
            except BaseException:
                for name in reversed(written):
                    if snapshot(agent, name) == final[name]:
                        put(agent, name, original[name])
                raise
        report['backup'] = str(journal) if journal else None
        put(state.parent, state.name, (json.dumps(report, indent=2).encode() + b'\n', 0o600))
        return report


def rollback(agent, backup, state):
    backup = backup.resolve()
    if backup.parent != state.parent.resolve() or not backup.name.startswith('patch-backup-'):
        raise UnsafePatch('Rollback must name a backup from this status directory')
    with open(agent.parent / (agent.name + '.local-patches.lock'), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = json.loads((backup / 'receipt.json').read_text())
        head = git(agent, 'rev-parse', '--verify', 'HEAD', required=False).stdout.decode().strip()
        if head != receipt['head'] or str(agent) != receipt['agent']:
            raise UnsafePatch('Checkout HEAD changed; do not restore old bytes over an update')
        paths = [safe_target(n) for n in receipt['changes']]
        before = {n: None if n in receipt['absent_before'] else snapshot(backup / 'before', n) for n in paths}
        after = {n: None if n in receipt['absent_after'] else snapshot(backup / 'after', n) for n in paths}
        if any(fingerprint(before[n]) != receipt['before_hashes'][n]
               or fingerprint(after[n]) != receipt['after_hashes'][n] for n in paths):
            raise UnsafePatch('Backup changed or missing; rollback refused')
        if any(snapshot(agent, n) != after[n] for n in paths):
            raise UnsafePatch('Target edited after installation; rollback refused')
        restored = []
        try:
            for n in paths:
                if snapshot(agent, n) != after[n]:
                    raise UnsafePatch(f'Concurrent edit: {n}')
                put(agent, n, before[n])
                restored.append(n)
        except BaseException:
            for n in reversed(restored):
                if snapshot(agent, n) == before[n]:
                    put(agent, n, after[n])
            raise
        report = {'ok': True, 'mode': 'rollback', 'head': head, 'backup': str(backup)}
        put(state.parent, state.name, (json.dumps(report).encode() + b'\n', 0o600))
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Preflight only; never change checkout')
    mode.add_argument('--rollback', type=Path, help='Restore an unchanged installation from its backup')
    args = parser.parse_args()
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    agent = Path(os.environ.get('LOCAL_PATCHES_AGENT_DIR', str(home / 'hermes-agent'))).resolve()
    directory = Path(os.environ.get('LOCAL_PATCHES_DIR', str(Path(__file__).resolve().parent / 'local-patches'))).resolve()
    state = Path(os.environ.get('LOCAL_PATCHES_STATE', str(home / 'state/local-patches.status')))
    try:
        report = rollback(agent, args.rollback, state) if args.rollback else ensure(agent, directory, state, check=args.check)
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, UnsafePatch) as exc:
        print(f'local-patches: REFUSED: {exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
