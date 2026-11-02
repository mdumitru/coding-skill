#!/usr/bin/env python3
"""Run, inspect, stop, and reset the repository preview of a plan, worktree, or branch.

A name resolves, in order, to an executed plan's recorded task worktree, an
existing worktree (by faur name or branch), or — for `run` in a faur workspace
only — a new faur worktree checking out an existing local or origin branch.
This helper never commits, creates branches, or changes plan checkboxes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import preview_protocol as pp
import workflow
from plan_file import GitContext, PlanFileError, git_context, git_output, json_strings, resolve_plan
from state_io import StateError, atomic_write, plan_lock

LOGGER = logging.getLogger('preview')
DEFAULT_TIMEOUT = 600.0
MAX_LOG_LINES = 10000
MAX_LOG_BYTES = 256 * 1024
INTENT_PHASES = frozenset({'pending', 'starting', 'discharged'})
DEFAULT_FAUR = ['faur', 'worktree']
MAX_LABEL = 63
UNSAFE_NAME = re.compile(r'[\x00-\x20\x7f]')


@dataclass(frozen=True)
class Target:
    plan: Path | None
    context: GitContext
    identity: pp.RuntimeIdentity
    task_root: Path
    paths: pp.RuntimePaths
    source: str = 'plan'  # plan | worktree | created-worktree


@dataclass(frozen=True)
class Invocation:
    exit_code: int
    stdout: str
    stderr: str
    duration: float


# ----------------------------------------------------------------- pure rules


def check_target_name(name: str) -> str:
    """A plan slug, worktree name, or branch name; resolution decides which."""
    if not name or name.startswith('-') or UNSAFE_NAME.search(name) or name.endswith('.md'):
        raise StateError(f'invalid run target name: {name!r}')
    return name


def resolve_run_slug(run_slug: str | None, execute_slugs: list[str]) -> str | None:
    """Explicit target name, the single execute slug of a combined request, or None for the current worktree."""
    if len(set(execute_slugs)) > 1:
        raise StateError(f'ambiguous combined request: multiple execute slugs {sorted(set(execute_slugs))}')
    if execute_slugs:
        pp.hostname_for(execute_slugs[0])
        if run_slug is not None and execute_slugs[0] != run_slug:
            raise StateError(f'run slug {run_slug!r} conflicts with execute slug {execute_slugs[0]!r}')
        return execute_slugs[0]
    return None if run_slug is None else check_target_name(run_slug)


def host_label(name: str) -> str:
    """Kebab-case DNS label for <label>.localhost; plan slugs and kebab names map to themselves."""
    if pp.SLUG_RE.fullmatch(name) and len(name) <= MAX_LABEL:
        return name
    label = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    if not label:
        raise StateError(f'cannot derive a preview hostname from {name!r}')
    if not label[0].isalpha():
        label = f'b-{label}'
    if len(label) > MAX_LABEL:
        label = label[:MAX_LABEL - 9].rstrip('-') + '-' + hashlib.sha256(name.encode()).hexdigest()[:8]
    pp.hostname_for(label)
    return label


def plain_task_id(common_dir: str, worktree: str, prefix: str = 'plain') -> str:
    """Worktrees without a faur task record get a stable ID derived from their identity."""
    return f'{prefix}-' + hashlib.sha256(f'{common_dir}\0{worktree}'.encode()).hexdigest()[:24]


def check_intent(data: object, slug: str) -> dict[str, object]:
    if (not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('plan_slug') != slug
            or data.get('phase') not in INTENT_PHASES or not isinstance(data.get('start_args'), list)
            or any(not isinstance(a, str) for a in data['start_args'])):
        raise StateError(f'corrupt run intent for {slug}; preserved for review')
    return data


# -------------------------------------------------------------------- effects


def intent_path(plan: Path) -> Path:
    path = workflow.state_path(plan).with_name(f'{plan.stem}.run.json')
    if path.is_symlink():
        raise StateError(f'refusing symlinked run intent: {path}')
    return path


def load_intent(plan: Path) -> dict[str, object] | None:
    path = intent_path(plan)
    return check_intent(pp.read_json_file(path, 'run intent'), plan.stem) if path.exists() else None


def save_intent(plan: Path, intent: dict[str, object]) -> None:
    intent['updated_at'] = pp.now()
    atomic_write(intent_path(plan), json.dumps(intent, sort_keys=True, indent=2) + '\n')


def record_intent(slug: str, start_args: list[str]) -> dict[str, object]:
    """Persist a combined execute/run request so retries and compaction keep it.

    Empty start options on a retry inherit the recorded ones; different options conflict.
    """
    path, _ = resolve_plan(slug)
    with plan_lock(path):
        intent = load_intent(path)
        if intent is not None:
            if start_args and intent['start_args'] != start_args:
                raise StateError(f'run intent for {slug} already records different start options: {intent["start_args"]}')
            return intent
        intent = {'schema_version': 1, 'plan_slug': slug, 'phase': 'pending', 'start_args': start_args, 'created_at': pp.now(), 'error': None}
        save_intent(path, intent)
        return intent


def ensure_plain_ignored(root: Path) -> None:
    """Keep plain-repository runtime state out of status, commits, and audits."""
    probe = f'{pp.PLAIN_RUNS}/probe'
    if subprocess.run(['git', 'check-ignore', '-q', '--no-index', probe], cwd=root, capture_output=True).returncode == 0:
        return
    exclude = Path(git_output(root, 'rev-parse', '--git-path', 'info/exclude'))
    exclude = exclude if exclude.is_absolute() else root / exclude
    current = exclude.read_text(encoding='utf-8') if exclude.exists() else ''
    separator = '' if not current or current.endswith('\n') else '\n'
    atomic_write(exclude, f'{current}{separator}/{pp.PLAIN_RUNS}/\n')
    LOGGER.info('added /%s/ to %s', pp.PLAIN_RUNS, exclude)


def build_target(context: GitContext, label: str, task_id: str, task_root: Path, *, plan: Path | None, source: str) -> Target:
    if not context.is_faur:
        ensure_plain_ignored(task_root)
    base = context.plan_root.parent if context.is_faur else task_root
    identity = pp.make_identity(label, task_id, str(task_root), str(context.common_dir))
    return Target(plan, context, identity, task_root, pp.runtime_paths(base, label, faur=context.is_faur), source)


def plan_target(slug: str) -> Target:
    """The recorded, faur-validated task worktree of an executed plan."""
    path, context = resolve_plan(slug)
    if not workflow.state_path(path).exists():
        raise StateError(f'plan {slug} has no recorded task worktree; run execute: {slug} first')
    state = workflow.load(path)
    if state.identity is None or state.phase not in {'ready', 'revising'}:
        raise StateError(f'plan {slug} execution startup is incomplete ({state.phase}); resume execute: {slug} first')
    task_root = workflow.check_identity(state, context, require_cwd=False)
    task_id = state.identity.task_id or plain_task_id(state.common_dir, str(task_root))
    return build_target(context, slug, task_id, task_root, plan=path, source='plan')


def executed_plan(name: str, context: GitContext) -> bool:
    """True when <name> is a plan with an execution journal; a journal-less plan falls through."""
    if not pp.SLUG_RE.fullmatch(name) or not (context.plan_root / f'{name}.md').is_file():
        return False
    path, _ = resolve_plan(name)
    return workflow.state_path(path).exists()


def listed_worktrees(cwd: Path) -> list[tuple[Path, str | None]]:
    """(path, branch) for every non-bare Git worktree; detached ones carry no branch."""
    entries: list[tuple[Path, str | None]] = []
    path: Path | None = None
    branch: str | None = None
    bare = False
    for line in [*git_output(cwd, 'worktree', 'list', '--porcelain').splitlines(), '']:
        if line.startswith('worktree '):
            path, branch, bare = Path(line.removeprefix('worktree ')).resolve(), None, False
        elif line.startswith('branch refs/heads/'):
            branch = line.removeprefix('branch refs/heads/')
        elif line == 'bare':
            bare = True
        elif not line and path is not None:
            if not bare:
                entries.append((path, branch))
            path = None
    return entries


def find_worktree(name: str, context: GitContext) -> Path | None:
    """The single worktree whose faur name (workspace-relative path) or branch equals <name>."""
    root = context.plan_root.parent
    matches = {path for path, branch in listed_worktrees(Path.cwd())
               if branch == name or (context.is_faur and path.is_relative_to(root) and path.relative_to(root).as_posix() == name)}
    if len(matches) > 1:
        raise StateError(f'ambiguous run target {name!r}: matches worktrees {sorted(map(str, matches))}')
    return matches.pop() if matches else None


def branch_ref(cwd: Path, name: str) -> str | None:
    for ref in (f'refs/heads/{name}', f'refs/remotes/origin/{name}'):
        if subprocess.run(['git', 'show-ref', '--verify', '--quiet', ref], cwd=cwd, capture_output=True).returncode == 0:
            return ref
    return None


def existing_branch(cwd: Path, name: str) -> str | None:
    """Local or origin branch <name>, fetching origin once when it is not known locally."""
    if subprocess.run(['git', 'check-ref-format', '--branch', name], cwd=cwd, capture_output=True).returncode:
        return None
    ref = branch_ref(cwd, name)
    if ref is None and subprocess.run(['git', 'remote', 'get-url', 'origin'], cwd=cwd, capture_output=True).returncode == 0:
        fetched = subprocess.run(['git', 'fetch', '--quiet', '--prune', 'origin'], cwd=cwd, capture_output=True, text=True)
        if fetched.returncode:
            LOGGER.warning('git fetch origin failed: %s', pp.bounded(fetched.stderr.strip()))
        ref = branch_ref(cwd, name)
    return ref


def create_worktree(name: str, context: GitContext, runner: list[str]) -> Path:
    """Check out an existing branch in a new faur worktree; never create a branch."""
    cwd = Path.cwd()
    if existing_branch(cwd, name) is None:
        raise StateError(f'no executed plan, worktree, or local/origin branch named {name!r}')
    if not context.is_faur:
        raise StateError(f'branch {name} has no worktree; in a plain repository check it out first (git worktree add <path> {name})')
    data = workflow.run_faur(runner, cwd, 'add', name, '--json')
    worktree = workflow.object_value(data.get('worktree'), 'created worktree')
    if data.get('dry_run') is not False or worktree.get('branch') != name:
        raise StateError(f'faur add returned an unexpected worktree for {name}: {worktree}')
    path = Path(workflow.string_value(worktree.get('path'), 'worktree.path'))
    LOGGER.info('created worktree %s for branch %s', path, name)
    return path


def worktree_target(path: Path, label: str, context: GitContext, runner: list[str], *, source: str) -> Target:
    """Preview an existing worktree as-is, validated through faur context when available."""
    if context.is_faur:
        identity = workflow.context_identity(workflow.run_faur(runner, path, 'context', '--json'), str(context.common_dir))
    else:
        identity = workflow.plain_identity(context, path)
    task_root = Path(identity.path)
    if task_root != path.resolve():
        raise StateError(f'worktree identity mismatch: expected {path}, faur reports {task_root}')
    task_id = identity.task_id or plain_task_id(str(context.common_dir), str(task_root), 'worktree' if context.is_faur else 'plain')
    return build_target(context, label, task_id, task_root, plan=None, source=source)


def current_target(context: GitContext, runner: list[str]) -> Target:
    """The worktree containing the current directory, named by its faur name or branch."""
    path = Path(git_output(Path.cwd(), 'rev-parse', '--show-toplevel')).resolve()
    root = context.plan_root.parent
    name = path.relative_to(root).as_posix() if context.is_faur and path.is_relative_to(root) else git_output(path, 'symbolic-ref', '--quiet', '--short', 'HEAD')
    return worktree_target(path, host_label(name), context, runner, source='worktree')


def resolve_target(name: str | None, *, create: bool = False, plan_only: bool = False, runner: list[str] | None = None) -> Target:
    """Executed plan, then existing worktree, then (run only) a new worktree for an existing branch."""
    runner = runner or DEFAULT_FAUR
    context = git_context()
    if name is None:
        return current_target(context, runner)
    check_target_name(name)
    if plan_only or executed_plan(name, context):
        return plan_target(name)
    path = find_worktree(name, context)
    if path is not None:
        return worktree_target(path, host_label(name), context, runner, source='worktree')
    unexecuted = f'; plan {name} exists but is not executed: execute: {name} first' if pp.SLUG_RE.fullmatch(name) and (context.plan_root / f'{name}.md').is_file() else ''
    if not create:
        hint = ' (run creates one for an existing branch)' if context.is_faur and existing_branch(Path.cwd(), name) else ''
        raise StateError(f'no executed plan or worktree named {name!r}{hint}{unexecuted}')
    try:
        path = create_worktree(name, context, runner)
    except StateError as exc:
        raise StateError(f'{exc}{unexecuted}') from exc
    return worktree_target(path, host_label(name), context, runner, source='created-worktree')


def invoke(target: Target, operation: str, *extra: str, timeout: float, machine: bool = True) -> Invocation:
    executable = pp.adapter_executable(target.task_root, operation)
    argv = pp.lifecycle_argv(executable, target.identity, target.paths.state_dir, *extra, machine=machine)
    started = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=target.task_root, env=pp.lifecycle_env(target.identity, os.environ),
                                capture_output=True, text=True, errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise StateError(f'{operation} timed out after {timeout:g}s; runtime record retained for recovery') from exc
    except OSError as exc:
        raise StateError(f'cannot execute {executable}: {exc}') from exc
    duration = time.monotonic() - started
    LOGGER.debug('%s exited %s in %.3fs', operation, result.returncode, duration)
    return Invocation(result.returncode, result.stdout, result.stderr, duration)


def adapter_response(target: Target, operation: str, invocation: Invocation) -> pp.Response:
    try:
        data = pp.parse_json_text(invocation.stdout, f'{operation} response')
    except StateError as exc:
        raise StateError(f'{operation} exited {invocation.exit_code} without a valid JSON envelope: {pp.bounded(invocation.stderr.strip()) or exc}') from exc
    response = pp.parse_response(data, target.identity, operation)
    if invocation.exit_code != 0 and response.success:
        raise StateError(f'{operation} exited {invocation.exit_code} but reported success')
    if invocation.exit_code != 0 and invocation.stderr.strip():
        LOGGER.error('%s: %s', operation, pp.bounded(invocation.stderr.strip()))
    return response


def report(target: Target, operation: str, response: pp.Response | None, duration: float, *, phase: str, note: str | None = None) -> dict[str, object]:
    return {'schema_version': pp.SCHEMA_VERSION, 'operation': operation, 'success': True if response is None else response.success,
            'plan_slug': target.identity.plan_slug, 'hostname': target.identity.hostname, 'task_id': target.identity.task_id,
            'worktree': target.identity.worktree, 'runtime_id': target.identity.runtime_id, 'phase': phase,
            'status': response.status if response else 'absent', 'ready': bool(response and response.ready),
            'readiness': response.readiness_detail if response else '', 'services': [asdict(s) for s in response.services] if response else [],
            'details': response.details if response else {}, 'record': str(target.paths.record), 'source': target.source,
            'duration_seconds': round(duration, 3), 'note': note}


def new_record(target: Target, protocol: pp.Protocol, start_args: list[str]) -> pp.RuntimeRecord:
    i = target.identity
    return pp.RuntimeRecord(pp.SCHEMA_VERSION, i.runtime_id, i.plan_slug, i.hostname, i.task_id, i.worktree, i.common_dir,
                            protocol.adapter, str(target.paths.state_dir), 'starting', protocol.pairing, start_args)


def claim(target: Target, protocol: pp.Protocol) -> None:
    i = target.identity
    pp.claim_hostname(pp.registry_dir(), pp.Claim(i.hostname, i.runtime_id, i.task_id, i.worktree, i.common_dir, str(target.paths.record), protocol.pairing))


def require_adapter(record: pp.RuntimeRecord | None, protocol: pp.Protocol) -> None:
    if record is not None and record.adapter != protocol.adapter:
        raise StateError(f'repository adapter changed from {record.adapter} to {protocol.adapter}; tear down with the recorded adapter first')


def start_runtime(target: Target, start_args: list[str], *, timeout: float) -> dict[str, object]:
    """Start or reuse the runtime; success only after validated readiness."""
    protocol = pp.load_protocol(target.task_root)
    args = pp.validate_start_arguments(protocol, start_args)
    with pp.runtime_lock(target.paths, target.identity):
        record = pp.load_record(target.paths, target.identity)
        require_adapter(record, protocol)
        if record is not None and record.phase in {'stopping', 'tearing-down'}:
            option = ' --teardown' if record.phase == 'tearing-down' else ''
            raise StateError(f'interrupted {record.phase} for {record.runtime_id}: finish it with preview stop{option} {target.identity.plan_slug} first')
        if record is not None and record.phase == 'running':
            if record.start_args != args:
                raise StateError(f'preview already running with start options {record.start_args}; use reset or stop before changing them')
            status = invoke(target, 'status', timeout=timeout)
            current = adapter_response(target, 'status', status)
            if pp.readiness_error(protocol, current) is None:
                return report(target, 'run', current, status.duration, phase='running', note='reused running preview')
            LOGGER.warning('recorded running preview is not ready (%s); restarting', current.status)
        claim(target, protocol)
        record = record or new_record(target, protocol, args)
        record.phase, record.start_args, record.pairing, record.error = 'starting', args, protocol.pairing, None
        pp.save_record(target.paths, record)
        invocation = invoke(target, 'start', *(['--', *args] if args else []), timeout=timeout)
        response: pp.Response | None = None
        try:
            response = adapter_response(target, 'start', invocation)
            error = pp.readiness_error(protocol, response)
        except StateError as exc:
            error = str(exc)
        if error:
            record.phase, record.error = 'failed', pp.bounded(error)
            if response is not None:
                record.services = [asdict(s) for s in response.services]
            pp.save_record(target.paths, record)
            raise StateError(f'preview startup failed: {error}; record and partial resources retained at {target.paths.directory}; retry run or use stop --teardown')
        assert response is not None
        record.phase, record.services = 'running', [asdict(s) for s in response.services]
        pp.save_record(target.paths, record)
        return report(target, 'run', response, invocation.duration, phase='running')


def stop_runtime(target: Target, *, teardown: bool, timeout: float) -> dict[str, object]:
    """Stop (keeping data) or tear down owned resources; absent runtimes are no-ops."""
    operation = 'teardown' if teardown else 'stop'
    with pp.runtime_lock(target.paths, target.identity):
        record = pp.load_record(target.paths, target.identity)
        if record is None or record.phase == 'torn-down':
            return report(target, operation, None, 0.0, phase=record.phase if record else 'absent', note='no owned preview resources')
        if record.phase == 'tearing-down' and not teardown:
            raise StateError(f'interrupted teardown for {record.runtime_id}; rerun stop --teardown')
        protocol = pp.load_protocol(target.task_root)
        require_adapter(record, protocol)
        record.phase, record.error = ('tearing-down' if teardown else 'stopping'), None
        pp.save_record(target.paths, record)
        invocation = invoke(target, 'stop', *(['--teardown'] if teardown else []), timeout=timeout)
        try:
            response = adapter_response(target, 'stop', invocation)
            if not response.success or response.status not in {'stopped', 'absent'}:
                raise StateError(f'adapter did not confirm {operation}: status {response.status}: {response.readiness_detail}')
        except StateError as exc:
            record.error = pp.bounded(str(exc))
            pp.save_record(target.paths, record)
            raise StateError(f'{operation} failed: {exc}; record retained at {target.paths.record}; retry the same command') from exc
        if teardown:
            pp.release_hostname(pp.registry_dir(), target.identity.hostname, target.identity.runtime_id, target.identity.task_id)
            record.phase, record.services = 'torn-down', []
        else:
            record.phase = 'stopped'
        pp.save_record(target.paths, record)
        return report(target, operation, response, invocation.duration, phase=record.phase)


def reset_runtime(target: Target, start_args: list[str] | None, *, timeout: float) -> dict[str, object]:
    """Tear down and recreate fresh state through the repository's reset entrypoint."""
    protocol = pp.load_protocol(target.task_root)
    with pp.runtime_lock(target.paths, target.identity):
        record = pp.load_record(target.paths, target.identity)
        require_adapter(record, protocol)
        args = pp.validate_start_arguments(protocol, start_args if start_args is not None else (record.start_args if record else []))
        claim(target, protocol)
        record = record or new_record(target, protocol, args)
        record.phase, record.start_args, record.pairing, record.error = 'tearing-down', args, protocol.pairing, None
        pp.save_record(target.paths, record)
        invocation = invoke(target, 'reset', *(['--', *args] if args else []), timeout=timeout)
        try:
            response = adapter_response(target, 'reset', invocation)
            error = pp.readiness_error(protocol, response) if response.status != 'stopped' or not response.success else None
        except StateError as exc:
            error = str(exc)
        if error:
            record.error = pp.bounded(error)
            pp.save_record(target.paths, record)
            raise StateError(f'reset failed: {error}; run stop --teardown {target.identity.plan_slug}, then run again')
        record.phase, record.services = response.status if response.status == 'stopped' else 'running', [asdict(s) for s in response.services]
        pp.save_record(target.paths, record)
        return report(target, 'reset', response, invocation.duration, phase=record.phase)


