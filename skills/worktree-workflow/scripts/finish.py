#!/usr/bin/env python3
"""Finish a faur task: context, dry-run, owned preview teardown, then the real faur finish.

Self-contained by design: it reads the documented generic runtime records and
hostname registry itself and never imports another skill. A teardown failure
blocks the real finish and keeps the worktree and its recovery state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

LOGGER = logging.getLogger('finish')
SCHEMA_VERSION = 1
TOOLS_DIR = 'worktree-tools'
PROTOCOL_FILE = 'protocol.json'
FAUR_RUNS = '_runs'
RECORD_FILE = 'runtime.json'
RUNTIME_ENV = 'WORKTREE_RUNTIME_ID'
SLUG_RE = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
RECORD_FIELDS = ('schema_version', 'runtime_id', 'plan_slug', 'hostname', 'task_id', 'worktree', 'common_dir', 'adapter',
                 'state_dir', 'phase', 'pairing', 'start_args', 'services', 'updated_at', 'error')
RECORD_PHASES = frozenset({'starting', 'running', 'stopping', 'stopped', 'failed', 'tearing-down', 'torn-down'})
MAX_JSON_BYTES = 256 * 1024
MAX_TEXT = 4000
SECRET_TEXT = re.compile(r'(?i)\b(password|passwd|secret|token|api[-_]?key|authorization|cookie)\b(\s*[=:]\s*)("[^"]*"|\S+)')
DEFAULT_TIMEOUT = 600.0


class FinishError(Exception):
    """Finish cannot safely proceed; state is preserved for recovery."""


@dataclass(frozen=True)
class TaskFacts:
    name: str
    task_id: str
    path: Path
    root: Path
    common_dir: str


@dataclass
class Record:
    schema_version: int
    runtime_id: str
    plan_slug: str
    hostname: str
    task_id: str
    worktree: str
    common_dir: str
    adapter: str
    state_dir: str
    phase: str
    pairing: str | None = None
    start_args: list[str] = field(default_factory=list)
    services: list[dict[str, object]] = field(default_factory=list)
    updated_at: str = ''
    error: str | None = None


# ----------------------------------------------------------------- pure rules


def bounded(text: str, limit: int = MAX_TEXT) -> str:
    scrubbed = SECRET_TEXT.sub(r'\1\2<redacted>', text)
    return scrubbed if len(scrubbed) <= limit else scrubbed[:limit] + '...'


def runtime_id_for(common_dir: str, task_id: str, plan_slug: str) -> str:
    """Must equal the task-workflow derivation; contract tests pin both."""
    return 'rt-' + hashlib.sha256(f'{common_dir}\0{task_id}\0{plan_slug}'.encode()).hexdigest()[:20]


def parse_record(data: object, location: Path) -> Record:
    """Strictly parse a generic runtime record; malformed manifests never run commands."""
    if not isinstance(data, dict) or set(data) - set(RECORD_FIELDS) or data.get('schema_version') != SCHEMA_VERSION or type(data.get('schema_version')) is not int:
        raise FinishError(f'malformed or incompatible runtime record {location}; preserved for review')
    try:
        record = Record(**data)
    except TypeError as exc:
        raise FinishError(f'malformed runtime record {location}: {exc}') from exc
    strings = ('runtime_id', 'plan_slug', 'hostname', 'task_id', 'worktree', 'common_dir', 'adapter', 'state_dir', 'phase')
    if (any(not isinstance(getattr(record, n), str) for n in strings) or record.phase not in RECORD_PHASES
            or not SLUG_RE.fullmatch(record.plan_slug) or not SLUG_RE.fullmatch(record.adapter)
            or not isinstance(record.start_args, list) or any(not isinstance(a, str) for a in record.start_args)):
        raise FinishError(f'malformed runtime record {location}; preserved for review')
    return record


def ownership_error(record: Record, task: TaskFacts, directory: Path) -> str | None:
    """None when the record is fully consistent with this task; otherwise why it is unsafe."""
    expected = {
        'hostname': f'{record.plan_slug}.localhost',
        'runtime_id': runtime_id_for(task.common_dir, task.task_id, record.plan_slug),
        'worktree': str(task.path),
        'common_dir': task.common_dir,
        'state_dir': str(directory / 'state'),
    }
    if directory.name != record.plan_slug:
        return f'record {directory} names plan {record.plan_slug}'
    for key, value in expected.items():
        if getattr(record, key) != value:
            return f'record {directory} has inconsistent {key}'
    return None


def teardown_argv(executable: Path, record: Record) -> list[str]:
    return [str(executable), '--plan-slug', record.plan_slug, '--hostname', record.hostname, '--task-id', record.task_id,
            '--state-dir', record.state_dir, '--json', '--teardown']


def response_error(data: object, record: Record) -> str | None:
    """Teardown counts only when the owner adapter confirms stopped/absent for this runtime."""
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data.get('schema_version') != SCHEMA_VERSION:
        return 'incompatible teardown response schema'
    if data.get('operation') != 'stop':
        return 'teardown response names another operation'
    for key in ('plan_slug', 'task_id', 'worktree', 'runtime_id'):
        if data.get(key) != getattr(record, key):
            return f'teardown response {key} does not match the runtime'
    if data.get('success') is not True or data.get('status') not in {'stopped', 'absent'}:
        readiness = data.get('readiness')
        detail = readiness.get('detail', '') if isinstance(readiness, dict) else ''
        return f'adapter did not confirm teardown (status {data.get("status")!r}): {bounded(str(detail), 500)}'
    return None


# -------------------------------------------------------------------- effects


def atomic_write(path: Path, content: str) -> None:
    if path.is_symlink():
        raise FinishError(f'refusing symlink state destination: {path}')
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(lock: Path, description: str) -> Iterator[None]:
    if lock.is_symlink():
        raise FinishError(f'refusing symlink lock: {lock}')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise FinishError(f'{description} is locked: {lock}; inspect its PID and preserve evidence before removing a stale lock') from exc
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump({'pid': os.getpid(), 'owner': 'finish'}, stream)
        yield
    finally:
        lock.unlink()


def read_json(path: Path, description: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise FinishError(f'{description} is missing or not a regular file: {path}')
    try:
        text = path.read_text(encoding='utf-8')
        if len(text.encode()) > MAX_JSON_BYTES:
            raise FinishError(f'{description} is too large: {path}')
        return json.loads(text)
    except (OSError, UnicodeError, ValueError) as exc:
        raise FinishError(f'cannot read {description} {path}: {exc}; preserved for review') from exc


def run_faur(runner: list[str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run([*runner, *args], cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        raise FinishError(f'faur unavailable: {exc}; install a version supporting context JSON or pass --faur-argv') from exc


def task_facts(runner: list[str], cwd: Path, name: str) -> TaskFacts:
    """Consume faur's immutable task record from version-1 context JSON."""
    result = run_faur(runner, cwd, 'context', name, '--json')
    try:
        data = json.loads(result.stdout)
    except ValueError as exc:
        raise FinishError(f'faur context lacks the JSON contract: {bounded(result.stderr.strip() or result.stdout.strip())}') from exc
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data['schema_version'] != 1 or data.get('command') != 'context':
        raise FinishError('unsupported faur context schema; require schema_version 1')
    if result.returncode or data.get('ok') is not True:
        raise FinishError(f'faur context {name} failed: {bounded(str(data.get("error") or result.stderr.strip()))}')
    workspace, worktree, task = data.get('workspace'), data.get('worktree'), data.get('task')
    if not isinstance(workspace, dict) or not isinstance(worktree, dict) or not isinstance(task, dict):
        raise FinishError('faur context is missing workspace/worktree/task objects')
    if task.get('status') != 'valid' or not isinstance(task.get('record'), dict):
        raise FinishError(f'{name} has no valid faur task record ({task.get("status")}); preview teardown needs an immutable task ID')
    record = task['record']
    task_id, path, root, common = record.get('task_id'), worktree.get('path'), workspace.get('root'), workspace.get('common_dir')
    if not all(isinstance(v, str) and v for v in (task_id, path, root, common)):
        raise FinishError('faur context lacks task ID, worktree path, or workspace identity')
    resolved = Path(path).resolve()
    if str(resolved) != path or not resolved.is_dir() or worktree.get('name') != name:
        raise FinishError('faur worktree path is not canonical and present')
    if str(Path(root).resolve()) != root or Path(common).resolve() != Path(root) / '.bare':
        raise FinishError('faur workspace root/common directory is not canonical')
    return TaskFacts(name, str(task_id), resolved, Path(root), str(common))


