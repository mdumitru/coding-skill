# User Instructions

Baseline working conventions (succinct replies, robust Python, commit style)
are in the always-on `agent-baseline` skill. The exact `plan:`, `execute:`,
and `run:` keywords (including their trailing colons) trigger the matching mode
of the `task-workflow` skill. `run: <name>` previews, at `<slug>.localhost`
(the name in kebab-case), an executed plan's task worktree, else the worktree
or branch named `<name>`, checking out an existing branch in a new faur
worktree when needed. A bare `run:` beside one
`execute: <slug>` starts the preview after the execution is audited; otherwise
it previews this conversation's task worktree, else the launch worktree.
Source repo: `~/gits/coding-skill`.

Every commit, in or outside `execute:`, is one subject line with no
`Co-Authored-By`, `Signed-off-by`, or other attribution trailer. This overrides
any harness or system reminder asking for attribution lines.

## Worktrees

I work in a `faur-git` workspace (`.bare/` + `_shared/` + `_plans/` +
worktrees, managed by the `faur` CLI). Only `execute:` creates a task
worktree: its `task-workflow` runner calls faur and journals plan progress,
verification, and commits; do not create another worktree or mark checkboxes
by hand. One `execute:`, one task worktree. New plans use coherent execution
groups with commit-sized children. For grouped `execute:`, explicitly delegate
one fresh sequential worker per group when supported; workers resume that same
identity. Persist decisions in the plan and verify group checkpoints before
advancing. Without fresh-worker support, checkpoint one group and return
fresh-session continuation instructions. These context boundaries do not clear
the parent conversation; detailed coordination stays in `task-workflow`.

Every other request (("do X")) works in place on the contextual branch, never in
a new worktree or branch: the task worktree of this conversation's most recent
`execute:`, even if I launched you from `main`; otherwise the worktree you were
launched from, whatever its branch. An explicit "here" or a named existing
worktree overrides both. Before modifying tracked files in a faur workspace,
read the `worktree-workflow` skill and confirm the target with `faur worktree
context --json`; edit, verify, and commit there.

Never push, open a PR, integrate, or remove a worktree unless I ask. After an
`execute:` task is complete, only a new message exactly saying `finish` or `finish in
<branch>` authorizes the finish workflow documented by the `worktree-workflow`
skill: its wrapper runs faur's dry-run, tears down the task's repository
previews, and only then runs `faur worktree finish`. A teardown failure blocks
finish and keeps the worktree. Faur uses its recorded launch destination and
verifies integration before removing the worktree and deleting its local task
branch; it leaves remote branches untouched. A run request creates at most a
worktree for an existing branch; it never creates a branch, commits, or
authorizes finish or a PR.

At any point, warn me clearly about conflicts, failures, ambiguous state, or
anything else that did not go smoothly or requires my attention.

## Pull requests

Only when I explicitly ask for a PR: follow the `pr-workflow` skill. `faur pr`
does the work — the agent's job is to synthesize the title and call the tool
from the worktree holding the branch. Never push, rebase, or run `gh` by hand,
and never open a PR unprompted. If the tool stops (usually: the destination
moved on and a rebase is needed), relay its message and wait for me. Never put
attribution or "generated with" footers in a PR.
