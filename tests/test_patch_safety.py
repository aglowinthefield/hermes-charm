"""Exercise the startup entry point against real Git files, never the live install."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parents[1] / 'scripts'


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.agent = self.root / 'agent'
        self.agent.mkdir()
        self.patches = self.root / 'patches'
        self.patches.mkdir()
        self.env = {**os.environ, 'LOCAL_PATCHES_AGENT_DIR': str(self.agent),
                    'LOCAL_PATCHES_DIR': str(self.patches),
                    'LOCAL_PATCHES_STATE': str(self.root / 'state/status'),
                    'LOCAL_PATCHES_NOTIFY': '/usr/bin/true'}
        self.git('init', '-q')
        (self.agent / 'f.txt').write_text('one\ntwo\nthree\n')
        self.git('add', '--', 'f.txt')
        (self.agent / 'f.txt').write_text('one\nTWO\nthree\n')
        self.good = self.git('diff').stdout
        (self.agent / 'f.txt').write_text('one\ntwo\nthree\n')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.agent), *args],
                              text=True, capture_output=True, check=True)

    def configure(self, entries):
        (self.patches / 'ACTIVE').write_text('\n'.join(entries) + '\n')
        pins = {}
        for name, patch in entries.items():
            (self.patches / (name + '.patch')).write_text(patch)
            pins[name] = hashlib.sha256(patch.encode()).hexdigest()
        (self.patches / 'pins.json').write_text(json.dumps(pins))

    def run_manager(self, *args):
        return subprocess.run(['sh', str(HERE / 'local-patches-ensure.sh'), *args],
                              env=self.env, text=True, capture_output=True)

    def test_failed_batch_never_applies_earlier_patch(self):
        self.configure({'good': self.good, 'stale': self.good.replace('-two', '-absent').replace('+TWO', '+OTHER')})
        result = self.run_manager()
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\ntwo\nthree\n')
        self.assertEqual(self.git('diff', '--cached').stdout.count('TWO'), 0)


    def test_rollback_restores_only_unchanged_installed_bytes(self):
        self.configure({'good': self.good})
        first = self.run_manager()
        self.assertEqual(first.returncode, 0, first.stderr)
        backup = json.loads(first.stdout)['backup']
        rolled_back = self.run_manager('--rollback', backup)
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\ntwo\nthree\n')
        installed = self.run_manager()
        backup = json.loads(installed.stdout)['backup']
        (self.agent / 'f.txt').write_text('operator changed this')
        refused = self.run_manager('--rollback', backup)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'operator changed this')

    def test_corrupt_backup_refuses_rollback(self):
        self.configure({'good': self.good})
        report = json.loads(self.run_manager().stdout)
        backup = Path(report['backup'])
        (backup / 'before/f.txt').write_text('corrupt backup')
        result = self.run_manager('--rollback', str(backup))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\nTWO\nthree\n')

    def test_check_does_not_apply(self):
        self.configure({'good': self.good})
        result = self.run_manager('--check')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['pending'], ['good'])
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\ntwo\nthree\n')

    def test_apply_is_idempotent_preserves_other_edits_and_index(self):
        self.configure({'good': self.good})
        (self.agent / 'unrelated').write_text('operator edit')
        index = (self.agent / '.git/index').read_bytes()
        first = self.run_manager()
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(first.stdout)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\nTWO\nthree\n')
        self.assertEqual((Path(report['backup']) / 'before/f.txt').read_text(), 'one\ntwo\nthree\n')
        second = self.run_manager()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)['pending'], [])
        self.assertEqual((self.agent / '.git/index').read_bytes(), index)
        self.assertEqual((self.agent / 'unrelated').read_text(), 'operator edit')

    def test_changed_artifact_is_refused(self):
        self.configure({'good': self.good})
        (self.patches / 'good.patch').write_text(self.good.replace('+TWO', '+OTHER'))
        result = self.run_manager()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unreviewed', result.stderr)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\ntwo\nthree\n')

    def test_symlink_is_refused_without_touching_referent(self):
        self.configure({'good': self.good})
        target = self.root / 'outside'
        target.write_text('one\ntwo\nthree\n')
        (self.agent / 'f.txt').unlink()
        (self.agent / 'f.txt').symlink_to(target)
        result = self.run_manager()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Symlink', result.stderr)
        self.assertEqual(target.read_text(), 'one\ntwo\nthree\n')

    def test_unsafe_names_and_paths_are_refused(self):
        for bad in ('../f.txt', '.git/config', '/outside'):
            with self.subTest(path=bad):
                self.configure({'good': self.good.replace('f.txt', bad)})
                result = self.run_manager()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Unsafe patch path', result.stderr)
        (self.patches / 'ACTIVE').write_text('../good\n')
        self.assertNotEqual(self.run_manager().returncode, 0)

    def test_unified_patch_format_is_supported(self):
        patch = '\n'.join(self.good.splitlines()[2:]) + '\n'
        self.configure({'unified': patch})
        result = self.run_manager()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\nTWO\nthree\n')

    def test_overlapping_context_requires_one_bundle(self):
        second = self.good.replace('-two', '-TWO').replace('+TWO', '+OTHER')
        self.configure({'first': self.good, 'second': second})
        result = self.run_manager()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bundle overlapping', result.stderr)
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\ntwo\nthree\n')

    def test_failed_write_restores_earlier_files(self):
        import local_patches
        from unittest.mock import patch
        (self.agent / 'g.txt').write_text('old\n')
        self.git('add', '--', 'g.txt')
        (self.agent / 'f.txt').write_text('one\nTWO\nthree\n')
        (self.agent / 'g.txt').write_text('new\n')
        self.configure({'both': self.git('diff').stdout})
        (self.agent / 'f.txt').write_text('one\ntwo\nthree\n')
        (self.agent / 'g.txt').write_text('old\n')
        real_put = local_patches.put
        def fail_second(root, name, value):
            if root == self.agent and name == 'g.txt':
                raise OSError('injected write failure')
            return real_put(root, name, value)
        with patch.object(local_patches, 'put', side_effect=fail_second):
            with self.assertRaises(OSError):
                local_patches.ensure(self.agent, self.patches, self.root / 'state/status')
        self.assertEqual((self.agent / 'f.txt').read_text(), 'one\ntwo\nthree\n')
        self.assertEqual((self.agent / 'g.txt').read_text(), 'old\n')

    def test_missing_pin_is_refused(self):
        self.configure({'good': self.good})
        (self.patches / 'pins.json').unlink()
        self.assertNotEqual(self.run_manager().returncode, 0)


class InventoryTests(unittest.TestCase):
    def test_active_inventory_has_pinned_parseable_artifacts(self):
        import local_patches
        root = HERE.parent
        entries, paths = local_patches.load_patches(root, root / 'local-patches')
        self.assertTrue(entries)
        self.assertTrue(paths)
        self.assertEqual(len(entries), len({entry[0] for entry in entries}))


if __name__ == '__main__':
    unittest.main()
