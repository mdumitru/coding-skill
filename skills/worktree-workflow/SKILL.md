---
name: worktree-workflow
description: Use whenever a task will modify files in a faur-git workspace (a repo laid out as .bare/ + main/ + _shared/, managed by the `faur` CLI), when managing its worktrees, or when the user sends `finish` or `finish in` followed by a branch after a task. Create one task worktree per change, then on an explicit later finish request cherry-pick its commits into the requested integration worktree and remove it.
---

# Worktree Workflow

Follow `worktree-workflow.md` in this skill directory for the full details.

In short:
- The user launches the agent from an integration worktree, usually `main/` or
  `develop/` but not necessarily. Record it and never do task work there.
- Before changing anything, create a task worktree: `faur worktree add <slug>`.
  Then do all work, all commits, and all test runs inside that worktree.
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