def status_runtime(target: Target, *, timeout: float) -> dict[str, object]:
    with pp.runtime_lock(target.paths, target.identity):
        record = pp.load_record(target.paths, target.identity)
        if record is None or record.phase == 'torn-down':
            return report(target, 'status', None, 0.0, phase=record.phase if record else 'absent', note='no owned preview resources')
        invocation = invoke(target, 'status', timeout=timeout)
        response = adapter_response(target, 'status', invocation)
        result = report(target, 'status', response, invocation.duration, phase=record.phase)
        result['error'] = record.error
        return result


def logs_runtime(target: Target, *, lines: int, follow: bool, timeout: float) -> int:
    if not 0 < lines <= MAX_LOG_LINES:
        raise StateError(f'--lines must be between 1 and {MAX_LOG_LINES}')
    if pp.load_record(target.paths, target.identity) is None:
        raise StateError(f'no preview runtime recorded for {target.identity.plan_slug}')
    extra = ['--lines', str(lines)] + (['--follow'] if follow else [])
    if follow:
        # Explicit follow streams until interrupted; no capture and no timeout.
        argv = pp.lifecycle_argv(pp.adapter_executable(target.task_root, 'logs'), target.identity, target.paths.state_dir, *extra, machine=False)
        return subprocess.run(argv, cwd=target.task_root, env=pp.lifecycle_env(target.identity, os.environ)).returncode
    invocation = invoke(target, 'logs', *extra, timeout=timeout, machine=False)
    output = invocation.stdout.encode('utf-8', 'replace')[-MAX_LOG_BYTES:].decode('utf-8', 'replace')
    sys.stdout.write('\n'.join(output.splitlines()[-lines:]) + ('\n' if output else ''))
    if invocation.exit_code:
        raise StateError(f'logs exited {invocation.exit_code}: {pp.bounded(invocation.stderr.strip())}')
    return 0


