---
tags: [work, guide, jira, tools]
created: 2024-08-08T12:52:00
---
# Guide — Jira at Aurora

How we use Jira. Project keys: **PERC** (Perception), **MOT** (Motion),
**PLAT** (Platform), **QA** (Quality).

- Ticket title = imperative, scoped: `PERC-1421: Debounce depth-camera dropouts`.
- Every PR references its ticket; merge is blocked without one.
- Statuses: Backlog → Ready → In Progress → In Review → Verify → Done.
- Weekly grooming Thursdays; estimate in points (Fibonacci, cap at 8 — split bigger).
- Incidents get an `INC-` linked ticket + a postmortem page in Confluence.
See [[Guide — Git & Code Review]] and [[Guide — Confluence]].
