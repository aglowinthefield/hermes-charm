"""Run the native discovery path in clean homes, without home-side helper scripts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    hermes = sys.argv[1] if len(sys.argv) > 1 else 'hermes'
    with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as temp:
        base = Path(temp)
        feature = 'kanban-worktree-teardown'
        for label in ('a', 'b', 'a-again'):
            home = base / label
            plugin = home / 'plugins/charm'
            shutil.copytree(root, plugin, ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv'))
            env = {**os.environ, 'HERMES_HOME': str(home)}
            # The runtime code is tested in fresh homes; a fixture patch tests the
            # native command, not the machine-specific eleven-patch overlay.
            agent = home / 'hermes-agent'
            agent.mkdir()
            subprocess.run(['git', 'init', '-q', str(agent)], check=True)
            (agent / 'sample').write_text('before\n')
            subprocess.run(['git', '-C', str(agent), 'add', 'sample'], check=True)
            (agent / 'sample').write_text('after\n')
            diff = subprocess.run(['git', '-C', str(agent), 'diff'], capture_output=True,
                                  text=True, check=True).stdout
            (agent / 'sample').write_text('before\n')
            patches = home / 'fixture-patches'
            patches.mkdir()
            (patches / 'ACTIVE').write_text(feature + '\n')
            (patches / (feature + '.patch')).write_text(diff)
            import hashlib
            (patches / 'pins.json').write_text(json.dumps({feature: hashlib.sha256(diff.encode()).hexdigest()}))
            for key in tuple(env):
                if key.startswith('LOCAL_PATCHES_'):
                    del env[key]
            env['LOCAL_PATCHES_DIR'] = str(patches)
            def call(*args):
                return subprocess.run([hermes, *args], env=env, capture_output=True,
                                      text=True, check=True, timeout=120)
            call('plugins', 'enable', 'charm')
            call('plugins', 'doctor', 'charm', '--ci')
            result = call('charm', 'check')
            report = json.loads(result.stdout)
            assert report['agent'] == str(agent), report
            assert report['pending'] == [feature], report
            assert (agent / 'sample').read_text() == 'before\n'
            assert not (home / 'scripts').exists()
            # Native plugin settings must reach both read-only check and explicit apply.
            call('charm', 'apply')
            assert (agent / 'sample').read_text() == 'after\n'
            call('charm', 'disable', feature)
            listed = json.loads(call('charm', 'list').stdout)
            selected = next(row for row in listed['customizations'] if row['id'] == feature)
            assert selected['desired_enabled'] is False, selected
            assert (agent / 'sample').read_text() == 'after\n'
            removal = json.loads(call('charm', 'check').stdout)
            assert removal['removing'] == [feature], removal
            call('charm', 'apply', '--check')
            assert (agent / 'sample').read_text() == 'after\n'
            subprocess.run(['sh', str(plugin / 'scripts/local-patches-ensure.sh'),
                            '--hermes', hermes], env=env, capture_output=True,
                           text=True, check=True, timeout=120)
            assert (agent / 'sample').read_text() == 'before\n'
            call('charm', 'enable', feature)
            guard = subprocess.run(['sh', str(plugin / 'scripts/charm-prestart.sh'), hermes],
                                   env=env, capture_output=True, text=True, check=True, timeout=120)
            assert (agent / 'sample').read_text() == 'after\n', guard.stdout
            receipt = json.loads((home / 'state/local-patches.status').read_text())['backup']
            call('charm', 'apply', '--rollback', receipt)
            assert (agent / 'sample').read_text() == 'before\n'
            call('charm', 'apply')
            call('charm', 'disable', feature)
            subprocess.run(['sh', str(plugin / 'scripts/charm-prestart.sh'), hermes],
                           env=env, capture_output=True, text=True, check=True, timeout=120)
            assert (agent / 'sample').read_text() == 'before\n', 'guard ignored desired disabled state'
            call('plugins', 'disable', 'charm')
            denied = subprocess.run(['sh', str(plugin / 'scripts/charm-prestart.sh'), hermes],
                                    env=env, capture_output=True, text=True, timeout=120)
            assert denied.returncode != 0, denied.stdout
            print(f'{label}: native settings, read-only checks, toggle apply/remove, startup guard and plugin refusal passed')


if __name__ == '__main__':
    main()
