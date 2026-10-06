# `plan:` Mode

Planning inspects the target repository and creates only an external readable
plan. It must not change tracked files, create a worktree, or commit. Read
`plan-format.md`; investigate enough for another agent to execute the content.

Choose the user's lowercase kebab-case slug or a short descriptive one. Resolve
the canonical location from the target repository:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py resolve <slug> --create-parent --json
```

Prepare UTF-8 Markdown content in a scratch file outside the repository. Include
a `# TODO:` title, relevant context and settled decisions, one top-level
Worktree slug for faur layouts, and ordered top-level unchecked tasks. Do not
record launch identity yourself; startup consumes faur's returned identity.
Every task needs a stable Task ID, imperative Commit subject, explicit JSON
Paths patterns, one or more nonempty JSON Verify argv arrays, and a subordinate
Done when condition. All executable metadata uses exactly two-space bullet
indentation. Keep task details and acceptance meaningful; helper validation
checks the mechanical format. Never include TODO/plan artifacts in commit paths.

Create and validate without overwriting existing plans:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py init <slug> --content <scratch-file>
python3 <task-workflow-dir>/scripts/plan_file.py validate <slug> --runtime
```

Init supplies IDs for legacy content lacking them, preserves prose, and refuses
an existing destination. Report the canonical path returned by the helper.
Structural validate remains available for old readable plans, but execute
requires the documented executable metadata. Planning never installs tools or
skills, pushes, opens a PR, or integrates/removes a worktree.
