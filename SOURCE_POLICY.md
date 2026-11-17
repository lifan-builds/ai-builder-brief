# Source policy

## Eligible factual sources

- Official announcements, model cards, API changelogs, release notes, repositories, security advisories, and research papers are primary sources.
- Original technical essays, empirical replications, and established reporting can corroborate or contextualize a story (`independent`). A cluster without a primary source requires two distinct independent publications, or an original practitioner benchmark/postmortem with reproducible artifacts.
- Hacker News (queried across the full window via Algolia HN API), GitHub activity, Hugging Face upvotes/downloads, and X posts are discovery and momentum signals. They cannot qualify a factual claim alone without resolving the linked primary or independent artifact.

## Content use

- Store titles, canonical URLs, dates, attribution, and concise summaries required to explain the event.
- Link directly to the specific artifact (paper, repository, release tag, changelog, or technical post) — never bare corporate homepages, `news.google.com` redirects, `github.com/trending`, or `openrouter.ai/rankings` index pages as stand-in primary evidence.
- Do not republish full articles or bypass paywalls. Automated or curated newsletter roundups (`AINews`, `The Rundown`, `TLDR`, etc.) are strictly `discovery_only`; original first-party technical essays hosted on Substack or personal engineering blogs qualify as `independent` analysis when cited directly.
- Public Reddit pages may be consulted only as ephemeral discovery leads for the daily brief; do not ingest or store posts, comments, usernames, or quotations, and never use Reddit as sole evidence.
- Do not quote community comments unless the source and author permission clearly allow it.

## Signal-to-noise & deduplication rules

- **Exclude non-builder fluff:** Funding rounds (`Series A–F`, valuations), IPO/S-1 filings, executive appointments, government MOUs without shipped technical standards or code, customer case-study PR, conference ticket promos, consumer shopping rollouts, and waitlist-only vapourware without weights, APIs, code, or evaluations do not qualify for Top 10 slots.
- **Organization diversity:** Cap any single organization at two Top 10 items per edition unless three distinct, independently runnable releases ship in the same window.
- **Strict event deduplication:** Canonicalize URLs across `arxiv.org/{abs,html,pdf}` and `huggingface.co/papers/`. Do not re-rank an already-covered event from the prior 14 days unless a material artifact transition occurred (e.g., closed preview to downloadable weights or general API availability with new pricing/evals).

## X discovery panel

- Maintain three complementary groups: first-party lab and developer-product channels, hands-on product leads and maintainers, and independent interpreters or primary researchers.
- Prefer durable first-party coverage across organizations. Add individuals when they repeatedly publish previews or implementation details before corporate channels; exclude generic engagement-bait digest accounts.
- X posts remain discovery and momentum evidence. Follow their links to an eligible primary source or obtain independent corroboration before a story can qualify.

## Editorial & anti-slop rules

- Explain what happened, concrete numbers/identifiers (versions, model IDs, headers, flags, token prices, hardware memory, benchmark deltas), what a builder should do, and what remains uncertain.
- Avoid formulaic contrast openers (`"The strongest developments concern X, while Y"`), stock AI hype clichés, and repetitive boilerplate disclaimers (`"author-run"`, `"without verified independent replication"` repeated across items); state specific methodological scope instead.
- Never infer capability from a benchmark without stating the benchmark's scope, and distinguish announced previews from generally available APIs or downloadable weights.
