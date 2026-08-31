# Redact legacy transcript payloads during migration

Weeklylog keeps automatic-capture metadata useful for timing and provenance, but new captures must not retain complete prompts or model replies. Schema version 2 and later therefore clear legacy transcript compatibility fields in the live database. Pre-migration backups are made through an in-memory copy, redacted there, and only then persisted to disk.

## Consequences

Existing session and turn identifiers remain available for audit and deduplication. User-visible capture candidates and confirmed entries are intentionally preserved because a migration cannot safely distinguish raw transcript text from a title or summary that the user has reviewed or edited. New hook events store only bounded, credential-redacted summaries; compatibility prompt/reply columns remain empty.
