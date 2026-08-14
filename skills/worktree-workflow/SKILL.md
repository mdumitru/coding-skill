---
name: worktree-workflow
description: Use whenever a task will modify files in a faur-git workspace (a repo laid out as .bare/ + worktrees + _shared/, managed by the `faur` CLI), when managing its worktrees, or when the user sends `finish` or `finish in` followed by a branch after a task. Always create a new task worktree regardless of which worktree the agent starts in, unless the user explicitly says to work here or in a named existing worktree. On an explicit later finish request, cherry-pick its commits into the requested integration worktree and remove it.
---

# Worktree Workflow

Follow `worktree-workflow.md` in this skill directory for the full details.

In short:
- The user may launch the agent from any worktree. Record it and never do task
  work there merely because it is current.
- Before changing anything, create a task worktree: `faur worktree add <slug>`.
  Then do all work, all commits, and all test runs inside that worktree.
- Only an explicit instruction to work "here" or in a named existing worktree
  permits skipping creation. Starting in a task worktree does not.
- `plan:` writes its TODO file in `main/` and creates no worktree. `execute:`
  creates the worktree, then works there while ticking off the TODO that stays
  in `main/`.
- Commit per item, then stop and report the worktree path and branch. Do not
  integrate or remove it until the user sends a new message explicitly saying
  `finish` or `finish in <branch>`.
- On `finish`, cherry-pick the task commits in order into the launch worktree;
  on `finish in <branch>`, use that branch's existing worktree. Remove the task
  worktree only after every cherry-pick succeeds, then ask whether to delete
  its branch.
