# User Instructions

Baseline working conventions (succinct replies, robust Python, commit style)
are in the always-on `agent-baseline` skill. The exact `plan:` and `execute:`
keywords (including their trailing colons) trigger the matching mode of the
`task-workflow` skill. Source repo: `~/gits/coding-skill`.

## Worktrees

I work in a `faur-git` workspace (`.bare/` + `_shared/` + `_plans/` +
worktrees, managed by the `faur` CLI). The agent may be launched from any
worktree, including an existing task worktree. Its location never authorizes
doing a new task there: the launch worktree is for reading and later
integration only.

Before modifying any tracked file, use `faur worktrees --json` to resolve the
launch path, branch, and slug collisions, then create a worktree with `faur
worktree add <slug> --base <launch-branch>`. Use the returned task path for
every edit, command, and commit. One task, one worktree, however small the
change. Read the `worktree-workflow` skill for the lifecycle and the
`task-workflow` skill for `plan:`/`execute:` behavior.

Exceptions: read-only tasks (including `plan:`), repos that are not faur
workspaces, and an explicit instruction to work "here" or in a named existing
worktree. Being launched from a non-main or task worktree is not an exception.
Never push, open a PR, integrate, or remove a worktree unless I ask. After a
task is complete, only a new message exactly saying `finish` or `finish in
<branch>` authorizes the `faur worktree finish` workflow documented by the
`worktree-workflow` skill. That command verifies integration before removing
the worktree and deleting its local task branch; it leaves remote branches
untouched.

At any point, warn me clearly about conflicts, failures, ambiguous state, or
anything else that did not go smoothly or requires my attention.

## Pull requests

Only when I explicitly ask for a PR: follow the `pr-workflow` skill. `faur pr`
does the work — the agent's job is to synthesize the title and call the tool
from the worktree holding the branch. Never push, rebase, or run `gh` by hand,
and never open a PR unprompted. If the tool stops (usually: the destination
moved on and a rebase is needed), relay its message and wait for me. Never put
attribution or "generated with" footers in a PR.