def faur_finish(runner: list[str], cwd: Path, name: str, branch: str | None, *, dry_run: bool) -> str:
    args = ['finish', name, *(['--branch', branch] if branch else []), *(['--dry-run'] if dry_run else [])]
    result = run_faur(runner, cwd, *args)
    output = bounded((result.stdout + result.stderr).strip())
    if result.returncode:
        raise FinishError(f'faur {" ".join(args)} failed (exit {result.returncode}): {output}')
    return output


def owned_records(task: TaskFacts) -> list[tuple[Path, Record]]:
    """Every runtime record of this task; unreadable records block finish rather than being skipped."""
    runs = task.root / FAUR_RUNS
    if runs.is_symlink():
        raise FinishError(f'refusing symlinked runtime directory: {runs}')
    if not runs.exists():
        return []
    owned: list[tuple[Path, Record]] = []
    for directory in sorted(runs.iterdir()):
        location = directory / RECORD_FILE
        if directory.is_symlink() or location.is_symlink():
            raise FinishError(f'refusing symlinked runtime state: {directory}')
        if not directory.is_dir() or not location.exists():
            continue
        record = parse_record(read_json(location, 'runtime record'), location)
        if record.task_id != task.task_id:
            continue
        error = ownership_error(record, task, directory)
        if error:
            raise FinishError(f'{error}; refusing to run commands from an inconsistent manifest')
        owned.append((directory, record))
    return owned


