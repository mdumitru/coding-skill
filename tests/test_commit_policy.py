"""Mechanical commit guards, including force-staged tracking files."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).parents[1] / 'skills/agent-baseline/scripts/commit_policy.py'
RUNTIME = Path(__file__).parents[1] / 'skills/task-workflow/scripts/policy.py'
spec = importlib.util.spec_from_file_location('commit_policy', HELPER)
assert spec and spec.loader
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class CommitPolicyTests(unittest.TestCase):
    def test_policy_copies_are_identical(self) -> None:
        self.assertEqual(HELPER.read_bytes(), RUNTIME.read_bytes())

    def test_message_boundaries_and_explicit_body_exception(self) -> None:
        for valid in ('fix input validation', 'a' * 80, 'fix input\n'):
            self.assertIsNone(policy.subject_error(valid))
        for invalid in ('', 'Fix input', '1fix input', 'a' * 81, 'fix input\n\nbody', 'fix input\n\n', 'fix input '):
            with self.subTest(invalid=invalid):
                self.assertIsNotNone(policy.subject_error(invalid))
        self.assertIsNone(policy.subject_error('fix input\n\nexplain the change', allow_body=True))
        for trailer in ('Signed-off-by: Name', 'Co-Authored-By: Name', 'Generated with Codex', 'Reviewed-by: Name'):
            self.assertIsNotNone(policy.subject_error('fix input\n\n' + trailer, allow_body=True))

    def test_force_staged_files_and_extra_patterns_are_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            (repo / '.gitignore').write_text('_plans/\nTODO*.md\n')
            (repo / '_plans').mkdir()
            for name in ('_plans/task.md', 'TODO-work.md', 'todo-private.md', 'safe.py'):
                (repo / name).write_text('fixture')
            subprocess.run(['git', 'add', '-f', '.'], cwd=repo, check=True)
            before = subprocess.check_output(['git', 'diff', '--cached'], cwd=repo)
            result = subprocess.run([sys.executable, str(HELPER), 'paths', '--repo', str(repo)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('TODO-work.md', result.stderr)
            self.assertIn('_plans/task.md', result.stderr)
            self.assertEqual(before, subprocess.check_output(['git', 'diff', '--cached'], cwd=repo))
            self.assertTrue(policy.forbidden_path('safe.py', ['*.py']))
            self.assertFalse(policy.forbidden_path('safe.py'))

    def test_cli_diagnostics_and_body_override(self) -> None:
        result = subprocess.run([sys.executable, str(HELPER), 'message', '--message', 'Bad subject'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('lowercase', result.stderr)
        result = subprocess.run([sys.executable, str(HELPER), 'message', '--message', 'fix input\n\nbody', '--allow-body'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
