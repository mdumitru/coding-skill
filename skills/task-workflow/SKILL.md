---
name: task-workflow
description: Use only when the user's message contains the exact keyword `plan:` or `execute:`, including the trailing colon. Create or execute a validated shared task plan; do not trigger for ordinary planning or implementation requests without either exact keyword.
---

# Task Workflow

This skill gives `plan:` and `execute:` one deterministic plan location and
format. Invoke `scripts/plan_file.py` by its absolute path resolved from this
skill directory; keep using that same script after changing working directory.

Read exactly one mode reference completely before acting:

- For `plan:`, read `plan-workflow.md`.
- For `execute:`, read `execute-workflow.md`.

Plan arguments are always lowercase kebab-case slugs without an `.md` suffix.
The helper maps a slug to `<plan-root>/_plans/<slug>.md`; never accept a path or
search worktrees for another copy. It determines whether the Git context is a
plain repository or a faur-git workspace and rejects path or symlink escapes.
For example, `plan: improve-routing` and `execute: improve-routing` both refer
to `_plans/improve-routing.md`.
