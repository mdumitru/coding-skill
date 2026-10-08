#!/usr/bin/env python3
"""Generic repository preview contract: typed protocol, runtime records, and hostname claims.

Pure decisions (parse_*, *_decision, *_error, redact) never touch the filesystem
or processes; the effectful helpers below them persist state atomically under
exclusive locks and refuse corrupt, foreign, or symlinked state.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_io import StateError, atomic_write, exclusive_lock

SCHEMA_VERSION = 1
LIFECYCLE = ('start', 'status', 'logs', 'stop', 'reset')
TOOLS_DIR = 'worktree-tools'
PROTOCOL_FILE = 'protocol.json'
FAUR_RUNS = '_runs'
PLAIN_RUNS = '.worktree-runs'
RECORD_FILE = 'runtime.json'
RUNTIME_ENV = 'WORKTREE_RUNTIME_ID'
SLUG_RE = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
TASK_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
OPTION_RE = re.compile(r'^--[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
STATUSES = frozenset({'starting', 'running', 'stopped', 'absent', 'failed'})
RECORD_PHASES = frozenset({'starting', 'running', 'stopping', 'stopped', 'failed', 'tearing-down', 'torn-down'})
PROTOCOL_KEYS = frozenset({'schema_version', 'adapter', 'start_arguments', 'pairing', 'description'})
ENVELOPE_KEYS = frozenset({'schema_version', 'operation', 'success', 'plan_slug', 'task_id', 'worktree', 'runtime_id',
                           'status', 'readiness', 'services', 'pairing', 'details'})
MAX_JSON_BYTES = 256 * 1024
MAX_TEXT = 4000
MAX_ITEMS = 100
MAX_DEPTH = 6
SECRET_KEY = re.compile(r'pass(?:word|wd)?|secret|token|credential|api[-_]?key|private[-_]?key|auth|cookie|session', re.I)
SECRET_TEXT = re.compile(r'(?i)\b(password|passwd|secret|token|api[-_]?key|authorization|cookie)\b(\s*[=:]\s*)("[^"]*"|\S+)')
URL_CREDENTIALS = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@')


# ----------------------------------------------------------------- pure values


@dataclass(frozen=True)
class StartOption:
    name: str
    value: bool


@dataclass(frozen=True)
class Protocol:
    schema_version: int
    adapter: str
    start_options: tuple[StartOption, ...] = ()
    pairing: str | None = None


@dataclass(frozen=True)
class RuntimeIdentity:
    plan_slug: str
    hostname: str
    task_id: str
    worktree: str
    common_dir: str
    runtime_id: str


@dataclass(frozen=True)
class Service:
    name: str
    url: str
    port: int


@dataclass(frozen=True)
class Response:
    operation: str
    success: bool
    status: str
    ready: bool
    readiness_detail: str
    services: tuple[Service, ...]
    pairing: str | None
    details: dict[str, object]
    runtime_id: str
    plan_slug: str
    task_id: str
    worktree: str


@dataclass(frozen=True)
class Claim:
    hostname: str
    runtime_id: str
    task_id: str
    worktree: str
    common_dir: str
    record: str
    pairing: str | None = None


@dataclass
class RuntimeRecord:
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


def bounded(text: str, limit: int = MAX_TEXT) -> str:
    """Scrub obvious credentials and cap diagnostics before reporting them."""
    scrubbed = URL_CREDENTIALS.sub(r'\1<redacted>@', SECRET_TEXT.sub(r'\1\2<redacted>', text))
    return scrubbed if len(scrubbed) <= limit else scrubbed[:limit] + f'... [{len(scrubbed) - limit} more characters]'


def redact(value: object, depth: int = 0) -> object:
    """Bound adapter-specific details and drop credential-like fields recursively."""
    if depth >= MAX_DEPTH:
        return '<truncated>'
    if isinstance(value, Mapping):
        items = list(value.items())[:MAX_ITEMS]
        return {str(k): '<redacted>' if SECRET_KEY.search(str(k)) else redact(v, depth + 1) for k, v in items}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:MAX_ITEMS]]
    if isinstance(value, str):
        return bounded(value, 1000)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return bounded(str(value), 1000)


def hostname_for(plan_slug: str) -> str:
    """The public hostname is always the plan slug, never a suffixed worktree name."""
    if not SLUG_RE.fullmatch(plan_slug):
        raise StateError(f'invalid plan slug for preview hostname: {plan_slug!r}')
    return f'{plan_slug}.localhost'


def runtime_id_for(common_dir: str, task_id: str, plan_slug: str) -> str:
    """Deterministic, so retries and restarts reuse one runtime identity."""
    return 'rt-' + hashlib.sha256(f'{common_dir}\0{task_id}\0{plan_slug}'.encode()).hexdigest()[:20]


def make_identity(plan_slug: str, task_id: str, worktree: str, common_dir: str) -> RuntimeIdentity:
    if not TASK_ID_RE.fullmatch(task_id):
        raise StateError(f'invalid immutable task ID: {task_id!r}')
    if not Path(worktree).is_absolute() or not Path(common_dir).is_absolute():
        raise StateError('preview identity requires absolute worktree and common directory paths')
    return RuntimeIdentity(plan_slug, hostname_for(plan_slug), task_id, worktree, common_dir,
                           runtime_id_for(common_dir, task_id, plan_slug))


def _strict_int(value: object) -> bool:
    return type(value) is int


def parse_protocol(data: object) -> Protocol:
    """Validate worktree-tools/protocol.json content; reject unknown or incompatible fields."""
    if not isinstance(data, dict):
        raise StateError('preview protocol must be a JSON object')
    version = data.get('schema_version')
    if not _strict_int(version):
        raise StateError('preview protocol requires an integer schema_version')
    if version != SCHEMA_VERSION:
        raise StateError(f'incompatible preview protocol schema_version {version}; this runner supports {SCHEMA_VERSION}')
    unknown = sorted(set(data) - PROTOCOL_KEYS)
    if unknown:
        raise StateError(f'unknown preview protocol fields: {", ".join(unknown)}')
    adapter = data.get('adapter')
    if not isinstance(adapter, str) or not SLUG_RE.fullmatch(adapter):
        raise StateError('preview protocol adapter kind must be a kebab-case string')
    raw_options = data.get('start_arguments', [])
    if not isinstance(raw_options, list):
        raise StateError('start_arguments must be a list of option declarations')
    options: list[StartOption] = []
    for item in raw_options:
        if not isinstance(item, dict) or set(item) - {'name', 'value', 'description'}:
            raise StateError('each start argument requires only name, value, and optional description')
        name, takes_value = item.get('name'), item.get('value', False)
        if not isinstance(name, str) or not OPTION_RE.fullmatch(name) or not isinstance(takes_value, bool):
            raise StateError(f'malformed start argument declaration: {item!r}')
        if name in {o.name for o in options} or name in {'--plan-slug', '--hostname', '--task-id', '--state-dir', '--json'}:
            raise StateError(f'duplicate or reserved start argument: {name}')
        options.append(StartOption(name, takes_value))
    pairing = data.get('pairing')
    if pairing is not None:
        if not isinstance(pairing, dict) or set(pairing) != {'group'} or not isinstance(pairing['group'], str) or not SLUG_RE.fullmatch(pairing['group']):
            raise StateError('pairing must be {"group": "<kebab-case>"}')
        pairing = pairing['group']
    return Protocol(SCHEMA_VERSION, adapter, tuple(options), pairing)


def validate_start_arguments(protocol: Protocol, argv: list[str]) -> list[str]:
    """Accept only declared options; values travel as separate argv items, never a shell."""
    declared = {o.name: o for o in protocol.start_options}
    result: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if '\x00' in token:
            raise StateError('start arguments must not contain NUL bytes')
        name = token.partition('=')[0]
        option = declared.get(name)
        if option is None:
            supported = ', '.join(sorted(declared)) or 'none'
            raise StateError(f'unsupported start argument {name!r}; repository protocol declares: {supported}')
        if option.value:
            if '=' in token:
                result.append(token)
            elif index + 1 < len(argv) and '\x00' not in argv[index + 1]:
                result.extend([token, argv[index + 1]])
                index += 1
            else:
                raise StateError(f'start argument {name} requires a value')
        elif '=' in token:
            raise StateError(f'start argument {name} does not take a value')
        else:
            result.append(token)
        index += 1
    return result


def lifecycle_argv(executable: Path, identity: RuntimeIdentity, state_dir: Path, *extra: str, machine: bool = True) -> list[str]:
    """Every lifecycle executable receives the same identity flags; argv only."""
    argv = [str(executable), '--plan-slug', identity.plan_slug, '--hostname', identity.hostname,
            '--task-id', identity.task_id, '--state-dir', str(state_dir)]
    return argv + (['--json'] if machine else []) + list(extra)


def lifecycle_env(identity: RuntimeIdentity, base: Mapping[str, str]) -> dict[str, str]:
    """Adapters echo the runtime ID in their envelope; it travels in the environment."""
    return {**base, RUNTIME_ENV: identity.runtime_id}


def parse_json_text(text: str, description: str) -> object:
    if len(text.encode('utf-8', 'surrogateescape')) > MAX_JSON_BYTES:
        raise StateError(f'{description} exceeds {MAX_JSON_BYTES} bytes')
    try:
        return json.loads(text)
    except ValueError as exc:
        raise StateError(f'{description} is not valid JSON: {exc}') from exc


def _service(item: object, hostname: str) -> Service:
    if not isinstance(item, dict):
        raise StateError('each service must be an object')
    name, url, port = item.get('name'), item.get('url'), item.get('port')
    if not isinstance(name, str) or not SLUG_RE.fullmatch(name):
        raise StateError('service name must be kebab-case')
    if not _strict_int(port) or not 0 < port < 65536:
        raise StateError(f'service {name} requires a TCP port number')
    if not isinstance(url, str) or len(url) > 2048:
        raise StateError(f'service {name} requires a URL')
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise StateError(f'service {name} URL must not embed credentials')
    if parts.scheme not in {'http', 'https'} or parts.hostname != hostname:
        raise StateError(f'service {name} URL must use http(s) on exactly {hostname}')
    try:
        explicit = parts.port
    except ValueError as exc:
        raise StateError(f'service {name} URL has an invalid port') from exc
    if explicit is not None and explicit != port:
        raise StateError(f'service {name} URL port disagrees with its declared port')
    return Service(name, url, port)


def parse_response(data: object, identity: RuntimeIdentity, operation: str) -> Response:
    """Validate a versioned adapter envelope against the expected runtime identity."""
    if not isinstance(data, dict):
        raise StateError('adapter response must be a JSON object')
    version = data.get('schema_version')
    if not _strict_int(version) or version != SCHEMA_VERSION:
        raise StateError(f'incompatible adapter response schema_version {version!r}; require {SCHEMA_VERSION}')
    unknown = sorted(set(data) - ENVELOPE_KEYS)
    if unknown:
        raise StateError(f'unknown adapter response fields (use details): {", ".join(unknown)}')
    if data.get('operation') != operation:
        raise StateError(f'adapter answered operation {data.get("operation")!r}, expected {operation!r}')
    expected = {'plan_slug': identity.plan_slug, 'task_id': identity.task_id, 'worktree': identity.worktree, 'runtime_id': identity.runtime_id}
    for key, value in expected.items():
        if data.get(key) != value:
            raise StateError(f'adapter response {key} does not match this runtime (foreign task/worktree identity)')
    success, status = data.get('success'), data.get('status')
    if not isinstance(success, bool) or status not in STATUSES:
        raise StateError('adapter response requires boolean success and a known status')
    readiness = data.get('readiness')
    if not isinstance(readiness, dict) or not isinstance(readiness.get('ready'), bool):
        raise StateError('adapter response requires readiness.ready')
    detail = readiness.get('detail', '')
    raw_services = data.get('services', [])
    if not isinstance(raw_services, list) or len(raw_services) > MAX_ITEMS:
        raise StateError('adapter services must be a bounded list')
    services = tuple(_service(s, identity.hostname) for s in raw_services)
    if len({s.name for s in services}) != len(services) or len({s.port for s in services}) != len(services):
        raise StateError('adapter services must have unique names and ports')
    pairing = data.get('pairing')
    if pairing is not None:
        if not isinstance(pairing, dict) or not isinstance(pairing.get('group'), str) or not SLUG_RE.fullmatch(pairing['group']):
            raise StateError('adapter pairing must be {"group": "<kebab-case>"}')
        pairing = pairing['group']
    details = data.get('details', {})
    if not isinstance(details, dict):
        raise StateError('adapter details must be an object')
    return Response(operation, success, str(status), readiness['ready'], bounded(str(detail), 1000), services, pairing,
                    cast(dict[str, object], redact(details)), identity.runtime_id, identity.plan_slug, identity.task_id, identity.worktree)


def readiness_error(protocol: Protocol, response: Response) -> str | None:
    """Startup succeeds only for a ready, running, correctly paired runtime with services."""
    if not response.success:
        return f'adapter reported failure: {response.readiness_detail or response.status}'
    if response.status != 'running' or not response.ready:
        return f'runtime is not ready (status {response.status}): {response.readiness_detail or "no detail"}'
    if not response.services:
        return 'ready runtime reported no services'
    if response.pairing != protocol.pairing:
        return f'adapter pairing {response.pairing!r} does not verify declared pairing {protocol.pairing!r}'
    return None


def claim_decision(existing: list[Claim], new: Claim) -> tuple[list[Claim], str | None]:
    """Return the next claim list or a refusal; independent runtimes never share a hostname."""
    if any(c.hostname != new.hostname for c in existing):
        return existing, 'hostname registry contains a claim for another hostname'
    others: list[Claim] = []
    for claim in existing:
        if claim.runtime_id == new.runtime_id:
            if (claim.task_id, claim.worktree, claim.common_dir) != (new.task_id, new.worktree, new.common_dir):
                return existing, f'runtime {new.runtime_id} is claimed by a foreign task/worktree identity ({claim.worktree})'
            continue
        others.append(claim)
    for claim in others:
        if new.pairing is None or claim.pairing != new.pairing:
            return existing, (f'hostname {new.hostname} is already claimed by independent runtime {claim.runtime_id} '
                              f'(worktree {claim.worktree}, record {claim.record}); tear it down first or declare a matching pairing group')
    return [*others, new], None


def release_decision(existing: list[Claim], hostname: str, runtime_id: str, task_id: str) -> tuple[list[Claim], bool, str | None]:
    """Remove only this runtime's own claim; a foreign owner of the ID is refused."""
    remaining = [c for c in existing if c.runtime_id != runtime_id]
    own = [c for c in existing if c.runtime_id == runtime_id]
    if any(c.task_id != task_id or c.hostname != hostname for c in own):
        return existing, False, f'claim for runtime {runtime_id} belongs to a foreign task'
    return remaining, bool(own), None


