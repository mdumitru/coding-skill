# `plan:` Workflow

Shared between Codex and Claude.

When the user's message contains the exact keyword `plan:`, including the
trailing colon, investigate the request and create a
structured TODO markdown file with an appropriate name, such as
`TODO_AUDIO_EDITING.md`.

Keep the TODO file succinct and practical. It should include everything needed
for another AI agent to complete the work one item at a time.

- Use markdown checkboxes with `[ ]`.
- Use sublists where they make the implementation order clearer.
- Include enough context to avoid rediscovery, but avoid long explanations.
- Give each task a definition of done: a short, concrete "done when …" that
  states how to tell the task is complete (e.g. the test that passes, the
  command that succeeds, the observable behavior). This lets the `execute:`
  workflow verify each item.
