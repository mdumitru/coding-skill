#!/usr/bin/env python3
"""Git change inspection and journaled completion, kept within this skill."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from plan_file import Plan, Task, git_output, mark_complete, read_plan, replace_plan, resolve_plan
from policy import forbidden_path, subject_error
from state_io import StateError, plan_lock


@dataclass(frozen=True)
class Changes:
    paths: list[str]
    staged: list[str]
    unstaged: list[str]
    fingerprint: str
    content_digest: str = ''


def git_bytes(path: Path, *args: str) -> bytes:
    return subprocess.run(['git', *args], cwd=path, check=True, capture_output=True).stdout


def names(data: bytes) -> list[str]:
    return [p.decode('utf-8', 'surrogateescape') for p in data.split(b'\0') if p]


def content_digest(path: Path, paths: list[str]) -> str:
    digest = hashlib.sha256()
    for name in paths:
        file = path / name
        digest.update(os.fsencode(name))
        if file.is_symlink():
            digest.update(b'link' + os.fsencode(os.readlink(file)))
        elif file.is_file():
            digest.update(b'file' + str(file.stat().st_mode & 0o111).encode() + file.read_bytes())
        elif not file.exists():
            digest.update(b'deleted')
        else:
            raise StateError(f'unsupported changed object: {name}')
    return digest.hexdigest()


def inspect(path: Path) -> Changes:
    staged = names(git_bytes(path, 'diff', '--cached', '--name-only', '--no-renames', '-z'))
    unstaged = names(git_bytes(path, 'diff', '--name-only', '--no-renames', '-z'))
    untracked = names(git_bytes(path, 'ls-files', '--others', '--exclude-standard', '-z'))
    digest = hashlib.sha256()
    for args in (('diff', '--binary', '--no-ext-diff', '--no-textconv', '--no-renames'), ('diff', '--cached', '--binary', '--no-ext-diff', '--no-textconv', '--no-renames')):
        digest.update(git_bytes(path, *args))
    for name in sorted(untracked):
        file = path / name
        digest.update(os.fsencode(name))
        if file.is_symlink():
            digest.update(os.fsencode(os.readlink(file)))
        elif file.is_file():
            digest.update(file.read_bytes())
            digest.update(str(file.stat().st_mode).encode())
        else:
            raise StateError(f'unsupported untracked object: {name}')
    changed_paths = sorted(set(staged + unstaged + untracked))
    return Changes(changed_paths, sorted(staged), sorted(unstaged), digest.hexdigest(), content_digest(path, changed_paths))


def guard(changes: Changes, task: Task) -> None:
    unexpected = [p for p in changes.paths if forbidden_path(p) or not any(fnmatch.fnmatchcase(p, pattern) for pattern in task.paths)]
    if unexpected:
        raise StateError(f'unauthorized changed/staged paths: {", ".join(unexpected)}')
    partial = sorted(set(changes.staged) & set(changes.unstaged))
    if partial:
        raise StateError(f'partial staging refused; preserve and review files: {", ".join(partial)}')


def evidence_valid(task: Task, evidence: object) -> bool:
    return (isinstance(evidence, list) and len(evidence) == len(task.verify)
            and all(isinstance(item, dict) and type(item.get('exit_code')) is int and item.get('exit_code') == 0 and item.get('argv') == argv
                    for item, argv in zip(evidence, task.verify)))


@dataclass(frozen=True)
class CommitFacts:
    parent: str
    tree: str
    message: str
    paths: list[str]


def commit_facts(path: Path, commit: str) -> CommitFacts:
    try:
        parent = git_output(path, 'rev-parse', f'{commit}^')
        tree = git_output(path, 'rev-parse', f'{commit}^{{tree}}')
        message = git_output(path, 'show', '-s', '--format=%B', commit)
        changed = names(git_bytes(path, 'diff-tree', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', commit))
        return CommitFacts(parent, tree, message, changed)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise StateError(f'cannot inspect recorded commit {commit}: {exc}') from exc


def record_valid(path: Path, task: Task, record: dict[str, object]) -> None:
    if record.get('task_id') != task.id or not evidence_valid(task, record.get('evidence')):
        raise StateError(f'task {task.id} lacks complete successful verification records')
    outcome = record.get('outcome')
    end_head = record.get('end_head')
    if not isinstance(end_head, str):
        raise StateError(f'task {task.id} has no recorded HEAD')
    if subprocess.run(['git', 'merge-base', '--is-ancestor', end_head, 'HEAD'], cwd=path, capture_output=True).returncode:
        raise StateError(f'task {task.id} commit ancestry was rewritten')
    if outcome == 'no-change':
        if record.get('start_head') != end_head or record.get('tree') != git_output(path, 'rev-parse', f'{end_head}^{{tree}}'):
            raise StateError('no-change outcome does not match its recorded start HEAD')
    elif isinstance(outcome, str):
        facts = commit_facts(path, outcome)
        parent, tree, message, paths = facts.parent, facts.tree, facts.message, facts.paths
        if (outcome != end_head or parent != record.get('start_head') or tree != record.get('tree') or message != task.commit or subject_error(message)):
            raise StateError(f'task {task.id} recorded commit no longer matches transaction')
        guard(Changes(paths, [], [], ''), task)
    else:
        raise StateError(f'task {task.id} has invalid outcome')


def import_completed(path: Path, plan: Plan) -> list[dict[str, object]]:
    """Adopt explicit legacy commit/evidence records, never bare checkboxes."""
    records: list[dict[str, object]] = []
    saw_pending = False
    for task in plan.tasks:
        if not task.completed:
            saw_pending = True
            continue
        if saw_pending:
            raise StateError('completed task occurs after a pending task')
        if not task.outcome or task.outcome == 'no-change' or not evidence_valid(task, task.evidence):
            raise StateError(f'completed task {task.id} requires a commit Outcome and matching Evidence before startup')
        facts = commit_facts(path, task.outcome)
        parent, tree, message, paths = facts.parent, facts.tree, facts.message, facts.paths
        record = {'task_id': task.id, 'outcome': task.outcome, 'start_head': parent, 'end_head': task.outcome, 'tree': tree, 'evidence': task.evidence}
        record_valid(path, task, record)
        records.append(record)
    return records


def finalize(slug: str, state: object, plan: Plan, record: dict[str, object]) -> object:
    # Imports are deferred to avoid the workflow/completion module cycle.
    import workflow as runtime
    assert isinstance(state, runtime.Execution)
    path, context = resolve_plan(slug)
    task_id = str(record['task_id'])
    pending_content = mark_complete(plan, task_id, outcome=str(record['outcome']), evidence=record['evidence'])
    state.pending_digest = hashlib.sha256(pending_content.encode()).hexdigest()
    runtime.save(path, state)
    updated = replace_plan(path, context, plan, pending_content)
    if not any(r.get('task_id') == task_id for r in state.records):
        state.records.append(record)
    state.active = None
    state.pending_digest = None
    state.plan_digest = updated.digest
    runtime.save(path, state)
    return state


def complete(slug: str) -> object:
    import workflow as runtime
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = runtime.load(path)
        worktree = runtime.check_identity(state, context)
        active = state.active
        if state.phase != 'ready' or active is None:
            raise StateError('completion requires an active begun task')
        task = next((t for t in plan.tasks if t.id == active.task_id), None)
        if task is None:
            raise StateError('active task is missing from plan')
        try:
            if plan.digest != state.plan_digest:
                if not (active.phase in {'committed', 'no-change'} and task.completed and state.pending_digest == plan.digest):
                    raise StateError('plan drift; completion journal preserved')
            if runtime.in_progress(worktree):
                raise StateError('Git operation in progress; completion refused')
            head = git_output(worktree, 'rev-parse', 'HEAD')
            if active.phase == 'no-change':
                if head != active.start_head or not runtime.clean(worktree) or not evidence_valid(task, active.evidence):
                    raise StateError('no-change transaction drift; preserve journal')
                record = {'task_id': task.id, 'outcome': 'no-change', 'start_head': head, 'end_head': head, 'tree': git_output(worktree, 'rev-parse', 'HEAD^{tree}'), 'evidence': active.evidence}
                if task.completed:
                    if task.outcome != 'no-change' or task.evidence != active.evidence:
                        raise StateError('no-change Markdown disagrees with transaction')
                    state.records.append(record)
                    state.active = None
                    state.plan_digest = plan.digest
                    state.pending_digest = None
                    runtime.save(path, state)
                    return state
                return finalize(slug, state, plan, record)
            if active.phase in {'committing', 'committed'}:
                if active.tree is None or not evidence_valid(task, active.evidence):
                    raise StateError('incomplete commit transaction evidence')
                if head != active.start_head:
                    facts = commit_facts(worktree, head)
                    parent, tree, message, paths = facts.parent, facts.tree, facts.message, facts.paths
                    if parent != active.start_head or tree != active.tree or message != task.commit or sorted(paths) != sorted(active.changes):
                        raise StateError('HEAD changed beyond expected commit; preserve journal for review')
                    active.commit = head
                    active.phase = 'committed'
                    runtime.save(path, state)
                elif active.phase == 'committed':
                    raise StateError('recorded commit is missing or history was rewritten')
                else:
                    # Commit failed or was interrupted before Git mutation. Keep staging for retry.
                    if git_output(worktree, 'write-tree') != active.tree:
                        raise StateError('staged tree drift since commit intent')
                if active.phase == 'committed':
                    if not runtime.clean(worktree):
                        raise StateError('worktree changed after commit; preserve journal and changes')
                    record = {'task_id': task.id, 'outcome': active.commit, 'start_head': active.start_head, 'end_head': active.commit, 'tree': active.tree, 'evidence': active.evidence}
                    if task.completed:
                        if task.outcome != active.commit or task.evidence != active.evidence:
                            raise StateError('completed Markdown disagrees with transaction')
                        if not any(r.get('task_id') == task.id for r in state.records):
                            state.records.append(record)
                        state.active = None
                        state.plan_digest = plan.digest
                        state.pending_digest = None
                        runtime.save(path, state)
                        return state
                    return finalize(slug, state, plan, record)
            elif head != active.start_head:
                raise StateError('HEAD changed after begin; expected commit was not journaled')
            changes = inspect(worktree)
            guard(changes, task)
            if active.phase == 'committing' and (changes.unstaged or changes.paths != active.changes or changes.staged != active.changes):
                raise StateError('task changes drifted since commit intent')
            if active.phase != 'committing':
                active.evidence = []
                active.error = None
                for argv in task.verify:
                    try:
                        result = subprocess.run(argv, cwd=worktree, capture_output=True, text=True, errors='replace')
                        entry: dict[str, object] = {'argv': argv, 'exit_code': result.returncode, 'stdout': result.stdout[:4000], 'stderr': result.stderr[:4000], 'output_sha256': hashlib.sha256((result.stdout + result.stderr).encode()).hexdigest()}
                    except OSError as exc:
                        entry = {'argv': argv, 'exit_code': 127, 'stderr': str(exc)}
                    active.evidence.append(entry)
                    runtime.save(path, state)
                    if entry['exit_code'] != 0:
                        raise StateError(f'verification failed for {argv}: {entry.get("stderr", "")}')
                if path.read_text() != plan.content:
                    raise StateError('plan drift during verification')
                after = inspect(worktree)
                guard(after, task)
                if after != changes:
                    raise StateError('verification mutated task files/staging; review changes and retry')
                if git_output(worktree, 'rev-parse', 'HEAD') != active.start_head:
                    raise StateError('verification changed HEAD')
                if not changes.paths:
                    record = {'task_id': task.id, 'outcome': 'no-change', 'start_head': head, 'end_head': head, 'tree': git_output(worktree, 'rev-parse', 'HEAD^{tree}'), 'evidence': active.evidence}
                    active.phase = 'no-change'
                    # Journal no-change completion just like a committed transaction.
                    active.commit = head
                    runtime.save(path, state)
                    return finalize(slug, state, plan, record)
                error = subject_error(str(task.commit))
                if error:
                    raise StateError(error)
                # Stage explicit known paths only. -- handles option-like filenames.
                to_stage = [p for p in changes.paths if p not in changes.staged]
                if to_stage:
                    subprocess.run(['git', 'add', '--all', '--', *to_stage], cwd=worktree, check=True, capture_output=True)
                staged = inspect(worktree)
                guard(staged, task)
                if staged.unstaged or staged.paths != changes.paths or staged.staged != changes.paths or staged.content_digest != changes.content_digest:
                    raise StateError('changes drifted or staging is incomplete')
                active.tree = git_output(worktree, 'write-tree')
                active.changes = changes.paths
                active.phase = 'committing'
                runtime.save(path, state)
            result = subprocess.run(['git', 'commit', '-m', str(task.commit)], cwd=worktree, capture_output=True, text=True)
            if result.returncode:
                raise StateError(f'commit failed; pending task and staged work retained: {result.stderr.strip()}')
            commit = git_output(worktree, 'rev-parse', 'HEAD')
            facts = commit_facts(worktree, commit)
            parent, tree, message, paths = facts.parent, facts.tree, facts.message, facts.paths
            if parent != active.start_head or tree != active.tree or message != task.commit or sorted(paths) != sorted(active.changes):
                raise StateError('commit hook changed the expected commit; inspect journal')
            active.commit = commit
            active.phase = 'committed'
            runtime.save(path, state)
            if not runtime.clean(worktree):
                raise StateError('commit hook changed worktree; completion not finalized')
            record = {'task_id': task.id, 'outcome': commit, 'start_head': active.start_head, 'end_head': commit, 'tree': tree, 'evidence': active.evidence}
            return finalize(slug, state, plan, record)
        except (StateError, OSError, subprocess.CalledProcessError) as exc:
            if state.active is not None:
                state.active.error = str(exc)
                runtime.save(path, state)
            raise


def audit(slug: str) -> object:
    import workflow as runtime
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context, runtime=True)
        state = runtime.load(path)
        worktree = runtime.check_identity(state, context)
        if state.phase != 'ready' or state.active or state.pending_digest:
            raise StateError('unresolved workflow transaction')
        if state.plan_digest != plan.digest:
            raise StateError('plan drift since recorded completion')
        if not runtime.clean(worktree) or runtime.in_progress(worktree):
            raise StateError('audit requires a clean worktree without Git operations')
        if any(not t.completed for t in plan.tasks):
            raise StateError('audit requires every task complete')
        if [r.get('task_id') for r in state.records] != [t.id for t in plan.tasks]:
            raise StateError('task completion records missing or out of order')
        previous: str | None = None
        for task, record in zip(plan.tasks, state.records):
            record_valid(worktree, task, record)
            if task.outcome != record.get('outcome') or task.evidence != record.get('evidence'):
                raise StateError('Markdown completion evidence differs from execution state')
            if previous and record.get('start_head') != previous:
                raise StateError('task commit chain is not contiguous')
            previous = str(record['end_head'])
        if previous != git_output(worktree, 'rev-parse', 'HEAD'):
            raise StateError('unrecorded commits follow the task chain')
        return state


def report(slug: str, *, markdown: bool = False) -> str:
    import workflow as runtime
    path, context = resolve_plan(slug)
    with plan_lock(path):
        plan = read_plan(path, context)
        state = runtime.load(path)
        worktree = runtime.check_identity(state, context, require_cwd=False)
        facts = {'plan': str(path), 'phase': state.phase, 'identity': asdict(state.identity), 'head': git_output(worktree, 'rev-parse', 'HEAD'), 'dirty': not runtime.clean(worktree), 'pending': [t.id for t in plan.tasks if not t.completed], 'active': asdict(state.active) if state.active else None, 'records': state.records}
        if markdown:
            commits = ', '.join(str(r['outcome'])[:7] for r in state.records if r.get('outcome') != 'no-change') or 'none'
            return f'Worktree: {worktree} (branch {state.identity.branch}).\nCommits: {commits}. Pending tasks: {len(facts["pending"])}. Dirty: {facts["dirty"]}.\n'
        return json.dumps(facts, sort_keys=True)
