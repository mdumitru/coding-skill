"""Focused tests for the task-workflow plan-file helper."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import ModuleType


HELPER = (
    Path(__file__).parents[1]
    / "skills"
    / "task-workflow"
    / "scripts"
    / "plan_file.py"
)


def load_helper() -> ModuleType:
    """Load the helper whose skill directory is not an importable package."""

    spec = importlib.util.spec_from_file_location("plan_file", HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper from {HELPER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


plan_file = load_helper()


def run_git(*args: str, cwd: Path) -> None:
    """Run Git quietly and fail the test with its diagnostics."""

    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


class RepositoryFixture:
    """Create disposable plain and faur-style Git repositories."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def plain(self) -> Path:
        repository = self.root / "plain"
        repository.mkdir()
        run_git("init", "--quiet", cwd=repository)
        return repository

    def faur(self) -> tuple[Path, Path]:
        seed = self.root / "seed"
        seed.mkdir()
        run_git("init", "--quiet", "--initial-branch=main", cwd=seed)
        run_git("config", "user.name", "Plan Helper Tests", cwd=seed)
        run_git("config", "user.email", "tests@example.invalid", cwd=seed)
        (seed / "README.md").write_text("fixture\n", encoding="utf-8")
        run_git("add", "README.md", cwd=seed)
        run_git("commit", "--quiet", "-m", "create fixture", cwd=seed)

        workspace = self.root / "workspace"
        workspace.mkdir()
        run_git("clone", "--quiet", "--bare", str(seed), ".bare", cwd=workspace)
        run_git("--git-dir=.bare", "worktree", "add", "--quiet", "main", cwd=workspace)
        return workspace, workspace / "main"


