#!/usr/bin/env python3
"""Run journaled task workflows; never infer faur inventory or execute shell strings."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_file import (GitContext, Plan, PlanFileError, Task, git_context, git_output,
                       json_strings, read_plan, replace_plan, resolve_plan, set_identity)
from state_io import StateError, atomic_write, plan_lock

LOGGER = logging.getLogger('workflow')


@dataclass(frozen=True)
class Identity:
    path: str
    branch: str
    common_dir: str
    name: str | None = None
    task_id: str | None = None


@dataclass
class ActiveTask:
    task_id: str
    start_head: str
    phase: str = 'begun'
    tree: str | None = None
    commit: str | None = None
    evidence: list[dict[str, object]] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Execution:
    schema_version: int
    plan_path: str
    common_dir: str
    plan_digest: str
    phase: str
    runner: list[str]
    origin: str
    identity: Identity | None = None
    launch: dict[str, object] | None = None
    response: dict[str, object] | None = None
    active: ActiveTask | None = None
    records: list[dict[str, object]] = field(default_factory=list)
    requested_slug: str | None = None
    preview: dict[str, object] | None = None
    pending_digest: str | None = None


def object_value(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise StateError(f'{description} must be a JSON object')
    return value


def string_value(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise StateError(f'{description} must be a nonempty string')
    return value


def state_path(plan: Path) -> Path:
    directory = plan.parent / '.state'
    if directory.resolve() != directory or directory.is_symlink():
        raise StateError('state directory escapes canonical plan root')
    path = directory / f'{plan.stem}.json'
    if path.is_symlink():
        raise StateError('state file is a symlink')
    return path


def save(plan: Path, state: Execution) -> None:
    atomic_write(state_path(plan), json.dumps(asdict(state), sort_keys=True, indent=2) + '\n')


def load(plan: Path) -> Execution:
    try:
        data = object_value(json.loads(state_path(plan).read_text()), 'execution state')
        if data.get('schema_version') != 1 or data.get('plan_path') != str(plan):
            raise StateError('unsupported or mismatching execution state identity')
        identity = Identity(**object_value(data.pop('identity'), 'identity')) if data.get('identity') is not None else data.pop('identity', None)
        active = ActiveTask(**object_value(data.pop('active'), 'active task')) if data.get('active') is not None else data.pop('active', None)
        state = Execution(**data, identity=identity, active=active)
        if not isinstance(state.runner, list) or not state.runner or any(not isinstance(x, str) or not x for x in state.runner):
            raise StateError('invalid recorded argv runner')
        if not isinstance(state.records, list) or any(not isinstance(x, dict) for x in state.records):
            raise StateError('invalid task records')
        return state
    except (OSError, ValueError, TypeError) as exc:
        raise StateError(f'cannot read execution state: {exc}') from exc


def run_faur(runner: list[str], cwd: Path, *args: str) -> dict[str, object]:
    """Require a clean versioned JSON envelope before trusting any identity."""
    try:
        result = subprocess.run([*runner, *args], cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise StateError(f'faur unavailable: {exc}; install a version supporting context/task JSON or supply --faur-argv') from exc
    try:
        data = object_value(json.loads(result.stdout), 'faur response')
    except (ValueError, StateError) as exc:
        raise StateError(f'faur lacks the required JSON contract: {result.stderr.strip() or result.stdout.strip()}') from exc
    if type(data.get('schema_version')) is not int or data['schema_version'] != 1 or data.get('command') != args[0]:
        raise StateError('unsupported faur JSON schema/command; require schema_version 1')
    if result.returncode or data.get('ok') is not True:
        raise StateError(f'faur {args[0]} failed: {data.get("error", result.stderr.strip())}')
    return data


def context_identity(data: dict[str, object], expected_common: str) -> Identity:
    workspace = object_value(data.get('workspace'), 'faur workspace')
    if workspace.get('common_dir') != expected_common:
        raise StateError('faur returned a different Git common directory')
    worktree = object_value(data.get('worktree'), 'faur worktree')
    path = string_value(worktree.get('path'), 'worktree.path')
    if str(Path(path).resolve()) != path or not Path(path).is_dir():
        raise StateError('faur worktree path is not canonical and present')
    lookup = object_value(data.get('task'), 'task context')
    status = lookup.get('status')
    if status not in {'none', 'valid'}:
        raise StateError(f'faur task metadata is {status}: {lookup.get("error")}')
    record = object_value(lookup.get('record'), 'task record') if status == 'valid' else None
    return Identity(path, string_value(worktree.get('branch'), 'branch'), expected_common,
                    string_value(worktree.get('name'), 'name'), string_value(record.get('task_id'), 'task ID') if record else None)


def created_identity(data: dict[str, object], common: str) -> Identity:
    worktree = object_value(data.get('worktree'), 'created worktree')
    record = object_value(data.get('task'), 'created task record')
    if record.get('schema_version') != 1 or object_value(record.get('workspace'), 'task workspace').get('common_dir') != common:
        raise StateError('created task record schema/workspace mismatch')
    task_id = string_value(data.get('task_id'), 'created task ID')
    if record.get('task_id') != task_id or data.get('dry_run') is not False:
        raise StateError('created task ID mismatch or unexpected preview')
    if record.get('phase') != 'ready':
        raise StateError(f'faur task creation/setup incomplete: {record.get("phase")}')
    return Identity(string_value(worktree.get('path'), 'path'), string_value(worktree.get('branch'), 'branch'), common,
                    string_value(worktree.get('name'), 'name'), task_id)


def plain_identity(context: GitContext, cwd: Path) -> Identity:
    root = Path(git_output(cwd, 'rev-parse', '--show-toplevel')).resolve()
    branch = git_output(root, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    return Identity(str(root), branch, str(context.common_dir))


def check_identity(state: Execution, context: GitContext, *, require_cwd: bool = True) -> Path:
    identity = state.identity
    if identity is None or state.common_dir != str(context.common_dir):
        raise StateError('missing or mismatching repository identity')
    path = Path(identity.path)
    if not path.is_dir() or path.resolve() != path:
        raise StateError('recorded worktree is missing or moved')
    if require_cwd and not Path.cwd().resolve().is_relative_to(path):
        raise StateError(f'run this command from recorded worktree: {path}')
    current = context_identity(run_faur(state.runner, path, 'context', '--json'), state.common_dir) if context.is_faur else plain_identity(context, path)
    if current != identity:
        raise StateError('recorded worktree branch/path/task identity changed')
    return path


def in_progress(path: Path) -> bool:
    for name in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'BISECT_START'):
        target = Path(git_output(path, 'rev-parse', '--git-path', name))
        if (path / target).exists():
            return True
    return False


def clean(path: Path) -> bool:
    return not git_output(path, 'status', '--porcelain', '--untracked-files=all')


def start(slug: str, *, runner: list[str], existing: str | None = None, recover_task_id: str | None = None) -> Execution:
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        common = str(context.common_dir)
        if state_path(path).exists():
            state = load(path)
            if state.common_dir != common or state.runner != runner:
                raise StateError('startup repository/argv runner changed; use the recorded runner')
            if state.phase == 'ready':
                if plan.digest != state.plan_digest:
                    raise StateError('plan drift since startup; state preserved')
                check_identity(state, context)
                return state
            if state.origin != str(Path.cwd().resolve()):
                raise StateError('resume startup from its recorded origin directory')
        else:
            state = Execution(1, str(path), common, plan.digest, 'prepared', runner, str(Path.cwd().resolve()), requested_slug=plan.worktree)
            if context.is_faur:
                data = run_faur(runner, Path.cwd(), 'context', '--json')
                launch = context_identity(data, common)
                state.launch = object_value(data.get('worktree'), 'launch')
                if existing:
                    reused = run_faur(runner, Path.cwd(), 'context', existing, '--json')
                    state.identity = context_identity(reused, common)
                    state.response = reused
                    state.phase = 'created'
                else:
                    # Dry-run proves task capability and lets faur select the preview identity.
                    state.preview = run_faur(runner, Path.cwd(), 'add', str(plan.worktree), '--task', '--unique', '--json', '--dry-run')
                    if state.preview.get('dry_run') is not True or not isinstance(state.preview.get('worktree'), dict):
                        raise StateError('faur does not support strict task creation previews')
                del launch  # Identity selection belongs to faur; persist its actual response.
            else:
                if existing:
                    raise StateError('--existing is only valid in a faur workspace')
                state.identity = plain_identity(context, Path.cwd())
                state.phase = 'created'
            save(path, state)
        if state.phase == 'creating':
            # No second add: an unacknowledged subprocess may already have succeeded.
            if not recover_task_id:
                raise StateError('startup interrupted before durable faur response; inspect faur context and retry with --recover-task-id <immutable-id>; no second task will be created')
            preview = object_value(state.preview, 'creation preview')
            name = string_value(object_value(preview.get('worktree'), 'preview worktree').get('name'), 'preview name')
            recovered = run_faur(runner, Path.cwd(), 'context', name, '--json')
            identity = context_identity(recovered, common)
            lookup = object_value(recovered.get('task'), 'recovered lookup')
            record = object_value(lookup.get('record'), 'recovered record')
            if identity.task_id != recover_task_id or record.get('requested_slug') != state.requested_slug:
                raise StateError('recovery task ID/request does not match startup intent')
            launch = object_value(record.get('launch'), 'recovered launch')
            if state.launch is None or any(launch.get(k) != state.launch.get(k) for k in ('path', 'branch', 'head')):
                raise StateError('recovery launch identity changed')
            state.response = {'task': record, 'worktree': recovered['worktree'], 'launch': launch}
            state.identity = identity
            state.phase = 'created'
            save(path, state)
        if state.phase == 'prepared':
            if plan.digest != state.plan_digest:
                raise StateError('plan drift before creation')
            state.phase = 'creating'
            save(path, state)
            response = run_faur(runner, Path.cwd(), 'add', str(state.requested_slug), '--task', '--unique', '--json')
            state.response = response
            state.identity = created_identity(response, common)
            state.phase = 'created'
            save(path, state)
        if state.phase != 'created' or state.identity is None:
            raise StateError(f'unsupported startup journal phase: {state.phase}')
        if context.is_faur:
            response = object_value(state.response, 'startup response')
            launch = response.get('launch')
            if launch is None and state.identity.task_id:
                record = object_value(object_value(response.get('task'), 'lookup').get('record'), 'task record')
                launch = record.get('launch')
            launch = object_value(launch if launch is not None else state.launch, 'launch identity')
            content = set_identity(plan, str(state.identity.name), string_value(launch.get('path'), 'launch path'), string_value(launch.get('branch'), 'launch branch'))
            expected = hashlib.sha256(content.encode()).hexdigest()
            if plan.digest != state.plan_digest and plan.digest != expected:
                raise StateError('plan drift after creation; task identity preserved')
            state.pending_digest = expected
            # Journal expected replacement before writing Markdown.
            save(path, state)
            if plan.content != content:
                plan = replace_plan(path, context, plan, content)
        state.plan_digest = plan.digest
        state.pending_digest = None
        state.phase = 'ready'
        save(path, state)
        check_identity(state, context, require_cwd=False)
        return state


def begin(slug: str) -> Execution:
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = load(path)
        worktree = check_identity(state, context)
        if state.phase != 'ready' or plan.digest != state.plan_digest:
            raise StateError('startup incomplete or plan drift; state preserved')
        task = next((t for t in plan.tasks if not t.completed), None)
        if task is None:
            raise StateError('all tasks are already complete')
        if state.active:
            if state.active.task_id != task.id:
                raise StateError('another task is already active')
            return state
        if not clean(worktree) or in_progress(worktree):
            raise StateError('begin requires a clean worktree without Git operations in progress')
        state.active = ActiveTask(task.id, git_output(worktree, 'rev-parse', 'HEAD'))
        save(path, state)
        return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('start', 'begin', 'complete', 'audit', 'report'):
        p = sub.add_parser(name)
        p.add_argument('slug')
        if name == 'start':
            p.add_argument('--faur-argv', default='["faur", "worktree"]', help='JSON executable argv prefix')
            p.add_argument('--existing', help='explicitly authorized existing faur worktree name')
            p.add_argument('--recover-task-id', help='explicit immutable ID for unacknowledged creation recovery')
        if name == 'report':
            p.add_argument('--markdown', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    try:
        if args.operation == 'start':
            state = start(args.slug, runner=json_strings(args.faur_argv, 'faur argv'), existing=args.existing, recover_task_id=args.recover_task_id)
        elif args.operation == 'begin':
            state = begin(args.slug)
        else:
            raise StateError(f'{args.operation} transaction is not yet available')
        print(json.dumps(asdict(state), sort_keys=True))
        return 0
    except (PlanFileError, StateError, OSError, ValueError) as exc:
        LOGGER.error('%s', exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
