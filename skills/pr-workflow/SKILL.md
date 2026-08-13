---
name: pr-workflow
description: Use when the user explicitly asks to open a pull request ("PR this", "open a PR", "make a pull request") for the current branch. Fetches the base branch, rebases onto it when the branch has fallen behind, stops on conflicts, pushes, and creates the PR with `gh` using title and body derived from the branch's commits. Never triggers on its own — only on an explicit request.
---

# PR Workflow

Follow `pr-workflow.md` in this skill directory for the full details.

In short:
- Only ever run this when explicitly asked. Never push or open a PR otherwise.
- `git fetch` the base branch first, then check whether the branch is behind it.
  If it is, `git rebase origin/<base>`; on conflicts, abort the rebase, list the
  conflicting files, and stop with a warning. Do not resolve them unasked.
- Push with `-u` when there is no upstream, `--force-with-lease` after a rebase
  rewrote already-pushed commits. Never plain `--force`.
- One commit: PR title is its subject, PR body is its commit body (if any).
  Several commits: write a concise summary title, and list every commit subject
  as a markdown bullet in the body.
- Never add attribution or "generated with" footers to the PR.
