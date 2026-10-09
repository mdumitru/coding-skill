#!/usr/bin/env python3
"""Run journaled task workflows; never infer faur inventory or execute shell strings."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_file import (GitContext, Plan, PlanFileError, Task, git_context, git_output,
                       Group, mark_group_complete, successful_evidence, json_strings, parse_plan, read_plan, replace_plan, resolve_plan, set_identity)
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
class ActiveGroup:
    group_id: str
    assignment: str
    start_head: str
    phase: str = 'assigned'
    end_head: str | None = None
    evidence: list[dict[str, object]] = field(default_factory=list)
    checkpoint: str | None = None
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
    revision: str | None = None
    group_protocol: int = 0
    active_group: ActiveGroup | None = None
    group_records: list[dict[str, object]] = field(default_factory=list)


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
        if type(data.get('schema_version')) is not int or data.get('schema_version') != 1 or data.get('plan_path') != str(plan):
            raise StateError('unsupported or mismatching execution state identity')
        identity = Identity(**object_value(data.pop('identity'), 'identity')) if data.get('identity') is not None else data.pop('identity', None)
        active = ActiveTask(**object_value(data.pop('active'), 'active task')) if data.get('active') is not None else data.pop('active', None)
        active_group = ActiveGroup(**object_value(data.pop('active_group'), 'active group')) if data.get('active_group') is not None else data.pop('active_group', None)
        state = Execution(**data, identity=identity, active=active, active_group=active_group)
        if type(state.group_protocol) is not int or state.group_protocol not in {0, 1}:
            raise StateError('unsupported execution group protocol')
        if not isinstance(state.group_records, list) or any(not isinstance(x, dict) for x in state.group_records):
            raise StateError('invalid group records')
        if active_group:
            if active_group.phase not in {'assigned', 'released', 'checkpointing'}:
                raise StateError('unknown group checkpoint phase')
            for label, value in [('group ID', active_group.group_id), ('assignment', active_group.assignment), ('group start HEAD', active_group.start_head)]:
                string_value(value, label)
            if not isinstance(active_group.evidence, list) or any(not isinstance(x, dict) for x in active_group.evidence):
                raise StateError('invalid group evidence')
        if not isinstance(state.runner, list) or not state.runner or any(not isinstance(x, str) or not x for x in state.runner):
            raise StateError('invalid recorded argv runner')
        if not isinstance(state.records, list) or any(not isinstance(x, dict) for x in state.records):
            raise StateError('invalid task records')
        for label, value in [('common_dir', state.common_dir), ('plan_digest', state.plan_digest), ('phase', state.phase), ('origin', state.origin)]:
            string_value(value, label)
        if state.phase not in {'prepared', 'creating', 'created', 'ready', 'revising'}:
            raise StateError('unknown execution phase')
        if state.identity:
            for label, value in [('path', state.identity.path), ('branch', state.identity.branch), ('identity common_dir', state.identity.common_dir)]:
                string_value(value, label)
        if state.active:
            if state.active.phase not in {'begun', 'committing', 'committed', 'no-change'}:
                raise StateError('unknown active transaction phase')
            string_value(state.active.task_id, 'active task ID')
            string_value(state.active.start_head, 'active start HEAD')
            if not isinstance(state.active.evidence, list) or not isinstance(state.active.changes, list) or any(not isinstance(x, str) for x in state.active.changes):
                raise StateError('invalid active evidence/paths')
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
    actual = git_context(root)
    if actual.common_dir != context.common_dir:
        raise StateError('plain worktree belongs to a different repository')
    return Identity(str(root), branch, str(actual.common_dir))


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
            state = Execution(1, str(path), common, plan.digest, 'prepared', runner, str(Path.cwd().resolve()), requested_slug=plan.worktree, group_protocol=1 if plan.groups else 0)
            if any(g.completed for g in plan.groups):
                raise StateError('checked groups require their original execution checkpoint journal; cannot adopt bare group Evidence')
            if context.is_faur:
                data = run_faur(runner, Path.cwd(), 'context', '--json')
                launch = context_identity(data, common)
                state.launch = object_value(data.get('worktree'), 'launch')
                if existing:
                    reused = run_faur(runner, Path.cwd(), 'context', existing, '--json')
                    state.identity = context_identity(reused, common)
                    state.response = reused
                    if state.identity.task_id is None and plan.launch_worktree:
                        legacy_launch = run_faur(runner, Path(plan.launch_worktree), 'context', '--json')
                        checked = context_identity(legacy_launch, common)
                        if checked.path != plan.launch_worktree or checked.branch != plan.launch_branch:
                            raise StateError('legacy launch metadata disagrees with faur context')
                        state.launch = object_value(legacy_launch.get('worktree'), 'legacy launch')
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
        if state.phase == 'creating' and state.response is not None:
            state.identity = created_identity(state.response, common)
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
            save(path, state)  # Preserve acknowledgement even if validation fails.
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
        from completion import import_completed
        state.records = import_completed(Path(state.identity.path), plan)
        state.plan_digest = plan.digest
        state.pending_digest = None
        state.phase = 'ready'
        save(path, state)
        check_identity(state, context, require_cwd=False)
        return state


def begin(slug: str, *, group_id: str | None = None, assignment: str | None = None) -> Execution:
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = load(path)
        worktree = check_identity(state, context)
        if state.phase != 'ready' or plan.digest != state.plan_digest:
            raise StateError('startup incomplete or plan drift; state preserved')
        task = next((t for t in plan.tasks if not t.completed), None)
        check_scope(plan, state, group_id, assignment, task)
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


def check_scope(plan: Plan, state: Execution, group_id: str | None,
                assignment: str | None, task: Task | None = None) -> None:
    if not plan.groups:
        if state.group_protocol or state.active_group or state.group_records or group_id or assignment:
            raise StateError('flat execution does not accept group assignments')
        return
    active = state.active_group
    if state.group_protocol != 1 or active is None:
        raise StateError('grouped execution requires group-begin and a durable assignment')
    if active.group_id != group_id or active.assignment != assignment:
        raise StateError('wrong or stale group assignment; use the recorded group and assignment')
    if active.phase != 'assigned':
        raise StateError('worker assignment is released or checkpointing; child execution refused')
    pending = next((g for g in plan.groups if not g.completed), None)
    if pending is None or pending.id != active.group_id or (task is not None and task.group_id != pending.id):
        raise StateError('worker cannot advance across its execution group boundary')


def group_ready(path: Path, context: GitContext, plan: Plan, state: Execution,
                *, recovery: bool = False) -> Path:
    worktree = check_identity(state, context)
    if not plan.groups or state.group_protocol != 1:
        raise StateError('operation requires a grouped execution journal')
    if state.phase != 'ready' or (state.pending_digest and not recovery) or (plan.digest != state.plan_digest and not (recovery and plan.digest == state.pending_digest)):
        raise StateError('unresolved transaction or plan drift; preserve group journal')
    if state.active and not recovery:
        raise StateError('child transaction remains active; finish that exact child first')
    if not clean(worktree) or in_progress(worktree):
        raise StateError('group operation requires a clean worktree without Git operations')
    return worktree


def group_next(slug: str) -> dict[str, object]:
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = load(path)
        group_ready(path, context, plan, state)
        from completion import validate_prefix, validate_groups
        worktree = Path(state.identity.path)
        validate_prefix(worktree, plan, state)
        validate_groups(plan, state)
        group = next((g for g in plan.groups if not g.completed), None)
        return {'group': asdict(group) if group else None,
                'assignment': asdict(state.active_group) if state.active_group else None}


def group_begin(slug: str, group_id: str) -> Execution:
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = load(path)
        worktree = group_ready(path, context, plan, state)
        from completion import validate_prefix, validate_groups
        validate_prefix(worktree, plan, state)
        validate_groups(plan, state)
        group = next((g for g in plan.groups if not g.completed), None)
        if group is None or group.id != group_id:
            raise StateError('group-begin requires the next pending group ID')
        if state.active_group:
            if state.active_group.group_id != group_id or state.active_group.phase != 'assigned':
                raise StateError('existing assignment must be recovered or checkpointed; never replace it')
            return state
        state.active_group = ActiveGroup(group_id, uuid.uuid4().hex, git_output(worktree, 'rev-parse', 'HEAD'))
        save(path, state)
        return state


def assigned_group(plan: Plan, state: Execution, group_id: str, assignment: str) -> Group:
    active = state.active_group
    if active is None or active.group_id != group_id or active.assignment != assignment:
        raise StateError('wrong or stale group assignment')
    group = next((g for g in plan.groups if g.id == group_id), None)
    if group is None:
        raise StateError('assigned group is missing from plan')
    return group


def group_release(slug: str, group_id: str, assignment: str) -> Execution:
    """Coordinator calls only after its worker has returned; never replaces a token."""
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = load(path)
        worktree = group_ready(path, context, plan, state)
        group = assigned_group(plan, state, group_id, assignment)
        if any(not t.completed for t in plan.tasks if t.group_id == group_id):
            raise StateError('release requires every assigned child complete')
        from completion import validate_prefix, validate_groups
        validate_prefix(worktree, plan, state)
        validate_groups(plan, state)
        if group.completed or state.active_group.phase not in {'assigned', 'released'}:
            raise StateError('assignment cannot be released in its current phase')
        state.active_group.phase = 'released'
        save(path, state)
        return state


def group_complete(slug: str, group_id: str, assignment: str, *, accepted: bool) -> Execution:
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = load(path)
        worktree = group_ready(path, context, plan, state, recovery=True)
        if state.active:
            raise StateError('group completion requires no active child transaction')
        group = assigned_group(plan, state, group_id, assignment)
        active = state.active_group
        assert active
        if not accepted:
            raise StateError('judge group Done when and pass --accepted before checkpointing')
        if active.phase not in {'released', 'checkpointing'}:
            raise StateError('wait for worker return then group-release before checkpointing')
        from completion import validate_prefix, validate_groups, verification
        try:
            validate_prefix(worktree, plan, state)
            # During recovery the Markdown may already contain this checkpoint.
            validate_groups(plan, state, recovering=group_id if active.phase == 'checkpointing' else None)
            head = git_output(worktree, 'rev-parse', 'HEAD')
            group_tasks = [r for r in state.records if r.get('task_id') in group.task_ids]
            if len(group_tasks) != len(group.task_ids) or group_tasks[0].get('start_head') != active.start_head:
                raise StateError('group child evidence prefix does not match assignment boundary')
            if active.phase == 'released':
                if group.completed:
                    raise StateError('group Markdown completed without checkpoint intent')
                active.error = None
                active.evidence = []
                for argv in group.verify:
                    entry = verification(argv, worktree)
                    active.evidence.append(entry)
                    save(path, state)
                    if path.read_text() != plan.content or git_output(worktree, 'rev-parse', 'HEAD') != head:
                        raise StateError('group verification changed plan or HEAD; preserve journal')
                    if not clean(worktree) or in_progress(worktree):
                        raise StateError('group verification mutated worktree; review changes before retry')
                    if entry['exit_code'] != 0:
                        raise StateError(f'group verification failed for {argv}: {entry.get("stderr", "")}')
                active.end_head = head
                active.checkpoint = mark_group_complete(plan, group_id, active.evidence)
                state.pending_digest = hashlib.sha256(active.checkpoint.encode()).hexdigest()
                active.phase = 'checkpointing'
                save(path, state)
            if active.end_head != head or not successful_evidence(group.verify, active.evidence) or active.checkpoint is None:
                raise StateError('group checkpoint HEAD/evidence drift; preserve journal')
            if hashlib.sha256(active.checkpoint.encode()).hexdigest() != state.pending_digest:
                raise StateError('group checkpoint content differs from journal digest')
            proposed = parse_plan(active.checkpoint, is_faur=context.is_faur, runtime=True)
            expected_group = next(g for g in proposed.groups if g.id == group_id)
            if not expected_group.completed or expected_group.evidence != active.evidence:
                raise StateError('group checkpoint Markdown differs from recorded evidence')
            if plan.digest == state.pending_digest:
                updated = plan
            else:
                expected = mark_group_complete(plan, group_id, active.evidence)
                if expected != active.checkpoint:
                    raise StateError('group checkpoint content drift')
                updated = replace_plan(path, context, plan, active.checkpoint)
            state.group_records.append({'group_id': group_id, 'assignment': assignment,
                                        'start_head': active.start_head, 'end_head': head,
                                        'task_ids': group.task_ids, 'accepted': True, 'evidence': active.evidence})
            state.plan_digest = updated.digest
            state.pending_digest = None
            state.active_group = None
            save(path, state)
            return state
        except (StateError, OSError, PlanFileError, subprocess.CalledProcessError) as exc:
            # A failed final state write must leave the durable checkpoint intent intact.
            if state.active_group is not None:
                state.active_group.error = str(exc)
                save(path, state)
            raise


def revise(slug: str, content_path: Path | None) -> Execution:
    """Record an approved pending-task decision without losing runtime evidence."""
    path, context = resolve_plan(slug)
    with plan_lock(path):
        previous = read_plan(path, context, runtime=True)
        state = load(path)
        check_identity(state, context)
        if state.phase == 'revising':
            if state.revision is None or state.pending_digest is None:
                raise StateError('incomplete revision journal')
            content = state.revision
            if content_path and content_path.read_text() != content:
                raise StateError('revision retry content differs from journal')
            if previous.digest not in {state.plan_digest, state.pending_digest}:
                raise StateError('plan drift during revision')
        else:
            if state.phase != 'ready' or previous.digest != state.plan_digest or state.pending_digest:
                raise StateError('revision requires a matching ready plan')
            if state.active and state.active.phase != 'begun':
                raise StateError('finish active completion recovery before revising')
            if content_path is None:
                raise StateError('revision requires --content with approved Markdown')
            content = content_path.read_text(encoding='utf-8')
            proposed = parse_plan(content, is_faur=context.is_faur, runtime=True)
            if (proposed.worktree, proposed.launch_worktree, proposed.launch_branch) != (previous.worktree, previous.launch_worktree, previous.launch_branch):
                raise StateError('revision must preserve worktree identity')
            if [t.id for t in proposed.tasks] != [t.id for t in previous.tasks]:
                raise StateError('revision must preserve stable task IDs and order')
            if [(g.id, g.task_ids) for g in proposed.groups] != [(g.id, g.task_ids) for g in previous.groups]:
                raise StateError('revision must preserve group IDs, order, and membership')
            if state.active_group and state.active_group.phase != 'assigned':
                raise StateError('finish released/checkpointing group before revising')
            for old_group, new_group in zip(previous.groups, proposed.groups):
                old_fields = {k: v for k, v in asdict(old_group).items() if k not in {'line', 'end'}}
                new_fields = {k: v for k, v in asdict(new_group).items() if k not in {'line', 'end'}}
                if old_group.completed != new_group.completed or (old_group.completed and old_fields != new_fields):
                    raise StateError('revision must preserve completed group content/evidence and checked state')
            for old, new in zip(previous.tasks, proposed.tasks):
                if old.completed != new.completed:
                    raise StateError('revision cannot change checked state')
                old_fields = {k: v for k, v in asdict(old).items() if k not in {'line', 'end'}}
                new_fields = {k: v for k, v in asdict(new).items() if k not in {'line', 'end'}}
                if old.completed and old_fields != new_fields:
                    raise StateError('revision cannot change completed task evidence/content')
            state.revision = content
            state.pending_digest = proposed.digest
            state.phase = 'revising'
            save(path, state)
        if previous.content != content:
            updated = replace_plan(path, context, previous, content)
        else:
            updated = previous
        state.plan_digest = updated.digest
        state.phase = 'ready'
        state.pending_digest = None
        state.revision = None
        save(path, state)
        return state


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    for name in ('start', 'begin', 'complete', 'audit', 'report', 'revise', 'group-next', 'group-begin', 'group-release', 'group-complete'):
        p = sub.add_parser(name)
        p.add_argument('slug')
        if name == 'start':
            p.add_argument('--faur-argv', default='["faur", "worktree"]', help='JSON executable argv prefix')
            p.add_argument('--existing', help='explicitly authorized existing faur worktree name')
            p.add_argument('--recover-task-id', help='explicit immutable ID for unacknowledged creation recovery')
        if name in {'begin', 'complete', 'group-begin', 'group-release', 'group-complete'}:
            p.add_argument('--group', required=name.startswith('group-'))
        if name in {'begin', 'complete', 'group-release', 'group-complete'}:
            p.add_argument('--assignment', required=name.startswith('group-'))
        if name == 'group-complete':
            p.add_argument('--accepted', action='store_true')
        if name == 'revise':
            p.add_argument('--content', type=Path)
        if name == 'report':
            p.add_argument('--markdown', action='store_true')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    try:
        if args.operation == 'start':
            state = start(args.slug, runner=json_strings(args.faur_argv, 'faur argv'), existing=args.existing, recover_task_id=args.recover_task_id)
        elif args.operation == 'group-next':
            print(json.dumps(group_next(args.slug), sort_keys=True))
            return 0
        elif args.operation == 'group-begin':
            state = group_begin(args.slug, args.group)
        elif args.operation == 'group-release':
            state = group_release(args.slug, args.group, args.assignment)
        elif args.operation == 'group-complete':
            state = group_complete(args.slug, args.group, args.assignment, accepted=args.accepted)
        elif args.operation == 'revise':
            state = revise(args.slug, args.content)
        elif args.operation == 'begin':
            state = begin(args.slug, group_id=args.group, assignment=args.assignment)
        else:
            from completion import complete, audit, report
            if args.operation == 'report':
                print(report(args.slug, markdown=args.markdown))
                return 0
            state = complete(args.slug, group_id=args.group, assignment=args.assignment) if args.operation == 'complete' else audit(args.slug)
        print(json.dumps(asdict(state), sort_keys=True))
        return 0
    except (PlanFileError, StateError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        LOGGER.error('%s', exc)
        return 2


if __name__ == '__main__':
    # Deferred completion imports must share these dataclasses in CLI processes.
    sys.modules['workflow'] = sys.modules[__name__]
    sys.exit(main())
