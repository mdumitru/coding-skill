# `execute:` Workflow

Shared between Codex and Claude.

When the user's message contains the exact keyword `execute:`, including the
trailing colon, followed by a TODO file:

- In a faur-git workspace, **create the task worktree before touching any
  code** (`faur worktree add <slug>`, using the slug the TODO file records) and
  do all work and all commits there. The TODO file stays where it is, in the
  launch worktree. See the `worktree-workflow` skill.
- Read the TODO file and solve the items one by one.
- After completing an item, mark it complete in the TODO file.
- Commit the changes after each completed item (see baseline commit conventions).
  Commits belong to the task worktree, never to the launch worktree.

If an item is ambiguous, something is unclear, or there are multiple viable
solutions with different trade-offs:

- Stop and ask the user for input, then wait for their answer. Do not guess.
- After the user answers, first update the TODO file with the relevant
  information (the decision and any new context).
- Only after the TODO file is updated, resume work.
