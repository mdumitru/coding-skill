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
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Permit direct execution and importlib loading without another installed skill.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_io import StateError, atomic_write, plan_lock
from policy import subject_error

LOGGER = logging.getLogger('plan-file')
SLUG_RE = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
HASH_RE = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
CHECKBOX = re.compile(r'^- \[([ xX])\] (\S.*)$')
METADATA = re.compile(r'^(  |    )- (Group|Task|Commit|Paths|Verify|Evidence|Outcome):\s*(.*)$')
RESERVED = re.compile(r'^\s*(?:-\s*)?(Group|Task|Commit|Paths|Verify|Evidence|Outcome|Done when|Worktree|Launch worktree|Launch branch):', re.I)


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
    group_id: str | None = None
    indent: int = 0


@dataclass(frozen=True)
class Group:
    id: str
    title: str
    completed: bool
    line: int
    end: int
    done_when: str
    verify: list[list[str]]
    evidence: list[dict[str, object]]
    task_ids: list[str]


@dataclass(frozen=True)
class Plan:
    content: str
    worktree: str | None
    launch_worktree: str | None
    launch_branch: str | None
    tasks: list[Task]
    groups: list[Group] = field(default_factory=list)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.content.encode()).hexdigest()


@dataclass(frozen=True)
class ValidationResult:
    path: str
    faur: bool
    worktree: str | None
    tasks: TaskCounts
    groups: TaskCounts = field(default_factory=lambda: TaskCounts(0, 0, 0))


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


def parse_evidence(values: list[str]) -> list[dict[str, object]]:
    evidence: list[dict[str, object]] = []
    for raw in values:
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanFileError('Evidence must be a JSON object') from exc
        if not isinstance(item, dict) or not item:
            raise PlanFileError('Evidence must be a nonempty JSON object')
        evidence.append(item)
    return evidence


def explicit_id(raw: str, kind: str) -> str:
    match = re.fullmatch(r'(?:`([a-z][a-z0-9-]*)`|([a-z][a-z0-9-]*))', raw)
    if match is None or not SLUG_RE.fullmatch(match[1] or match[2]):
        raise PlanFileError(f'malformed {kind} ID: {raw}')
    return match[1] or match[2]


