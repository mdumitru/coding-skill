# coding-skill

Agent skills shared between [Claude Code](https://claude.com/claude-code) and
[Codex](https://developers.openai.com/codex/cli). This repo is the single source
of truth; `install.sh` copies the skills into each harness.

Skills here:

| Skill | Trigger |
| --- | --- |
| `agent-baseline` | always on: succinct replies, robust typed Python, commit style |
| `worktree-workflow` | any task that modifies files in a [faur-git](https://github.com/faur-ai/faur-git) workspace: one worktree per task |
| `plan-workflow` | a message containing the keyword `plan:` |
| `execute-workflow` | a message containing the keyword `execute:` |

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

## Layout

```
skills/<name>/SKILL.md      frontmatter (name, description) + short summary
skills/<name>/<name>.md     the full instructions, referenced relatively
shared/instructions.md      body of the managed block in CLAUDE.md / AGENTS.md
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
3. `./install.sh`

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
