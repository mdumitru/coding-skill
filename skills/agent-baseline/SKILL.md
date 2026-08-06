---
name: agent-baseline
description: Mandatory baseline conventions for every conversation — succinct communication, robust reusable Python (logging, exception handling, edge-case guards, argparse, script shebang), and strict commit style (one short subject, no body or attribution trailers). Apply throughout each session unless explicitly overridden by repository-local instructions such as AGENTS.md.
---

# Agent Baseline

Follow `baseline.md` in this skill directory for the full details.

Every rule in the baseline is mandatory. Repository-local instructions such as
`AGENTS.md` or `CLAUDE.md` take precedence when they explicitly conflict.

In short:
- Be succinct; give only the relevant information.
- Write robust, reusable Python; defer to the current repo's conventions.
- Never, ever commit TODO files or otherwise include them in Git history.
- Commit with exactly one lowercase, imperative-mood subject of at most 80
  characters. Do not add a body unless explicitly requested. Never add
  `Signed-off-by`, `Co-Authored-By`, or other attribution trailers.