def parse_plan(content: str, *, is_faur: bool, runtime: bool = False) -> Plan:
    lines = visible_lines(content)
    if not any(re.fullmatch(r'# TODO:\s*\S.*', line) for line in lines):
        raise PlanFileError('plan must contain a # TODO: title outside fenced examples')
    top: dict[str, str] = {}
    starts: list[int] = []
    for i, line in enumerate(lines):
        if re.match(r'^\s*-\s*\[', line):
            if not re.fullmatch(r'(?:  )?- \[([ xX])\] (\S.*)', line):
                raise PlanFileError(f'malformed, nested, or unsupported checkbox on line {i + 1}; only groups and one child level are supported')
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
    reserved_ids = {explicit_id(m[3], m[2]) for line in lines
                    if (m := METADATA.fullmatch(line)) and m[2] in {'Task', 'Group'}}
    tasks: list[Task] = []
    groups: list[Group] = []
    consumed: set[int] = set()
    seen_ids: set[str] = set()
    grouped: bool | None = None
    current: Group | None = None
    for n, start in enumerate(starts):
        indent = len(lines[start]) - len(lines[start].lstrip())
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        section = next((i for i in range(start + 1, end) if re.match(r'^#{1,6} ', lines[i])), end)
        fields: dict[str, list[str]] = {}
        done: list[str] = []
        prefix = ' ' * (indent + 2)
        for i in range(start + 1, section):
            line = lines[i]
            if re.match(r'^' + prefix + r'- Done when(?::|\s)\s*\S', line, re.I):
                done.append(re.sub(r'^' + prefix + r'- Done when:?\s*', '', line, flags=re.I))
                consumed.add(i)
            match = METADATA.fullmatch(line)
            if match and match[1] == prefix:
                fields.setdefault(match[2], []).append(match[3])
                consumed.add(i)
        if len(done) != 1:
            raise PlanFileError(f'checkbox task on line {start + 1} requires one subordinate Done when clause')
        for key, values in fields.items():
            if key not in {'Verify', 'Evidence'} and len(values) != 1:
                raise PlanFileError(f'duplicate {key} on task line {start + 1}')
        is_group = 'Group' in fields
        if indent == 0:
            if grouped is not None and grouped != is_group:
                raise PlanFileError('mixed flat tasks and execution groups are forbidden')
            grouped = is_group
            current = None
        elif not grouped or not groups:
            raise PlanFileError('nested task requires a top-level Group ID')
        else:
            current = groups[-1]
            if any(re.match(r'^#{1,6} ', line) for line in lines[current.line + 1:start]):
                raise PlanFileError('child task must remain inside its group before a heading')
        if is_group and indent:
            raise PlanFileError('nested groups are forbidden')
        kind = 'Group' if is_group else 'Task'
        if kind in fields:
            item_id = explicit_id(fields[kind][0], kind)
        else:
            candidate = len(tasks) + 1
            item_id = f'task-{candidate}'
            while item_id in reserved_ids or item_id in seen_ids:
                candidate += 1
                item_id = f'task-{candidate}'
        if item_id in seen_ids:
            raise PlanFileError(f'invalid or duplicate {kind} ID: {item_id}')
        seen_ids.add(item_id)
        match = CHECKBOX.fullmatch(lines[start][indent:])
        assert match
        completed = match[1].lower() == 'x'
        verify = [json_strings(v, 'Verify') for v in fields.get('Verify', [])]
        evidence = parse_evidence(fields.get('Evidence', []))
        if is_group:
            if any(key not in {'Group', 'Verify', 'Evidence'} for key in fields):
                raise PlanFileError(f'group {item_id} contains child-only metadata')
            if not verify:
                raise PlanFileError(f'group {item_id} requires at least one Verify')
            group_end = next((i for i in starts[n + 1:] if not lines[i].startswith(' ')), len(lines))
            groups.append(Group(item_id, match[2], completed, start, group_end, done[0], verify, evidence, []))
            continue
        if 'Group' in fields:
            raise PlanFileError('misplaced Group metadata')
        commit = fields.get('Commit', [None])[0]
        if commit is not None and (error := subject_error(commit)):
            raise PlanFileError(error)
        raw_paths = fields.get('Paths', [''])[0]
        legacy_paths = raw_paths and not raw_paths.startswith('[') and 'Commit' not in fields and 'Verify' not in fields
        paths = json_strings(raw_paths, 'Paths') if raw_paths and not legacy_paths else []
        if any(p.startswith('/') or '..' in Path(p).parts or '\\' in p for p in paths):
            raise PlanFileError('Paths must be relative patterns without parent traversal')
        outcome = fields.get('Outcome', [None])[0]
        if outcome is not None and outcome != 'no-change' and not HASH_RE.fullmatch(outcome):
            raise PlanFileError('Outcome requires a full commit hash or no-change')
        task = Task(item_id, match[2], completed, start, end, 'Task' in fields,
                    done[0], commit, paths, verify, evidence, outcome, current.id if current else None, indent)
        if runtime:
            missing = [key for key, present in [('Task', task.explicit_id), ('Commit', commit), ('Paths', paths), ('Verify', verify)] if not present]
            if missing:
                raise PlanFileError(f'task {item_id} missing executable fields: {", ".join(missing)}')
        if current:
            current.task_ids.append(item_id)
        tasks.append(task)
    for i, line in enumerate(lines):
        if RESERVED.match(line) and i not in consumed and not re.match(r'^(Worktree|Launch worktree|Launch branch):', line):
            raise PlanFileError(f'malformed or misplaced metadata on line {i + 1}')
    if grouped:
        pending_seen = False
        for group in groups:
            children = [t for t in tasks if t.group_id == group.id]
            if not children:
                raise PlanFileError(f'empty group {group.id} requires child tasks')
            if group.completed and (pending_seen or any(not t.completed for t in children)):
                raise PlanFileError(f'inconsistent completion state for group {group.id}')
            if not group.completed:
                pending_seen = True
            if group.completed and not successful_evidence(group.verify, group.evidence):
                raise PlanFileError(f'completed group {group.id} requires matching successful Evidence')
            if not group.completed and group.evidence:
                raise PlanFileError(f'pending group {group.id} cannot carry completion Evidence')
        pending_task_seen = False
        pending_group = next((g.id for g in groups if not g.completed), None)
        for task in tasks:
            if not task.completed:
                pending_task_seen = True
            elif pending_task_seen or not task.outcome or not successful_evidence(task.verify, task.evidence):
                raise PlanFileError(f'inconsistent completion state or missing Outcome/Evidence for child {task.id}')
            if not task.completed and (task.outcome or task.evidence):
                raise PlanFileError(f'pending child {task.id} cannot carry completion records')
            group_index = next(i for i, g in enumerate(groups) if g.id == task.group_id)
            pending_index = next((i for i, g in enumerate(groups) if g.id == pending_group), len(groups))
            if task.completed and group_index > pending_index:
                raise PlanFileError('completed child cannot follow an uncheckpointed group')
    return Plan(content, top.get('Worktree'), top.get('Launch worktree'), top.get('Launch branch'), tasks, groups)


def successful_evidence(verify: list[list[str]], evidence: list[dict[str, object]]) -> bool:
    return bool(verify) and len(verify) == len(evidence) and all(
        type(item.get('exit_code')) is int and item.get('exit_code') == 0 and item.get('argv') == argv
        for argv, item in zip(verify, evidence))


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
            lines.insert(task.line + 1, f'{" " * (task.indent + 2)}- Task: {task.id}\n')
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
    prefix = ' ' * (task.indent + 2)
    added = [f'{prefix}- Outcome: {outcome}\n'] + [f'{prefix}- Evidence: {json.dumps(e, sort_keys=True)}\n' for e in evidence]
    lines[task.line + 1:task.line + 1] = added
    return ''.join(lines)


def mark_group_complete(plan: Plan, group_id: str, evidence: list[dict[str, object]]) -> str:
    group = next((g for g in plan.groups if not g.completed), None)
    if group is None or group.id != group_id:
        raise PlanFileError('group completion requires the next pending group')
    if any(not t.completed for t in plan.tasks if t.group_id == group_id):
        raise PlanFileError('group completion requires every child complete')
    if not successful_evidence(group.verify, evidence):
        raise PlanFileError('group completion requires matching successful Evidence')
    lines = plan.content.splitlines(keepends=True)
    lines[group.line] = lines[group.line].replace('- [ ]', '- [x]', 1)
    lines[group.line + 1:group.line + 1] = [f'  - Evidence: {json.dumps(e, sort_keys=True)}\n' for e in evidence]
    return ''.join(lines)


def validate_plan(slug: str, *, cwd: Path | None = None, runtime: bool = False) -> ValidationResult:
    path, context = resolve_plan(slug, cwd=cwd)
    plan = read_plan(path, context, runtime=runtime)
    completed = sum(t.completed for t in plan.tasks)
    groups_done = sum(g.completed for g in plan.groups)
    return ValidationResult(str(path), context.is_faur, plan.worktree, TaskCounts(len(plan.tasks), len(plan.tasks) - completed, completed), TaskCounts(len(plan.groups), len(plan.groups) - groups_done, groups_done))


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
