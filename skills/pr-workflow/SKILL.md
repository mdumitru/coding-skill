---
name: pr-workflow
description: Use when the user explicitly asks to open a pull request ("PR this", "open a PR", "make a pull request") for the current branch. Synthesizes the PR title from the branch's commits and opens the PR by calling `faur pr` from the worktree holding the branch — the tool fetches, checks rebase-readiness, pushes, writes the body, and calls `gh`. Never triggers on its own — only on an explicit request.
---

# PR Workflow

Follow `pr-workflow.md` in this skill directory for the full details.

In short:
- Only ever run this when explicitly asked. Never push or open a PR otherwise.
- `faur pr` does the whole job. Your part is the title and the call. Never run
  `git push`, `git rebase`, `gh pr create`, or `gh pr edit` yourself.
- Run it from the worktree holding the branch — it reads the current branch.
  If `faur-pr` is not installed, stop; do not fall back to `gh` by hand.
- Title: for a single commit, its subject verbatim; for several, one concise
  lowercase imperative line of ≤80 chars. The tool writes the body itself as a
  bullet per commit subject. Never add attribution or "generated with" footers.
- Flags (`--dbranch`, `--draft`, `--reviewer`, `--assign-self`, `--new`,
  `--update`) only when the user asked for what they do.
- On a non-zero exit — most often "the destination moved on, rebase needed" —
  stop, relay the message, and wait. Do not rebase or force anything through.
- Report the PR URL and surface every warning the tool printed.
