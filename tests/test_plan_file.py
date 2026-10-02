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
        run_git("init", "--quiet", cwd=seed)
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
        plan.write_text(body, encoding="utf-8")
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
                planned_path.write_text(body, encoding="utf-8")
                execution = plan_file.validate_plan("skill-improvement", cwd=cwd)
                self.assertEqual(execution.path, str(planned_path))
                self.assertTrue(
                    planned_path.is_relative_to(
                        (faur_workspace if cwd == faur_worktree else plain) / "_plans"
                    )
                )


if __name__ == "__main__":
    unittest.main()
