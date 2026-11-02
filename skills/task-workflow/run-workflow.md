# `run:` Mode

`run:` previews a plan's recorded task worktree, or any worktree or branch,
through a repository-owned lifecycle adapter. Skills stay generic: they hold no product names, endpoints,
credentials, database choices, sample paths, or application commands. The
repository's executables own dependencies, ports, configuration, data,
subprocesses, and cleanup. `scripts/preview_protocol.py` implements the
contract below with standard-library code only.

## Commands

`run: <name>` passes `<name>` verbatim to the helper, which resolves it in
this order and reports the choice as `source`:

1. `plan` — an executed plan `<name>` (one with an execution journal): its
   recorded task worktree. An incomplete execution is refused; resume it.
2. `worktree` — the one existing worktree whose faur name or branch is
   `<name>`, such as an impromptu task worktree created without a plan.
   Several matches are refused as ambiguous.
3. `created-worktree` — `run` only, faur workspaces only: an existing local
   or `origin/` branch `<name>` with no worktree is checked out through
   `faur worktree add <name> --json` (origin is fetched once when the branch
   is unknown locally). Plain repositories refuse with a checkout hint.

Anything else is refused; a plan with no journal and no matching worktree or
branch must be executed first. The helper never creates a branch, and
`status`, `logs`, `stop`, and `reset` never create a worktree.

A bare `run:` together with exactly one `execute: <plan-slug>` in the same
request uses that slug (see below). A bare `run:` alone previews the worktree
containing the helper's working directory: run it from this conversation's
task worktree when one exists, else from the launch worktree. Never guess a
name from conversation context; a conflicting or ambiguous name is rejected.

Resolve `scripts/preview.py` by absolute path from this skill directory and
run it from any worktree of the repository; faur version-1 context validates
every resolved worktree:

```sh
python3 <task-workflow-dir>/scripts/preview.py run [<name>] [-- <start options>]
python3 <task-workflow-dir>/scripts/preview.py status [<name>]
python3 <task-workflow-dir>/scripts/preview.py logs [<name>] [--lines N] [--follow]
python3 <task-workflow-dir>/scripts/preview.py stop [<name>] [--teardown]
python3 <task-workflow-dir>/scripts/preview.py reset [<name>] [-- <start options>]
```

Use the same `<name>` for later operations on one preview. The hostname label
is the name itself when it is kebab-case, otherwise a kebab-case derivation
(`feature/Login` becomes `feature-login.localhost`). The task ID is faur's
immutable task ID when the worktree has a task record, otherwise one derived
from the worktree identity. A created or non-task worktree has no faur finish
path that tears previews down: run `stop --teardown` before removing it, and
report the created worktree's path to the user.

A standalone run previews the worktree as-is, including uncommitted edits; it
never commits, stages, or changes plan checkboxes. Repeating `run` reuses a running,
ready runtime; a stopped runtime restarts with its retained data. Changing
start options requires `reset` or `stop` first. Start options after `--` must
match the repository protocol and travel as argv, never through a shell.

For a combined `execute: <slug>` plus `run:` request, record the intent first,
from the launch worktree, before `workflow.py start`:

```sh
python3 <task-workflow-dir>/scripts/preview.py intent --execute-slug <slug> [-- <start options>]
```

The intent lives beside the execution journal in `_plans/.state/` and survives
retries and compaction; `workflow.py report` shows it as `run_intent`. For grouped plans, the coordinator retains this intent across fresh workers
and manual fresh-session continuations. Workers never start previews. After
every child and group checkpoint and the execute workflow's final audit pass, run:

```sh
python3 <task-workflow-dir>/scripts/preview.py run --execute-slug <slug>
```

It re-audits, starts or reuses the preview with the recorded options, and marks
the intent discharged. Retrying never starts a second runtime.

Each command prints one JSON report: plan slug, hostname, immutable task ID,
canonical worktree, runtime ID, phase, status, readiness, services, redacted
details, record path, and monotonic duration. Report the URLs only from a
successful run. Failures exit 2 with a bounded stderr diagnostic; a failed or
interrupted startup keeps its runtime record and partial resources, so retry
`run` or clean up with `stop --teardown`. An interrupted stop or teardown must
be finished with the same command before the runtime can start again.

## Repository protocol

