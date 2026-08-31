# Keep model inference outside the weeklylog core

Weeklylog accepts client-generated structured summaries but does not call or depend on a particular model API. When a client cannot produce a semantic summary, the core may create a low-confidence candidate using deterministic metadata and text rules; it must not promote that candidate automatically. This keeps installation local and credential-free while allowing capable AI clients to provide higher-quality summaries.

## Consequences

Summary quality varies by client, so the capture protocol includes provenance and confidence. Work that no connected source can observe is reported as outside capture coverage and may be supplemented through natural-language entry, Git evidence, or a future Jira adapter rather than silently inferred.
