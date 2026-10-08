#!/usr/bin/env python3
"""Keep the core patch bundle in step with upstream Hermes.

verify   Apply ACTIVE, in order, to a pristine shared clone at --ref.
         Read-only for the agent checkout.
rebase   Same walk, but a patch that no longer applies is retried with
         `git apply --3way`. A clean merge regenerates that artifact from the
         resulting tree, new files included, and refreshes its pin when --write
         is given. A real conflict stops the walk and keeps the clone for a
         hand port (exit 2).

--tests runs every test file the bundle carries in the resulting tree.

Neither command writes to the agent checkout or its repository, and neither
retires a patch: a conflict is reported, never skipped.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

TEST_PATH = re.compile(r'^\+\+\+ b/(tests/\S+\.py)$', re.M)


class SeriesError(RuntimeError):
    pass


def git(cwd, *args, check=True, input_data=None):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            input=input_data, timeout=300)
    if check and result.returncode:
        raise SeriesError(f"git {' '.join(args)}: {result.stderr.decode(errors='replace').strip()}")
    return result


def load(directory):
    names = [line.strip() for line in (directory / 'ACTIVE').read_text().splitlines()
             if line.strip() and not line.lstrip().startswith('#')]
    pins = json.loads((directory / 'pins.json').read_text())
    patches = []
    for name in names:
        data = (directory / f'{name}.patch').read_bytes()
        if pins.get(name) != hashlib.sha256(data).hexdigest():
            raise SeriesError(f'Pin mismatch for {name}; refusing an unreviewed artifact')
        patches.append((name, data))
    return patches


def scratch_base():
    """Hermes tests refuse file I/O under the real home, so never test from inside it."""
    base = Path(os.environ.get('TMPDIR') or tempfile.gettempdir()).resolve()
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).resolve()
    if base == home or base.is_relative_to(home):
        cache = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache')
        base = cache / 'charm-series'
        base.mkdir(parents=True, exist_ok=True)
    return str(base)


def snapshot(tree):
    """Stage everything so the next diff starts from the bundle-so-far."""
    git(tree, 'add', '-A')
    return git(tree, 'write-tree').stdout.decode().strip()


def walk(agent, ref, directory, rebase=False, keep=False):
    patches = load(directory)
    root = Path(tempfile.mkdtemp(prefix='charm-series-', dir=scratch_base()))
    tree = root / 'agent'
    commit = git(agent, 'rev-parse', '--verify', f'{ref}^{{commit}}').stdout.decode().strip()
    # A shared clone borrows objects but owns its git dir, so nothing is registered in the
    # agent repository and tests never resolve paths back into the live checkout.
    git(root, 'clone', '-q', '--shared', '--no-checkout', str(agent), str(tree))
    git(tree, 'checkout', '-q', '--detach', commit)
    results, regenerated, failed = [], {}, False
    try:
        base = snapshot(tree)
        for name, data in patches:
            row = {'name': name}
            if git(tree, 'apply', '--check', input_data=data, check=False).returncode == 0:
                git(tree, 'apply', input_data=data)
                row['state'] = 'applies'
            elif rebase:
                merged = git(tree, 'apply', '--3way', input_data=data, check=False)
                conflicts = git(tree, 'diff', '--name-only', '--diff-filter=U').stdout.decode().split()
                if merged.returncode or conflicts:
                    row.update(state='conflict', files=conflicts,
                               detail=merged.stderr.decode(errors='replace').strip()[-600:])
                    results.append(row)
                    failed = True
                    keep = True
                    break
                row['state'] = 'merged'
            else:
                detail = git(tree, 'apply', '--check', input_data=data, check=False).stderr
                row.update(state='stale', detail=detail.decode(errors='replace').strip()[-600:])
                results.append(row)
                failed = True
                continue
            after = snapshot(tree)
            if row['state'] == 'merged':
                regenerated[name] = git(tree, 'diff', '--no-color', '--no-ext-diff', '--src-prefix=a/',
                                        '--dst-prefix=b/', base, after).stdout
            base = after
            results.append(row)
        return {'ref': git(tree, 'rev-parse', 'HEAD').stdout.decode().strip(), 'ok': not failed,
                'patches': results, 'clone': str(tree) if keep else None}, regenerated, tree
    except BaseException:
        keep = False
        raise
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)


def bundle_tests(directory):
    found = []
    for name, data in load(directory):
        for path in TEST_PATH.findall(data.decode(errors='replace')):
            if path not in found:
                found.append(path)
    return found


def run_tests(tree, python, files):
    runner = tree / 'scripts/run_tests.sh'
    command = [str(runner), *files, '-q'] if runner.exists() else [python, '-m', 'pytest', '-q', *files]
    env = {**os.environ}
    if python and Path(python).exists():
        env['PATH'] = f'{Path(python).parent}{os.pathsep}{env["PATH"]}'
        env['HERMES_PYTHON'] = python
    result = subprocess.run(command, cwd=tree, env=env, stdout=sys.stderr, timeout=1800)
    return result.returncode == 0


def write_regenerated(directory, regenerated):
    pins_path = directory / 'pins.json'
    pins = json.loads(pins_path.read_text())
    for name, data in regenerated.items():
        (directory / f'{name}.patch').write_bytes(data)
        pins[name] = hashlib.sha256(data).hexdigest()
    pins_path.write_text(json.dumps(pins, indent=2) + '\n')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('command', choices=['verify', 'rebase'])
    home = Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes')))
    parser.add_argument('--agent', type=Path, default=home / 'hermes-agent',
                        help='Hermes Git checkout to borrow objects from (default: active home)')
    parser.add_argument('--ref', default='HEAD', help='Upstream revision to test against')
    parser.add_argument('--patches', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'local-patches')
    parser.add_argument('--tests', action='store_true', help='Also run every test file the bundle carries in the resulting tree')
    parser.add_argument('--python', help='Interpreter whose bin dir runs the tests (default: <agent>/.venv)')
    parser.add_argument('--write', action='store_true', help='rebase: rewrite merged artifacts and pins')
    args = parser.parse_args(argv)
    agent, directory = args.agent.expanduser().resolve(), args.patches.resolve()
    try:
        rebase = args.command == 'rebase'
        report, regenerated, tree = walk(agent, args.ref, directory, rebase=rebase, keep=args.tests)
        conflict = any(row['state'] == 'conflict' for row in report['patches'])
        if args.tests and report['clone'] and not conflict:
            try:
                if report['ok']:
                    venv = agent / '.venv'
                    if venv.exists() and not (tree / '.venv').exists():
                        (tree / '.venv').symlink_to(venv)
                    report['tests'] = bundle_tests(directory)
                    python = args.python or str(venv / 'bin/python')
                    report['tests_ok'] = run_tests(tree, python, report['tests'])
                    report['ok'] = report['tests_ok']
            finally:
                shutil.rmtree(tree.parent, ignore_errors=True)
                report['clone'] = None
        if rebase:
            report['regenerated'] = sorted(regenerated)
            if regenerated and report['ok'] and args.write:
                write_regenerated(directory, regenerated)
                report['written'] = True
        print(json.dumps(report, indent=2))
        return 0 if report['ok'] else (2 if conflict else 1)
    except (OSError, ValueError, SeriesError, subprocess.SubprocessError) as exc:
        print(f'patch-series: REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
