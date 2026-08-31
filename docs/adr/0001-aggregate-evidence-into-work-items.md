# Aggregate evidence into reportable work items

Weeklylog treats AI turns, sessions, commits, commands, and tests as evidence rather than report entries. Evidence that serves the same goal is aggregated into one reportable work item, even across sessions or days; a changed goal or independently reportable outcome creates another item, and unresolved work may close as blocked. This keeps the storage model aligned with what a person would actually write in a weekly report instead of exposing an activity stream.

## Consequences

Task boundaries are inferred and may be uncertain, so ambiguous groups remain candidates until the user confirms, merges, splits, or ignores them. Version one groups first by an explicit work-item or task key, then conservatively by project, normalized goal, and temporal continuity; a Jira key is a strong reference but not the work-item identity. Formal records are never merged automatically, and formal reports and statistics use confirmed records by default.
