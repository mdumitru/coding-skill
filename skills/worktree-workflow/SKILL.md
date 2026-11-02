---
name: worktree-workflow
description: Use whenever a task will modify files in a faur-git workspace (`.bare/` plus sibling worktrees), when managing those worktrees, or when the user later sends exact `finish` or `finish in` authorization naming a branch. Ordinary requests work in place on the contextual branch (the latest `execute:` task worktree, else the launch worktree); only `execute:` creates task worktrees. Resolve state with versioned `faur worktree context --json`, delegate integration and removal to `faur` commands, and run finish through the bundled wrapper that tears down the task's repository previews first.
---

# Worktree Workflow

Follow `worktree-workflow.md` for the deterministic lifecycle. In short:

- Ordinary requests never create a worktree or branch. Work in place on the
  contextual branch: this conversation's latest `execute:` task worktree, else
  the launch worktree, unless the user names "here" or another worktree.
  Confirm it with `faur worktree context --json` before editing.
- Only `execute:` creates a task worktree, through the task-workflow runner.
- Keep task plans in workspace-level `_plans/`; the `task-workflow` skill owns
  their path and validation rules.
- Commit and verify in the contextual worktree, then stop. Do not push, open a PR,
  integrate, or remove it without the corresponding explicit request.
- Only a later exact `finish` or `finish in <branch>` authorizes finish, run
  through `scripts/finish.py`: faur context, faur dry-run, teardown of the
  task's own repository previews, then the real `faur worktree finish`. Faur
  performs and verifies integration, removes the worktree, and deletes the
  verified local task branch while leaving remote branches untouched. A
  preview teardown failure blocks the real finish.
