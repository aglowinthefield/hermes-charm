import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import deployment_settings
spec = importlib.util.spec_from_file_location('sweep_home_paths', ROOT / 'scripts/pr-conflict-sweep.py')
sweep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sweep)

class HomePathTests(unittest.TestCase):
    def test_collector_setting_expands_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'deployment.json'
            path.write_text(json.dumps({'pr-conflict-sweep': {'collector': '~/silk/collect.py'}}))
            with patch.dict(os.environ, {'HERMES_DEPLOYMENT_CONFIG': str(path)}):
                self.assertEqual(deployment_settings.get('pr-conflict-sweep', 'collector'),
                                 str(Path('~/silk/collect.py').expanduser().resolve()))

    def test_sweep_expands_board_workdir_before_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            board = Path(tmp) / 'kanban/boards/silk'
            board.mkdir(parents=True)
            (board / 'board.json').write_text(json.dumps({'default_workdir': '~/silk/silk-remix'}))
            settings = {'repo': 'example/repo', 'board': 'silk', 'park_assignee': 'tom', 'collector': '/collect.py'}
            def run(settings, db, state, cmd):
                cmd(['gh', 'pr', 'list'])
                return ''
            with patch.dict(os.environ, {'HERMES_HOME': tmp}), \
                 patch.object(deployment_settings, 'get', side_effect=lambda section, key: settings[key]), \
                 patch.object(sweep, 'run', side_effect=run), \
                 patch.object(sweep.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='[]', stderr='')) as process, \
                 patch.object(sys, 'argv', ['pr-conflict-sweep.py']):
                self.assertEqual(sweep.main(), 0)
                self.assertEqual(str(process.call_args.kwargs['cwd']), str(Path('~/silk/silk-remix').expanduser()))

if __name__ == '__main__':
    unittest.main()
