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
