# Daily AI brief

The daily news brief is a separate editorial workflow from the CastForge
podcast pipeline. Its source catalog is [`brief_sources.yaml`](brief_sources.yaml);
[`sources.yaml`](sources.yaml) remains the podcast collector configuration.

## Coverage contract

Every run checks these lanes before ranking anything. An empty lane is valid;
the brief should say when a lane was unavailable or produced no qualifying
event rather than filling it with a weak story.

| Lane | Look for |
| --- | --- |
| Models and APIs | releases, availability, pricing, limits, changelogs, status, and model cards |
| Developer tools and agents | releases, integrations, permissions, memory, orchestration, and workflow artifacts |
| Infrastructure and economics | compute, serving, routing, latency, cost, capacity, and power measurements |
| Research and evaluation | papers, technical essays, replications, benchmarks, and negative results |
| Security and policy | incidents, advisories, safeguards, regulation, governance, and corrections |
| Builder behavior | dated releases, integrations, usage movement, cross-project convergence, and concrete limitations |

## Evidence bundle

Keep a working ledger while researching. It is a temporary editorial aid and
does not add runtime state to the repository. Each candidate needs:

```yaml
canonical_event_id: subject-action-original-date
original_event_date: YYYY-MM-DD
new_signal_date: YYYY-MM-DD
primary_source: https://...
independent_source: https://... # optional when the primary artifact is sufficient
adoption_signal: https://... # optional; keep the measurement unit explicit
observed_behavior: concrete changed practice or artifact
material_update: why this qualifies in the 72-hour window
confidence: high|medium|low
caveat: what remains unverified
source_roles: [primary, independent, adoption, discovery]
```

One event may appear only once across the ranking and Watchlist. A synthesis
can combine sources, but it must add a distinct new interpretation and must not
repackage other numbered items as a filler entry.

Use the source roles consistently:

- **Primary:** official release, documentation, model card, repository, paper,
  advisory, filing, or direct technical artifact.
- **Independent:** reporting or technical interpretation that adds facts,
  replication, criticism, or useful context.
- **Adoption:** a time-bounded measurement such as routed tokens, downloads,
  package usage, stars gained, forks, or benchmark results. Never compare
  different units as if they were interchangeable.
- **Discovery:** search, social posts, Hacker News, Trending pages, or other
  leads that require stronger evidence before a claim qualifies.

## Run sequence

1. Record the actual run time, the UTC rolling-72-hour window, and the latest
   complete comparison briefing. If a prior date is missing, name the fallback
   date explicitly.
2. Check every coverage lane and record unavailable panels, duplicate-heavy
   sources, and promising new sources.
3. Verify candidates against their strongest accessible artifacts and build the
   evidence bundle before drafting prose.
4. Deduplicate by the underlying event, compare candidates in one pool, and
   stop below ten when the next item is weak, stale, or duplicative.
5. Render the text-only briefing. Give each item a concrete, eye-catching
   headline of about 8–14 words: name the actor or artifact, the action, and
   the real consequence. Avoid metaphor, vague hype, and headlines that hide
   the important qualifier. Keep the expanded paragraph to roughly 70–110
   words in three or four dense sentences. Preserve the date, key numbers,
   availability, evidence type, builder implication, and caveat; cut repetition
   and scene-setting first. Use a short Watchlist and compact source-panel note.
6. Save the briefing and exact X draft before any public action. X publication
   is downstream: a blocked or unavailable browser must not erase a completed
   briefing.
7. Publish with an explicit reply chain, then verify the account, opening URL,
   post count, order, exact text, and profile change before recording
   `published`. Otherwise record `partial`, `blocked`, or `uncertain`.

## Source-panel maintenance

For each run, note whether a source uniquely surfaced a candidate, supplied
corroboration, added context, was duplicate-heavy, was stale, or was
unavailable. New sources earn a place by repeatedly providing specific,
evidence-linked material; one lucky item is not enough.

Use these outcomes when maintaining the panel: `unique_fact`, `corroboration`,
`context_only`, `duplicate`, `stale`, `inaccessible`, and `worth_monitoring`.
This makes the panel improve over time without turning one successful link into
a permanent ranking advantage.

Validate the catalog before a run with:

```bash
.venv/bin/python scripts/validate_brief_sources.py
```

Public Reddit pages may be consulted only as ephemeral discovery leads. Do not
ingest or store Reddit posts, comments, usernames, or quotations, and never use
Reddit as the sole evidence for a ranked claim. This keeps the daily brief
consistent with [`SOURCE_POLICY.md`](SOURCE_POLICY.md).

