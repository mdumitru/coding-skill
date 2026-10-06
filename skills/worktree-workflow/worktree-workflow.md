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
Use the task name returned by creation, validate its context, and run:

```sh
faur worktree context <task-name> --json
faur worktree finish <task-name> --dry-run
faur worktree finish <task-name>
```

Run the real finish only after successful preflight. For `finish in <branch>`,
append `--branch <branch>` to both finish commands. Faur resolves the recorded
launch destination, even in a new process or from another worktree. Do not
remember a launch path, switch branches, resolve destination inventory entries,
compute commit ranges, cherry-pick, rebase, or remove branches manually.

Faur refuses dirty/ambiguous/stale/in-progress state, journals replay, skips
already integrated commits, and verifies integration before removal and local
branch deletion. It preserves task metadata on conflicts or cleanup failures.
Relay conflicting paths and recovery instructions. Once resolved and staged,
rerun the same finish command; do not remove a partially integrated worktree.
It leaves remote branches untouched. After success, refresh the destination
context and relay the tool's verified removal/deletion result; do not ask for
separate local-branch deletion permission. Legacy worktrees without task records
retain faur's invocation-branch finish default; do not pretend they have a stored
launch destination.

## Other explicitly requested operations

Use faur commands for management: `worktrees --json` for inventory display,
`worktree remove <name> --dry-run`, `prune --dry-run`, `rename <old> <new> --dry-run`,
`sync-shared --dry-run`, and `health`. Prefer supported dry-run checks for
unclear/destructive effects. Never add --force without explicit authorization.
