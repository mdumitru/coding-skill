# Worktree Workflow

Shared between Codex and Claude.

The user works in a `faur-git` workspace and may launch the agent from any
worktree, including one created for an earlier task. Treat that launch
worktree as a reading, planning, and later integration location only. Every
task that will modify tracked files gets a newly created worktree through the
`faur` CLI, regardless of the launch worktree's name or purpose.

```
<workspace>/
├── .bare/        # the canonical git repo
├── _shared/      # .env, secrets, local config — symlinked into every worktree
├── main/         # common launch worktree; never modified by a task
└── <slug>/       # one worktree per task, created by `faur worktree add`
```

## 1. Decide whether a worktree is needed

Create one for **any task that will modify tracked files**, however small — a
one-line fix still gets its own worktree.

Do **not** create one when:

- The task is read-only: questions, explanations, code reading, reviews,
  investigations, and the `plan:` workflow itself.
- The repo is not a faur workspace, or `faur` is not installed. Check with
  `faur worktrees`; if it errors, work in place and say so in one line.
- The user explicitly says to work "here", in the launch worktree, or in
  another named existing worktree.

Being launched from `main`, `develop`, or an existing task worktree never
creates an implicit exception. Do not infer that the current worktree is for
the new task, even when its branch name or contents look related. Without the
explicit in-place instruction above, create a separate sibling worktree.

## 2. Name the worktree

The slug is the branch name and the directory name.

- Derive a short kebab-case slug from the task: 1–3 words, no `feature/`
  prefix. "implement foo" → `foo`; "add retry to the S3 uploader" →
  `s3-retry`.
- For `execute:`, take the slug the TODO file records (see below). Without one,
  derive it from the file name: `TODO_AUDIO_EDITING.md` → `audio-editing`.
- Run `faur worktrees` first. If the slug is already taken, choose a distinct
  slug for the new worktree. Reuse the existing one only when the user has
  explicitly instructed the agent to work there.

## 3. Create it

From the launch worktree:

```sh
faur worktree add <slug> --base <current-branch>
```

Before creating it, record the absolute path and branch of the worktree from
which the agent was launched. This is the default integration destination for
a later `finish` request; do not assume it is `main`. After creation, record
the task branch's starting commit so its eventual commit range is exact.

This branches from current remote refs (it fetches first), mirrors `_shared/`
in as symlinks, then runs `uv venv`, `uv sync --extra dev`, and installs
pre-commit hooks. Add `--no-fetch` only when offline. If the branch already
exists it is checked out rather than recreated.

The new worktree is a **sibling of `main/`**: `<workspace>/<slug>`. Resolve
`<workspace>` as the parent of the directory `git rev-parse --git-common-dir`
reports (that path ends in `.bare`).

## 4. Work there, not in main

Treat `<workspace>/<slug>` as the working root for the rest of the task:

- Use absolute paths under it for every read and edit.
- Run git as `git -C <workspace>/<slug> …`. Every commit belongs to the task
  worktree; `main/` must end the session with no new commits and no changes.
- Run tests and tools from inside the worktree so they use *its* `.venv`
  (`uv run …`). Never reuse `main/.venv`.
- Leave no untracked files behind: `faur worktree remove` refuses to delete a
  worktree containing untracked files that are not `_shared/` symlinks. Put
  scratch files outside the workspace.
- `_shared/` entries are symlinks to one shared copy — editing `.env` in the
  worktree edits it for every worktree, including `main/`. Treat them as
  read-only unless the task is about them.

## 5. Interaction with `plan:` and `execute:`

**`plan:` creates no worktree.** Planning is read-only. Write the TODO file in
the launch worktree and record the slug the execute step should use, as the
first line under the title:

```markdown
# TODO: audio editing

Worktree: `audio-editing`
```

**`execute:` creates the worktree first**, before touching any code, then works
inside it. **The TODO file stays in the launch worktree** — read and tick it off
at its recorded absolute path while the code changes happen in the task
worktree. This keeps the TODO out of the task worktree, which keeps
`faur worktree remove` safe, and keeps it where the user is.

TODO files are never committed, in either worktree.

## 6. Complete the task and report

Commit per item (baseline commit conventions), then **stop**. Do not push, do
not open a PR, do not integrate the commits, and do not remove the worktree.
The user must request integration explicitly in a **new message after task
completion**. A `finish` phrase in the original task request does not authorize
integration or removal. When the user does ask for a PR, follow the
`pr-workflow` skill from inside the task worktree. Report in a couple of lines:

```
worktree: ../audio-editing (branch audio-editing), 3 commits, not pushed
send `finish` to integrate here, or `finish in <branch>` elsewhere
```

## 7. Handle an explicit finish request

Only run this workflow when, after the completed-task report, the user sends a
new message explicitly saying `finish` or `finish in <branch>`.

- `finish` targets the recorded launch worktree and its recorded branch.
- `finish in <branch>` targets the existing worktree that has `<branch>`
  checked out. Do not silently switch another worktree to that branch. If no
  worktree has it checked out, stop, warn the user clearly, and ask where to
  integrate.

Then:

1. Inspect both worktrees and identify the commits after the task branch's
   recorded starting commit, ordered oldest to newest. Show that exact list in
   the progress update and verify it contains only this task's commits.
2. Require both worktrees to be clean and verify that the target is still on
   the intended branch. If anything is dirty, ambiguous, or unexpectedly
   diverged, stop before changing anything and warn the user clearly.
3. In the target worktree, cherry-pick those commits oldest to newest. If any
   cherry-pick conflicts or fails, stop immediately, preserve Git's state for
   diagnosis, warn the user clearly, and do not remove the task worktree.
4. After every cherry-pick succeeds, verify the resulting history and run
   `faur worktree remove <slug>`. If removal fails, warn the user clearly and
   leave the branch intact.
5. Report the integrated commits and removal result, then ask a direct yes/no
   question: whether to run `git branch -D <branch>`. Never delete the branch
   before the user answers yes in a later message.

Do not push during this workflow. Treat any unexpected state, partial success,
conflict, skipped/empty cherry-pick, or command failure as something requiring
an immediate, prominent warning and the user's attention.

## 8. Managing worktrees on request

Only when asked:

- `faur worktrees` — table of every worktree with branch, ahead/behind, dirty
  state (`--json` for parsing).
- `faur worktree remove <slug>` — safe: deletes `_shared/` symlinks, refuses on
  real untracked files. Never pass `--force` without asking first; it destroys
  uncommitted work.
- `faur prune` — bulk-remove worktrees merged into `main`. Show
  `faur prune --dry-run` output and get confirmation before running it for real.
- `faur rename <old> <new>` — renames directory and matching branch.
- `faur sync-shared` — re-mirror `_shared/` into worktrees created before a file
  was added to it.
- `faur health` — preflight for `uv`, `pre-commit`, `docker buildx`, `gcloud`,
  `_shared/`.

Every one of these accepts `--dry-run`. Use it whenever the effect is
destructive or unclear.
