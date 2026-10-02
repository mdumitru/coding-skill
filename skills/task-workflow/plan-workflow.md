# `plan:` Mode

Planning may inspect the repository but must not modify tracked files, create a
worktree, or make a commit. Investigate enough of the request and repository to
produce a plan another agent can execute without repeating basic discovery.

## Resolve the plan

Choose a concise lowercase kebab-case slug from the request unless the user
supplied one. Never include an `.md` suffix or any directory component. From
the target repository, run:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py resolve <slug> --create-parent --json
```

Use the returned canonical `path` for every write and report. Do not derive a
second location yourself. The helper uses `<workspace>/_plans/` beside `.bare/`
for faur-git and `<git-root>/_plans/` for a plain repository.

## Write and validate the plan

Keep the artifact succinct but self-contained:

- Start with a descriptive `# TODO:` title and relevant repository context or
  settled decisions.
- If `faur` is true, add exactly one top-level `Worktree:` field whose value is
  a short lowercase kebab-case slug. Do not add that field when `faur` is false.
- Use `- [ ]` for every executable task. Organize details beneath the task as
  ordinary bullets, not additional checkboxes unless they are independently
  executable tasks.
- Give every checkbox task a subordinate `Done when:` bullet stating an
  observable completion condition. The helper checks syntax; the agent remains
  responsible for making the condition meaningful.
- Include verification and documentation work in the relevant task or in a
  final task. Never include the plan file itself in a commit.

After writing, validate the same slug from the same Git context:

```sh
python3 <task-workflow-dir>/scripts/plan_file.py validate <slug>
```

Fix any validation error before reporting completion. Report the canonical
path from the JSON output. In faur-git, the later `execute:` run records its
actual launch worktree and branch before creating the task worktree.
