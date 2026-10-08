"""Isolated lifecycle tests for the POSIX installer."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path


REPOSITORY = Path(__file__).parents[1]
INSTALLER = REPOSITORY / "install.sh"
ACTIVE_SKILLS = ("agent-baseline", "pr-workflow", "task-workflow", "worktree-workflow")
RETIRED_SKILLS = ("plan-workflow", "execute-workflow")


@dataclass(frozen=True)
class IsolatedHome:
    """Temporary Claude and Codex installation roots."""

    root: Path

    @property
    def claude(self) -> Path:
        return self.root / ".claude"

    @property
    def codex(self) -> Path:
        return self.root / ".codex"

    @property
    def backups(self) -> Path:
        return self.root / "coding-skill_backups"

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("NO_COLOR", None)
        environment.pop("CLICOLOR_FORCE", None)
        environment["HOME"] = str(self.root)
        environment["CODEX_HOME"] = str(self.codex)
        return environment

    def initialize_targets(self) -> None:
        self.claude.mkdir()
        self.codex.mkdir()

    def add_legacy_skills(self) -> None:
        for target in (self.claude, self.codex):
            for skill in RETIRED_SKILLS:
                skill_dir = target / "skills" / skill
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "legacy.txt").write_text(
                    f"legacy {skill}\n", encoding="utf-8"
                )

    def run(
        self,
        *arguments: str,
        check: bool = True,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = self.environment()
        if extra_environment is not None:
            environment.update(extra_environment)
        return subprocess.run(
            [str(INSTALLER), *arguments],
            cwd=REPOSITORY,
            env=environment,
            check=check,
            capture_output=True,
            text=True,
        )


class InstallerTests(unittest.TestCase):
    """Verify fresh, upgrade, idempotent, check, and uninstall behavior."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = IsolatedHome(Path(self.temp_dir.name))
        self.home.initialize_targets()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_legacy_upgrade_is_backed_up_clean_and_idempotent(self) -> None:
        self.home.add_legacy_skills()

        check_before = self.home.run("--check", check=False)
        self.assertNotEqual(check_before.returncode, 0)
        self.assertIn("plan-workflow (retired): STALE", check_before.stdout)

        dry_run = self.home.run("--dry-run")
        self.assertIn("plan-workflow (retired): would remove", dry_run.stdout)
        self.assertIn("execute-workflow (retired): would remove", dry_run.stdout)
        self.assertFalse(self.home.backups.exists())

        first_install = self.home.run()
        self.assertIn("plan-workflow (retired): migrating", first_install.stdout)
        for target in (self.home.claude, self.home.codex):
            self.assertTrue((target / "skills/task-workflow/SKILL.md").is_file())
            for skill in RETIRED_SKILLS:
                self.assertFalse((target / "skills" / skill).exists())

        backed_up = list(self.home.backups.glob("*/.*/*/*/legacy.txt"))
        self.assertEqual(len(backed_up), 4)
        self.assertEqual(self.home.run("--check").returncode, 0)

        backup_directories = set(self.home.backups.iterdir())
        second_install = self.home.run()
        self.assertNotIn("backup:", second_install.stdout)
        self.assertEqual(set(self.home.backups.iterdir()), backup_directories)

    def test_fresh_install_dry_run_and_uninstall(self) -> None:
        install = self.home.run()
        self.assertIn("Installing shared agent skills", install.stdout)
        self.assertIn("✓ Installation complete.", install.stdout)
        self.assertNotIn("\x1b[", install.stdout)
        for target in (self.home.claude, self.home.codex):
            for skill in ACTIVE_SKILLS:
                self.assertTrue((target / "skills" / skill / "SKILL.md").is_file())

        self.home.add_legacy_skills()
        dry_run = self.home.run("--uninstall", "--dry-run")
        self.assertIn("task-workflow: would remove", dry_run.stdout)
        self.assertIn("plan-workflow (retired): would remove", dry_run.stdout)
        self.assertTrue((self.home.claude / "skills/task-workflow").is_dir())

        self.home.run("--uninstall")
        for target in (self.home.claude, self.home.codex):
            for skill in (*ACTIVE_SKILLS, *RETIRED_SKILLS):
                self.assertFalse((target / "skills" / skill).exists())
            self.assertFalse((target / "CLAUDE.md").exists())
            self.assertFalse((target / "AGENTS.md").exists())

    def test_color_can_be_forced_and_disabled(self) -> None:
        colored = self.home.run(
            "--dry-run", extra_environment={"CLICOLOR_FORCE": "1"}
        )
        self.assertIn("\x1b[", colored.stdout)
        self.assertIn("✓ Dry run complete", colored.stdout)

        plain = self.home.run(
            "--dry-run",
            extra_environment={"CLICOLOR_FORCE": "1", "NO_COLOR": "1"},
        )
        self.assertNotIn("\x1b[", plain.stdout)

    def test_python_bytecode_is_ignored_and_cleaned(self) -> None:
        self.home.run()

        for target in (self.home.claude, self.home.codex):
            scripts = target / "skills/task-workflow/scripts"
            cache = scripts / "__pycache__"
            cache.mkdir()
            (cache / "plan_file.cpython-312.pyc").write_bytes(b"cache")
            (scripts / "legacy.pyo").write_bytes(b"cache")

        self.assertEqual(self.home.run("--check").returncode, 0)

        reinstall = self.home.run()
        self.assertIn("removed generated Python cache files", reinstall.stdout)
        self.assertIn("backup:", reinstall.stdout)
        for target in (self.home.claude, self.home.codex):
            scripts = target / "skills/task-workflow/scripts"
            self.assertFalse((scripts / "__pycache__").exists())
            self.assertFalse((scripts / "legacy.pyo").exists())


