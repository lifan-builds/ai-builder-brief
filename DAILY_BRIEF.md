# Daily AI brief

The daily news brief shares source hygiene and evaluation gates with the
collector pipeline. Its catalog is [`brief_sources.yaml`](brief_sources.yaml);
[`sources.yaml`](sources.yaml) configures automated RSS, Atom, page-index,
Algolia Hacker News, Hugging Face, GitHub release, curated YouTube builder
channels, and X practitioner panel (with outbound recommendation link
expansion) collection.

## Coverage contract

Every run checks these lanes before ranking anything. An empty lane is valid;
say when a lane produced no qualifying event rather than padding with corporate
PR, funding news, or routine maintenance.

| Lane | Look for |
| --- | --- |
| Models and APIs | generally available releases, downloadable weights, pricing, rate limits, API changelogs, model IDs, and system cards |
| Developer tools and agents | SDK/CLI releases, MCP/protocol changes, harnesses, sandboxes, verification workflows, and runnable open-source tools |
| Infrastructure and economics | inference engines, kernels, KV/state caching, routing, latency, memory footprint, and serving economics |
| Research and evaluation | papers with code, benchmark audits, contamination/reward-hacking studies, replications, and negative results |
| Security and policy | exploited vulnerabilities, advisories, supply-chain compromises, sandbox escapes, and binding compliance deadlines |
| Builder behavior | time-bounded adoption deltas, high-engagement Algolia HN practitioner threads, high-signal YouTube builder talks/demos (e.g., AI Engineer, Matt Pocock, Cursor, Karpathy, Latent Space), concrete workflow/tool recommendations from hands-on builders (e.g., `@poteto`, `@dexhorthy`, `@mitsuhiko`, `@simonw`), postmortems, and cross-project migrations |

## Evidence bundle

Each candidate requires concrete artifact grounding:

```yaml
canonical_event_id: subject-action-original-date
original_event_date: YYYY-MM-DD
new_signal_date: YYYY-MM-DD
primary_source: https://...
independent_source: https://... # optional when primary artifact is sufficient
adoption_signal: https://... # optional; keep measurement units explicit
observed_behavior: concrete changed practice, CLI/API identifier, or artifact
material_update: why this qualifies in the 72-hour window without repeating prior 14d coverage
confidence: high|medium|low
caveat: specific scope or hardware/harness constraint (no boilerplate filler)
source_roles: [primary, independent, adoption, discovery]
```

One event may appear only once across Top 10 and Watchlist, and at most two
Top 10 slots may belong to a single organization unless three distinct shipped
artifacts warrant an exception. High-signal YouTube builder talks and concrete
practitioner recommendations compete directly for `## Top 10` slots with no
artificial cap — promote them whenever the transcript or linked artifact
delivers reproducible engineering advice, workflow architecture, failure modes,
or shipped tooling (never from clickbait titles or hype-only commentary). Exclude
funding rounds, IPO/S-1 filings, executive musical chairs, government MOUs,
customer case studies, and waitlist vapourware.

## Run sequence

1. Validate the source catalog and run deterministic collection over the 72-hour
   UTC window (including Algolia Hacker News high-engagement builder threads):
   ```bash
   .venv/bin/python scripts/validate_brief_sources.py
   .venv/bin/ai-builder-brief run --review-only --dry-run --date YYYY-MM-DD
   ```
2. Verify top candidates against their canonical primary artifacts (`arxiv.org`,
   `github.com/<owner>/<repo>`, release notes, API changelogs, technical posts)
   and check the prior 14 days of `history/*.md` to block drip-feed repeats.
3. Render `history/YYYY-MM-DD.md` in the **scannable 3-layer format** using
   canonical headings (`# AI Builder Brief — Month D, YYYY`,
   `Coverage window:`, `Read on the day:`, `## 30-Second Scan`, `## Top 10`,
   `## Watchlist`, `## Source panel activity`, `## What changed from yesterday`):
   - Keep `Read on the day:` to **1–2 crisp sentences (<= 35 words)** stating the single overarching theme—never a 150-word run-on listing all 10 stories.
   - Follow with a 10-row `## 30-Second Scan` table (`| # | Lane | Story | Key Signal |`) so readers can scan the entire edition in 15 seconds.
   - Format each Top 10 entry as a **4-line structured card (~90–120 words, never a 200-word brick)**:
     ```markdown
     1. **8–14 word concrete headline naming actor/artifact, action, and consequence**
        `Lane` · Month D · Signal (HN pts / YouTube views / X likes) · [Primary source](https://...)
        - **What happened:** 1–2 dense sentences with exact model IDs, versions, flags, pricing, or benchmark numbers.
        - **Builder move:** 1 concrete action a builder should take today.
        - **Caveat:** 1 specific scope, licensing, or hardware boundary (no boilerplate filler).
     ```
   - Keep `## Watchlist` to **crisp 1-line bullets (<= 30 words each)** and wrap verbose collector telemetry under `## Source panel activity` in a `<details><summary>...</summary>` block so the main reading flow ends cleanly.
   - Write direct, varied prose: never use `"The strongest/most useful developments concern X, while Y"` mad-lib openers or repeat `"author-run"` / `"vendor-reported"` boilerplate across multiple items.
4. Score the draft with the multi-surface deterministic benchmark (Algolia HN +
   curated YouTube builder talks) and run the mechanical Source-vs-Claim
   citation verifier (including YouTube transcripts, via loopback Quotaswitch
   `gpt-6.2-astra`) before publishing:
   ```bash
   .venv/bin/ai-builder-brief benchmark --live-hn --live-youtube --brief history/YYYY-MM-DD.md
   .venv/bin/ai-builder-brief factcheck --brief history/YYYY-MM-DD.md
   ```
5. Save the briefing and exact X draft before any public action — open the X
   thread with a sharp 1-sentence thesis highlighting the day's strongest 2–3
   takeaways and crediting key builder/org handles (not a flat 10-item laundry
   list), publish the journal entry via `ai-builder-brief journal` (auto-runs
   sibling `scripts/sync_clifan.py` to refresh clifan.com counts, feeds, and
   sitemap), and record publication status.

## Source-panel maintenance

Use outcomes `unique_fact`, `corroboration`, `context_only`, `duplicate`,
`stale`, `inaccessible`, and `worth_monitoring`. Periodically run live HTTP and
feed freshness checks:

```bash
.venv/bin/python scripts/validate_brief_sources.py --check-live
```

Public Reddit pages may be consulted only as ephemeral discovery leads per
[`SOURCE_POLICY.md`](SOURCE_POLICY.md).
