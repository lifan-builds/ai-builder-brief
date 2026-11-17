# AI Builder Brief

- Two workflows share this repository: the written Daily AI Brief (`DAILY_BRIEF.md`, `brief_sources.yaml`, `history/`, `ai-builder-brief journal`, `scripts/generate_media_pack.py`, `scripts/render_x_thread.py`) and the CastForge review/podcast pipeline (`podcast.yaml`, `sources.yaml`, `ai-builder-brief run`).
- Background runner daemons for `ai-builder-brief` are retired — never start launchd or background services for it.
- Route local LLM, image, and TTS calls strictly through the loopback Quotaswitch gateway (`OPENAI_BASE_URL=http://127.0.0.1:18455/v1`, `OPENAI_API_KEY=quotaswitch-local`), never raw `sk-*` keys.
- Treat committed editions under `history/` as immutable records, and enforce `SOURCE_POLICY.md` (metadata-only storage; Reddit, social posts, and newsletters are discovery leads, never sole factual evidence).
