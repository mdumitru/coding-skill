# Worktree Workflow

Faur owns canonical context, collision selection, creation, durable launch/task
identity, and integration/removal. Consume its version 1 JSON object responses;
never parse the inventory array to choose a worktree or reconstruct paths.

## Choose the contextual worktree

Only `execute:` creates a task worktree; the task-workflow skill's
start/begin/complete runner owns those faur calls and its external plan/state
mutations. Do not create a second worktree before calling start. Keep one task
worktree per `execute:`, including when the agent was launched from another
existing task worktree.

Every other request modifies files in place on the contextual branch and never
creates a worktree or branch. Resolve it in this order:

1. a worktree the user explicitly names, or "here" (the launch worktree);
2. the recorded task worktree of this conversation's most recent `execute:`,
   even when the agent was launched from `main`;
3. the launch worktree, whatever branch it has checked out.

Before the first tracked-file mutation, validate the target:

```sh
faur worktree context --json          # launch worktree
faur worktree context <name> --json   # named or execute task worktree
```

Use the returned canonical path for every later command, edit, test, and
commit. If context fails, or the target is detached, mid-rebase/merge, or
otherwise ambiguous, stop and warn instead of choosing another location. Relay
failures and preserve partial state. Old tools lacking the versioned contract
must be updated separately; do not silently install a release during this task.

Read-only work needs no validation. Plain Git repositories work in place.
Leave shared mutable `_shared/` symlinks alone unless the request concerns
them. Scratch data stays outside the workspace; task plans stay in the
external canonical `_plans/` directory.

## Complete without integration

Commit and verify in the contextual worktree. Refresh its context and report
actual path, branch, clean/dirty state, and commits. For execute runs, use the
runner's audit/report; ordinary changes use faur context and Git read-only checks.
Stop. Never push, open a PR, integrate, remove a worktree, or delete branches
without the corresponding explicit request. A PR invokes the pr-workflow skill.

## Exact finish authorization

Only a later message exactly `finish` or `finish in <branch>` authorizes finish
of an `execute:` task worktree. It also authorizes tearing down the task's
repository previews. Use the task name recorded by execute and run the bundled wrapper, resolved by absolute
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
