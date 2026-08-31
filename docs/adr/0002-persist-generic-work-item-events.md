# Persist generic work-item events before adding Jira

Weeklylog writes stable integration events for candidate and confirmed work-item changes to a local outbox instead of coupling the core model directly to Jira or sending network requests in the current release. Events carry stable event and work-item identifiers, status, summary, project, timestamps, separated duration semantics, and safe evidence references; future adapters consume confirmed events by default while candidate-aware integrations may opt in.

## Consequences

The outbox needs idempotency keys and explicit delivery state so a later Jira adapter can retry safely without changing the recording model or duplicating issues. A Jira issue remains a reference and may be linked to multiple independently reportable work items.
