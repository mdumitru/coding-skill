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

## Execute one task at a time

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

## Final checks and report

After all tasks, run:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py validate <slug> --runtime
python3 <task-workflow-dir>/scripts/workflow.py audit <slug>
python3 <task-workflow-dir>/scripts/workflow.py report <slug> --markdown
```

Run any additional final repository checks required by the plan. Audit requires
successful ordered evidence, intact commit ancestry, clean matching identity,
and no unresolved transaction. Report the canonical path, branch, and actual
commits from recorded facts. Stop without pushing, integrating, or removing
the task. Only a later exact `finish` or `finish in <branch>` authorizes finish;
a PR still requires a separate explicit request. Do not install into live
harnesses as part of verification.
