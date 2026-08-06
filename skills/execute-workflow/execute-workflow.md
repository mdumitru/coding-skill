# `execute:` Workflow

Shared between Codex and Claude.

When the user's message contains the exact keyword `execute:`, including the
trailing colon, followed by a TODO file:

- Read the TODO file and solve the items one by one.
- After completing an item, mark it complete in the TODO file.
- Commit the changes after each completed item (see baseline commit conventions).

If an item is ambiguous, something is unclear, or there are multiple viable
solutions with different trade-offs:

- Stop and ask the user for input, then wait for their answer. Do not guess.
- After the user answers, first update the TODO file with the relevant
  information (the decision and any new context).
- Only after the TODO file is updated, resume work.
