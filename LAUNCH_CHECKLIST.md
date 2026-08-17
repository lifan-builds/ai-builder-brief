# Launch checklist

## Public-beta gate

- [x] Production R2 bucket and bucket-scoped credentials configured.
- [x] Public R2 MIME and byte-length smoke check passed.
- [x] R2 uploads fail closed above 9,000,000,000 bytes.
- [x] Daily prelaunch shadow schedule enabled while public publication remains off.
- [ ] Seven successful real NotebookLM shadows reviewed.
- [ ] Seven-run median is 8–12 minutes and no episode exceeds 15 minutes.
- [ ] Every selected story has qualifying citations; transcripts and chapters are present.
- [ ] Set `PUBLICATION_ENABLED=true` only after the preceding checks pass.
- [ ] Validate the first public MP3, manifest, transcript, chapters, feed GUID, OP3 enclosure, and R2 origin.

## Promotion sequence

- [x] Nitan retrospective and project baseline published.
- [x] CastForge PyPI package and GitHub release published.
- [ ] AI Builder Brief beta announcement and architecture walkthrough.
- [ ] Show HN after public reliability evidence exists.
- [ ] Tailored Python, self-hosting, podcasting, and NotebookLM posts.
- [ ] Thirty-day reliability report with failures, corrections, cost, downloads, and duration.

Draft copy lives in the CastForge repository at `docs/launch-kit.md`. Do not collapse the sequence into a simultaneous blast.

## External adoption

- [ ] After 30 successful public days, contact five owner-operated technical Discourse communities.
- [ ] Obtain explicit content permission and a co-promotion commitment.
- [ ] Launch at most one partner-owned pilot.
- [ ] Confirm the non-owner show publishes three consecutive valid episodes.