def check_record(data: object, identity: RuntimeIdentity) -> RuntimeRecord:
    """Parse a durable runtime record and require it to belong to this identity."""
    if not isinstance(data, dict):
        raise StateError('runtime record must be a JSON object')
    if not _strict_int(data.get('schema_version')) or data.get('schema_version') != SCHEMA_VERSION:
        raise StateError(f'incompatible runtime record schema_version {data.get("schema_version")!r}')
    try:
        record = RuntimeRecord(**data)
    except TypeError as exc:
        raise StateError(f'malformed runtime record: {exc}') from exc
    for name in ('runtime_id', 'plan_slug', 'hostname', 'task_id', 'worktree', 'common_dir', 'adapter', 'state_dir', 'phase', 'updated_at'):
        if not isinstance(getattr(record, name), str):
            raise StateError(f'runtime record field {name} must be a string')
    if record.phase not in RECORD_PHASES:
        raise StateError(f'unknown runtime record phase: {record.phase}')
    if not isinstance(record.start_args, list) or any(not isinstance(a, str) for a in record.start_args):
        raise StateError('runtime record start_args must be strings')
    if not isinstance(record.services, list) or any(not isinstance(s, dict) for s in record.services):
        raise StateError('runtime record services must be objects')
    if record.pairing is not None and not isinstance(record.pairing, str):
        raise StateError('runtime record pairing must be a string')
    if record.error is not None and not isinstance(record.error, str):
        raise StateError('runtime record error must be a string')
    actual = (record.plan_slug, record.hostname, record.task_id, record.worktree, record.common_dir, record.runtime_id)
    expected = (identity.plan_slug, identity.hostname, identity.task_id, identity.worktree, identity.common_dir, identity.runtime_id)
    if actual != expected:
        raise StateError(f'runtime record belongs to a foreign task/worktree identity ({record.task_id} at {record.worktree}); state preserved')
    return record