class PlanFileTests(unittest.TestCase):
    """Exercise plan resolution and Markdown validation rules."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fixture = RepositoryFixture(self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_plan(self, root: Path, name: str, body: str) -> Path:
        plan = root / "_plans" / name
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text("# TODO: fixture\n" + body, encoding="utf-8")
        return plan.resolve()

    def test_plain_resolution_appends_markdown_suffix(self) -> None:
        repository = self.fixture.plain()
        path, context = plan_file.resolve_plan(
            "example", cwd=repository, create_parent=True
        )
        self.assertEqual(path, (repository / "_plans/example.md").resolve())
        self.assertTrue(path.parent.is_dir())
        self.assertFalse(context.is_faur)

    def test_faur_resolution_uses_workspace_shared_directory(self) -> None:
        workspace, worktree = self.fixture.faur()
        expected = self.write_plan(workspace, "example.md", "unused\n")
        path, context = plan_file.resolve_plan("example", cwd=worktree)
        self.assertEqual(path, expected)
        self.assertTrue(context.is_faur)

    def test_plain_linked_worktree_uses_its_own_git_root(self) -> None:
        repository = self.fixture.plain()
        run_git("config", "user.name", "Plan Helper Tests", cwd=repository)
        run_git("config", "user.email", "tests@example.invalid", cwd=repository)
        (repository / "README.md").write_text("fixture\n", encoding="utf-8")
        run_git("add", "README.md", cwd=repository)
        run_git("commit", "--quiet", "-m", "create fixture", cwd=repository)
        linked = self.root / "linked"
        run_git(
            "worktree",
            "add",
            "--quiet",
            "-b",
            "linked",
            str(linked),
            cwd=repository,
        )

        path, context = plan_file.resolve_plan(
            "example", cwd=linked, create_parent=True
        )
        self.assertEqual(path, (linked / "_plans/example.md").resolve())
        self.assertFalse(context.is_faur)

    def test_missing_plan_is_rejected_without_create_parent(self) -> None:
        repository = self.fixture.plain()
        with self.assertRaisesRegex(plan_file.PlanFileError, "does not exist"):
            plan_file.resolve_plan("missing", cwd=repository)

    def test_paths_and_non_slug_names_are_rejected(self) -> None:
        repository = self.fixture.plain()
        for slug in ("/tmp/plan", "../plan", "nested/plan", "Plan", "two words"):
            with self.subTest(slug=slug):
                with self.assertRaises(plan_file.PlanFileError):
                    plan_file.resolve_plan(slug, cwd=repository, create_parent=True)

    def test_explicit_markdown_suffix_is_rejected(self) -> None:
        repository = self.fixture.plain()
        with self.assertRaisesRegex(plan_file.PlanFileError, "omit the .md suffix"):
            plan_file.resolve_plan("example.md", cwd=repository, create_parent=True)

    def test_symlink_escape_is_rejected(self) -> None:
        repository = self.fixture.plain()
        plans = repository / "_plans"
        plans.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        outside_plan = outside / "plan.md"
        outside_plan.write_text("outside\n", encoding="utf-8")
        os.symlink(outside_plan, plans / "escape.md")
        with self.assertRaisesRegex(plan_file.PlanFileError, "symlink"):
            plan_file.resolve_plan("escape", cwd=repository, create_parent=True)

    def test_faur_worktree_metadata_is_required_and_validated(self) -> None:
        workspace, worktree = self.fixture.faur()
        invalid_bodies = (
            "- [ ] task\n  - Done when: complete\n",
            "Worktree: `good-slug`\nWorktree: `other-slug`\n"
            "- [ ] task\n  - Done when: complete\n",
            "Worktree: `Bad_Slug`\n- [ ] task\n  - Done when: complete\n",
            "Worktree: two slugs\n- [ ] task\n  - Done when: complete\n",
        )
        for index, body in enumerate(invalid_bodies):
            with self.subTest(index=index):
                self.write_plan(workspace, "invalid.md", body)
                with self.assertRaises(plan_file.PlanFileError):
                    plan_file.validate_plan("invalid", cwd=worktree)

    def test_plain_plan_rejects_worktree_metadata(self) -> None:
        repository = self.fixture.plain()
        self.write_plan(
            repository,
            "invalid.md",
            "Worktree: `not-allowed`\n- [ ] task\n  - Done when: complete\n",
        )
        with self.assertRaisesRegex(plan_file.PlanFileError, "must not contain"):
            plan_file.validate_plan("invalid", cwd=repository)

    def test_malformed_and_unsupported_checkboxes_are_rejected(self) -> None:
        repository = self.fixture.plain()
        for checkbox in ("- [] task", "- [~] task", "- [xx] task"):
            with self.subTest(checkbox=checkbox):
                self.write_plan(
                    repository,
                    "invalid.md",
                    f"{checkbox}\n  - Done when: complete\n",
                )
                with self.assertRaises(plan_file.PlanFileError):
                    plan_file.validate_plan("invalid", cwd=repository)

    def test_missing_checkbox_and_done_when_are_rejected(self) -> None:
        repository = self.fixture.plain()
        invalid_bodies = (
            "- ordinary bullet\n  - Done when: complete\n",
            "- [ ] task without completion clause\n",
            "- [ ] first task\n- [ ] second task\n  - Done when: only second is covered\n",
            "- [ ] parent task\n"
            "  - [ ] child task\n"
            "    - Done when: only the child is covered\n",
        )
        for index, body in enumerate(invalid_bodies):
            with self.subTest(index=index):
                self.write_plan(repository, "invalid.md", body)
                with self.assertRaises(plan_file.PlanFileError):
                    plan_file.validate_plan("invalid", cwd=repository)

    def test_valid_json_contains_path_worktree_and_task_counts(self) -> None:
        workspace, worktree = self.fixture.faur()
        expected = self.write_plan(
            workspace,
            "valid.md",
            "Worktree: `feature-one`\n"
            "- [x] completed\n"
            "  - Done when: completed condition holds\n"
            "- [ ] pending\n"
            "  - Done when: pending condition holds\n",
        )

        previous_cwd = Path.cwd()
        stdout = StringIO()
        stderr = StringIO()
        try:
            os.chdir(worktree)
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = plan_file.main(["validate", "valid"])
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(status, 0, stderr.getvalue())
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["path"], str(expected))
        self.assertTrue(payload["faur"])
        self.assertEqual(payload["worktree"], "feature-one")
        self.assertEqual(
            payload["tasks"], {"completed": 1, "pending": 1, "total": 2}
        )

    def test_resolve_json_identifies_plain_repository(self) -> None:
        repository = self.fixture.plain()
        previous_cwd = Path.cwd()
        stdout = StringIO()
        try:
            os.chdir(repository)
            with redirect_stdout(stdout):
                status = plan_file.main(
                    ["resolve", "example", "--create-parent", "--json"]
                )
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(status, 0)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["faur"])
        self.assertEqual(payload["plan_root"], str((repository / "_plans").resolve()))
        self.assertEqual(payload["path"], str((repository / "_plans/example.md").resolve()))

    def test_cli_returns_nonzero_for_invalid_input(self) -> None:
        repository = self.fixture.plain()
        previous_cwd = Path.cwd()
        stderr = StringIO()
        try:
            os.chdir(repository)
            with redirect_stderr(stderr):
                status = plan_file.main(["resolve", "../escape", "--create-parent"])
        finally:
            os.chdir(previous_cwd)

        self.assertEqual(status, 2)
        self.assertIn("lowercase kebab-case", stderr.getvalue())

    def test_plan_and_execute_flows_share_one_canonical_path(self) -> None:
        plain = self.fixture.plain()
        faur_workspace, faur_worktree = self.fixture.faur()
        cases = (
            (
                plain,
                "- [ ] plain task\n  - Done when plain result exists\n",
            ),
            (
                faur_worktree,
                "Worktree: `shared-plan`\n"
                "- [ ] faur task\n"
                "  - Done when: faur result exists\n",
            ),
        )

        for cwd, body in cases:
            with self.subTest(cwd=cwd):
                planned_path, _context = plan_file.resolve_plan(
                    "skill-improvement", cwd=cwd, create_parent=True
                )
                planned_path.write_text("# TODO: fixture\n" + body, encoding="utf-8")
                execution = plan_file.validate_plan("skill-improvement", cwd=cwd)
                self.assertEqual(execution.path, str(planned_path))
                self.assertTrue(
                    planned_path.is_relative_to(
                        (faur_workspace if cwd == faur_worktree else plain) / "_plans"
                    )
                )


if __name__ == "__main__":
    unittest.main()

class TypedPlanTests(unittest.TestCase):
    """Regression coverage for containment, grammar, and atomic mutation."""

    setUp = PlanFileTests.setUp
    tearDown = PlanFileTests.tearDown
    write_plan = PlanFileTests.write_plan

    def test_plan_root_symlinks_rejected_in_both_layouts(self) -> None:
        plain = self.fixture.plain()
        workspace, worktree = self.fixture.faur()
        outside = self.root / 'outside'
        outside.mkdir()
        for root, cwd in ((plain, plain), (workspace, worktree)):
            (root / '_plans').symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(plan_file.PlanFileError, 'root.*symlink'):
                plan_file.resolve_plan('escape', cwd=cwd, create_parent=True)
        self.assertEqual(list(outside.iterdir()), [])

    def test_fences_titles_and_clause_ownership(self) -> None:
        invalid = [
            '```markdown\n# TODO: example\n- [ ] task\n  - Done when: yes\n```\n',
            '# TODO: task\n- [ ] work\n## Other section\n  - Done when: unrelated\n',
            '# TODO: task\n- [ ] work\nDone when: unrelated\n',
            '# TODO: task\n- [ ] work\n```\n  - Done when: example\n```\n',
            '# TODO: task\n  - Task: misplaced\n- [ ] work\n  - Done when: done\n',
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(plan_file.PlanFileError):
                plan_file.parse_plan(content, is_faur=False)
        valid = '# TODO: task\n```\n- [ ] example\n```\n- [ ] work\n  - Done when: done\n'
        self.assertEqual(len(plan_file.parse_plan(valid, is_faur=False).tasks), 1)

    def test_launch_pairs_singletons_and_plain_rejection(self) -> None:
        base = '# TODO: task\nWorktree: `feature`\n'
        task = '- [ ] work\n  - Done when: done\n'
        for extra in ('Launch branch: main\n', 'Launch worktree: /tmp/main\n', 'Worktree: other\n', 'Launch worktree: relative\nLaunch branch: main\n'):
            with self.subTest(extra=extra), self.assertRaises(plan_file.PlanFileError):
                plan_file.parse_plan(base + extra + task, is_faur=True)
        for extra in ('Launch branch: main\n', 'Launch worktree: /tmp/main\nLaunch branch: main\n'):
            with self.assertRaisesRegex(plan_file.PlanFileError, 'must not contain'):
                plan_file.parse_plan('# TODO: task\n' + extra + task, is_faur=False)

    def test_typed_executable_metadata_and_precise_missing_fields(self) -> None:
        legacy = '# TODO: task\n- [ ] work\n  - Done when: done\n'
        with self.assertRaisesRegex(plan_file.PlanFileError, 'task-1 missing executable fields: Task, Commit, Paths, Verify'):
            plan_file.parse_plan(legacy, is_faur=False, runtime=True)
        legacy_scope = legacy.replace('  - Done', '  - Paths: `old.py`, focused sibling files\n  - Done')
        self.assertEqual(plan_file.parse_plan(legacy_scope, is_faur=False).tasks[0].paths, [])
        fields = '  - Task: change-input\n  - Commit: validate input\n  - Paths: ["src/*.py"]\n  - Verify: ["python3", "-m", "unittest"]\n'
        plan = plan_file.parse_plan(legacy.replace('  - Done', fields + '  - Done'), is_faur=False, runtime=True)
        self.assertEqual(plan.tasks[0].id, 'change-input')
        self.assertEqual(plan.tasks[0].verify, [['python3', '-m', 'unittest']])
        for bad in ('  - Task: Bad_ID\n', '  - Verify: "echo unsafe"\n', '  - Paths: ["../outside"]\n', '  - Commit: Uppercase subject\n', '  - Verify: []\n', '  - Task: one\n  - Task: two\n'):
            with self.subTest(bad=bad), self.assertRaises(plan_file.PlanFileError):
                plan_file.parse_plan(legacy.replace('  - Done', bad + '  - Done'), is_faur=False)

    def test_migration_preserves_content_and_completion_requires_evidence(self) -> None:
        source = '# TODO: task\n\nSome readable prose.\n- [x] previous\n  - Done when: done\n- [ ] work\n  - Done when: done\n'
        plan = plan_file.parse_plan(source, is_faur=False)
        migrated = plan_file.migrate_ids(plan)
        self.assertEqual(migrated.replace('  - Task: task-1\n', '').replace('  - Task: task-2\n', ''), source)
        migrated_plan = plan_file.parse_plan(migrated, is_faur=False)
        with self.assertRaisesRegex(plan_file.PlanFileError, 'evidence'):
            plan_file.mark_complete(migrated_plan, 'task-2', outcome='a' * 40, evidence=[])
        completed = plan_file.mark_complete(migrated_plan, 'task-2', outcome='a' * 40, evidence=[{'argv': ['true'], 'exit_code': 0}])
        self.assertTrue(plan_file.parse_plan(completed, is_faur=False).tasks[1].completed)

    def test_atomic_mutator_refuses_drift_and_lock_contention(self) -> None:
        from state_io import StateError, plan_lock
        root = self.fixture.plain()
        path = self.write_plan(root, 'example.md', '- [ ] work\n  - Done when: done\n')
        context = plan_file.git_context(root)
        plan = plan_file.read_plan(path, context)
        with plan_lock(path):
            with self.assertRaisesRegex(StateError, 'locked'):
                with plan_lock(path):
                    self.fail('second lock acquired')
            path.write_text(plan.content + '\nExternal change\n')
            with self.assertRaisesRegex(plan_file.PlanFileError, 'changed'):
                plan_file.replace_plan(path, context, plan, plan_file.migrate_ids(plan))
        self.assertIn('External change', path.read_text())

    def test_init_refuses_overwrite_and_next_reports_stable_task(self) -> None:
        root = self.fixture.plain()
        content = self.root / 'content.md'
        content.write_text('# TODO: task\n- [ ] work\n  - Done when: done\n')
        first = subprocess.run([sys.executable, str(HELPER), 'init', 'example', '--content', str(content)], cwd=root, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr)
        original = (root / '_plans/example.md').read_bytes()
        second = subprocess.run([sys.executable, str(HELPER), 'init', 'example', '--content', str(content)], cwd=root, capture_output=True, text=True)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual((root / '_plans/example.md').read_bytes(), original)
        selected = subprocess.run([sys.executable, str(HELPER), 'next', 'example'], cwd=root, capture_output=True, text=True)
        self.assertEqual(json.loads(selected.stdout)['id'], 'task-1')

class StableIdTests(unittest.TestCase):
    def test_mixed_legacy_and_explicit_ids_are_unique_and_malformed_ids_fail(self) -> None:
        content = '# TODO: fixture\n- [ ] legacy\n  - Done when: accepted\n- [ ] explicit\n  - Task: task-1\n  - Done when: accepted\n'
        plan = plan_file.parse_plan(content, is_faur=False)
        self.assertEqual([task.id for task in plan.tasks], ['task-2', 'task-1'])
        migrated = plan_file.parse_plan(plan_file.migrate_ids(plan), is_faur=False)
        self.assertEqual([task.id for task in migrated.tasks], ['task-2', 'task-1'])
        for malformed in ('`task-1', 'task-1`', '``task-1``'):
            with self.assertRaisesRegex(plan_file.PlanFileError, 'malformed Task ID'):
                plan_file.parse_plan(content.replace('Task: task-1', f'Task: {malformed}'), is_faur=False)


GROUPED = """# TODO: grouped fixture

