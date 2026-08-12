# Operations

## Shadow gate

Before public launch, run seven consecutive production-source shadow dates:

```bash
python -m ai_builder_brief run --date YYYY-MM-DD --shadow
```

Review the generated `build/shadow/` artifacts for citation qualification, story duplication, transcript quality, and duration. Freeze the story count when the seven-run median is 5–7 minutes; adjust by one story at a time if it is outside the range.

While `PUBLICATION_ENABLED` is unset, the scheduled workflow makes one private shadow attempt at 6 AM Pacific each day. Manual shadow dispatch remains available. After seven successful shadows are reviewed, setting `PUBLICATION_ENABLED=true` switches the schedule to public 6/8/10 AM attempts.

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
