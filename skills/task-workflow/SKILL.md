---
name: task-workflow
description: Use only when the user's message contains the exact keyword `plan:`, `execute:`, or `run:`, including the trailing colon. Create or execute a validated shared task plan, or start/stop the repository preview of a plan's task worktree, another worktree, or a branch; do not trigger for ordinary planning, implementation, or run requests without one of these exact keywords.
---

# Task Workflow

This skill gives `plan:`, `execute:`, and `run:` one deterministic plan
location and format. Read `plan-format.md` for the typed grammar and recovery. Invoke
`scripts/workflow.py` for execution and `scripts/plan_file.py` by its absolute path resolved from this
skill directory; keep using that same script after changing working directory.

Read the matching mode reference completely before acting:

- For `plan:`, read `plan-workflow.md`.
- For `execute:`, read `execute-workflow.md`; grouped plans also require
  `group-execution.md`. This skill explicitly requests one fresh sequential
  worker per group when supported, using the recorded execution worktree.
- For `run:`, alone or combined with `execute:`, read `run-workflow.md` (and
  `execute-workflow.md` for a combined request).

Plan arguments are always lowercase kebab-case slugs without an `.md` suffix.
The helper maps a slug to `<plan-root>/_plans/<slug>.md`; never accept a path or
search worktrees for another copy. It determines whether the Git context is a
plain repository or a faur-git workspace and rejects path or symlink escapes.
For example, `plan: improve-routing` and `execute: improve-routing` both refer
to `_plans/improve-routing.md`.
`run: improve-routing` previews that plan's recorded task worktree at
`improve-routing.localhost`; `run:` also accepts a worktree or branch name when
no executed plan has that name (see `run-workflow.md`).
