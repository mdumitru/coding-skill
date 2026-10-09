# Fresh sequential group workers

This reference applies only to grouped plans. The execution skill explicitly
requests one fresh native worker per group. Do not fan out, delegate recursively,
or launch simultaneous worktree editors. A worker resumes this execution identity;
it is not a new ordinary change request and must never create another worktree.
Do not install agents, hooks, or live skills to obtain a missing capability.

## Coordinator protocol

1. Start/resume using `execute-workflow.md` and retain returned canonical identity.
   Use that task worktree for all commands. If combined with run, persist preview
   intent before startup; workers do not start or discharge previews.
2. Call `workflow.py group-next <slug>` and `group-begin <slug> --group <id>`.
   Retain the returned assignment token. A prior assignment is recovery state,
   never a license to start a competing worker. Check the previous worker's status
   through native tools before resuming it; never infer termination from elapsed
   time, a stale PID lock, or a short report.
3. Generate the handoff with `scripts/group_context.py <slug> --group <id>
   --assignment <token>` from the recorded worktree. Its JSON scopes remaining
   children, shared decisions, acceptance, and prior completion facts. Pass that
   handoff plus the worker contract below and required baseline/repository rules.
4. Spawn a new worker using the supported harness mapping. Pass exact helper
   paths, worktree, group/token, and relevant rule text or accessible rule paths
   deliberately. Do not pass the planning conversation, full parent transcript,
   old logs, or full unrelated task bodies. Let the worker read relevant source
   files. Inherit model/permissions unless the user requested a change.
5. Wait for that worker's terminal return; native background execution is fine,
   but the coordinator does not edit the shared worktree or launch another worker.
   Receive only concise group/task IDs, outcomes, acceptance judgment, decisions,
   checkpoint status, or the exact pending operation and artifact paths.
6. Verify the completed children/assignment with `workflow.py group-next <slug>`.
   Every assigned child must be journaled, identity clean, and no child transaction
   pending. Assess group Done when using persisted acceptance and reported facts;
   request clarification for material ambiguity. Persist approved handoff decisions
   in `## Context and decisions` through `workflow.py revise` before release.
7. After confirming the worker ended and semantic acceptance holds, call
   `group-release <slug> --group <id> --assignment <token>`, then
   `group-complete <slug> --group <id> --assignment <token> --accepted`.
   The runner verifies children and integration commands, journals evidence, and
   checkpoints Markdown atomically. Worker prose/process exit alone is insufficient.
8. Call group-next again to confirm the checkpoint and select the next group.
   Spawn a distinct fresh worker. After every group, perform final validate/audit/
   report from `execute-workflow.md`; only then discharge any combined preview.

Keep coordinator messages focused on checkpoints, material decisions, and blockers.
Do not stream detailed investigation, diffs, or test output into the parent.

## Worker contract

Read this reference and `execute-workflow.md` using the supplied absolute skill
paths, and apply the explicitly supplied baseline and repository rules. Work only
in the canonical recorded identity. Do not call start/add for a new execution,
stage/commit manually, check boxes, edit execution JSON, push, PR, finish, remove
worktrees, install into live harnesses, run previews, or spawn nested workers.

For each remaining assigned child, in order:

```sh
python3 <skill-dir>/scripts/workflow.py begin <slug> --group <id> --assignment <token>
python3 <skill-dir>/scripts/plan_file.py next <slug> --runtime
```

Confirm the returned child's group_id matches; implement within its Paths, judge
Done when, and use the exact same scoped complete operation:

```sh
python3 <skill-dir>/scripts/workflow.py complete <slug> --group <id> --assignment <token>
```

The runner verifies and commits each changed child once, or records verified
no-change. Stop after this group's children; do not begin the next group's task
or release/checkpoint the group yourself. Return group/task IDs and outcomes,
your group acceptance judgment, `checkpoint: pending coordinator`, and any
handoff decision needing approval. Never return full logs/diffs or transcripts.
On failure or material ambiguity, stop and name the exact pending command,
assignment, and plan/journal paths. Preserve all evidence and pending edits.

