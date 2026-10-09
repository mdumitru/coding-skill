#!/usr/bin/env python3
"""Emit a validated, scoped handoff without transcript or prior verification logs."""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import workflow
from completion import validate_groups, validate_prefix
from plan_file import Plan, PlanFileError, Task, read_plan, resolve_plan, visible_lines
from state_io import StateError

LOGGER = logging.getLogger('group-context')


@dataclass(frozen=True)
class WorkerTask:
    id: str
    title: str
    group_id: str
    commit: str
    paths: list[str]
    verify: list[list[str]]
    done_when: str
    source_line: int
    instructions: str


@dataclass(frozen=True)
class CompletionFact:
    id: str
    kind: str
    outcome: str
    end_head: str
    verified: bool


@dataclass(frozen=True)
class SummaryLimits:
    previous_limit: int
    omitted_tasks: int
    omitted_groups: int
    truncated: bool
    details_plan: str
    details_journal: str


@dataclass(frozen=True)
class GroupAcceptance:
    id: str
    title: str
    done_when: str
    verify: list[list[str]]
    instructions: str


@dataclass(frozen=True)
class WorkerContext:
    schema_version: int
    plan_slug: str
    plan_path: str
    journal_path: str
    plan_digest: str
    identity: workflow.Identity
    helpers: dict[str, str]
    assignment: workflow.ActiveGroup
    active_task: str | None
    shared_context: str
    group: GroupAcceptance
    remaining_tasks: list[WorkerTask]
    previous_tasks: list[CompletionFact]
    previous_groups: list[CompletionFact]
    limits: SummaryLimits


def shared_context(plan: Plan) -> str:
    lines = visible_lines(plan.content)
    starts = [i for i, line in enumerate(lines) if line == '## Context and decisions']
    if len(starts) != 1 or starts[0] >= plan.groups[0].line:
        raise StateError('group handoff requires exactly one ## Context and decisions section before the groups; record an approved revision')
    start = starts[0] + 1
    end = next((i for i in range(start, plan.groups[0].line)
                if re.match(r'^#{1,2} ', lines[i])), plan.groups[0].line)
    content = '\n'.join(plan.content.splitlines()[start:end]).strip()
    if not content:
        raise StateError('shared Context and decisions section is empty; persist the settled decisions')
    return content


def task_instructions(plan: Plan, task: Task) -> str:
    """Keep the selected child's prose, never a following group or section."""
    visible = visible_lines(plan.content)
    end = next((i for i in range(task.line + 1, task.end)
                if re.match(r'^#{1,6} ', visible[i])), task.end)
    return '\n'.join(plan.content.splitlines()[task.line:end]).rstrip()


def context(slug: str, group_id: str, assignment: str, *, previous_limit: int = 12) -> WorkerContext:
    if not 0 <= previous_limit <= 100:
        raise StateError('previous-limit must be between 0 and 100')
    path, git = resolve_plan(slug)
    lock = path.parent / '.state' / f'{slug}.lock'
    if lock.exists() or lock.is_symlink():
        raise StateError(f'workflow is locked: {lock}; wait for the current operation, preserve stale lock evidence')
    journal_path = workflow.state_path(path)
    snapshot = journal_path.read_bytes()
    plan = read_plan(path, git, runtime=True)
    state = workflow.load(path)
    worktree = workflow.check_identity(state, git)
    if state.phase != 'ready' or state.pending_digest or state.plan_digest != plan.digest:
        raise StateError('group handoff refused: unresolved transaction or plan drift; recover the exact pending operation')
    if state.active and state.active.phase != 'begun':
        raise StateError('group handoff refused: complete the journaled child transaction before handing off')
    if workflow.in_progress(worktree) or not workflow.clean(worktree):
        raise StateError('group handoff requires a clean worktree; retain and resolve pending edits first')
    task = next((t for t in plan.tasks if not t.completed), None)
    workflow.check_scope(plan, state, group_id, assignment)
    if state.active and (task is None or state.active.task_id != task.id):
        raise StateError('active child differs from next pending task')
    validate_prefix(worktree, plan, state)
    validate_groups(plan, state)
    group = workflow.assigned_group(plan, state, group_id, assignment)
    active = state.active_group
    assert active and state.identity
    children = [t for t in plan.tasks if t.group_id == group_id and not t.completed]
    tasks = [WorkerTask(t.id, t.title, group_id, str(t.commit), t.paths, t.verify, t.done_when,
                        t.line + 1, task_instructions(plan, t)) for t in children]
    task_records = state.records[-previous_limit:] if previous_limit else []
    group_records = state.group_records[-previous_limit:] if previous_limit else []
    previous_tasks = [CompletionFact(str(r['task_id']), 'task', str(r['outcome']), str(r['end_head']), True)
                      for r in task_records]
    previous_groups = [CompletionFact(str(r['group_id']), 'group', 'checkpoint', str(r['end_head']), True)
                       for r in group_records]
    omitted_tasks = len(state.records) - len(task_records)
    omitted_groups = len(state.group_records) - len(group_records)
    limits = SummaryLimits(previous_limit, omitted_tasks, omitted_groups,
                           bool(omitted_tasks or omitted_groups), str(path), str(journal_path))
    scripts = Path(__file__).resolve().parent
    helpers = {name: str((scripts / name).resolve()) for name in ('workflow.py', 'plan_file.py', 'group_context.py')}
    helpers['execute_reference'] = str(scripts.parent / 'execute-workflow.md')
    # Group prose ends where its first child starts; drop prior verification output.
    first_child = next(t.line for t in plan.tasks if t.group_id == group_id)
    group_lines = plan.content.splitlines()[group.line:first_child]
    group_instructions = '\n'.join(line for line in group_lines if not line.startswith('  - Evidence:')).rstrip()
    result = WorkerContext(1, slug, str(path), str(journal_path), plan.digest, state.identity, helpers,
                           active, state.active.task_id if state.active else None, shared_context(plan),
                           GroupAcceptance(group.id, group.title, group.done_when, group.verify, group_instructions),
                           tasks, previous_tasks, previous_groups, limits)
    if path.read_text(encoding='utf-8') != plan.content or journal_path.read_bytes() != snapshot or lock.exists():
        raise StateError('workflow changed while reading context; retry after the current operation')
    return result


def render(value: WorkerContext, *, max_chars: int = 60000) -> str:
    if max_chars < 1000:
        raise StateError('max-chars must be at least 1000')
    output = json.dumps(asdict(value), sort_keys=True, indent=2)
    if len(output) > max_chars:
        raise StateError(f'handoff exceeds max-chars ({len(output)} > {max_chars}); no tasks or decisions were truncated; raise the limit or revise group size. Details: {value.plan_path}')
    return output


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('slug')
    parser.add_argument('--group', required=True)
    parser.add_argument('--assignment', required=True)
    parser.add_argument('--previous-limit', type=int, default=12)
    parser.add_argument('--max-chars', type=int, default=60000)
    args = parser.parse_args(argv)
    try:
        value = context(args.slug, args.group, args.assignment, previous_limit=args.previous_limit)
        print(render(value, max_chars=args.max_chars))
        return 0
    except (PlanFileError, StateError, OSError, ValueError) as exc:
        LOGGER.error('%s', exc)
        return 2


if __name__ == '__main__':
    sys.exit(main())
