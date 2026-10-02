#!/usr/bin/env python3
"""Resolve and validate shared task-workflow plan files."""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


LOGGER = logging.getLogger("plan-file")
WORKTREE_FIELD_RE = re.compile(r"^Worktree:\s*(?:`([^`]+)`|(\S+))\s*$")
WORKTREE_PREFIX_RE = re.compile(r"^Worktree\s*:", re.IGNORECASE)
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
CHECKBOX_RE = re.compile(r"^(?P<indent>\s*)-\s+\[(?P<state>.)\]\s+\S")
CHECKBOX_PREFIX_RE = re.compile(r"^\s*-\s+\[[^]]*\]")
DONE_WHEN_RE = re.compile(r"^\s*(?:-\s+)?Done when(?::|\s)\s*\S", re.IGNORECASE)


class PlanFileError(Exception):
    """An actionable plan resolution or validation failure."""


@dataclass(frozen=True)
class GitContext:
    """Canonical repository locations relevant to plan storage."""

    common_dir: Path
    plan_root: Path
    is_faur: bool


@dataclass(frozen=True)
class TaskCounts:
    """Checkbox task totals for machine-readable output."""

    total: int
    pending: int
    completed: int


@dataclass(frozen=True)
class ResolutionResult:
    """Resolved plan location emitted by the command-line interface."""

    path: str
    plan_root: str
    faur: bool


@dataclass(frozen=True)
class ValidationResult:
    """Validated plan metadata emitted by the command-line interface."""

    path: str
    faur: bool
    worktree: str | None
    tasks: TaskCounts