def parse_claims(data: object, hostname: str) -> list[Claim]:
    if not isinstance(data, dict) or data.get('schema_version') != SCHEMA_VERSION or data.get('hostname') != hostname or not isinstance(data.get('claims'), list):
        raise StateError(f'corrupt hostname claim registry for {hostname}; preserved for review')
    claims: list[Claim] = []
    for item in data['claims']:
        try:
            claim = Claim(**item)
        except TypeError as exc:
            raise StateError(f'corrupt hostname claim for {hostname}: {exc}') from exc
        if (any(not isinstance(v, str) for k, v in asdict(claim).items() if k != 'pairing') or claim.hostname != hostname
                or (claim.pairing is not None and not isinstance(claim.pairing, str))):
            raise StateError(f'corrupt hostname claim for {hostname}; preserved for review')
        claims.append(claim)
    return claims


def is_within(root: Path, candidate: Path) -> bool:
    """Pure containment check over already-resolved absolute paths."""
    return candidate == root or candidate.is_relative_to(root)


def now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


# ------------------------------------------------------------------- effects


def safe_child(root: Path, *parts: str) -> Path:
    """Join below root, refusing any symlinked component or resolved escape."""
    resolved_root = root.resolve()
    current = root
    for part in parts:
        if part in {'', '.', '..'} or '/' in part:
            raise StateError(f'invalid path component: {part!r}')
        current = current / part
        if current.is_symlink():
            raise StateError(f'refusing symlinked preview path: {current}')
    if not is_within(resolved_root, current.resolve()):
        raise StateError(f'preview path escapes {root}: {current}')
    return current


