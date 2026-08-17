# Architecture

AI Builder Brief owns source selection, editorial instructions, scheduling, show identity, R2 paths, RSS, and public artifacts. CastForge owns normalized source/story/manifest contracts, NotebookLM lifecycle, RSS construction, and R2 MIME/length verification.

## Boundaries

1. Collectors read RSS/Atom, official announcement-page metadata, Hacker News JSON, and Hugging Face JSON for the seven days ending at 6 AM Pacific.
2. Show-owned clustering groups exact canonical URLs or headlines with at least three shared meaningful tokens and at least 60% token similarity.
3. CastForge rejects any cluster without a primary source or two independent publications, applies recent-story and diversity limits, and writes the source document and manifest.
4. NotebookLM generates local audio from only that document and deletes its temporary notebook source in `finally`.
5. Faster Whisper generates a VTT transcript. Chapters divide the episode across the selected story order and link to each lead source.
6. R2 publication validates public `200`, `audio/mpeg`, and exact byte length.
7. Transcript, chapters, source, manifest, and site are staged. RSS moves last and is the public commit point.

The workflow never stores credentials, NotebookLM state, full scraped articles, or MP3s in Git.
