# Worktree Workflow

Faur owns canonical context, collision selection, creation, durable launch/task
identity, and integration/removal. Consume its version 1 JSON object responses;
never parse the inventory array to choose a worktree or reconstruct paths.

## Isolate an ordinary change

Before any tracked-file mutation in a faur workspace, however small:

```sh
faur worktree context --json
faur worktree add <slug> --task --unique --json
```

Context validates the current branch and canonical location. Add chooses a free
lowercase kebab-case slug, creates from the launch branch, and stores an immutable
task ID and launch destination. Use the returned worktree path for every later
command, edit, test, and commit. Relay failures and preserve partial state.
Do not reimplement branch creation, setup, fetch, collision handling, or path
resolution. Old tools lacking the versioned contract must be updated separately;
do not silently install a release during this task.

For `execute:`, the task-workflow skill's start/begin/complete runner owns these
calls and its external plan/state mutations. Do not create a second worktree
before calling start. Keep one task worktree per request, including when the
agent was launched from another existing task worktree.

Read-only work, plain repositories, and an explicit instruction to work "here"
or in a named existing worktree are exceptions. Validate an explicitly reused
faur worktree with `context <name> --json`; never infer authorization from launch
location. Plain Git work proceeds in place. Leave shared mutable `_shared/`
symlinks alone unless the request concerns them. Scratch data stays outside the
workspace; task plans stay in the external canonical `_plans/` directory.

## Complete without integration

Commit and verify in the task worktree. Refresh its context and report actual
path, branch, clean/dirty state, and commits. For execute runs, use the runner's
audit/report; ordinary changes use faur context and Git read-only checks.
Stop. Never push, open a PR, integrate, remove a worktree, or delete branches
without the corresponding explicit request. A PR invokes the pr-workflow skill.

## Exact finish authorization

Only a later message exactly `finish` or `finish in <branch>` authorizes finish.
It also authorizes tearing down the task's repository previews. Use the task
name returned by creation and run the bundled wrapper, resolved by absolute
path from this skill directory, from any worktree of the workspace:

```sh
python3 <worktree-workflow-dir>/scripts/finish.py <task-name> --dry-run
python3 <worktree-workflow-dir>/scripts/finish.py <task-name>
```

For `finish in <branch>`, append `--branch <branch>` to both; the wrapper
passes it to both faur finish calls. `--faur-argv` takes a JSON argv prefix for
a companion faur CLI. The wrapper, in order:

1. validates `faur worktree context <task-name> --json` and reads the immutable
   task ID from its version-1 task record;
2. collects that task's runtime records under the workspace `_runs/` directory;
   an unreadable, malformed, or inconsistent record stops finish without
   running any command;
3. runs `faur worktree finish <task-name> --dry-run`; a failure stops here;
4. for each owned runtime not already torn down, runs the repository's
   worktree-tools `stop --teardown` resolved from the validated task worktree,
   requires a matching stopped/absent response, then releases its hostname
   claim; one adapter call stops all of that runtime's cooperating services;
5. runs the real `faur worktree finish`, verifies the worktree is gone, and
   only then deletes the owned runtime records.

`--dry-run` performs steps 1–3 only and never tears anything down. Tasks
without previews, stopped previews with retained data, and partial startups are
handled the same way. A teardown failure exits non-zero before the real finish
and keeps the worktree, its record (phase tearing-down, with the error), and
the hostname claim; fix the cause and rerun the same command. Already torn-down
runtimes are skipped on retries. Unrelated runtimes and other tasks' claims
are never touched. The record format is the generic runtime contract that the
task-workflow skill writes; this wrapper reads it independently.

Do not remember a launch path, switch branches, resolve destination inventory
entries, compute commit ranges, cherry-pick, rebase, remove worktrees, or
delete branches manually, and never bypass a failed dry-run or teardown.

Faur refuses dirty/ambiguous/stale/in-progress state, journals replay, skips
already integrated commits, and verifies integration before removal and local
branch deletion. It preserves task metadata on conflicts or cleanup failures.
Relay conflicting paths and recovery instructions. Once resolved and staged,
rerun the same finish command; do not remove a partially integrated worktree.
It leaves remote branches untouched. After success, refresh the destination
context and relay the verified removal/deletion result; do not ask for
separate local-branch deletion permission. Legacy worktrees without task
records have no immutable ID for preview teardown; the wrapper refuses them,
so finish those with faur directly only when they have no previews:

```sh
faur worktree finish <name> --dry-run
faur worktree finish <name>
```

## Other explicitly requested operations

Use faur commands for management: `worktrees --json` for inventory display,
`worktree remove <name> --dry-run`, `prune --dry-run`, `rename <old> <new> --dry-run`,
`sync-shared --dry-run`, and `health`. Prefer supported dry-run checks for
unclear/destructive effects. Never add --force without explicit authorization.
