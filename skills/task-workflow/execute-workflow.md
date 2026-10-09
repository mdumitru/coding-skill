# `execute:` Mode

The text after the exact keyword is a lowercase kebab-case plan slug, without
an extension or directory. Read `plan-format.md` for grammar and recovery.
Resolve helpers by absolute path within this skill, retaining that path after
changing directories. Never search for a fallback plan or derive task paths.

## Start or resume

From the target repository, run:

```sh
python3 <task-workflow-dir>/scripts/workflow.py start <slug>
```

The runner validates executable metadata, locks the canonical external plan,
and delegates faur identity and strict unique task creation to faur. It updates
Worktree/Launch fields atomically using faur's response. Plain repositories work
in place. Use the returned identity path for all subsequent work. For an
existing run, call start from its recorded task path; interrupted startup may
require its recorded origin as described in the diagnostic.

An old faur version is a blocker: relay the diagnostic. When explicitly using
a companion project, supply `--faur-argv` as a JSON executable argv prefix;
it is recorded for all later calls. Use `--existing <name>` only when the user
explicitly authorized that existing worktree, or when adopting this same task's
already-created worktree during an authorized workflow migration. Never infer
reuse from launch location. Legacy plans can be migrated with
`scripts/plan_file.py init <slug> --migrate`; add the missing executable content
before startup and validate with `--runtime`. Checked legacy tasks require
real commit Outcomes and matching verification Evidence for adoption.

## Grouped execution

For plans with Group IDs, read `group-execution.md` completely. This skill
explicitly requests sequential delegation: automatically create one new worker
with no conversation-history inheritance for each coherent group when the
harness supports it. Pass scoped persisted context and required rules, use the
one recorded worktree, wait for worker return, then verify and journal its group
checkpoint before advancing. Keep investigation, diffs, and test logs in the
worker/artifacts. Never reuse a completed group's worker for the next group.
The coordinator owns final audit/report and preview intent. A failed or ambiguous
operation stops advancement and preserves the exact assignment and journal.

If the harness cannot create a confirmed fresh native worker, follow the
one-group/manual fresh-session fallback in that reference. Never silently run
all remaining groups in the current conversation or claim automatic isolation.
For an existing assignment, resume only after confirming its previous worker
has stopped; use the same group/token and recover pending transactions first.

## Flat execution: one task at a time

```sh
python3 <task-workflow-dir>/scripts/workflow.py begin <slug>
python3 <task-workflow-dir>/scripts/plan_file.py next <slug> --runtime
```

Begin chooses the next task in order, checks the clean recorded identity, and
journals start HEAD. Implement that task within its allowed Paths. Evaluate
its Done when condition semantically. If materially ambiguous, stop and ask;
preserve the journal, put the approved decision in scratch Markdown, and call
`workflow.py revise <slug> --content <scratch-file>` before resuming. Revisions
preserve stable IDs/order, identity, checked state, and completed evidence.
Do not bypass drift checks or discard diagnostic state.

After semantic acceptance, run:

```sh
python3 <task-workflow-dir>/scripts/workflow.py complete <slug>
```

The helper checks paths/message policy, executes Verify argv arrays without a
shell, detects verification mutations and partial staging, stages only allowed
changes, commits once, and writes progress with evidence. It handles verified
no-change outcomes. Do not pre-mark checkboxes, stage the plan, or manually
commit runner-managed tasks. On a failure, preserve pending state and relay
its diagnostic. Retry complete to reconcile a journaled matching commit after
an interruption; never reset state to manufacture a clean run.

## Combined `run:` requests

When the same request also contains `run:` (bare or naming this slug), read
`run-workflow.md`, record the preview intent with `preview.py intent
--execute-slug <slug>` before start, and keep it through retries. Only after
the final audit below passes, start the preview with `preview.py run
--execute-slug <slug>` and report its URLs. A conflicting run slug or several
execute slugs stop the request before any work.

## Final checks and report

After all tasks and (for grouped plans) every verified group checkpoint, run:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py validate <slug> --runtime
python3 <task-workflow-dir>/scripts/workflow.py audit <slug>
python3 <task-workflow-dir>/scripts/workflow.py report <slug> --markdown
```

Run any additional final repository checks required by the plan. Audit requires
successful ordered evidence, intact commit ancestry, clean matching identity,
every required group checkpoint, and no unresolved transaction. Report the canonical path, branch, and actual
commits from recorded facts. Stop without pushing, integrating, or removing
the task. Only a later exact `finish` or `finish in <branch>` authorizes finish;
a PR still requires a separate explicit request. Do not install into live
harnesses as part of verification.
