"""Precise source lint diagnostics and non-mutating install preflight."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_installer import IsolatedHome, REPOSITORY

LINTER = REPOSITORY / 'scripts/lint_skills.py'
spec = importlib.util.spec_from_file_location('skill_lint', LINTER)
assert spec and spec.loader
lint_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lint_module
spec.loader.exec_module(lint_module)


class SkillLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.skill = self.root / 'skills/example'
        self.skill.mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('---\nname: example\ndescription: Use when asked for an example.\n---\nRead `reference.md`.\n')
        (self.skill / 'reference.md').write_text('Reference.\n')

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def codes(self) -> set[str]:
        return {d.code for d in lint_module.lint(self.root)}

    def test_repository_and_valid_fixtures_pass(self) -> None:
        self.assertEqual(lint_module.lint(REPOSITORY), [])
        self.assertEqual(self.codes(), set())

    def test_frontmatter_names_and_duplicates(self) -> None:
        other = self.root / 'skills/other'
        other.mkdir()
        (other / 'SKILL.md').write_text('---\nname: example\ndescription: duplicate\n---\n')
        self.assertEqual(self.codes(), {'name-mismatch', 'duplicate-name'})
        (other / 'SKILL.md').write_text('---\nname: other\nname: other\ndescription: |\n---\n')
        self.assertIn('frontmatter', self.codes())
        (other / 'SKILL.md').write_text('name: other\n')
        self.assertIn('frontmatter', self.codes())

    def test_missing_and_escaped_references_but_examples_and_routing_names_are_ignored(self) -> None:
        document = self.skill / 'reference.md'
        document.write_text('Read `missing.md` and [escape](<../../outside.md>).\n')
        self.assertEqual(self.codes(), {'missing-reference', 'reference-escape'})
        document.write_text('Use `agent-baseline`, `AGENTS.md`, and `_plans/example.md`.\n[external](https://example.invalid/docs)\n```\nRead `missing.md`.\n```\n')
        self.assertEqual(self.codes(), set())
        (self.skill / 'linked.md').symlink_to(self.root / 'outside.md')
        self.assertIn('symlink', self.codes())

    def test_python_scripts_imports_shebang_and_executable_bits(self) -> None:
        scripts = self.skill / 'scripts'
        scripts.mkdir()
        helper = scripts / 'helper.py'
        helper.write_text('import requests\nfrom other_skill import helper\n')
        self.assertEqual(self.codes(), {'external-import', 'shebang', 'executable'})
        helper.write_text('#!/usr/bin/env python3\nimport json\nfrom sibling import value\n')
        helper.chmod(0o755)
        sibling = scripts / 'sibling.py'
        sibling.write_text('#!/usr/bin/env python3\nvalue = 1\n')
        sibling.chmod(0o755)
        self.assertEqual(self.codes(), set())
        helper.write_text('#!/usr/bin/env python3\nfrom ...outside import value\n')
        self.assertIn('import-escape', self.codes())
        cache = scripts / '__pycache__'
        cache.mkdir()
        (cache / 'generated.pyc').write_bytes(b'ignored')
        (scripts / 'generated.pyo').write_bytes(b'ignored')
        helper.write_text('#!/usr/bin/env python3\nimport os\n')
        self.assertEqual(self.codes(), set())

    def test_stable_json_diagnostics(self) -> None:
        (self.skill / 'reference.md').write_text('Read `missing.md`.\n')
        result = subprocess.run([sys.executable, str(LINTER), '--root', str(self.root), '--json'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data, [{'path': 'skills/example/reference.md', 'code': 'missing-reference', 'message': 'local reference does not exist: missing.md'}])

    def test_invalid_source_preflight_changes_no_targets_and_uninstall_remains_usable(self) -> None:
        source = self.root / 'source'
        shutil.copytree(REPOSITORY, source, ignore=shutil.ignore_patterns('.git', '__pycache__', '*.pyc', '_plans'))
        (source / 'skills/agent-baseline/SKILL.md').write_text('invalid frontmatter\n')
        home = IsolatedHome(self.root / 'home')
        home.root.mkdir()
        home.initialize_targets()
        for args in ([], ['--dry-run']):
            result = subprocess.run([str(source / 'install.sh'), *args], env=home.environment(), capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('source skill preflight failed', result.stderr)
            self.assertEqual(list(home.claude.iterdir()), [])
            self.assertEqual(list(home.codex.iterdir()), [])
            self.assertFalse(home.backups.exists())
        result = subprocess.run([str(source / 'install.sh'), '--uninstall'], env=home.environment(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_copied_helpers_run_without_source_or_sibling_skill_imports(self) -> None:
        home = IsolatedHome(self.root / 'home')
        home.root.mkdir()
        home.initialize_targets()
        home.run()
        for target in (home.claude, home.codex):
            scripts = target / 'skills/task-workflow/scripts'
            repo = self.root / target.name
            repo.mkdir()
            subprocess.run(['git', 'init', '-q'], cwd=repo, check=True)
            env = {**os.environ, 'PYTHONPATH': '', 'PYTHONDONTWRITEBYTECODE': '1'}
            result = subprocess.run([sys.executable, str(scripts / 'workflow.py'), '--help'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            guard = target / 'skills/agent-baseline/scripts/commit_policy.py'
            result = subprocess.run([sys.executable, str(guard), 'message', '--message', 'validate copied helper'], cwd=repo, env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


class RoutingContractTests(unittest.TestCase):
    """Skill descriptions and managed instructions agree; preview skills stay generic."""

    @staticmethod
    def description(skill: str) -> str:
        lines = (REPOSITORY / 'skills' / skill / 'SKILL.md').read_text().splitlines()
        return next(line.removeprefix('description: ') for line in lines if line.startswith('description: '))

    def test_run_trigger_and_combined_mode_agree(self) -> None:
        instructions = (REPOSITORY / 'shared/instructions.md').read_text()
        task = self.description('task-workflow')
        for keyword in ('`plan:`', '`execute:`', '`run:`'):
            self.assertIn(keyword, task)
            self.assertIn(keyword, instructions)
        self.assertIn('beside one\n`execute: <slug>`', instructions)
        self.assertIn('<slug>.localhost', instructions)
        run = (REPOSITORY / 'skills/task-workflow/run-workflow.md').read_text()
        self.assertIn('preview.py intent --execute-slug', run)
        self.assertIn('run-workflow.md', (REPOSITORY / 'skills/task-workflow/execute-workflow.md').read_text())

    def test_finish_teardown_routing_agrees(self) -> None:
        instructions = (REPOSITORY / 'shared/instructions.md').read_text()
        self.assertIn('previews', self.description('worktree-workflow'))
        self.assertIn('tears down the task\'s repository\npreviews', instructions)
        for name in ('SKILL.md', 'worktree-workflow.md'):
            self.assertIn('scripts/finish.py', (REPOSITORY / 'skills/worktree-workflow' / name).read_text())

    def test_preview_skills_contain_no_application_specific_knowledge(self) -> None:
        import re
        forbidden = re.compile(r'(?i)\b(docker|compose|postgres(?:ql)?|mysql|redis|sqlite|npm|yarn|node(?:js)?|django|rails|flask|vite|'
                               r'webpack|kubernetes|kubectl|aws|gcp|azure|supabase|stripe|heroku)\b|localhost:\d|/home/|/Users/')
        for skill in ('task-workflow', 'worktree-workflow'):
            for file in sorted((REPOSITORY / 'skills' / skill).rglob('*')):
                if file.is_file() and file.suffix in {'.md', '.py'}:
                    with self.subTest(file=file.name):
                        self.assertIsNone(forbidden.search(file.read_text()))

    def test_preview_workflow_preserves_shared_dependencies(self) -> None:
        run = (REPOSITORY / 'skills/task-workflow/run-workflow.md').read_text()
        self.assertIn('pre-existing shared services', run)
        self.assertIn('must not restart, reconfigure, or tear down', run)