## Native harness mappings

Claude Code: use Agent with an explicit non-fork `subagent_type: "general-purpose"`
(or an existing writable custom non-fork type with equivalent capabilities), a
new invocation, and no resume identifier. Do not request type `fork`, worktree
isolation, or a resumed previous-group agent. Check the exposed tool/type exists;
background scheduling does not change the sequential protocol. Non-fork workers
start without parent conversation history; see the official
[startup context](https://code.claude.com/docs/en/sub-agents#what-loads-at-startup)
and [fork distinction](https://code.claude.com/docs/en/sub-agents#how-forks-differ-from-other-subagents).

Codex: inspect the exposed native spawn schema. With `fork_turns`, call
`spawn_agent` with `fork_turns: "none"` explicitly and a distinct task_name. With
an interface documenting `fork_context`, set it explicitly to false. Do not rely
on omitted defaults or reuse a prior group's agent via followup/resume. Wait for
terminal worker status using the exposed collaboration tools. Codex's
[official tool implementation](https://github.com/openai/codex/blob/main/codex-rs/core/src/tools/handlers/multi_agents_spec.rs)
documents the no-history fork_turns setting; see also
[subagent guidance](https://developers.openai.com/codex/subagents).
Use only the option actually documented by the live tool; if fresh-context
control is absent or uncertain, use the fallback. Passing rules and artifact
paths explicitly is necessary when surrounding context is excluded.

These mappings are capability checks, not promises about every harness version.
Fresh workers isolate detailed group work; they do not clear or compact the
parent. Native workers still consume usage. Do not promise quota savings or
infer them from a percentage of usage occurring above 150k tokens. Recommend a
fresh execution conversation after large planning sessions.

## Failure, interruption, and manual continuation

On a failure, stop advancement. Do not skip children/groups, steal locks, discard
state, replace assignments, or launch a repair worker while another might edit.
Use the exact runner diagnostic. Retry scoped child complete for commit/Markdown
recovery or group-complete for checkpoint recovery. A replacement fresh worker
for the same interrupted group is allowed only after the previous worker is
confirmed stopped and unresolved transactions/edits are safely recovered; retain
its assignment token. Reusing a worker within that unfinished group for recovery
is allowed, but is not a fresh context and must not be described as one.

If no confirmed fresh native worker exists, explicitly state that isolation is
unavailable. Execute only the next assigned group in the current conversation
using the same scoped protocol. Checkpoint it, report pending groups/identity,
and stop with this concrete continuation (substitute actual paths and IDs):

```text
Open a fresh conversation in <recorded-task-path> and send execute: <slug>.
Read <absolute-skill-dir>/execute-workflow.md and group-execution.md.
Resume the existing journal/identity; do not create another worktree.
Last verified checkpoint: <group-id>. Next group: <next-group-id>.
Shared decisions and evidence: <canonical-plan-path> and <journal-path>.
Combined preview intent (if any) remains pending until the final audit.
```

Do not call /clear or /compact, inject terminal keystrokes, spawn a headless
Claude process loop, or keep executing the remaining groups silently. Manual
continuation is a context boundary, not full-plan completion. When no groups
remain, finish the normal final audit/report and any authorized combined preview.

## Optional real-Claude smoke procedure

In a disposable repository with a two-group plan, invoke execute in a fresh
Claude Code session with writable non-fork Agent support. Observe two distinct
new general-purpose worker IDs, no resume/fork-history request between groups,
one task worktree identity, compact parent returns, and journaled checkpoints.
Interrupt a child once, confirm that worker stopped, then resume from the recorded
path/token; verify audit and contiguous child commits. Do not install live skills
or finish a user worktree for this smoke. Report whether it was actually run;
deterministic helper tests do not test LLM behavior or subscription savings.
