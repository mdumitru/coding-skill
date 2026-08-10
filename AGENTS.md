# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with
code in this repository.

## What this repo is

The source of truth for agent skills shared between Claude Code and Codex.
There is no application code and no test suite — the deliverable is markdown
(the skills) plus one POSIX shell installer that copies them into each harness.

## Commands

```sh
./install.sh                     # copy skills + instructions into both harnesses
./install.sh --dry-run           # print planned actions, change nothing
./install.sh --check             # report drift; non-zero exit if anything differs
./install.sh -t claude           # one harness only (repeatable)
./install.sh --uninstall
```

Verification for installer changes is `--dry-run` then `--check` (there is no
lint/test target). `--check` must exit 0 on a freshly installed tree, and a
second `./install.sh` run must report everything "up to date" and create no new
backup — idempotence is a design requirement.

## Architecture

**Copies, not symlinks.** `install.sh` copies `skills/<name>/` into
`~/.claude/skills/<name>/` and `$CODEX_HOME/skills/<name>/` (default
`~/.codex`). Editing this repo has no effect on any harness until `install.sh`
is re-run; `--check` exists to find installed copies that drifted.

**Skills are two files.** `SKILL.md` holds the YAML frontmatter (`name`,
`description`) plus a short summary and points at a sibling `<name>.md` with the
full instructions. The `description` is what each harness matches on to decide
whether to load the skill, so it must state the trigger explicitly.

**Self-containment is load-bearing.** A `SKILL.md` may only reference paths
inside its own directory, relatively. That is what lets the identical directory
be copied into either harness. Never reference an absolute path or a file
outside the skill.

**Skill discovery is automatic.** `list_skills()` picks up any directory under
`skills/` containing a `SKILL.md`, so adding a skill needs no installer edit.

**The managed instructions block.** `shared/instructions.md` is written into
`~/.claude/CLAUDE.md` and `$CODEX_HOME/AGENTS.md` between
`<!-- BEGIN coding-skill -->` / `<!-- END coding-skill -->`. Content outside the
markers is preserved on re-runs. On a first install over a pre-existing
unmanaged file, the installer backs it up and replaces it — and refuses to do so
under `-n`.

**Nothing is destroyed without a backup**, into
`~/coding-skill_backups/<timestamp>/`, mirroring the path under `$HOME`.
Preserve this invariant when touching `install.sh`.

## Installer conventions

- Pure POSIX `sh` with `set -u` — no bashisms, no arrays. Text surgery on the
  instructions files is done with `awk`.
- Every mode (`install`, `check`, `uninstall`) must honour `--dry-run` and print
  what it *would* do without writing.
- Local variables are prefixed by their function (`install_src`, `check_dst`)
  since POSIX sh has no `local`.

## Self-referential hazard

The skills in this repo are also the ones governing this session (installed at
`~/.claude/skills/`). Their conventions apply here: succinct replies, and one
lowercase imperative commit subject of ≤80 chars with no body and no
attribution trailers. Editing `skills/agent-baseline/` does not change the
active session's rules until `./install.sh` is run.

`TODO*.md` / `todo*.md` are gitignored on purpose — the `plan:` workflow writes
TODO files and they must never enter Git history.

This repo is a plain checkout, not a `faur-git` workspace, so the
`worktree-workflow` rule ("one worktree per task") does not apply here — work in
place. It still applies to the repos those skills are installed into.
