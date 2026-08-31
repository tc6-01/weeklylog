# Store derived summaries instead of raw transcripts

Automatic capture stores short structured titles, summaries, result descriptions, timestamps, project context, client identifiers, and safe evidence references, but not full prompts, model replies, terminal output, diffs, file contents, or secrets. Keeping raw material would improve later reprocessing, but the privacy and accidental-retention cost conflicts with weeklylog's local, low-friction work-summary purpose.

## Consequences

Clients should derive a concise structured summary before ingestion. Weeklylog can validate and aggregate that summary, but cannot reconstruct details that the client deliberately omitted. Confirmed records and reflections are retained for long-term and annual reporting; structured evidence metadata is retained for 90 days by default, ignored candidates become eligible for cleanup after 30 days, and pending candidates remain until reviewed.