if __name__ == "__main__":
    unittest.main()

class InstallerSafetyTests(unittest.TestCase):
    setUp = InstallerTests.setUp
    tearDown = InstallerTests.tearDown

    def test_malformed_marker_preflight_is_non_mutating_in_all_modes(self) -> None:
        begin = '<!-- BEGIN coding-skill -->\n'
        end = '<!-- END coding-skill -->\n'
        source = (REPOSITORY / 'shared/instructions.md').read_text()
        malformed = [begin + source, end, end + begin, begin + begin + end + end, begin + end + begin + end, '  ' + begin + end]
        for body in malformed:
            for arguments in ((), ('--dry-run',), ('--uninstall',), ('--uninstall', '--dry-run'), ('--check',)):
                with self.subTest(body=body[:40], arguments=arguments):
                    target = self.home.codex / 'AGENTS.md'
                    target.write_text(body)
                    before = target.read_bytes()
                    result = self.home.run(*arguments, check=False)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('malformed managed markers', result.stdout + result.stderr)
                    self.assertEqual(target.read_bytes(), before)
                    self.assertFalse((self.home.claude / 'skills').exists())
                    self.assertFalse(self.home.backups.exists())

    def test_refresh_and_uninstall_preserve_outside_content_and_mode(self) -> None:
        target = self.home.claude / 'CLAUDE.md'
        prefix = 'Personal notes\n\n\tKeep whitespace.\n'
        suffix = '\nTrailing notes\n\n'
        target.write_text(prefix + '<!-- BEGIN coding-skill -->\nold\n<!-- END coding-skill -->\n' + suffix)
        target.chmod(0o640)
        self.home.run()
        expected = prefix + '<!-- BEGIN coding-skill -->\n' + (REPOSITORY / 'shared/instructions.md').read_text() + '<!-- END coding-skill -->\n' + suffix
        self.assertEqual(target.read_text(), expected)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(self.home.run('--check').returncode, 0)
        self.home.run('--uninstall')
        self.assertEqual(target.read_text(), prefix + suffix)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)

    def test_equal_skill_symlink_is_backed_up_and_replaced_without_touching_target(self) -> None:
        import shutil
        outside = self.home.root / 'outside-skill'
        shutil.copytree(REPOSITORY / 'skills/agent-baseline', outside)
        skills = self.home.claude / 'skills'
        skills.mkdir()
        link = skills / 'agent-baseline'
        link.symlink_to(outside, target_is_directory=True)
        source = (outside / 'SKILL.md').read_bytes()
        self.assertNotEqual(self.home.run('--check', check=False).returncode, 0)
        self.home.run('--dry-run')
        self.assertTrue(link.is_symlink())
        self.home.run()
        self.assertFalse(link.is_symlink())
        self.assertEqual((outside / 'SKILL.md').read_bytes(), source)
        backups = list(self.home.backups.glob('*/.claude/skills/agent-baseline'))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].is_symlink())
        self.home.run('--check')

    def test_unique_backups_for_same_timestamp_and_idempotence(self) -> None:
        commands = self.home.root / 'commands'
        commands.mkdir()
        date = commands / 'date'
        date.write_text('#!/bin/sh\nprintf "fixed-time\\n"\n')
        date.chmod(0o755)
        env = {'PATH': str(commands) + os.pathsep + os.environ['PATH']}
        target = self.home.claude / 'CLAUDE.md'
        for n in range(2):
            target.write_text(f'unmanaged {n}\n')
            self.home.run(extra_environment=env)
        backups = list(self.home.backups.iterdir())
        self.assertEqual(len(backups), 2)
        bodies = {(p / '.claude/CLAUDE.md').read_text() for p in backups}
        self.assertEqual(bodies, {'unmanaged 0\n', 'unmanaged 1\n'})
        before = set(self.home.backups.iterdir())
        second = self.home.run(extra_environment=env)
        self.assertIn('up to date', second.stdout)
        self.assertEqual(set(self.home.backups.iterdir()), before)

    def test_replacement_failure_keeps_original_mode_backup_and_cleans_temp(self) -> None:
        self.home.run()
        target = self.home.claude / 'CLAUDE.md'
        target.write_text('before\n<!-- BEGIN coding-skill -->\nold\n<!-- END coding-skill -->\nafter\n')
        target.chmod(0o640)
        before = target.read_bytes()
        commands = self.home.root / 'commands'
        commands.mkdir()
        mv = commands / 'mv'
        mv.write_text('#!/bin/sh\nexit 1\n')
        mv.chmod(0o755)
        result = self.home.run(check=False, extra_environment={'PATH': str(commands) + os.pathsep + os.environ['PATH']})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertFalse(list(self.home.claude.glob('.coding-skill.*')))
        backup = next(self.home.backups.glob('*/.claude/CLAUDE.md'))
        self.assertEqual(backup.read_bytes(), before)

    def test_generation_failure_and_signal_leave_original_and_remove_temporary(self) -> None:
        self.home.run()
        commands = self.home.root / 'commands'
        commands.mkdir()
        target = self.home.claude / 'CLAUDE.md'
        before = b'unmanaged original\n'
        cat = commands / 'cat'
        cat.write_text('#!/bin/sh\nexit 1\n')
        cat.chmod(0o755)
        target.write_bytes(before)
        result = self.home.run(check=False, extra_environment={'PATH': str(commands) + os.pathsep + os.environ['PATH']})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.home.claude.glob('.coding-skill.*')))
        cat.unlink()
        mv = commands / 'mv'
        mv.write_text('#!/bin/sh\nkill -TERM "$PPID"\nexit 1\n')
        mv.chmod(0o755)
        result = self.home.run(check=False, extra_environment={'PATH': str(commands) + os.pathsep + os.environ['PATH']})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_bytes(), before)
        self.assertFalse(list(self.home.claude.glob('.coding-skill.*')))


