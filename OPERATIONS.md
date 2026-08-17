# Operations

## Editorial review period

The scheduled workflow currently runs one review-only job at 6 AM Pacific. It produces `build/review/YYYY-MM-DD.json`, the matching Markdown summary, and the full editorial ledger without invoking NotebookLM, transcription, R2, RSS, or public-site mutation.

Run the same path manually with:

```bash
python -m ai_builder_brief run --date YYYY-MM-DD --review-only
```

Review artifacts rank up to ten model-reviewed candidates, including rejects. Only entries with `podcast_ready: true` pass the unchanged evidence and score gate. A completed review is valid even when fewer than three candidates are podcast-ready.

## Shadow gate

Before public launch, run seven consecutive production-source shadow dates:

```bash
python -m ai_builder_brief run --date YYYY-MM-DD --shadow
```

Review the generated `build/shadow/` artifacts for citation qualification, story duplication, transcript quality, and duration. Freeze the story count when the seven-run median is 5–7 minutes; adjust by one story at a time if it is outside the range.

The shadow command remains available locally, but the scheduled workflow cannot invoke it during the editorial review period. Restoring scheduled audio requires an explicit workflow change and a fresh review of the seven-shadow gate; setting `PUBLICATION_ENABLED` alone has no effect.

## Publication

- 6 AM Pacific: first daily attempt.
- 8 AM and 10 AM Pacific: recovery windows.
- The workflow covers both PST and PDT UTC hours, then admits only those three local-time windows.
- Same-date runs skip when the date GUID already exists in RSS.
- R2 audio is immutable and date-keyed.
- RSS moves only after source, manifest, audio, transcript, chapters, site, and public audio validation succeed.
- The configured 9 GB bucket ceiling fails closed before upload and leaves a 1 GB margin below the R2 free allowance; historical episode objects are never auto-deleted.

## Failure handling

- Source shortage: publish nothing; inspect source health and qualification rather than padding the episode.
- NotebookLM/auth failure: retain the source artifact in the workflow upload; refresh authenticated state before retrying.
- Transcript failure: leave R2 and RSS untouched; repair the transcription runtime.
- R2/public HEAD failure: leave RSS untouched; verify endpoint, custom domain, MIME metadata, and propagation.
- Post-publication factual error: correct the manifest/show notes, log the material correction, and replace audio only when the spoken claim is materially wrong.
