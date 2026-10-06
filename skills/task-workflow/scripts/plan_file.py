#!/usr/bin/env python3
"""Resolve, parse, and safely mutate the documented Markdown task grammar."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Permit direct execution and importlib loading without another installed skill.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_io import StateError, atomic_write, plan_lock
from policy import subject_error

LOGGER = logging.getLogger('plan-file')
SLUG_RE = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
HASH_RE = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
CHECKBOX = re.compile(r'^- \[([ xX])\] (\S.*)$')
METADATA = re.compile(r'^  - (Task|Commit|Paths|Verify|Evidence|Outcome):\s*(.*)$')
RESERVED = re.compile(r'^\s*(?:-\s*)?(Task|Commit|Paths|Verify|Evidence|Outcome|Worktree|Launch worktree|Launch branch):', re.I)


class PlanFileError(Exception):
    """An actionable plan resolution or validation failure."""


@dataclass(frozen=True)
class GitContext:
    common_dir: Path
    plan_root: Path
    is_faur: bool


@dataclass(frozen=True)
class TaskCounts:
    total: int
    pending: int
    completed: int


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    completed: bool
    line: int
    end: int
    explicit_id: bool
    done_when: str
    commit: str | None
    paths: list[str]
    verify: list[list[str]]
    evidence: list[dict[str, object]]
    outcome: str | None


@dataclass(frozen=True)
class Plan:
    content: str
    worktree: str | None
    launch_worktree: str | None
    launch_branch: str | None
    tasks: list[Task]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True)
class ValidationResult:
    path: str
    faur: bool
    worktree: str | None
    tasks: TaskCounts


def git_output(cwd: Path, *args: str) -> str:
    try:
        return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise PlanFileError(f'cannot determine Git context: {detail}') from exc


def git_context(cwd: Path | None = None) -> GitContext:
    start = (cwd or Path.cwd()).resolve()
    common = Path(git_output(start, 'rev-parse', '--git-common-dir'))
    common = (start / common).resolve() if not common.is_absolute() else common.resolve()
    faur = common.name == '.bare'
    root = common.parent if faur else Path(git_output(start, 'rev-parse', '--show-toplevel')).resolve()
    expected = root / '_plans'
    resolved = expected.resolve()
    if resolved != expected:
        raise PlanFileError(f'plan root escapes expected directory through a symlink: {expected}')
    return GitContext(common, resolved, faur)


def resolve_plan(slug: str, *, cwd: Path | None = None, create_parent: bool = False) -> tuple[Path, GitContext]:
    if slug.endswith('.md'):
        raise PlanFileError('plan slug must omit the .md suffix')
    if not SLUG_RE.fullmatch(slug):
        raise PlanFileError('plan slug must be lowercase kebab-case and start with a letter')
    context = git_context(cwd)
    expected = context.plan_root / f'{slug}.md'
    path = expected.resolve()
    if path != expected:
        raise PlanFileError(f'plan path escapes or aliases another file through a symlink: {expected}')
    if create_parent:
        context.plan_root.mkdir(parents=True, exist_ok=True)
    elif not path.is_file():
        raise PlanFileError(f'plan file does not exist: {path}')
    if path.exists() and not path.is_file():
        raise PlanFileError(f'plan path is not a regular file: {path}')
    return path, context


def visible_lines(content: str) -> list[str]:
    """Blank fenced examples without changing source line positions."""
    result: list[str] = []
    fence = ''
    for line in content.splitlines():
        match = re.match(r'^\s*(`{3,}|~{3,})', line)
        if match:
            token = match.group(1)
            if not fence:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = ''
            result.append('')
        else:
            result.append('' if fence else line)
    return result


def json_strings(raw: str, field: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanFileError(f'{field}: requires a JSON argument/pattern array: {exc.msg}') from exc
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x or '\x00' in x for x in value):
        raise PlanFileError(f'{field}: requires a nonempty JSON array of nonempty strings')
    return value


def parse_plan(content: str, *, is_faur: bool, runtime: bool = False) -> Plan:
    lines = visible_lines(content)
    if not any(re.fullmatch(r'# TODO:\s*\S.*', line) for line in lines):
        raise PlanFileError('plan must contain a # TODO: title outside fenced examples')
    top: dict[str, str] = {}
    starts: list[int] = []
    for i, line in enumerate(lines):
        if re.match(r'^\s*-\s*\[', line):
            if not CHECKBOX.fullmatch(line):
                raise PlanFileError(f'malformed, nested, or unsupported checkbox on line {i + 1}')
            starts.append(i)
        match = re.match(r'^(Worktree|Launch worktree|Launch branch):\s*(.*)$', line)
        if match:
            if starts or match[1] in top:
                raise PlanFileError(f'duplicate or misplaced {match[1]} field on line {i + 1}')
            raw = match[2]
            value = raw[1:-1] if raw.startswith('`') and raw.endswith('`') else raw
            if not value or '`' in value or (match[1] != 'Launch worktree' and re.search(r'\s', value)):
                raise PlanFileError(f'malformed {match[1]} field')
            top[match[1]] = value
        elif RESERVED.match(line) and not METADATA.fullmatch(line):
            raise PlanFileError(f'malformed or misplaced metadata on line {i + 1}')
    if not is_faur and top:
        raise PlanFileError('plain-repository plan must not contain worktree metadata')
    if is_faur and ('Worktree' not in top or not SLUG_RE.fullmatch(top['Worktree'])):
        raise PlanFileError('faur plan must contain exactly one valid Worktree: slug')
    if ('Launch worktree' in top) != ('Launch branch' in top):
        raise PlanFileError('launch metadata must contain both Launch worktree and Launch branch')
    if 'Launch worktree' in top and not Path(top['Launch worktree']).is_absolute():
        raise PlanFileError('Launch worktree must be an absolute path')
    if not starts:
        raise PlanFileError('plan must contain at least one checkbox task')
    reserved_ids = {match[1].strip('`') for line in lines if (match := re.fullmatch(r'  - Task:\s*(.*)', line))}
    tasks: list[Task] = []
    consumed: set[int] = set()
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        section = next((i for i in range(start + 1, end) if re.match(r'^#{1,6} ', lines[i])), end)
        fields: dict[str, list[str]] = {}
        done: list[str] = []
        for i in range(start + 1, section):
            line = lines[i]
            if re.match(r'^  - Done when(?::|\s)\s*\S', line, re.I):
                done.append(re.sub(r'^  - Done when:?\s*', '', line, flags=re.I))
            match = METADATA.fullmatch(line)
            if match:
                fields.setdefault(match[1], []).append(match[2])
                consumed.add(i)
        if len(done) != 1:
            raise PlanFileError(f'checkbox task on line {start + 1} requires one subordinate Done when clause')
        for key, values in fields.items():
            if key not in {'Verify', 'Evidence'} and len(values) != 1:
                raise PlanFileError(f'duplicate {key} on task line {start + 1}')
        if 'Task' in fields:
            raw_id = fields['Task'][0]
            match_id = re.fullmatch(r'(?:`([a-z][a-z0-9-]*)`|([a-z][a-z0-9-]*))', raw_id)
            if match_id is None:
                raise PlanFileError(f'malformed Task ID: {raw_id}')
            task_id = match_id[1] or match_id[2]
        else:
            candidate = n + 1
            task_id = f'task-{candidate}'
            while task_id in reserved_ids or any(t.id == task_id for t in tasks):
                candidate += 1
                task_id = f'task-{candidate}'
        if not SLUG_RE.fullmatch(task_id) or any(t.id == task_id for t in tasks):
            raise PlanFileError(f'invalid or duplicate Task ID: {task_id}')
        commit = fields.get('Commit', [None])[0]
        if commit is not None and (error := subject_error(commit)):
            raise PlanFileError(error)
        raw_paths = fields.get('Paths', [''])[0]
        legacy_paths = raw_paths and not raw_paths.startswith('[') and 'Commit' not in fields and 'Verify' not in fields
        paths = json_strings(raw_paths, 'Paths') if raw_paths and not legacy_paths else []
        if any(p.startswith('/') or '..' in Path(p).parts or '\\' in p for p in paths):
            raise PlanFileError('Paths must be relative patterns without parent traversal')
        verify = [json_strings(v, 'Verify') for v in fields.get('Verify', [])]
        evidence: list[dict[str, object]] = []
        for raw in fields.get('Evidence', []):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise PlanFileError('Evidence must be a JSON object') from exc
            if not isinstance(item, dict) or not item:
                raise PlanFileError('Evidence must be a nonempty JSON object')
            evidence.append(item)
        outcome = fields.get('Outcome', [None])[0]
        if outcome is not None and outcome != 'no-change' and not HASH_RE.fullmatch(outcome):
            raise PlanFileError('Outcome requires a full commit hash or no-change')
        match = CHECKBOX.fullmatch(lines[start])
        assert match
        task = Task(task_id, match[2], match[1].lower() == 'x', start, end, 'Task' in fields, done[0], commit, paths, verify, evidence, outcome)
        if runtime:
            missing = [key for key, present in [('Task', task.explicit_id), ('Commit', commit), ('Paths', paths), ('Verify', verify)] if not present]
            if missing:
                raise PlanFileError(f'task {task_id} missing executable fields: {", ".join(missing)}')
        tasks.append(task)
    for i, line in enumerate(lines):
        if METADATA.fullmatch(line) and i not in consumed:
            raise PlanFileError(f'misplaced task metadata on line {i + 1}')
    return Plan(content, top.get('Worktree'), top.get('Launch worktree'), top.get('Launch branch'), tasks)


def read_plan(path: Path, context: GitContext, *, runtime: bool = False) -> Plan:
    try:
        return parse_plan(path.read_text(encoding='utf-8'), is_faur=context.is_faur, runtime=runtime)
    except (OSError, UnicodeError) as exc:
        raise PlanFileError(f'cannot read plan {path}: {exc}') from exc


def replace_plan(path: Path, context: GitContext, previous: Plan, content: str) -> Plan:
    """Call while holding the plan lock; reject concurrent edits and bad replacements."""
    if path.read_text(encoding='utf-8') != previous.content:
        raise PlanFileError('plan changed during transaction; preserve state and review drift')
    updated = parse_plan(content, is_faur=context.is_faur)
    atomic_write(path, content)
    return updated


def migrate_ids(plan: Plan) -> str:
    lines = plan.content.splitlines(keepends=True)
    for task in reversed(plan.tasks):
        if not task.explicit_id:
            lines.insert(task.line + 1, f'  - Task: {task.id}\n')
    return ''.join(lines)


def set_identity(plan: Plan, worktree: str, launch_path: str, launch_branch: str) -> str:
    lines = plan.content.splitlines(keepends=True)
    visible = visible_lines(plan.content)
    for i in reversed(range(len(lines))):
        if re.match(r'^(Worktree|Launch worktree|Launch branch):', visible[i]):
            del lines[i]
    title = next(i for i, line in enumerate(lines) if line.startswith('# TODO:'))
    while title + 1 < len(lines) and not lines[title + 1].strip():
        del lines[title + 1]
    lines[title + 1:title + 1] = [f'\nWorktree: `{worktree}`\n', f'Launch worktree: `{launch_path}`\n', f'Launch branch: `{launch_branch}`\n\n']
    return ''.join(lines)


def mark_complete(plan: Plan, task_id: str, *, outcome: str, evidence: list[dict[str, object]]) -> str:
    if not evidence or any(type(e.get('exit_code')) is not int or e.get('exit_code') != 0 or not e.get('argv') for e in evidence):
        raise PlanFileError('completion requires successful verification evidence')
    if outcome != 'no-change' and not HASH_RE.fullmatch(outcome):
        raise PlanFileError('completion requires a full commit hash or verified no-change outcome')
    task = next((t for t in plan.tasks if t.id == task_id), None)
    if task is None or task.completed or next((t.id for t in plan.tasks if not t.completed), None) != task_id:
        raise PlanFileError('completion requires the next pending task')
    lines = plan.content.splitlines(keepends=True)
    lines[task.line] = lines[task.line].replace('- [ ]', '- [x]', 1)
    added = [f'  - Outcome: {outcome}\n'] + [f'  - Evidence: {json.dumps(e, sort_keys=True)}\n' for e in evidence]
    lines[task.line + 1:task.line + 1] = added
    return ''.join(lines)


def validate_plan(slug: str, *, cwd: Path | None = None, runtime: bool = False) -> ValidationResult:
    path, context = resolve_plan(slug, cwd=cwd)
    plan = read_plan(path, context, runtime=runtime)
    completed = sum(t.completed for t in plan.tasks)
    return ValidationResult(str(path), context.is_faur, plan.worktree, TaskCounts(len(plan.tasks), len(plan.tasks) - completed, completed))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('resolve', 'validate', 'init', 'next', 'complete'):
        p = sub.add_parser(name)
        p.add_argument('slug')
        if name == 'resolve':
            p.add_argument('--create-parent', action='store_true')
            p.add_argument('--json', action='store_true')
        elif name in {'validate', 'next'}:
            p.add_argument('--runtime', action='store_true')
        elif name == 'init':
            p.add_argument('--content', type=Path, help='UTF-8 Markdown input file')
            p.add_argument('--migrate', action='store_true', help='add stable IDs to an existing plan')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    try:
        if args.operation == 'complete':
            # Completion is a transaction, never an arbitrary checkbox mutation.
            from workflow import main as workflow_main
            return workflow_main(['complete', args.slug])
        path, context = resolve_plan(args.slug, create_parent=args.operation == 'init' or (args.operation == 'resolve' and args.create_parent))
        if args.operation == 'resolve':
            print(json.dumps({'path': str(path), 'plan_root': str(context.plan_root), 'faur': context.is_faur}, sort_keys=True) if args.json else path)
        elif args.operation == 'init':
            with plan_lock(path):
                if args.migrate:
                    plan = read_plan(path, context)
                    replace_plan(path, context, plan, migrate_ids(plan))
                else:
                    if args.content is None:
                        raise PlanFileError('init requires --content or --migrate')
                    content = args.content.read_text(encoding='utf-8')
                    plan = parse_plan(content, is_faur=context.is_faur)
                    atomic_write(path, migrate_ids(plan), exclusive=True)
            print(json.dumps(asdict(validate_plan(args.slug)), sort_keys=True))
        elif args.operation == 'next':
            plan = read_plan(path, context, runtime=args.runtime)
            task = next((t for t in plan.tasks if not t.completed), None)
            print(json.dumps(asdict(task) if task else None, sort_keys=True))
        else:
            print(json.dumps(asdict(validate_plan(args.slug, runtime=args.runtime)), sort_keys=True))
        return 0
    except (PlanFileError, StateError, OSError, UnicodeError) as exc:
        LOGGER.error('%s', exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