class InstalledPreviewTests(unittest.TestCase):
    """Copied preview and finish helpers work with the source tree deleted."""

    def test_copied_helpers_run_without_source_or_sibling_imports(self) -> None:
        import json
        import shutil
        import sys
        from tests.test_plan_file import RepositoryFixture, run_git
        from tests.test_preview_finish import FAKE_FAUR
        from tests.test_preview_workflow import install_tools
        from tests.test_workflow import executable_plan

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            source = root / "source"
            shutil.copytree(REPOSITORY, source, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
            home = IsolatedHome(root / "home")
            home.root.mkdir()
            home.initialize_targets()

            def installer(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([str(source / "install.sh"), *arguments], cwd=source, env=home.environment(), check=True, capture_output=True, text=True)

            installer("--dry-run")
            self.assertFalse((home.claude / "skills").exists())
            installer()
            installer("--check")
            backups = set(home.backups.glob("*"))
            self.assertIn("up to date", installer().stdout)
            self.assertEqual(set(home.backups.glob("*")), backups)
            installer("--check")
            shutil.rmtree(source)

            for target in (home.claude, home.codex):
                with self.subTest(target=target.name):
                    for skill in ("task-workflow", "worktree-workflow"):
                        for file in (target / "skills" / skill).rglob("*"):
                            if file.is_file():
                                self.assertNotIn(str(REPOSITORY), file.read_text(errors="replace"))
                    task_scripts = target / "skills/task-workflow/scripts"
                    finish = target / "skills/worktree-workflow/scripts/finish.py"
                    work = root / target.name
                    work.mkdir()
                    environment = {**os.environ, "HOME": str(home.root), "XDG_STATE_HOME": str(work / "xdg"), "PYTHONPATH": "",
                                   "PYTHONDONTWRITEBYTECODE": "1", "FAKE_PREVIEW_MODE": ""}

                    def helper(script: Path, *arguments: str, cwd: Path) -> dict[str, object]:
                        result = subprocess.run([sys.executable, "-I", str(script), *arguments], cwd=cwd, env=environment, capture_output=True, text=True)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        return json.loads(result.stdout.splitlines()[-1]) if result.stdout.strip().startswith("{") else {"text": result.stdout}

                    # Plain repository: preview lifecycle in place.
                    (work / "plain-root").mkdir()
                    repo = RepositoryFixture(work / "plain-root").plain()
                    run_git("config", "user.name", "Installed Helpers", cwd=repo)
                    run_git("config", "user.email", "test@example.invalid", cwd=repo)
                    (repo / ".gitignore").write_text("_plans/\n__pycache__/\n")
                    install_tools(repo)
                    run_git("add", ".", cwd=repo)
                    run_git("commit", "-qm", "create fixture", cwd=repo)
                    content = work / "plan.md"
                    content.write_text(executable_plan(count=1))
                    helper(task_scripts / "plan_file.py", "init", "example", "--content", str(content), cwd=repo)
                    helper(task_scripts / "workflow.py", "start", "example", cwd=repo)
                    started = helper(task_scripts / "preview.py", "run", "example", "--", "--seed", "copied", cwd=repo)
                    self.assertEqual((started["hostname"], started["details"]["start_args"]), ("example.localhost", ["--seed", "copied"]))
                    self.assertEqual(helper(task_scripts / "preview.py", "status", "example", cwd=repo)["status"], "running")
                    self.assertEqual(helper(task_scripts / "preview.py", "logs", "example", "--lines", "2", cwd=repo)["text"], "line 2\nline 3\n")
                    self.assertEqual(helper(task_scripts / "preview.py", "stop", "example", "--teardown", cwd=repo)["phase"], "torn-down")
                    self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout, "")

                    # Faur workspace: suffixed task, preview from launch, finish wrapper teardown.
                    (work / "faur-root").mkdir()
                    workspace, launch = RepositoryFixture(work / "faur-root").faur()
                    run_git("config", "user.name", "Installed Helpers", cwd=launch)
                    run_git("config", "user.email", "test@example.invalid", cwd=launch)
                    (launch / ".gitignore").write_text("__pycache__/\n")
                    install_tools(launch)
                    run_git("add", ".", cwd=launch)
                    run_git("commit", "-qm", "add tools", cwd=launch)
                    (workspace / "_plans").mkdir()
                    (workspace / "_plans/example.md").write_text(executable_plan(faur=True, count=1))
                    task = workspace / "example-2"
                    common = str(workspace / ".bare")
                    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=launch, capture_output=True, text=True, check=True).stdout.strip()
                    launch_tree = {"name": "main", "path": str(launch), "branch": "main", "head": head}
                    task_tree = {"name": "example-2", "path": str(task), "branch": "example-2", "head": head}
                    record = {"schema_version": 1, "task_id": "installed-task-id", "workspace": {"root": str(workspace), "common_dir": common},
                              "requested_slug": "example", "phase": "ready", "launch": launch_tree,
                              "task": {"name": "example-2", "path": str(task), "branch": "example-2"}}
                    base = {"schema_version": 1, "command": "context", "ok": True, "workspace": {"root": str(workspace), "common_dir": common, "faur": True}}
                    config = work / "faur.json"
                    config.write_text(json.dumps({
                        "launch_context": {**base, "worktree": launch_tree, "task": {"status": "none", "record": None}},
                        "context": {**base, "worktree": task_tree, "task": {"status": "valid", "record": record, "error": None}},
                        "created": {"schema_version": 1, "command": "add", "ok": True, "worktree": task_tree, "task_id": "installed-task-id", "task": record, "launch": launch_tree},
                        "task_path": str(task), "launch": str(launch), "record": str(workspace / "_runs/example/runtime.json"),
                        "running": str(workspace / "_runs/example/state/running")}))
                    fake_faur = work / "fake_faur.py"
                    fake_faur.write_text(FAKE_FAUR)
                    environment.update({"FAKE_FAUR_CONFIG": str(config), "FAKE_FAUR_LOG": str(work / "faur.log"), "FAKE_FAUR_FAIL": ""})
                    faur_argv = json.dumps([sys.executable, str(fake_faur)])
                    created = helper(task_scripts / "workflow.py", "start", "example", "--faur-argv", faur_argv, cwd=launch)
                    self.assertEqual(created["identity"]["name"], "example-2")
                    previewed = helper(task_scripts / "preview.py", "run", "example", cwd=launch)
                    self.assertEqual((previewed["hostname"], previewed["worktree"]), ("example.localhost", str(task)))
                    dry = helper(finish, "example-2", "--dry-run", "--faur-argv", faur_argv, cwd=launch)
                    self.assertEqual(dry["previews"][0]["phase"], "running")
                    finished = helper(finish, "example-2", "--faur-argv", faur_argv, cwd=launch)
                    self.assertEqual(finished["teardown"][0]["result"], "torn down")
                    self.assertFalse(task.exists())
                    self.assertFalse((workspace / "_runs/example").exists())