def read_json_file(path: Path, description: str) -> object:
    if path.is_symlink() or not path.is_file():
        raise StateError(f'{description} is missing or not a regular file: {path}')
    try:
        return parse_json_text(path.read_text(encoding='utf-8'), description)
    except (OSError, UnicodeError) as exc:
        raise StateError(f'cannot read {description}: {exc}') from exc


def adapter_executable(task_root: Path, operation: str) -> Path:
    if operation not in LIFECYCLE:
        raise StateError(f'unknown lifecycle operation: {operation}')
    tool = safe_child(task_root, TOOLS_DIR, operation)
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise StateError(f'missing required lifecycle tool: {TOOLS_DIR}/{operation} must be an executable file in {task_root}')
    return tool


def load_protocol(task_root: Path) -> Protocol:
    """Read the repository protocol and require every lifecycle executable."""
    path = safe_child(task_root, TOOLS_DIR, PROTOCOL_FILE)
    if not path.exists():
        raise StateError(f'repository does not support previews: {TOOLS_DIR}/{PROTOCOL_FILE} is missing in {task_root}')
    protocol = parse_protocol(read_json_file(path, 'preview protocol'))
    for operation in LIFECYCLE:
        adapter_executable(task_root, operation)
    return protocol


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    directory: Path
    record: Path
    lock: Path
    state_dir: Path


