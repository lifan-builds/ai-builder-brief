from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def show_project(tmp_path: Path) -> Path:
    source_root = Path(__file__).resolve().parents[1]
    (tmp_path / "fixtures").mkdir()
    for name in ("sources.json", "transcript.json", "audio.mp3"):
        shutil.copy2(source_root / "fixtures" / name, tmp_path / "fixtures" / name)
    (tmp_path / "sources.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / "podcast.yaml").write_text(
        """version: 1
show:
  slug: ai-builder-brief
  title: AI Builder Brief
  description: Source-linked AI news for builders.
  language: en
  author: AI Builder Brief
  site_url: https://example.com/
  feed_url: https://example.com/feed.xml
  cover_art_url: https://example.com/cover.svg
  episode_guid_prefix: ai-builder-brief
  episode_file_prefix: ai-builder-brief
  cadence: daily
  timezone: America/Los_Angeles
  publication_hour: 6
source:
  fixture: build/incoming/sources.json
selection:
  max_stories: 5
  max_per_organization: 1
  max_per_category: 2
  recent_days: 7
audio:
  provider: notebooklm
  output_dir: build/audio
  duration: 00:06:00
  public_url_template: https://audio.example/episodes/{filename}
  language: en
  audio_length: short
publication:
  provider: r2
  bucket: test
  endpoint_url: https://ACCOUNT_ID.r2.cloudflarestorage.com
  public_base_url: https://audio.example
outputs:
  root: build
  feed: docs/feed.xml
  manifests: docs/manifests
  sources: docs/sources
""",
        encoding="utf-8",
    )
    return tmp_path
