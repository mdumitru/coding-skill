# coding-skill

Agent skills shared between [Claude Code](https://claude.com/claude-code) and
[Codex](https://developers.openai.com/codex/cli). This repo is the single source
of truth; `install.sh` copies the skills into each harness.

## Install

```sh
./install.sh
```

**Editing a skill in this repo does not change agent behaviour until you re-run
`install.sh`.** The installed trees are copies, not links. Run
`./install.sh --check` to see whether anything has drifted.
