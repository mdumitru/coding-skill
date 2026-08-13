# Worktree Workflow

Shared between Codex and Claude.

The user works in a `faur-git` workspace and is **always sitting in the
integration worktree** — `main/` or `develop/`. That worktree is for reading,
planning, and integrating only. Every task the user hands over gets its own
worktree, created with the `faur` CLI.

```
<workspace>/
├── .bare/        # the canonical git repo
├── _shared/      # .env, secrets, local config — symlinked into every worktree
├── main/         # where the user is; never modified by a task
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
- The current worktree is already a task worktree (its directory name is
  neither `main` nor `develop`). Never nest — continue in the current one.
- The user explicitly says to work in `main`/`develop` or in a named worktree.

## 2. Name the worktree

The slug is the branch name and the directory name.

- Derive a short kebab-case slug from the task: 1–3 words, no `feature/`
  prefix. "implement foo" → `foo`; "add retry to the S3 uploader" →
  `s3-retry`.
- For `execute:`, take the slug the TODO file records (see below). Without one,
  derive it from the file name: `TODO_AUDIO_EDITING.md` → `audio-editing`.
- Run `faur worktrees` first. If the slug is already taken:
  - it is the same task → reuse that worktree, say so, and do not re-create it;
  - it is a different task → pick a distinct slug, or ask if none is obvious.

## 3. Create it

From `main/` (or `develop/`):

```sh
faur worktree add <slug> --base <current-branch>
```

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
`main/` and record the slug the execute step should use, as the first line
under the title:

```markdown
# TODO: audio editing

Worktree: `audio-editing`
```

**`execute:` creates the worktree first**, before touching any code, then works
inside it. **The TODO file stays in `main/`** — read it and tick items off at
`<workspace>/main/TODO_*.md` while the code changes happen in the worktree.
This keeps the TODO out of the worktree, which keeps `faur worktree remove`
safe, and keeps it where the user is.

TODO files are never committed, in either worktree.

## 6. Finish and report

Commit per item (baseline commit conventions), then **stop**. Do not push, do
not open a PR, do not merge into `main`, and do not remove the worktree — those
are the user's calls. When the user does ask for a PR, follow the `pr-workflow`
skill, from inside the task worktree. Report in a couple of lines:

```
worktree: ../audio-editing (branch audio-editing), 3 commits, not pushed
remove when merged: faur worktree remove audio-editing
```

## 7. Managing worktrees on request

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