A supporting repository provides, inside its task worktree, a directory named
worktree-tools containing real executable files `start`, `status`, `logs`,
`stop`, and `reset`, plus a protocol.json file:

```json
{
  "schema_version": 1,
  "adapter": "example-kind",
  "start_arguments": [{"name": "--example-option", "value": true}],
  "pairing": {"group": "example-group"}
}
```

`adapter` is a kebab-case kind. `start_arguments` (optional) declares the only
long options accepted after `--`; `value` says whether one value follows.
`pairing` (optional) declares a cooperating-service group. Unknown fields,
another schema version, or a missing/non-executable/symlinked entrypoint fail
with an actionable diagnostic. Entrypoints may use repo-owned shared modules.

Every entrypoint receives argv directly, never through a shell:

```text
<tool> --plan-slug SLUG --hostname SLUG.localhost --task-id ID --state-dir ABSOLUTE_PATH [--json] [operation options] [-- start options]
```

Machine-readable operations accept `--json`. `logs` supports bounded output and
an explicit follow option. `stop` preserves restartable data; `stop --teardown`
quiesces and deletes every resource the runtime owns; `reset` tears down, then
recreates fresh state. Stopping already stopped or absent owned resources is a
successful no-op. `start` and `reset` must be idempotent for one runtime ID, so
a retry after interrupted startup reuses partial resources. Diagnostics go to
stderr; credentials never appear in output. The runtime ID is also exported as
`WORKTREE_RUNTIME_ID` for adapters to echo.

Adapters may connect previews to pre-existing shared services. They configure
the preview-side routing and probe/report those dependencies, but a preview
lifecycle must not restart, reconfigure, or tear down a shared service. Only
resources created for the runtime are owned by `stop --teardown`. If a shared
dependency cannot provide complete application behavior, report that fact in
`readiness.detail` and application-specific `details`; the repository's live
acceptance test owns deeper functional verification.

## Response envelope

```json
{
  "schema_version": 1,
  "operation": "start",
  "success": true,
  "plan_slug": "SLUG",
  "task_id": "IMMUTABLE-ID",
  "worktree": "/canonical/task/worktree",
  "runtime_id": "rt-…",
  "status": "running",
  "readiness": {"ready": true, "detail": "short text"},
  "services": [{"name": "web", "url": "http://SLUG.localhost:PORT/", "port": 1234}],
  "pairing": {"group": "example-group"},
  "details": {}
}
```

`status` is one of starting, running, stopped, absent, or failed. Identity
fields must equal the runner's expectations, otherwise the response is a
foreign identity. Service names and ports are unique; URLs use http(s) on
exactly the plan hostname with no embedded credentials. Application-specific
data belongs in `details`, which reports redact and bound. Startup succeeds
only for a successful, running, ready response with at least one service and a
`pairing` that echoes the declared group (or is absent when none is declared).

## Identity and hostname

The hostname is exactly `<plan-slug>.localhost`, even when faur chose a
suffixed worktree such as `<plan-slug>-2`. The runtime ID is a deterministic
digest of the Git common directory, immutable task ID, and plan slug, so
retries and restarts reuse one runtime. Plain repositories have no faur task
ID; the runner derives a stable one from the recorded execution identity.

A machine-level registry under `$XDG_STATE_HOME/coding-skill/preview-hostnames`
(default `~/.local/state/...`, mode 0700) holds one claim list per hostname.
An independent runtime claiming a held hostname is refused with the owner's
worktree and record path. Runtimes declaring the same pairing group may share
it; the adapter must echo that group to verify the pairing. Claims are released
only by their own runtime after verified teardown.

## Durable runtime state

Runtime records never live in tracked sources or `_plans/.state/`:

- faur workspace: `<workspace>/_runs/<plan-slug>/`
- plain repository: `<repo>/.worktree-runs/<plan-slug>/`, kept ignored through
  the repository's local exclude file

Each directory holds a runtime record (mode 0600), an O_EXCL lock, and a
`state/` directory (mode 0700) passed as `--state-dir`. Only adapters store
secrets, in their own permission-restricted files under that directory.
Records carry schema version, runtime and task identity, canonical worktree,
adapter kind, phase, validated start options, and redacted services. They are
replaced atomically. Corrupt, foreign, or symlinked state and leftover locks
are preserved and reported, never discarded. The runner never signals
processes itself; adapters own their processes and verify their own PIDs.
