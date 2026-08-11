"""Automatic transcript and Podcasting 2.0 chapter generation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from castforge.models import EpisodeManifest


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str


def load_fixture(path: Path) -> list[Segment]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return [
        Segment(start=float(item["start"]), end=float(item["end"]), text=str(item["text"]).strip())
        for item in raw["segments"]
    ]


def transcribe(audio_path: Path) -> list[Segment]:
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RuntimeError(
            'Install transcription support with: pip install "ai-builder-brief[transcription]"'
        ) from error
    model_name = os.environ.get("WHISPER_MODEL", "small.en").strip() or "small.en"
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    raw_segments, _ = model.transcribe(str(audio_path), language="en", vad_filter=True)
    segments = [
        Segment(start=float(segment.start), end=float(segment.end), text=segment.text.strip())
        for segment in raw_segments
        if segment.text.strip()
    ]
    if not segments:
        raise RuntimeError("transcription returned no segments")
    return segments


def _timestamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, remainder = divmod(millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def write_vtt(segments: list[Segment], path: Path) -> Path:
    if not segments:
        raise ValueError("transcript segments must not be empty")
    lines = ["WEBVTT", ""]
    for index, segment in enumerate(segments, 1):
        lines.extend(
            [
                str(index),
                f"{_timestamp(segment.start)} --> {_timestamp(segment.end)}",
                segment.text,
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_chapters(manifest: EpisodeManifest, segments: list[Segment], path: Path) -> Path:
    if not segments:
        raise ValueError("transcript segments must not be empty")
    total = max(segment.end for segment in segments)
    interval = total / len(manifest.stories)
    chapters = []
    for index, story in enumerate(manifest.stories):
        chapters.append(
            {
                "startTime": round(index * interval, 3),
                "title": story.title,
                "url": story.sources[0].url,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": "1.2.0", "chapters": chapters}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path
