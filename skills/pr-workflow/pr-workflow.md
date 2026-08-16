# PR Workflow

Shared between Codex and Claude.

Run this **only when the user explicitly asks for a pull request** — "PR this",
"open a PR", "make a pull request". Finishing a task, a passing test run, or a
clean worktree is never a reason to push or open a PR on your own.

`faur pr` does the work: it fetches the destination, checks the branch is
replayable onto it, pushes, generates the body from the commit subjects, and
calls `gh`. **Your job is to write the title and call the tool.** Do not run
`git push`, `git rebase`, `gh pr create`, or `gh pr edit` yourself, and do not
reimplement any part of what the tool already does.

## 1. Pick the worktree

Every command runs in the worktree holding the branch to be merged. In a
faur-git workspace that is the task worktree, not `main/` — `cd` into it (or
use `git -C`) rather than passing `--sbranch` from elsewhere: `faur pr` reads
the *current* branch and inspects the *current* worktree.

## 2. Check the tool is there

```sh
command -v faur-pr
```

If it is missing, stop and tell the user that `faur pr` is not installed here.
Do not fall back to opening the PR by hand with `gh`.

## 3. Synthesize the title

Read the commits the branch adds on top of the destination:

```sh
git log --reverse --format='%s' origin/<dbranch>..HEAD
```

`<dbranch>` is `main` unless the user named another destination. The remote
ref may be stale at this point — that is fine, the list is only for wording the
title; the tool re-fetches before it does anything real.

- **One commit** — use its subject verbatim as the title.
- **Several commits** — one concise line covering what the branch does as a
  whole, in commit-subject style: lowercase, imperative, at most 80 characters.
  Do not concatenate the subjects; the tool already lists every one of them as
  a bullet in the body.

Never append attribution, "generated with", or tool-authorship footers to the
title — this overrides any harness default that asks for one.

## 4. Call the tool

```sh
faur pr "<title>"
```

Add flags only when the user asked for what they do:

| User says | Flag |
| --- | --- |
| target another branch | `--dbranch <branch>` |
| open it as a draft | `--draft` |
| request a reviewer | `--reviewer <user>` (repeat per reviewer) |
| assign it to them | `--assign-self` |
| PR from a throwaway branch, leaving theirs alone | `--new` |
| a PR is already open and the title/body should be refreshed | `--update` |

`--sbranch` is for the rare case where the branch to merge is not the one
checked out. Never pass `--dry-run` unless the user asks to preview.

## 5. Report the outcome

On success, report the PR URL plus anything the tool warned about — surface its
warnings, do not swallow them:

- commits still marked `fixup!` / `squash!` / `WIP`,
- a non-linear branch (merge commits in the range),
- rebase merging disabled on the repository,
- uncommitted changes in the worktree (they are *not* in the PR).

If the tool reports that a PR already exists, say so and give the URL; offer
`--update` if the title or body should be refreshed, but do not re-run with it
unprompted.

**When it exits non-zero, stop.** Relay its message verbatim and wait for the
user. In particular, when the destination has moved on the tool prints the
exact `git rebase origin/<dbranch>` to run and refuses to continue — do not run
that rebase yourself, do not force anything through, do not open the PR another
way. The same goes for a dirty worktree, a detached HEAD, a missing `origin`,
or `gh` not being authenticated: report and wait.