def _git_common_dir(cwd: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise PlanFileError("git is not installed or is not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "not inside a Git repository"
        raise PlanFileError(f"cannot determine the Git context: {detail}") from exc

    raw_path = result.stdout.strip()
    if not raw_path:
        raise PlanFileError("git returned an empty common-directory path")
    common_dir = Path(raw_path)
    if not common_dir.is_absolute():
        common_dir = cwd / common_dir
    return common_dir.resolve()


def _git_toplevel(cwd: Path) -> Path:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "working tree root is unavailable"
        raise PlanFileError(f"cannot determine the Git worktree root: {detail}") from exc

    raw_path = result.stdout.strip()
    if not raw_path:
        raise PlanFileError("git returned an empty worktree-root path")
    return Path(raw_path).resolve()


def git_context(cwd: Path | None = None) -> GitContext:
    """Return the canonical shared plan root for the current Git repository."""

    start = (cwd or Path.cwd()).resolve()
    common_dir = _git_common_dir(start)
    is_faur = common_dir.name == ".bare"
    repository_root = common_dir.parent if is_faur else _git_toplevel(start)
    return GitContext(
        common_dir=common_dir,
        plan_root=(repository_root / "_plans").resolve(),
        is_faur=is_faur,
    )


def resolve_plan(
    slug: str,
    *,
    cwd: Path | None = None,
    create_parent: bool = False,
) -> tuple[Path, GitContext]:
    """Resolve a plan slug to its Markdown file in the shared plan directory."""

    if not slug.strip():
        raise PlanFileError("plan slug must not be empty")
    if slug.endswith(".md"):
        raise PlanFileError("plan slug must omit the .md suffix")
    if SLUG_RE.fullmatch(slug) is None:
        raise PlanFileError(
            "plan slug must be lowercase kebab-case and start with a letter"
        )

    context = git_context(cwd)
    candidate = (context.plan_root / f"{slug}.md").resolve()
    try:
        candidate.relative_to(context.plan_root)
    except ValueError as exc:
        raise PlanFileError(
            f"plan path escapes the shared directory through a symlink: {candidate}"
        ) from exc

    if create_parent:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PlanFileError(
                f"cannot create plan directory {candidate.parent}: {exc}"
            ) from exc
    elif not candidate.is_file():
        raise PlanFileError(f"plan file does not exist: {candidate}")

    if candidate.exists() and not candidate.is_file():
        raise PlanFileError(f"plan path is not a regular file: {candidate}")
    return candidate, context


def _worktree_slug(lines: list[str], *, is_faur: bool) -> str | None:
    fields = [line for line in lines if WORKTREE_PREFIX_RE.match(line)]
    if is_faur and len(fields) != 1:
        raise PlanFileError(
            f"faur plan must contain exactly one Worktree: field; found {len(fields)}"
        )
    if not is_faur and fields:
        raise PlanFileError("plain-repository plan must not contain a Worktree: field")
    if not fields:
        return None

    match = WORKTREE_FIELD_RE.fullmatch(fields[0])
    if match is None:
        raise PlanFileError("Worktree: field must contain exactly one slug")
    slug = match.group(1) or match.group(2)
    if SLUG_RE.fullmatch(slug) is None:
        raise PlanFileError(
            "worktree slug must be lowercase kebab-case and start with a letter"
        )
    return slug


def _task_counts(lines: list[str]) -> TaskCounts:
    tasks: list[tuple[int, str, int]] = []
    for line_number, line in enumerate(lines, start=1):
        match = CHECKBOX_RE.match(line)
        if match is None:
            if CHECKBOX_PREFIX_RE.match(line):
                raise PlanFileError(f"malformed or unsupported checkbox on line {line_number}")
            continue
        state = match.group("state")
        if state not in {" ", "x", "X"}:
            raise PlanFileError(
                f"unsupported checkbox state {state!r} on line {line_number}"
            )
        tasks.append((len(match.group("indent")), state, line_number))

    if not tasks:
        raise PlanFileError("plan must contain at least one checkbox task")

    for index, (_indent, _state, line_number) in enumerate(tasks):
        block_end = tasks[index + 1][2] - 1 if index + 1 < len(tasks) else len(lines)
        block = lines[line_number - 1 : block_end]
        if not any(DONE_WHEN_RE.match(line) for line in block):
            raise PlanFileError(
                f"checkbox task on line {line_number} is missing a 'Done when' clause"
            )

    completed = sum(state in {"x", "X"} for _, state, _ in tasks)
    return TaskCounts(
        total=len(tasks),
        pending=len(tasks) - completed,
        completed=completed,
    )


def validate_plan(slug: str, *, cwd: Path | None = None) -> ValidationResult:
    """Resolve and structurally validate one task-workflow plan."""

    path, context = resolve_plan(slug, cwd=cwd)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise PlanFileError(f"cannot read plan file {path}: {exc}") from exc

    return ValidationResult(
        path=str(path),
        faur=context.is_faur,
        worktree=_worktree_slug(lines, is_faur=context.is_faur),
        tasks=_task_counts(lines),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""

    parser = argparse.ArgumentParser(
        description="Resolve and validate plans in a repository's shared _plans directory."
    )
    subparsers = parser.add_subparsers(dest="operation", required=True)

    resolve_parser = subparsers.add_parser("resolve", help="print a canonical plan path")
    resolve_parser.add_argument("slug", help="plan slug without an .md suffix")
    resolve_parser.add_argument(
        "--create-parent",
        action="store_true",
        help="create the plan's parent directory and allow a missing file",
    )
    resolve_parser.add_argument(
        "--json",
        action="store_true",
        help="emit the path and repository layout as JSON",
    )

    validate_parser = subparsers.add_parser(
        "validate", help="validate a plan and print JSON metadata"
    )
    validate_parser.add_argument("slug", help="plan slug without an .md suffix")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the helper CLI and convert expected failures to concise diagnostics."""

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        if args.operation == "resolve":
            path, context = resolve_plan(
                args.slug,
                create_parent=bool(args.create_parent),
            )
            if args.json:
                result = ResolutionResult(
                    path=str(path),
                    plan_root=str(context.plan_root),
                    faur=context.is_faur,
                )
                print(json.dumps(asdict(result), sort_keys=True))
            else:
                print(path)
            return 0

        result = validate_plan(args.slug)
        print(json.dumps(asdict(result), sort_keys=True))
        return 0
    except PlanFileError as exc:
        LOGGER.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
