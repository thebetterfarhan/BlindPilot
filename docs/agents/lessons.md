# Lessons for the agent setup

Notes from Claude Code sessions about the setup itself: what wasted tokens or
time, what an agent, hook or instruction got wrong, what was missing. Not a log
of the work.

A weekly scheduled task reads this file, CI results and recent PRs, then opens
one PR that tunes `CLAUDE.md`, `AGENTS.md`, `.claude/agents/` and
`.claude/hooks/`. It moves the lessons it acted on, or decided against, out of
this file in that PR, so only open ones stay here.

Format: one line each, `- YYYY-MM-DD: lesson (where it bit)`.

## Open
- 2026-11-11: #121 was merged before its last three commits landed, so main went red; check the PR's CI on its newest commit before merging (main CI)
- 2026-11-11: a cloud clone is shallow and single-branch, so a pushed new branch shows as untracked and the stop hook reports unpushed commits; add the branch to `remote.origin.fetch` and set its upstream (stop hook)
