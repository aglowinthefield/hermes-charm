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
            (patches / 'ACTIVE').write_text('fixture\n')
            (patches / 'fixture.patch').write_text(diff)
            import hashlib
            (patches / 'pins.json').write_text(json.dumps({'fixture': hashlib.sha256(diff.encode()).hexdigest()}))
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
            assert report['pending'] == ['fixture'], report
            assert (agent / 'sample').read_text() == 'before\n'
            assert not (home / 'scripts').exists()
            guard = subprocess.run(['sh', str(plugin / 'scripts/charm-prestart.sh'), hermes],
                                   env=env, capture_output=True, text=True, check=True, timeout=120)
            assert (agent / 'sample').read_text() == 'after\n', guard.stdout
            call('plugins', 'disable', 'charm')
            denied = subprocess.run(['sh', str(plugin / 'scripts/charm-prestart.sh'), hermes],
                                    env=env, capture_output=True, text=True, timeout=120)
            assert denied.returncode != 0, denied.stdout
            print(f'{label}: native registration, read-only check, startup apply, disabled-plugin refusal passed')


if __name__ == '__main__':
    main()
