"""Exercise Charm from an isolated home without user-home helper scripts."""
import argparse
import importlib.util
import json
import os
from pathlib import Path

import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CharmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'home'
        self.home.mkdir()
        self.agent = self.home / 'hermes-agent'
        self.agent.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.agent)], check=True)
        self.env = {**os.environ, 'HERMES_HOME': str(self.home)}
        for key in tuple(self.env):
            if key.startswith('LOCAL_PATCHES_'):
                del self.env[key]

    def run_charm(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'charm.py'), *args],
                              env=self.env, capture_output=True, text=True)

    def test_clean_home_check_reports_missing_patch_targets_without_helper_dependency(self):
        result = self.run_charm('check')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('REFUSED', result.stderr)
        self.assertNotIn('local_patches.py', result.stderr)
        self.assertFalse((self.home / 'scripts').exists())
        self.assertFalse((self.home / 'state/local-patches.status').exists())

    def test_register_only_adds_read_only_cli_and_does_not_touch_checkout(self):
        spec = importlib.util.spec_from_file_location('charm_test_plugin', ROOT / '__init__.py',
                                                    submodule_search_locations=[str(ROOT)])
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        commands = []
        class Context:
            def register_cli_command(self, **kwargs):
                commands.append(kwargs)
        before = list(self.agent.iterdir())
        module.register(Context())
        self.assertEqual([c['name'] for c in commands], ['charm'])
        parser = argparse.ArgumentParser()
        commands[0]['setup_fn'](parser)
        args = parser.parse_args(['check'])
        self.assertTrue(callable(args.func))
        self.assertEqual(list(self.agent.iterdir()), before)

    def test_startup_guard_stops_before_apply_when_native_cli_is_unavailable(self):
        launcher = self.home / 'hermes'
        launcher.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HERMES_HOME/calls"\nexit 23\n')
        launcher.chmod(0o755)
        result = subprocess.run(['sh', str(ROOT / 'scripts/charm-prestart.sh'), str(launcher)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual((self.home / 'calls').read_text().splitlines(), ['charm check'])
        self.assertFalse((self.home / 'state/local-patches.status').exists())


if __name__ == '__main__':
    unittest.main()
