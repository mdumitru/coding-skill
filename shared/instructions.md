# User Instructions

Baseline working conventions (succinct replies, robust Python, commit style)
are in the always-on `agent-baseline` skill. The exact `plan:` and `execute:`
keywords (including their trailing colons) trigger the `plan-workflow` and
`execute-workflow` skills. Source repo: `~/gits/coding-skill`.

## Worktrees

I always work from the integration worktree of a `faur-git` workspace
(`.bare/` + `_shared/` + `main/`, managed by the `faur` CLI). `main/` and
`develop/` are for reading and integrating, not for doing work.

Before modifying any tracked file, create a worktree for the task and work
there: `faur worktree add <slug>` from `main/`, then use
`<workspace>/<slug>/` for every edit, command, and commit. One task, one
worktree, however small the change. Read the `worktree-workflow` skill for the
naming rules, the `plan:`/`execute:` interaction, and the other `faur`
subcommands before running any of them.

Exceptions: read-only tasks (including `plan:`), repos that are not faur
workspaces, when I am already inside a task worktree, and when I say otherwise.
Never push, merge, or remove a worktree unless I ask.
