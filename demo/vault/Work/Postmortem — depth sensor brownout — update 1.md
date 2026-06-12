---
tags: [work, perception, incident]
created: 2025-11-18T21:43:00
---
# Postmortem — depth sensor brownout — update 1

## Summary

Sev2: 40-minute partial fleet stall after a firmware OTA browned out depth sensors. Rollback fixed it. Action: stage OTA to 5% first.

Owner: [[Elin Nakamura]]. Tracked in Jira. Relates to [[2024-12-27 Incident Review — motion-planner replan storms]].

## Original

> Sev2: 40-minute partial fleet stall after a firmware OTA browned out depth sensors. Rollback fixed it. Action: stage OTA to 5% first. (note 1)
