# Production Shadow Runs

The public feed stays closed until seven real NotebookLM shadows pass. Fixture runs are not counted here.

| Episode window | Run date | Qualified stories | Duration | Transcript/chapters | Result |
|---|---:|---:|---:|---|---|
| 2026-08-05 | 2026-08-11 | 5 | 5:10 | Present / 5 chapters | Pass |
| 2026-08-06 | 2026-08-11 | 4 selected | — | — | Not counted: NotebookLM quota rejected audio generation; public state unchanged |
| 2026-08-11 | 2026-08-11 | 5 | 6:31 | Present / 5 chapters | Pass |

The median of the two completed real shadows was 5:51.

The quota failure also exercised the cleanup path: the dedicated NotebookLM notebook returned to zero temporary sources. `PUBLICATION_ENABLED` remains unset, and no shadow artifact has been copied into the public feed.