def stop_executable(task: TaskFacts) -> Path:
    """Resolve the adapter from the validated task root, never from a manifest path."""
    tools = task.path / TOOLS_DIR
    for path in (tools, tools / 'stop', tools / PROTOCOL_FILE):
        if path.is_symlink():
            raise FinishError(f'refusing symlinked adapter path: {path}')
    executable = tools / 'stop'
    if not executable.resolve().is_relative_to(task.path) or not executable.is_file() or not os.access(executable, os.X_OK):
        raise FinishError(f'missing required lifecycle tool {TOOLS_DIR}/stop in {task.path}; cannot tear down the preview')
    return executable


def adapter_kind(task: TaskFacts) -> str:
    data = read_json(task.path / TOOLS_DIR / PROTOCOL_FILE, 'preview protocol')
    if not isinstance(data, dict) or type(data.get('schema_version')) is not int or data.get('schema_version') != SCHEMA_VERSION:
        raise FinishError(f'incompatible preview protocol in {task.path}; require schema_version {SCHEMA_VERSION}')
    kind = data.get('adapter')
    if not isinstance(kind, str) or not SLUG_RE.fullmatch(kind):
        raise FinishError('preview protocol adapter kind is malformed')
    return kind


def registry_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Same location as the task-workflow writer; contract tests pin both."""
    env = os.environ if environment is None else environment
    raw = env.get('XDG_STATE_HOME', '')
    base = Path(raw) if raw and Path(raw).is_absolute() else Path(env.get('HOME') or Path.home()) / '.local' / 'state'
    return base / 'coding-skill' / 'preview-hostnames'


def release_claim(record: Record) -> None:
    """Remove only this runtime's claim; foreign or corrupt registries are preserved and reported."""
    registry = registry_dir()
    path = registry / f'{record.hostname}.json'
    if registry.is_symlink() or not path.exists():
        return
    with exclusive_lock(registry / f'{record.hostname}.lock', f'hostname {record.hostname}'):
        data = read_json(path, 'hostname claims')
        if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION or data.get('hostname') != record.hostname or not isinstance(data.get('claims'), list):
            raise FinishError(f'corrupt hostname registry {path}; preserved for review')
        claims = data['claims']
        if any(not isinstance(c, dict) for c in claims):
            raise FinishError(f'corrupt hostname registry {path}; preserved for review')
        own = [c for c in claims if c.get('runtime_id') == record.runtime_id]
        if any(c.get('task_id') != record.task_id for c in own):
            raise FinishError(f'hostname claim for {record.runtime_id} belongs to a foreign task')
        remaining = [c for c in claims if c.get('runtime_id') != record.runtime_id]
        if not own:
            return
        if remaining:
            atomic_write(path, json.dumps({**data, 'claims': remaining}, sort_keys=True, indent=2) + '\n')
        else:
            path.unlink()


def save_record(location: Path, record: Record) -> None:
    record.updated_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    atomic_write(location, json.dumps(asdict(record), sort_keys=True, indent=2) + '\n')