def runtime_paths(base: Path, plan_slug: str, *, faur: bool) -> RuntimePaths:
    """faur: <workspace>/_runs/<slug>; plain: <repo>/.worktree-runs/<slug> (must be ignored)."""
    hostname_for(plan_slug)
    root = safe_child(base, FAUR_RUNS if faur else PLAIN_RUNS)
    directory = safe_child(base, root.name, plan_slug)
    return RuntimePaths(root, directory, directory / RECORD_FILE, directory / 'runtime.lock', directory / 'state')


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    for directory in (paths.root, paths.directory, paths.state_dir):
        if directory.is_symlink():
            raise StateError(f'refusing symlinked preview directory: {directory}')
        directory.mkdir(mode=0o700, exist_ok=True)
    os.chmod(paths.state_dir, 0o700)
    for path in (paths.record, paths.lock):
        if path.is_symlink():
            raise StateError(f'refusing symlinked preview state: {path}')


def runtime_lock(paths: RuntimePaths, identity: RuntimeIdentity) -> AbstractContextManager[None]:
    ensure_runtime_dirs(paths)
    return exclusive_lock(paths.lock, f'preview runtime {identity.plan_slug}', runtime_id=identity.runtime_id, task_id=identity.task_id)


def load_record(paths: RuntimePaths, identity: RuntimeIdentity) -> RuntimeRecord | None:
    if paths.record.is_symlink():
        raise StateError(f'refusing symlinked runtime record: {paths.record}')
    if not paths.record.exists():
        return None
    return check_record(read_json_file(paths.record, 'runtime record'), identity)


