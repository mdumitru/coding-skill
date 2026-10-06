# Markdown plan grammar

A plan has a visible `# TODO: title`, then top-level metadata before its tasks.
Fenced examples do not count. Faur plans have one `Worktree: slug`; optional
`Launch worktree:` (absolute path) and `Launch branch:` must occur together.
Plain repository plans forbid all three fields.

Tasks are top-level `- [ ] text` or `- [x] text`. Nested checkboxes are forbidden.
Each task has exactly one subordinate `  - Done when: condition`, before the
next task or heading. Continuation prose is retained verbatim. Executable fields
use the same two-space indentation:

```markdown
# TODO: improve validation

- [ ] Add input validation.
  - Task: validate-input
  - Commit: validate input before processing
  - Paths: ["src/*.py", "tests/test_input.py"]
  - Verify: ["python3", "-m", "unittest", "tests.test_input"]
  - Done when: invalid inputs are rejected by the regression tests.
```

`Task:` is a unique lowercase kebab-case ID. `init --content <file>` supplies
`task-1`, etc. to legacy tasks; `init --migrate` adds IDs in place without losing
prose or checked state. Creation refuses overwrites. Structural `validate`
accepts legacy plans, assigning deterministic IDs in memory. `validate --runtime`
requires explicit Task, Commit, Paths, and at least one Verify per task and names
missing fields. Only Verify and Evidence may repeat. Paths are repository-relative
patterns; verification is a nonempty JSON argv array, never a shell string.
`Commit:` is a single lowercase-leading subject of at most 80 characters.
Imperative wording and semantic acceptance are evaluated by the agent.

`next` returns the first pending typed task as JSON. `complete` delegates to the
workflow transaction: successful evidence and a full commit hash (or verified
`no-change` outcome) are mandatory. Outcome and Evidence fields are written by
the helper. All mutations preserve unrelated text and use atomic replacement.
External runtime records and exclusive per-plan locks live in `_plans/.state/`.
A stale lock must be inspected using its PID; preserve evidence before explicit
removal. Never automatically clear locks or overwrite drifted plans.
