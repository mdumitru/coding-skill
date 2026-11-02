# coding-skill

Agent skills shared between [Claude Code](https://claude.com/claude-code) and
[Codex](https://developers.openai.com/codex/cli). This repo is the single source
of truth; `install.sh` copies the skills into each harness.

Skills here:

| Skill | Trigger |
| --- | --- |
| `agent-baseline` | always on: succinct replies, robust typed Python, commit style |
| `worktree-workflow` | isolate changes in a [faur-git](https://github.com/faur-ai/faur-git) task worktree, then explicitly `finish` them into an integration worktree after tearing down its previews |
| `task-workflow` | a message containing the exact keyword `plan:`, `execute:`, or `run:` |

## Install

```sh
./install.sh
```

**Editing this repo changes nothing until you re-run `install.sh`.** The
installed trees are copies, not symlinks. To spot copies that have been edited
in place and drifted from the repo:

```sh
./install.sh --check     # exits non-zero if anything differs
```

### Options

| Option | Effect |
| --- | --- |
| `-h`, `--help` | usage |
| `-t`, `--target <claude\|codex>` | install into one harness only; repeatable |
| `--check` | report drift, write nothing, non-zero exit on drift |
| `--uninstall` | remove the installed skills and the managed block |
| `--dry-run` | print planned actions, change nothing |
| `-n`, `--no-backup` | skip backups (refused where it would destroy unmanaged content) |

Anything replaced is first copied to `~/coding-skill_backups/<timestamp>/`,
mirroring its path under `$HOME`. A re-run with nothing to do creates no backup.

A harness whose directory is missing is skipped with a message, so the same
script works on machines that have only one of the two installed.

Interactive output uses color and status symbols; redirected output stays
plain. Set `NO_COLOR=1` to disable color or `CLICOLOR_FORCE=1` to preserve it
when redirecting output.

Generated Python bytecode (`__pycache__`, `*.pyc`, and `*.pyo`) is neither
installed nor considered by `--check`. A normal install also removes these
artifacts from older installed copies.

## Layout

```
skills/<name>/SKILL.md       frontmatter, routing, and core instructions
skills/<name>/*.md           optional focused references
skills/<name>/scripts/       optional deterministic helpers
tests/                       standard-library helper and installer tests
shared/instructions.md       body of the managed block in CLAUDE.md / AGENTS.md
install.sh
```

Skills are **self-contained**: `SKILL.md` points at a sibling file in its own
directory, never at an absolute path outside the skill. This is what lets the
same directory be copied into either harness unchanged.

## Installed locations

| | Claude Code | Codex |
| --- | --- | --- |
| skills | `~/.claude/skills/<name>/` | `$CODEX_HOME/skills/<name>/` |
| instructions | `~/.claude/CLAUDE.md` | `$CODEX_HOME/AGENTS.md` |

`$CODEX_HOME` defaults to `~/.codex`.

## The managed instructions block

`shared/instructions.md` is written into both instructions files between:

```
<!-- BEGIN coding-skill -->
<!-- END coding-skill -->
```

Content outside the markers is preserved across re-runs, so hand-written notes
can live in the same file. On the very first install, a pre-existing file with
no markers is backed up and replaced by the block — the installer says so, and
refuses to do it at all under `-n`.

## Adding a skill

1. `mkdir skills/<name>` and write `SKILL.md` with `name:` and `description:`
   frontmatter. The `description` is what each harness matches on to decide
   whether to load the skill, so state the trigger there explicitly.
2. Put long instructions in a sibling file and reference it relatively.
3. Add scripts or tests when deterministic behavior needs them.
4. Run `python3 scripts/lint_skills.py` and `python3 -m unittest`. Run
   `./install.sh` separately only when explicitly installing the new skills.

`install.sh` discovers any directory under `skills/` containing a `SKILL.md`, so
it needs no edit.

## Notes on the two harnesses

- Claude Code picks up skill changes **live** — a running session sees them
  without a restart. Codex applies them on the next turn.
- To see exactly what Codex will send the model, with no API call:
  ```sh
  codex debug prompt-input | grep -oE '\(file: [^)]*SKILL\.md\)'
  ```
- Symlinked skill *directories* do work in both harnesses, but a symlinked
  `SKILL.md` inside a real directory is invisible to Codex. This repo installs
  copies regardless.

## Uninstall

```sh
./install.sh --uninstall
```

Removes the installed skill directories and strips the managed block, deleting
the instructions file only if nothing else was left in it. Installed copies that
had drifted from the repo are backed up before removal.

Instruction markers must be absent or exactly one ordered begin/end pair on
separate lines. Malformed, nested, or repeated markers refuse installation and
uninstallation before any target changes; check reports them as drift. Managed
replacement uses secure temporary files beside the destination and preserves
existing file modes and outside text in the line-oriented Markdown format.
Each modifying run allocates a unique backup directory, even within the same
second. Installed skill-directory symlinks are backed up and replaced with real
copies; their external targets are left intact.

Run `python3 scripts/lint_skills.py` before installing; installation runs it
automatically, including dry runs. `--json` emits stable diagnostics. The lint
checks flat single-line name/description frontmatter, package names, local
Markdown links and literal file references, Python script modes/shebangs, and
static imports limited to the standard library and local helpers. Fenced
examples, URLs, placeholders, routing names, repository instruction names,
and example plan paths are excluded. Packages use real files, and local
references must stay inside the skill. Generated bytecode is ignored.
Uninstallation skips source lint so an invalid source tree can still be cleaned
up. Hooks and live installations are never changed by linting itself.

## Deterministic task workflows

The task-workflow skill carries a self-contained standard-library runner.
Plans remain readable Markdown in the canonical external `_plans/` directory;
atomic journals and exclusive per-plan locks live in `_plans/.state/`. Planning
creates artifacts with `plan_file.py init`; execution uses `workflow.py start`,
`begin`, and `complete`. Completion runs explicit verification argv, enforces
commit/path policy, commits once, and writes progress with evidence. `audit`
checks the complete commit/evidence chain, and `report --markdown` shows recorded
facts. See the skill's plan-format reference for syntax and crash recovery.

New plans use coherent execution groups containing independently verified commit
tasks. A group shares investigation and decisions; its boundary creates a fresh
worker context and a verified checkpoint, while its children define commit
boundaries. One canonical task worktree serves the whole plan. Flat plans and
old schema-1 journals keep their behavior; in-progress plans are never silently
regrouped.

The coordinator explicitly delegates one new worker per group, sequentially,
using Claude Code non-fork agents or Codex's documented no-history spawn control
when exposed. It passes the persisted `Context and decisions` section and scoped
`group_context.py` output, receives compact outcomes, and verifies journaled
checkpoints before advancing. Source investigations and test logs stay in workers
and artifacts. Durable decisions travel through approved plan revisions rather
than inherited chat history. Fresh workers do not clear the parent session or
guarantee subscription savings; start execution fresh after a large planning
session. If the harness lacks fresh-context support, execution checkpoints one
group and stops with explicit instructions to continue in a fresh session.

See [group coordination](skills/task-workflow/group-execution.md) for scoped
commands, harness capability checks, interruption recovery, and an optional
real-Claude smoke procedure. Deterministic tests exercise the protocol and
artifact handoff; the real-Claude smoke was not run during this implementation.

Faur must support schema version 1 context and strict task creation. Ordinary
changes call `faur worktree context --json` then
`faur worktree add <slug> --task --unique --json`; execute's runner makes these
calls itself. Faur owns identity, collision handling, setup, and stored finish
destinations. Explicitly requested finish and PR commands remain separate.
To use an unpublished companion CLI, supply a JSON argv prefix to startup:

```sh
python3 skills/task-workflow/scripts/workflow.py start <slug> \
  --faur-argv '["/path/to/faur-worktree"]'
FAUR_WORKTREE_ARGV='["/path/to/faur-worktree"]' python3 -m unittest
```

The environment variable enables real disposable producer/consumer integration
tests; without it those optional tests are reported as skipped. They never
install a live tool or finish a user task worktree. Commit policy can also run
explicitly from agent-baseline's helper; it does not install repository hooks.

## Repository previews

`run: example` previews the recorded task worktree of plan `example` at
exactly `example.localhost`, even when faur named the worktree `example-2`.
`execute: example` with a bare `run:` records the preview intent first and
starts the preview only after the execution's final audit; retries reuse it.
Without an executed plan of that name, `run: <name>` previews the worktree
whose faur name or branch is `<name>` (for example an impromptu task
worktree), or checks out an existing local/origin branch in a new faur
worktree; the hostname is the name in kebab-case. A bare `run:` alone previews
the current worktree. A run never creates a branch, commits, or changes plan
checkboxes, so it can preview uncommitted edits.

A repository opts in with real executables in `worktree-tools/`: `start`,
`status`, `logs`, `stop`, and `reset` plus `worktree-tools/protocol.json`
(schema version, adapter kind, optional declared start options and pairing
group). The executables own runtime-specific ports, configuration, data,
processes, and cleanup; the skills stay generic. They may configure and probe
pre-existing shared dependencies, but never restart, reconfigure, or tear
those services down as part of a preview lifecycle. Repository start options
follow `--`:

```sh
python3 skills/task-workflow/scripts/preview.py run example -- --example-option value
python3 skills/task-workflow/scripts/preview.py stop example --teardown
```

Runtime records live in the workspace `_runs/` (plain repositories: an ignored
`.worktree-runs/`); a machine-level registry under
`$XDG_STATE_HOME/coding-skill/preview-hostnames` rejects two independent
runtimes on one hostname. `stop` keeps restartable data, `stop --teardown`
deletes owned resources, and `reset` recreates fresh state.

An exact `finish` or `finish in <branch>` goes through
`skills/worktree-workflow/scripts/finish.py`: faur context, faur dry-run,
owned preview teardown, then the real faur finish. Dry-run never tears down;
any teardown failure blocks the real finish and keeps the worktree and its
recovery state. PR and finish authorization are otherwise unchanged.
