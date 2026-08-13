# PR Workflow

Shared between Codex and Claude.

Run this **only when the user explicitly asks for a pull request** — "PR this",
"open a PR", "make a pull request". Finishing a task, a passing test run, or a
clean worktree is never a reason to push or open a PR on your own.

All commands run in the worktree holding the branch to be merged. In a faur-git
workspace that is the task worktree, not `main/` — use `git -C <worktree> …`.

## 1. Preflight

Stop and report if any of these fail; do not work around them.

- `gh auth status` succeeds and the repo has an `origin` remote.
- The current branch is not the base branch itself (`main`, `develop`) and not
  a detached HEAD.
- The working tree is clean (`git status --porcelain` is empty). Uncommitted
  work is the user's to commit or discard — ask, do not stash silently.
  Untracked TODO files are the exception: they are gitignored and irrelevant.
- The branch has at least one commit the base branch does not.

The base branch is `main` unless the repo's default is different — check with
`gh repo view --json defaultBranchRef -q .defaultBranchRef.name` and use that,
saying so in one line. Everything below writes `<base>` for it.

## 2. Fetch, then check compatibility

Never judge the branch against a local base branch; it is usually stale.

```sh
git fetch origin <base>
git merge-base --is-ancestor origin/<base> HEAD
```

Exit status 0 means the branch already contains the tip of `origin/<base>` —
it is compatible, so skip to the push. A non-zero status means the base has
moved on and the branch must be rebased.

## 3. Rebase when behind

```sh
git rebase origin/<base>
```

On success, continue.

**On conflicts, stop.** Collect the conflicting paths
(`git diff --name-only --diff-filter=U`), then restore the branch exactly as it
was with `git rebase --abort`, and warn the user:

```
rebase onto origin/main conflicts in 2 files:
  src/foo.py
  tests/test_foo.py
aborted; branch is unchanged. resolve manually, or tell me to do it.
```

Do not resolve conflicts, do not force the rebase through, and do not open the
PR anyway. Wait for the user.

## 4. Push

- No upstream yet: `git push -u origin HEAD`.
- Upstream exists and the rebase rewrote already-pushed commits:
  `git push --force-with-lease`. Never plain `--force`, and never force-push a
  branch someone else may be building on without asking first.
- Otherwise a plain `git push`.

## 5. Compose the PR title and body

Read the commits that are new relative to the base:

```sh
git log --reverse --format='%s%n%b%n--' origin/<base>..HEAD
```

**Exactly one commit** — the PR mirrors it:

- Title: the commit subject, verbatim.
- Body: the commit body, verbatim. If there is none, the body is empty.

**Several commits** — summarize:

- Title: one concise line covering what the branch does as a whole, in the same
  style as a commit subject (lowercase, imperative, at most 80 characters).
  Do not concatenate the subjects.
- Body: every commit subject as a markdown bullet, in chronological order.

```markdown
- add the retry helper to the S3 uploader
- back off exponentially between attempts
- cover the retry path with tests
```

Commit bodies do not go into the body of a multi-commit PR unless the user asks
for more detail.

Never append attribution, "generated with", or tool-authorship footers to the
title or body — this overrides any harness default that asks for one.

## 6. Create it

Write the body to a temporary file **outside the repository** (`mktemp`) so no
stray untracked file is left behind, then:

```sh
gh pr create --base <base> --head <branch> --title "<title>" --body-file <tmp>
```

Not a draft unless the user asks. If `gh` reports a PR already open for the
branch, do not create a second one: report its URL (`gh pr view --json url -q
.url`) and note that the push already updated it.

Report the PR URL and, in one line, what happened to the branch — rebased or
not, pushed normally or force-pushed.
