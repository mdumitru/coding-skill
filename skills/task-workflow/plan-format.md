# Markdown plan grammar

A plan has a visible `# TODO: title`, then top-level metadata before its tasks.
Fenced examples do not count. Faur plans have one `Worktree: slug`; optional
`Launch worktree:` (absolute path) and `Launch branch:` must occur together.
Plain repository plans forbid all three fields.

Legacy flat tasks are top-level `- [ ] text` or `- [x] text`. Flat plans retain their existing behavior. New plans use execution groups below.
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

Approved changes to pending-task details or decision prose use
`workflow.py revise <slug> --content <scratch-file>` while the plan matches its
recorded state. Revision preserves identity, task IDs/order, checked state, and
completed-task records. It refuses a completion transaction in progress and
journals its own replacement. Retry `revise <slug>` without --content to finish
an interrupted approved revision; it never adopts unrelated drift.

# Execution groups (opt-in grammar)

A grouped plan uses only top-level group checkboxes with an explicit `Group:`
ID. Each contains one or more child commit tasks indented two spaces; child
metadata is indented four spaces. Group metadata precedes its children. Do not
mix groups and flat tasks or nest deeper. Group and child IDs share a unique
lowercase kebab-case namespace. Headings terminate metadata ownership; fenced
examples and continuation prose are preserved verbatim.

```markdown
# TODO: improve input handling

## Context and decisions

Reject invalid input before processing it.

- [ ] Integrate validation.
  - Group: validation
  - Verify: ["python3", "-m", "unittest", "tests.test_input"]
  - Done when: request handling rejects invalid input.
  - [ ] Add validation rules.
    - Task: validation-rules
    - Commit: add validation rules
    - Paths: ["src/validation.py", "tests/test_input.py"]
    - Verify: ["python3", "-m", "unittest", "tests.test_input"]
    - Done when: valid and invalid input pass the acceptance checks.
```

A group requires one Group ID, one Done when condition, and at least one Verify
argv array. It has no Commit, Paths, Task, or Outcome. Child tasks use the same
executable fields as flat tasks. `init --migrate` adds only missing child IDs,
preserving explicit IDs, indentation, group membership, and completion evidence.
`validate` reports separate task/group counts; `next` returns a child with its
`group_id`, source bounds, and indentation. Ordered children remain available to
existing commit/evidence consumers. Identity replacement leaves groups intact.

Children carry commit Outcome/Evidence; groups carry independent Evidence.
Completion must form an ordered prefix. A checked group requires every child
complete and successful matching group Evidence. A pending group may contain
completed children (including all its children), but cannot carry group Evidence.
No child can complete beyond the first uncheckpointed group. Pending children
cannot carry completion records. Group checkpoints do not create commits.

Flat executable plans and schema-version-1 journals remain supported. Grouping
is explicit and must never be inferred or introduced into an in-progress flat
execution. Older readers reject the Group metadata/nesting instead of treating
a group as a commit task. Use the updated package for grouped plans.

# Group assignment and checkpoint protocol

Grouped journals opt into `group_protocol: 1` within execution schema 1. Old flat
journals omit these optional fields and retain flat behavior. A grouped journal
cannot be adopted from checked Markdown alone: retain its original checkpoint
state. Revisions preserve group IDs/order/membership, checked state, completed
content, child records, and group Evidence. Never implicitly regroup a flat run.

From the recorded task worktree, the coordinator calls:

```sh
python3 <skill-dir>/scripts/workflow.py group-next <slug>
python3 <skill-dir>/scripts/workflow.py group-begin <slug> --group <group-id>
```

`group-next` validates the completed child prefix and group checkpoints and
returns the next group plus any active assignment. `group-begin` requires clean
matching identity and returns a durable unique assignment token. Repeating it
for the same assigned group retains that token. Do not replace an interrupted
assignment or hold a filesystem lock throughout model execution. At most one
worker may edit; resume the recorded assignment after a stopped worker.

A worker supplies both scope arguments on every child operation:

```sh
python3 <skill-dir>/scripts/workflow.py begin <slug> --group <group-id> --assignment <token>
python3 <skill-dir>/scripts/workflow.py complete <slug> --group <group-id> --assignment <token>
```

Missing/wrong/stale scope and advancement beyond the assigned group are refused.
Child path, verification, staging, commit, evidence, and recovery checks still
apply. A child interruption retries that same scoped complete operation.

After the worker has returned and every child is journaled, the coordinator
judges group Done when and closes the assignment:

```sh
python3 <skill-dir>/scripts/workflow.py group-release <slug> --group <group-id> --assignment <token>
python3 <skill-dir>/scripts/workflow.py group-complete <slug> --group <group-id> --assignment <token> --accepted
```

Release requires all children complete and no active child transaction. Released
assignments refuse further child operations. `--accepted` records the agent's
semantic judgment; the runner independently verifies the child record prefix,
contiguous commit ancestry, clean identity, and group Verify argv without a shell.
It journals checkpoint intent/evidence before atomically replacing Markdown,
then records the group checkpoint before exposing the next group. This creates
no extra commit. Worker prose/process success never replaces this checkpoint.

Failed verification, mutations, drift, and interrupted writes retain the exact
assignment, evidence, pending digest, and error. Retry the same group-complete;
checkpoint write recovery reuses successful journaled evidence and validates HEAD
and Markdown. Never clear locks automatically, reset state, skip a failed group,
or start a competing worker. Final audit requires every child and group record;
report includes pending groups, active assignment, checkpoints, and plan drift.

# Persisted worker context

Every new grouped plan has exactly one `## Context and decisions` section
before its first group. Put the objective, settled constraints, cross-group
interfaces, and approved handoff decisions there. It ends at the next level-1
or level-2 heading (or first group); lower-level subheadings and fenced content
remain part of the section. Other introductory sections are not implicitly
included. Record later approved decisions through `workflow.py revise` in this
section, never only in a chat transcript.

The read-only helper emits JSON for an assigned worker:

```sh
python3 <skill-dir>/scripts/group_context.py <slug> --group <group-id> --assignment <token>
```

It returns validated canonical plan/journal/worktree/helper paths, immutable
identity, the active assignment, shared decisions, assigned group prose and
acceptance/Verify, only remaining child task bodies and executable fields, and
compact previous task/group completion facts. It omits old verification logs,
unrelated task bodies, and transcripts. The plan/journal paths locate the full
evidence. It creates no worktree and writes no plan, journal, or lock.

`--previous-limit` bounds previous facts (default 12, range 0..100 for each
kind); explicit omitted counts and `limits.truncated` report any omission.
`--max-chars` defaults to 60000; oversized handoffs fail without silently cutting
acceptance, decisions, or task metadata. Raise the limit or revise group size.
Output is deterministic across fresh processes. Missing identity, wrong/stale
assignments, plan drift, active completion/checkpoint transactions, locks, dirty
worktrees, or changes while reading are refused with recovery diagnostics.
A clean, begun child may resume with the same assignment and active task ID.

Worker returns must be concise: group/task IDs, outcomes, checkpoint status,
and approved handoff decisions; blockers name the exact pending operation and
artifact paths. The coordinator verifies the runner's records rather than
trusting prose. A completed-child/pending-group handoff can contain no remaining
children; acceptance and the coordinator checkpoint still remain necessary.