def save_record(paths: RuntimePaths, record: RuntimeRecord) -> None:
    record.updated_at = now()
    atomic_write(paths.record, json.dumps(asdict(record), sort_keys=True, indent=2) + '\n')
    os.chmod(paths.record, 0o600)


def registry_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Machine-level claims live under the user's private state directory."""
    env = os.environ if environment is None else environment
    raw = env.get('XDG_STATE_HOME', '')
    base = Path(raw) if raw and Path(raw).is_absolute() else Path(env.get('HOME') or Path.home()) / '.local' / 'state'
    directory = base / 'coding-skill' / 'preview-hostnames'
    for parent in (base / 'coding-skill', directory):
        if parent.is_symlink():
            raise StateError(f'refusing symlinked hostname registry: {parent}')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _claims_path(registry: Path, hostname: str) -> Path:
    hostname_for(hostname.removesuffix('.localhost'))
    return safe_child(registry, f'{hostname}.json')


def read_claims(registry: Path, hostname: str) -> list[Claim]:
    path = _claims_path(registry, hostname)
    return parse_claims(read_json_file(path, 'hostname claims'), hostname) if path.exists() else []


def _write_claims(registry: Path, hostname: str, claims: list[Claim]) -> None:
    path = _claims_path(registry, hostname)
    if not claims:
        path.unlink(missing_ok=True)
        return
    atomic_write(path, json.dumps({'schema_version': SCHEMA_VERSION, 'hostname': hostname, 'claims': [asdict(c) for c in claims]}, sort_keys=True, indent=2) + '\n')
    os.chmod(path, 0o600)


def claim_hostname(registry: Path, claim: Claim) -> None:
    with exclusive_lock(safe_child(registry, f'{claim.hostname}.lock'), f'hostname {claim.hostname}', runtime_id=claim.runtime_id):
        claims, error = claim_decision(read_claims(registry, claim.hostname), claim)
        if error:
            raise StateError(error)
        _write_claims(registry, claim.hostname, claims)


def release_hostname(registry: Path, hostname: str, runtime_id: str, task_id: str) -> bool:
    with exclusive_lock(safe_child(registry, f'{hostname}.lock'), f'hostname {hostname}', runtime_id=runtime_id):
        claims, released, error = release_decision(read_claims(registry, hostname), hostname, runtime_id, task_id)
        if error:
            raise StateError(error)
        if released:
            _write_claims(registry, hostname, claims)
        return released
