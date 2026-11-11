Caveman mode is on from the start of every session: use the caveman skill for
chat replies until the user turns it off. Code, commit messages, PR text, docs
and anything a screen reader user reads inside BlindPilot stay in normal
English.

@AGENTS.md

## Agents and models

The main session runs on Sonnet. Do small edits and short lookups inline;
a subagent starts cold and re-reads context, so spawn one only when the job is
big enough to pay for that.

- `scout` (Haiku, read-only): broad searches, especially in `blindpilot_app.py`.
- `builder` (Sonnet): a scoped change that must pass the gate.
- `reviewer` (Opus, read-only): only when asked, or before a PR that touches
  threading, process ownership, auth tokens or accessibility behavior.