def teardown(task: TaskFacts, directory: Path, record: Record, *, timeout: float) -> dict[str, object]:
    """Quiesce and delete one owned runtime through the repository's stop --teardown."""
    location = directory / RECORD_FILE
    with exclusive_lock(directory / 'runtime.lock', f'preview runtime {record.plan_slug}'):
        record = parse_record(read_json(location, 'runtime record'), location)
        if ownership_error(record, task, directory) or record.task_id != task.task_id:
            raise FinishError(f'runtime record {location} changed ownership during finish')
        if record.phase == 'torn-down':
            release_claim(record)
            return {'runtime_id': record.runtime_id, 'plan_slug': record.plan_slug, 'result': 'already torn down'}
        executable = stop_executable(task)
        kind = adapter_kind(task)
        if kind != record.adapter:
            raise FinishError(f'repository adapter {kind} differs from recorded {record.adapter}; teardown blocked')
        record.phase, record.error = 'tearing-down', None
        save_record(location, record)
        started = time.monotonic()
        try:
            result = subprocess.run(teardown_argv(executable, record), cwd=task.path, env={**os.environ, RUNTIME_ENV: record.runtime_id},
                                    capture_output=True, text=True, errors='replace', timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            record.error = bounded(f'teardown could not complete: {exc}')
            save_record(location, record)
            raise FinishError(f'{record.error}; worktree and runtime record retained') from exc
        try:
            data: object = json.loads(result.stdout) if len(result.stdout) <= MAX_JSON_BYTES else None
        except ValueError:
            data = None
        error = response_error(data, record)
        if error is None and result.returncode:
            error = f'teardown exited {result.returncode}'
        if error:
            record.error = bounded(f'{error}: {result.stderr.strip()}' if result.stderr.strip() else error)
            save_record(location, record)
            raise FinishError(f'preview teardown failed for {record.plan_slug}: {record.error}; real finish blocked, worktree and {location} retained')
        release_claim(record)
        record.phase, record.services = 'torn-down', []
        save_record(location, record)
        return {'runtime_id': record.runtime_id, 'plan_slug': record.plan_slug, 'result': 'torn down', 'duration_seconds': round(time.monotonic() - started, 3)}


def finish(name: str, *, branch: str | None, runner: list[str], dry_run: bool = False, timeout: float = DEFAULT_TIMEOUT) -> dict[str, object]:
    cwd = Path.cwd()
    task = task_facts(runner, cwd, name)
    records = owned_records(task)
    preflight = faur_finish(runner, cwd, name, branch, dry_run=True)
    report: dict[str, object] = {'task': name, 'task_id': task.task_id, 'worktree': str(task.path), 'branch': branch,
                                 'dry_run': dry_run, 'preflight': preflight,
                                 'previews': [{'runtime_id': r.runtime_id, 'plan_slug': r.plan_slug, 'phase': r.phase} for _, r in records]}
    if dry_run:
        report['teardown'] = 'not run during dry-run'
        return report
    report['teardown'] = [teardown(task, directory, record, timeout=timeout) for directory, record in records]
    report['finish'] = faur_finish(runner, cwd, name, branch, dry_run=False)
    if task.path.exists():
        raise FinishError(f'faur finish reported success but {task.path} still exists; runtime records retained')
    for directory, _ in records:
        # Cleanup state is discarded only after verified integration and removal.
        if not directory.is_symlink() and directory.parent == task.root / FAUR_RUNS:
            shutil.rmtree(directory)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('name', help='task worktree name returned by faur creation')
    parser.add_argument('--branch', help='destination for exact "finish in <branch>"')
    parser.add_argument('--dry-run', action='store_true', help='context and faur preflight only; no teardown')
    parser.add_argument('--faur-argv', default='["faur", "worktree"]', help='JSON executable argv prefix')
    parser.add_argument('--timeout', type=float, default=DEFAULT_TIMEOUT, help='seconds per teardown')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s', stream=sys.stderr)
    args = build_parser().parse_args(argv)
    try:
        runner = json.loads(args.faur_argv)
        if not isinstance(runner, list) or not runner or any(not isinstance(x, str) or not x for x in runner):
            raise FinishError('--faur-argv requires a nonempty JSON array of strings')
        print(json.dumps(finish(args.name, branch=args.branch, runner=runner, dry_run=args.dry_run, timeout=args.timeout), sort_keys=True))
        return 0
    except (FinishError, OSError, ValueError) as exc:
        LOGGER.error('%s', bounded(str(exc)))
        return 2


if __name__ == '__main__':
    sys.exit(main())
