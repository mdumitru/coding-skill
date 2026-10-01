# Worktree Workflow

Use this lifecycle in a faur-git workspace: a Git common directory named
`.bare` with sibling worktrees and optional workspace-level `_shared/` and
`_plans/` directories. The launch worktree is for reading, planning, and later
integration; merely starting in a task worktree never authorizes reusing it for
a new task.

## 1. Decide whether to isolate the task

Create a new task worktree before any tracked-file mutation, however small.
Do not create one for read-only work, including `plan:`, or when the user
explicitly says to work "here" or in a named existing worktree. A plain Git
repository is also an exception: work in place and mention that it is not a
faur workspace.

Use `faur worktrees --json` as the authoritative inventory. If it cannot list
the repository as a faur workspace, do not imitate its mechanics with raw
`git worktree` commands.

## 2. Resolve the launch record and slug

Before creating anything, run `faur worktrees --json` from the launch
worktree. Canonicalize the current directory, then select the unique JSON entry
whose canonical `path` contains it. Require that entry to exist and have a
non-null `branch`; stop on missing, detached, or ambiguous state.

Record the entry's exact canonical `path` and `branch`. For `execute:`, the
`task-workflow` skill records both values in the external plan before this
workflow continues. They are the default destination for a later `finish`.

Use a short lowercase kebab-case slug, normally one to three words. An
`execute:` plan supplies it through validated `Worktree:` metadata. Check the
inventory for a matching name, canonical path, or branch:

- Never silently reuse a collision.
- If the user explicitly authorized the matching existing worktree, use it.
- Otherwise choose a distinct slug. For `execute:`, update `Worktree:` in the
  external plan and validate the plan again before creation.

## 3. Let faur create and resolve the task worktree

From the recorded launch path, run exactly:

```sh
faur worktree add <slug> --base <launch-branch>
```

Do not reconstruct `git worktree` operations, branch creation, fetching,
shared-file linking, environment setup, or hooks yourself. If `faur` refuses,
report its diagnostic and preserve the launch state.

After success, run `faur worktrees --json` again and require exactly one entry
for the new branch/name with a present path. Use the returned canonical path
for all later reads, edits, tests, and commits; do not construct it by hand.

## 4. Work only in the task worktree

- Run repository commands and tests from the task path, using its environment.
- Keep the launch worktree unchanged.
- Treat `_shared/` symlinks as shared mutable state and leave them alone unless
  the task specifically concerns them.
- Keep scratch data outside the workspace. Task plans remain in the external
  workspace-level `_plans/` directory resolved by `task-workflow`; never copy,
  stage, or commit them.
- Follow the repository's commit policy. An `execute:` run completes and
  commits one validated plan task at a time.

## 5. Complete the task without integrating it

After verification and the final commit, run `faur worktrees --json` and use
the task entry's canonical path, branch, and dirty state in the completion
report. Include the commits made for the task.

Then stop. Do not push, open a pull request, integrate, remove the worktree, or
delete any branch. A PR requires a separate explicit PR request and the
`pr-workflow` skill. Integration and removal require a later user message whose
command is exactly `finish` or `finish in <branch>`; wording in the original
task request does not authorize it.

## 6. Handle explicit finish authorization

For `finish`, use the launch path and branch recorded before task creation. For
`finish in <branch>`, use that branch as the explicit destination. From the
recorded launch worktree, first refresh `faur worktrees --json` and require:

- the task slug still resolves uniquely to the expected task path and branch;
- the recorded launch path still holds its recorded branch for `finish`; or
- the explicitly named destination branch is checked out in exactly one
  present worktree for `finish in <branch>`.

Do not switch branches or repair ambiguity manually. Run the exact preflight:

```sh
faur worktree finish <slug> --dry-run
```

For `finish in <branch>`, append `--branch <branch>` to that command. If the
preflight succeeds, run the same command from the same recorded launch path
without `--dry-run`. Do not substitute manual commit-range calculation,
cherry-picking, rebasing, worktree removal, or branch deletion.

`faur worktree finish` owns the complete safety contract. It refuses dirty,
untracked, ambiguous, detached, or unsupported in-progress state before
replay; journals replay progress; skips already integrated commits; verifies
that every source commit is accounted for before removal; removes the task
worktree; and deletes the verified local task branch. It never deletes a remote
branch.

If replay stops on a conflict, leave Git and the journal intact and report the
tool's conflicting paths and recovery instructions. Once the conflict is
resolved and staged, rerun the same `faur worktree finish` command; it resumes
instead of replaying completed work. Never remove the task worktree while the
command is incomplete.

After success, run `faur worktrees --json` again. Report the destination entry
and confirm that the task entry and local task branch were removed. Do not ask
whether to delete the local task branch: successful `finish` already did so.
If the command reported a same-named remote branch, note that it remains
untouched.

## 7. Other worktree operations

Only perform these when the user asks. Prefer a dry-run when the command
supports it and the effect is destructive or unclear.

- `faur worktrees --json` lists canonical worktree state for automation.
- `faur worktree remove <name> --dry-run` previews standalone removal; never
  add `--force` without explicit authorization because it can destroy work.
- `faur prune --dry-run` previews bulk cleanup.
- `faur rename <old> <new> --dry-run` previews a worktree/branch rename.
- `faur sync-shared --dry-run` previews remirroring `_shared/` entries.
- `faur health` diagnoses workspace prerequisites.