@contextmanager
def working_directory(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def run(run_slug: str | None, execute_slugs: list[str], start_args: list[str], *, timeout: float = DEFAULT_TIMEOUT,
        runner: list[str] | None = None) -> dict[str, object]:
    """Standalone run previews the resolved worktree as-is; combined run requires an audited execution."""
    slug = resolve_run_slug(run_slug, execute_slugs)
    if not execute_slugs:
        return start_runtime(resolve_target(slug, create=True, runner=runner), start_args, timeout=timeout)
    assert slug is not None
    intent = record_intent(slug, start_args)
    args = list(intent['start_args'])  # type: ignore[arg-type]
    target = resolve_target(slug, plan_only=True)
    assert target.plan is not None
    from completion import audit
    with working_directory(target.task_root):
        audit(slug)
    with plan_lock(target.plan):
        intent = load_intent(target.plan) or intent
        if intent['phase'] == 'pending':
            intent['phase'] = 'starting'
            save_intent(target.plan, intent)
    try:
        result = start_runtime(target, args, timeout=timeout)
    except StateError as exc:
        with plan_lock(target.plan):
            intent = load_intent(target.plan) or intent
            intent['error'] = pp.bounded(str(exc))
            save_intent(target.plan, intent)
        raise
    with plan_lock(target.plan):
        intent = load_intent(target.plan) or intent
        intent['phase'], intent['error'] = 'discharged', None
        intent.setdefault('discharged_at', pp.now())
        save_intent(target.plan, intent)
    result['intent'] = 'discharged'
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, epilog='Repository start options follow a literal --.')
    parser.add_argument('-v', '--verbose', action='store_true')
    sub = parser.add_subparsers(dest='operation', required=True)
    run_parser = sub.add_parser('run', help='start or reuse the preview; may create a worktree for an existing branch')
    run_parser.add_argument('slug', nargs='?', help='plan slug, worktree name, or branch (default: current worktree)')
    run_parser.add_argument('--execute-slug', action='append', default=[], help='slug from the same request\'s execute:')
    intent = sub.add_parser('intent', help='record a combined execute/run request before execution')
    intent.add_argument('slug', nargs='?')
    intent.add_argument('--execute-slug', action='append', default=[], required=True)
    for name in ('status', 'logs', 'stop', 'reset'):
        p = sub.add_parser(name)
        p.add_argument('slug', nargs='?', help='plan slug, worktree name, or branch (default: current worktree)')
        if name == 'logs':
            p.add_argument('--lines', type=int, default=200)
            p.add_argument('--follow', action='store_true', help='stream until interrupted')
        if name == 'stop':
            p.add_argument('--teardown', action='store_true', help='delete every owned resource')
    for p in (run_parser, *[sub.choices[n] for n in ('status', 'logs', 'stop', 'reset')]):
        p.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help='seconds per adapter call')
        p.add_argument('--faur-argv', default=json.dumps(DEFAULT_FAUR), help='JSON executable argv prefix')
    return parser


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    start_args: list[str] | None = None
    if '--' in raw:
        start_args = raw[raw.index('--') + 1:]
        raw = raw[:raw.index('--')]
    args = build_parser().parse_args(raw)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s: %(message)s', stream=sys.stderr)
    try:
        if start_args is not None and args.operation not in {'run', 'intent', 'reset'}:
            raise StateError(f'{args.operation} does not accept start options')
        if args.operation == 'intent':
            slug = resolve_run_slug(args.slug, args.execute_slug)
            assert slug is not None
            print(json.dumps(record_intent(slug, start_args or []), sort_keys=True))
            return 0
        runner = json_strings(args.faur_argv, 'faur argv')
        if args.operation == 'logs':
            return logs_runtime(resolve_target(args.slug, runner=runner), lines=args.lines, follow=args.follow, timeout=args.timeout)
        if args.operation == 'run':
            result: dict[str, object] = run(args.slug, args.execute_slug, start_args or [], timeout=args.timeout, runner=runner)
        elif args.operation == 'status':
            result = status_runtime(resolve_target(args.slug, runner=runner), timeout=args.timeout)
        elif args.operation == 'stop':
            result = stop_runtime(resolve_target(args.slug, runner=runner), teardown=args.teardown, timeout=args.timeout)
        else:
            result = reset_runtime(resolve_target(args.slug, runner=runner), start_args, timeout=args.timeout)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (PlanFileError, StateError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        LOGGER.error('%s', pp.bounded(str(exc)))
        return 2
    except KeyboardInterrupt:
        LOGGER.error('interrupted; runtime record and locks are evidence for recovery')
        return 130


if __name__ == '__main__':
    sys.exit(main())
