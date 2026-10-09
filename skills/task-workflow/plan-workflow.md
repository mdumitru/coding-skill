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
a `# TODO:` title, one `## Context and decisions` section, one top-level
Worktree slug for faur layouts, and ordered top-level unchecked groups. Do not
record launch identity yourself; startup consumes faur's returned identity.
Group changes that share investigation, files, dependencies, or decisions into
coherent execution units. Avoid arbitrary tiny groups and one oversized group.
Each group must stand on its persisted context without the planning conversation:
record its purpose, relevant interfaces/findings, a stable Group ID, meaningful
Done when acceptance, and at least one integration Verify argv. Group metadata
uses two spaces and precedes the children. Choose integration checks that prove
the children work together; group checkpoints do not create commits.

Inside each group, indent independently verified commit tasks two spaces and
their metadata four spaces. Every child needs a stable Task ID, imperative Commit
subject, explicit JSON Paths patterns, one or more nonempty JSON Verify argv,
and a subordinate Done when condition. Context boundaries and commit boundaries
are independent. Keep the objective, settled decisions, cross-group constraints,
and durable handoffs in `## Context and decisions`. Workers read relevant files
themselves; do not rely on chat history or include investigation/test transcripts.
Use the exact grouped example in `plan-format.md`. Never include TODO/plan
artifacts in commit paths. Flat executable plans remain supported; do not
silently regroup an existing or in-progress plan.

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

Recommend starting `execute: <slug>` in a fresh conversation after a large
planning session. Native workers isolate each group's detailed work; they do
not clear the parent session or guarantee subscription savings. If fresh native
workers are unavailable, execution checkpoints one group and returns explicit
fresh-session continuation instructions (see `group-execution.md`).
