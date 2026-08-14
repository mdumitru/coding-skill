# User Instructions

Baseline working conventions (succinct replies, robust Python, commit style)
are in the always-on `agent-baseline` skill. The exact `plan:` and `execute:`
keywords (including their trailing colons) trigger the `plan-workflow` and
`execute-workflow` skills. Source repo: `~/gits/coding-skill`.

## Worktrees

I work in a `faur-git` workspace (`.bare/` + `_shared/` + worktrees, managed
by the `faur` CLI). The agent may be launched from any worktree, including an
existing task worktree. Its location never authorizes doing a new task there:
the launch worktree is for reading and later integration only.

Before modifying any tracked file, create a worktree for the task and work
there: `faur worktree add <slug>` from the launch worktree, then use
`<workspace>/<slug>/` for every edit, command, and commit. One task, one
worktree, however small the change. Read the `worktree-workflow` skill for the
naming rules, the `plan:`/`execute:` interaction, and the other `faur`
subcommands before running any of them.

Exceptions: read-only tasks (including `plan:`), repos that are not faur
workspaces, and an explicit instruction to work "here" or in a named existing
worktree. Being launched from a non-main or task worktree is not an exception.
Never push, merge, or remove a worktree unless I ask. After a task is complete,
only a new message explicitly saying `finish` or `finish in <branch>` authorizes
the integration-and-removal workflow documented by the `worktree-workflow`
skill. Afterward, ask before deleting the task branch.

At any point, warn me clearly about conflicts, failures, ambiguous state, or
anything else that did not go smoothly or requires my attention.

## Pull requests

Only when I explicitly ask for a PR: follow the `pr-workflow` skill — fetch the
base branch, rebase if the branch is behind, stop and warn me on conflicts,
push, then open the PR with `gh`. Never push or open a PR unprompted, and never
put attribution or "generated with" footers in a PR.
