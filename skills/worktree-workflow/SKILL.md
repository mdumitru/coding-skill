---
name: worktree-workflow
description: Use whenever a task will modify files in a faur-git workspace (`.bare/` plus sibling worktrees), when managing those worktrees, or when the user later sends exact `finish` or `finish in` authorization naming a branch. Resolve state with `faur worktrees --json` and delegate creation, integration, and removal to `faur` commands.
---

# Worktree Workflow

Follow `worktree-workflow.md` for the deterministic lifecycle. In short:

- Resolve the launch record and collisions with `faur worktrees --json`, then
  create a new task using `faur worktree add <slug> --base <launch-branch>`.
- Keep task plans in workspace-level `_plans/`; the `task-workflow` skill owns
  their path and validation rules.
- Commit and verify in the task worktree, then stop. Do not push, open a PR,
  integrate, or remove it without the corresponding explicit request.
- Only a later exact `finish` or `finish in <branch>` authorizes running
  `faur worktree finish`: first with `--dry-run`, then for real. That command
  performs and verifies integration, removes the worktree, and deletes the
  verified local task branch while leaving remote branches untouched.
