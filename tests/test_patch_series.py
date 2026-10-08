"""Patch-series sync against real Git repositories, never the live install."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/patch_series.py'
# Throwaway repositories must not inherit the operator's Git identity or hooks.
ENV = {**os.environ, 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1',
       'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
       'GIT_COMMITTER_EMAIL': 't@t'}
BASE = ''.join(f'line {i}\n' for i in range(1, 21))


class PatchSeriesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.agent = self.root / 'agent'
        self.agent.mkdir()
        self.patches = self.root / 'patches'
        self.patches.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.commit({'f.txt': BASE})

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.agent), *args], env=ENV,
                              text=True, capture_output=True, check=True).stdout

    def commit(self, files):
        for name, text in files.items():
            (self.agent / name).parent.mkdir(parents=True, exist_ok=True)
            (self.agent / name).write_text(text)
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'c')
        return self.git('rev-parse', 'HEAD').strip()

    def diff_for(self, files):
        """A patch against HEAD, produced the way artifacts are produced."""
        before = {n: (self.agent / n).read_text() if (self.agent / n).exists() else None for n in files}
        for name, text in files.items():
            (self.agent / name).parent.mkdir(parents=True, exist_ok=True)
            (self.agent / name).write_text(text)
        self.git('add', '-A')
        patch = self.git('diff', '--cached')
        self.git('reset', '-q', '--hard')
        for name, text in before.items():
            if text is None:
                (self.agent / name).unlink(missing_ok=True)
        return patch

    def configure(self, entries):
        (self.patches / 'ACTIVE').write_text('# order\n' + '\n'.join(entries) + '\n')
        pins = {}
        for name, patch in entries.items():
            (self.patches / f'{name}.patch').write_text(patch)
            pins[name] = hashlib.sha256(patch.encode()).hexdigest()
        (self.patches / 'pins.json').write_text(json.dumps(pins))

    def run_series(self, *args):
        result = subprocess.run([sys.executable, str(SCRIPT), *args, '--agent', str(self.agent),
                                 '--patches', str(self.patches)], env={**ENV, 'TMPDIR': str(self.root), 'HERMES_HOME': str(self.root / 'home')},
                                text=True, capture_output=True)
        report = json.loads(result.stdout) if result.stdout.strip() else {}
        return result, report

    def worktrees(self):
        return self.git('worktree', 'list').strip().splitlines()

    def leftovers(self):
        return sorted(p.name for p in self.root.glob('charm-series-*'))

    def test_verify_reports_each_patch_and_leaves_checkout_alone(self):
        self.configure({'one': self.diff_for({'f.txt': BASE.replace('line 3\n', 'LINE 3\n')})})
        result, report = self.run_series('verify')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p['state'] for p in report['patches']], ['applies'])
        self.assertEqual((self.agent / 'f.txt').read_text(), BASE)
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual(len(self.worktrees()), 1, 'nothing is registered in the agent repository')
        self.assertEqual(self.leftovers(), [])

    def test_verify_fails_on_upstream_drift_without_skipping_later_patches(self):
        stale = self.diff_for({'f.txt': BASE.replace('line 3\n', 'LINE 3\n')})
        later = self.diff_for({'g.txt': 'new\n'})
        self.configure({'stale': stale, 'later': later})
        self.commit({'f.txt': BASE.replace('line 3\n', 'upstream 3\n')})
        result, report = self.run_series('verify')
        self.assertEqual(result.returncode, 1)
        self.assertEqual([p['state'] for p in report['patches']], ['stale', 'applies'])
        with_tests, report = self.run_series('verify', '--tests')
        self.assertEqual(with_tests.returncode, 1)
        self.assertNotIn('tests', report, 'a stale bundle never reaches the test run')
        self.assertEqual(self.leftovers(), [], 'only a conflict keeps its clone')

    def test_rebase_regenerates_a_cleanly_merged_artifact_including_new_files(self):
        old = self.git('rev-parse', 'HEAD').strip()
        patch = self.diff_for({'f.txt': BASE.replace('line 18\n', 'LINE 18\n'), 'new.py': 'x = 1\n'})
        self.configure({'fix': patch})
        upstream = BASE.replace('line 15\n', 'upstream 15\n')
        self.commit({'f.txt': upstream})
        self.assertNotEqual(self.git('rev-parse', 'HEAD').strip(), old)
        dry, report = self.run_series('rebase')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(report['regenerated'], ['fix'])
        self.assertEqual((self.patches / 'fix.patch').read_text(), patch, 'dry run must not write')
        written, report = self.run_series('rebase', '--write')
        self.assertEqual(written.returncode, 0, written.stderr)
        artifact = (self.patches / 'fix.patch').read_bytes()
        self.assertEqual(json.loads((self.patches / 'pins.json').read_text())['fix'],
                         hashlib.sha256(artifact).hexdigest())
        self.assertIn(b'new file mode', artifact)
        again, report = self.run_series('verify')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(report['patches'][0]['state'], 'applies')

    def test_rebase_stops_on_a_real_conflict_and_keeps_the_clone(self):
        original = self.diff_for({'f.txt': BASE.replace('line 3\n', 'LINE 3\n')})
        self.configure({'fix': original})
        self.commit({'f.txt': BASE.replace('line 3\n', 'upstream 3\n')})
        result, report = self.run_series('rebase', '--write')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(report['patches'][0]['state'], 'conflict')
        self.assertTrue((Path(report['clone']) / 'f.txt').is_file())
        self.assertEqual(self.leftovers(), [Path(report['clone']).parent.name])
        self.assertEqual(len(self.worktrees()), 1)
        self.assertEqual((self.patches / 'fix.patch').read_text(), original,
                         'conflicts never rewrite artifacts')

    def test_scratch_clone_never_lands_inside_the_hermes_home(self):
        home = self.root / 'home'
        (home / 'scratch').mkdir(parents=True)
        cache = self.root / 'cache'
        self.configure({'fix': self.diff_for({'f.txt': BASE.replace('line 3\n', 'LINE 3\n')})})
        self.commit({'f.txt': BASE.replace('line 3\n', 'upstream 3\n')})
        result = subprocess.run([sys.executable, str(SCRIPT), 'rebase', '--agent', str(self.agent),
                                 '--patches', str(self.patches)], text=True, capture_output=True,
                                env={**ENV, 'HERMES_HOME': str(home), 'TMPDIR': str(home / 'scratch'),
                                     'XDG_CACHE_HOME': str(cache)})
        clone = Path(json.loads(result.stdout)['clone'])
        self.assertTrue(clone.is_relative_to(cache / 'charm-series'), clone)
        self.assertEqual(list((home / 'scratch').iterdir()), [])

    def test_unpinned_artifact_is_refused(self):
        self.configure({'fix': self.diff_for({'f.txt': BASE.replace('line 3\n', 'LINE 3\n')})})
        (self.patches / 'fix.patch').write_text((self.patches / 'fix.patch').read_text() + ' ')
        result, _ = self.run_series('verify')
        self.assertEqual(result.returncode, 1)
        self.assertIn('Pin mismatch', result.stderr)


if __name__ == '__main__':
    unittest.main()