## Context and decisions

Persist this decision for workers.

```markdown
- [ ] fenced task
  - Group: ignored
```

- [ ] Implement inputs.
  - Group: inputs
  - Verify: ["true"]
  - Done when: inputs work together.
  Group prose retained.
  - [ ] Validate inputs.
    - Task: validate-inputs
    - Commit: validate inputs
    - Paths: ["input.py"]
    - Verify: ["true"]
    - Done when: inputs are valid.
    Child prose retained.
  - [ ] Connect inputs.
    - Task: connect-inputs
    - Commit: connect inputs
    - Paths: ["input.py"]
    - Verify: ["true"]
    - Done when: inputs are connected.
- [ ] Implement outputs.
  - Group: outputs
  - Verify: ["true"]
  - Done when: outputs work.
  - [ ] Produce outputs.
    - Task: produce-outputs
    - Commit: produce outputs
    - Paths: ["output.py"]
    - Verify: ["true"]
    - Done when: outputs exist.
"""


class GroupedPlanTests(unittest.TestCase):
    def parse(self, content: str = GROUPED) -> object:
        return plan_file.parse_plan(content, is_faur=False, runtime=True)

    def test_order_membership_and_source_locations(self) -> None:
        plan = self.parse()
        self.assertEqual([g.id for g in plan.groups], ['inputs', 'outputs'])
        self.assertEqual(plan.groups[0].task_ids, ['validate-inputs', 'connect-inputs'])
        self.assertEqual([t.group_id for t in plan.tasks], ['inputs', 'inputs', 'outputs'])
        for task in plan.tasks:
            self.assertTrue(GROUPED.splitlines()[task.line].startswith('  - [ ]'))
            self.assertEqual(task.indent, 2)
            self.assertGreater(task.end, task.line)
        self.assertEqual(plan.groups[0].end, plan.groups[1].line)

    def test_round_trip_child_completion_and_identity_preserve_structure(self) -> None:
        plan = self.parse()
        completed = plan_file.mark_complete(plan, 'validate-inputs', outcome='a' * 40,
                    evidence=[{'argv': ['true'], 'exit_code': 0}])
        updated = self.parse(completed)
        self.assertTrue(updated.tasks[0].completed)
        self.assertFalse(updated.groups[0].completed)
        stripped = '\n'.join(line for line in completed.splitlines()
                             if not line.startswith('    - Outcome:') and not line.startswith('    - Evidence:')) + '\n'
        self.assertEqual(stripped.replace('  - [x]', '  - [ ]'), GROUPED)
        identity = plan_file.set_identity(updated, 'fixture', '/tmp/main', 'main')
        faur = plan_file.parse_plan(identity, is_faur=True, runtime=True)
        self.assertEqual(faur.groups[0].task_ids, updated.groups[0].task_ids)
        self.assertIn('Group prose retained.', identity)
        self.assertIn('fenced task', identity)

    def test_id_migration_only_adds_missing_child_ids_at_four_spaces(self) -> None:
        legacy = GROUPED.replace('    - Task: connect-inputs\n', '')
        plan = plan_file.parse_plan(legacy, is_faur=False)
        self.assertEqual(plan.tasks[0].id, 'validate-inputs')
        migrated = plan_file.migrate_ids(plan)
        self.assertIn('    - Task: task-2\n', migrated)
        self.assertEqual(migrated.replace('    - Task: task-2\n', ''), legacy)
        self.parse(migrated)

    def test_invalid_nesting_mixed_ids_metadata_and_completion(self) -> None:
        invalid = [
            GROUPED.replace('  - Group: inputs\n', ''),
            GROUPED.replace('  - Group: inputs', '  - Task: inputs'),
            GROUPED.replace('  - Group: outputs', '  - Group: inputs'),
            GROUPED.replace('    - Task: validate-inputs', '    - Task: inputs'),
            GROUPED.replace('    - Task: connect-inputs', '    - Task: validate-inputs'),
            GROUPED.replace('  - [ ] Validate inputs.', '    - [ ] Validate inputs.'),
            GROUPED.replace('    - Task: validate-inputs', '  - Task: validate-inputs'),
            GROUPED.replace('  - [ ] Validate inputs.', '- [ ] Validate inputs.'),
            GROUPED.replace('  - [ ] Validate inputs.', '## Boundary\n  - [ ] Validate inputs.'),
            GROUPED.replace('- [ ] Implement inputs.', '- [x] Implement inputs.'),
            GROUPED.replace('  - [ ] Validate inputs.', '  - [x] Validate inputs.'),
            GROUPED.replace('  - Group: inputs', '  - Group: inputs\n  - Commit: implement inputs'),
            GROUPED[:GROUPED.index('  - [ ] Produce outputs.')],
            GROUPED.replace('  - Done when: outputs work.\n', ''),
            GROUPED.replace('  - Group: outputs', '    - Group: outputs'),
        ]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(plan_file.PlanFileError):
                self.parse(content)

    def test_group_requires_matching_evidence_and_child_completion_prefix(self) -> None:
        plan = self.parse()
        for task_id in ['validate-inputs', 'connect-inputs']:
            content = plan_file.mark_complete(plan, task_id, outcome='no-change',
                      evidence=[{'argv': ['true'], 'exit_code': 0}])
            plan = self.parse(content)
        checked = plan.content.replace('- [ ] Implement inputs.', '- [x] Implement inputs.')
        with self.assertRaisesRegex(plan_file.PlanFileError, 'Evidence'):
            self.parse(checked)
        checked = checked.replace('  - Group: inputs', '  - Evidence: {"argv": ["true"], "exit_code": 0}\n  - Group: inputs')
        self.assertTrue(self.parse(checked).groups[0].completed)
        with self.assertRaisesRegex(plan_file.PlanFileError, 'uncheckpointed'):
            self.parse(plan_file.mark_complete(plan, 'produce-outputs', outcome='no-change',
                       evidence=[{'argv': ['true'], 'exit_code': 0}]))

    def test_validation_counts_and_next_include_group_membership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            run_git('init', '--quiet', cwd=repository)
            path = repository / '_plans/example.md'
            path.parent.mkdir()
            path.write_text(GROUPED)
            counts = plan_file.validate_plan('example', cwd=repository, runtime=True)
            self.assertEqual(counts.tasks.total, 3)
            self.assertEqual(counts.groups.pending, 2)
            result = subprocess.run([sys.executable, str(HELPER), 'next', 'example', '--runtime'],
                       cwd=repository, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['group_id'], 'inputs')


DONE_PLAN = "Worktree: `{slug}`\n- [x] completed\n  - Done when: completed condition holds\n"


class PrunePlansTests(unittest.TestCase):
    """Prune only completed plans whose worktree, branch, and preview are gone."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace, self.worktree = RepositoryFixture(Path(self.temp_dir.name)).faur()
        self.plans = self.workspace / "_plans"
        (self.plans / ".state").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def plan(self, slug: str, body: str | None = None) -> Path:
        path = self.plans / f"{slug}.md"
        path.write_text("# TODO: fixture\n" + (body or DONE_PLAN.format(slug=slug)), encoding="utf-8")
        return path

    def journal(self, slug: str, **fields: object) -> Path:
        path = self.plans / ".state" / f"{slug}.json"
        path.write_text(json.dumps(fields), encoding="utf-8")
        return path

    def reasons(self, report: dict[str, object]) -> dict[str, str]:
        return {item["slug"]: item["reason"] for item in report["skipped"]}

    def test_dry_run_lists_without_deleting(self) -> None:
        plan = self.plan("gone")
        report = plan_file.prune_plans(cwd=self.worktree)
        self.assertEqual([c["slug"] for c in report["candidates"]], ["gone"])
        self.assertEqual(report["deleted"], [])
        self.assertTrue(plan.exists())

    def test_apply_deletes_plan_and_journal(self) -> None:
        plan = self.plan("gone")
        journal = self.journal("gone", identity={"branch": "gone", "path": str(self.workspace / "gone")})
        report = plan_file.prune_plans(cwd=self.worktree, apply=True)
        self.assertEqual([d["slug"] for d in report["deleted"]], ["gone"])
        self.assertFalse(plan.exists())
        self.assertFalse(journal.exists())
        self.assertFalse((self.plans / ".state" / "gone.lock").exists())

    def test_blockers_are_reported_and_preserved(self) -> None:
        self.plan("pending", "Worktree: `pending`\n- [ ] work\n  - Done when: done\n")
        self.plan("broken", "Worktree: `broken`\n- [x] work\n  - Verify: not json\n")
        self.plan("live")
        run_git("worktree", "add", "--quiet", "-b", "live", str(self.workspace / "live"), cwd=self.worktree)
        self.plan("branch")
        run_git("branch", "branch", cwd=self.worktree)
        self.plan("preview")
        (self.workspace / "_runs" / "preview").mkdir(parents=True)
        self.plan("active")
        self.journal("active", active={"task_id": "x"}, identity=None)
        self.plan("renamed")
        self.journal("renamed", identity={"branch": "main", "path": str(self.workspace / "renamed-2")})
        self.plan("locked")
        (self.plans / ".state" / "locked.lock").write_text("{}", encoding="utf-8")

        report = plan_file.prune_plans(cwd=self.worktree, apply=True)
        reasons = self.reasons(report)
        self.assertEqual(report["deleted"], [])
        self.assertEqual(reasons["pending"], "plan has pending tasks or groups")
        self.assertIn("does not parse", reasons["broken"])
        self.assertEqual(reasons["live"], "worktree still exists")
        self.assertEqual(reasons["branch"], "branch still exists")
        self.assertIn("preview runtime state remains", reasons["preview"])
        self.assertEqual(reasons["active"], "execution journal has an unfinished transaction")
        self.assertEqual(reasons["renamed"], "branch still exists")
        self.assertEqual(reasons["locked"], "plan is locked")
        self.assertEqual(len(list(self.plans.glob("*.md"))), 8)

    def test_cli_requires_yes_to_delete(self) -> None:
        plan = self.plan("gone")
        for argv, exists in ((["prune"], True), (["prune", "--yes"], False)):
            with self.subTest(argv=argv):
                result = subprocess.run([sys.executable, str(HELPER), *argv], cwd=self.worktree, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["applied"], not exists)
                self.assertEqual(plan.exists(), exists)
