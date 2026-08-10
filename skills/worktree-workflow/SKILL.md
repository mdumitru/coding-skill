---
name: worktree-workflow
description: Use whenever a task will modify files in a faur-git workspace (a repo laid out as .bare/ + main/ + _shared/, managed by the `faur` CLI). Each task gets its own worktree created with `faur worktree add`; the user stays in main/ or develop/ and never works there. Also use when asked to list, rename, prune, or remove worktrees. Applies to `execute:` runs and to direct "implement X" requests alike.
---

# Worktree Workflow

Follow `worktree-workflow.md` in this skill directory for the full details.

In short:
- The user is always in `main/` (or `develop/`). Never modify files there.
- Before changing anything, create a task worktree: `faur worktree add <slug>`.
  Then do all work, all commits, and all test runs inside that worktree.
- `plan:` writes its TODO file in `main/` and creates no worktree. `execute:`
  creates the worktree, then works there while ticking off the TODO that stays
  in `main/`.
- Commit per item, then stop and report the worktree path and branch. Never
  push, merge, or remove a worktree unless explicitly asked.
