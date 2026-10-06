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

# Startup and selection

`workflow.py start <slug>` requires an executable plan. Plain repositories work
in place. Faur startup probes `context --json` and a strict task preview before
creation, then calls `add <slug> --task --unique --json`. Version 1 envelopes are
required. Supply `--faur-argv '["/path/to/faur-worktree"]'` to use a companion CLI
without installation; this argv prefix is recorded and must match on retries.
The final JSON includes the returned canonical identity and immutable faur ID.
Run subsequent commands from that task path.

`--existing <name>` is explicit authorization to reuse a named worktree; launch
location alone never implies reuse. Faur validates the requested context.
`begin` selects the first pending task, requires the recorded clean branch/path,
and journals its start HEAD. Repeating the same begin is safe.

Startup journals its boundary before creation and persists the response before
changing Markdown. A retry after response persistence reuses its immutable ID.
If a process dies before the response is durable, startup refuses another add.
Inspect faur's recorded context; `--recover-task-id <id>` explicitly identifies
the interrupted task and verifies its requested slug and launch identity. If
concurrent creation changed the preview name, preserve the journal and inspect
it rather than guessing. Plan drift, stale/corrupt context, wrong branches, and
old faur versions stop with diagnostics; state is retained for inspection.

# Completion, recovery, audit, and reporting

`workflow.py complete <slug>` checks every changed/staged path, refuses partial
staging, and runs each Verify array from the recorded worktree without a shell.
The agent must first judge the Done when condition semantically. Verification
records include argv, exit status, bounded output, and a full-output digest.
Unexpected verification mutations stop completion and remain available for
review. Only allowed changes are staged. Commit intent records start HEAD and
expected tree before Git runs; retries reconcile the one matching commit.
Failed commits retain staging and diagnostics. No-change tasks need successful
verification and record `no-change`, keeping HEAD intact.

Both the commit and Markdown replacement are journaled. Re-run `complete` after
an interrupted transaction; it verifies identity, tree, subject, and ancestry
before finalizing. Drift is refused, never repaired by discarding user edits.
Do not reset state to bypass a failure. Locks and journal files are evidence.
Checked legacy tasks can be adopted at startup only with full commit Outcome
and Evidence matching the executable Verify commands; the helper verifies
commit subject, paths, and ancestry. Bare checkboxes are insufficient.

`audit` requires ordered evidence for every task, matching Markdown records,
a contiguous commit chain ending at HEAD, no active transaction, and a clean
recorded worktree. `report` emits JSON facts with actual branch/path, refreshed
identity, commits, pending tasks, and dirty state; `--markdown` is concise.
Neither command integrates or removes worktrees. Live skill installation and
publishing remain separate explicitly requested actions.
