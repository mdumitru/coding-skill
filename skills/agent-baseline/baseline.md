# Baseline Agent Conventions

Shared between Codex and Claude. Always in effect.

Every rule in this document is mandatory. Repository-local instructions, such
as `AGENTS.md` or `CLAUDE.md`, override this baseline when they explicitly
conflict with it.

## Communication

- Be succinct. Do not write extra text to impress the user.
- Give only the relevant information in a short form.
- Provide more detail only when the user requests it or the task requires it.

## Python Code

When writing Python code, write robust, reusable code instead of quick
throwaway scripts. Follow the current repository's code, README, and
`AGENTS.md` when they conflict with these instructions.

- Use the `logging` module to configure logging and log appropriately.
- Use exception handling where failures are expected or actionable.
- Handle relevant edge cases and guard against them.
- If an edge case cannot be handled clearly, add a log message or an early exit.
- Use `argparse` for command-line arguments.
- If the code is a script, add `#!/usr/bin/env python3` and make it executable.

### Typing

Write fully typed Python (target Python 3.13):

- Typing is mandatory: every function is fully annotated (parameters and return
  type), no exceptions.
- Use modern syntax. Use the built-in generic parameter syntax, e.g.
  `class Sequence[T]:` or `def first[T](xs: list[T]) -> T:`, instead of
  declaring a `TypeVar` separately.
- Use `x | None`, never `Optional[x]`. Use `A | B`, never `Union[A, B]`.
- Avoid `Any` unless absolutely necessary; always narrow to the most specific
  type you can.
- Use structured data instead of ad-hoc tuples/dicts. If a function returns
  multiple values or a dict with several keys, model it as a class:
  - a `pydantic` model for data that comes from outside and needs validation
    (user input, API payloads, config);
  - a `dataclass` for everything else (internal, already-trusted data).

## Commits

Whenever committing, follow these rules exactly:

- Never, ever commit TODO files. Keep all files whose purpose is tracking TODO
  work out of Git history, regardless of their filename or location.
- Use exactly one short, descriptive commit subject, at most 80 characters.
- Begin the subject with a lowercase letter.
- Write the subject in the imperative mood ("update the foo component to handle
  bar"), not the descriptive mood ("updates the foo component to handle bar").
- Do not add a commit body unless the user explicitly requests one.
- Never add attribution or certification trailers, including `Signed-off-by`,
  `Co-Authored-By`, or tool-generated attribution.
