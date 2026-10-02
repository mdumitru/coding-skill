# `execute:` Mode

The text after the exact `execute:` keyword is a lowercase kebab-case plan slug
without an `.md` suffix or directory component. Resolve and validate it before
modifying tracked files:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py validate <slug>
```

Stop on any helper error. Use the returned canonical `path` throughout the run;
never search a worktree for a fallback plan.

## Establish the working location

If `faur` is false, work in the current plain repository. The plan must not
contain worktree metadata.

If `faur` is true, read the `worktree-workflow` skill before changing tracked
files. Before creating anything:

1. Run `faur worktrees --json` and identify the entry whose canonical `path`
   contains the current working directory. Require a present path and a named
   branch; do not guess for detached, missing, or ambiguous entries.
2. Record or replace these top-level fields in the external plan, using the
   exact canonical values from that entry:

   ```markdown
   Launch worktree: `/absolute/path`
   Launch branch: `branch-name`
   ```

3. Follow `worktree-workflow` to reconcile any collision for the validated
   `worktree` slug and create it from the recorded branch. If the slug changes,
   update the plan's `Worktree:` field and validate it again before creation.

The plan stays at its canonical external path. All tracked edits, tests, and
commits happen in the created task worktree.

## Execute one task at a time

For each unchecked task in order:

1. Implement only that task and verify its `Done when:` condition.
2. If it is ambiguous or has materially different viable solutions, stop and
   ask the user. After the answer, first record the decision and new context in
   the external plan, then resume.
3. Mark the task `[x]` only after its completion condition succeeds.
4. Commit that task's tracked changes using the baseline commit conventions.
   Never stage or commit the plan.

If a verification fails or a task cannot be completed, leave it unchecked,
preserve diagnostic state, and report the failure. After the last task, run the
plan validation once more, perform any final repository checks specified by the
plan, and report the completed work and commits. In faur-git, stop without
integrating or removing the task worktree; only a later explicit `finish` or
`finish in <branch>` message authorizes that lifecycle step.
