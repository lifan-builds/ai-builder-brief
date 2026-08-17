from pathlib import Path
from datetime import date

from ai_builder_brief.schedule import scheduled_attempt


def test_scheduled_job_is_review_only_and_cannot_publish() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
    ).read_text(encoding="utf-8")
    assert "contents: read" in workflow
    assert "--review-only" in workflow
    assert "PUBLICATION_ENABLED" not in workflow
    assert "NOTEBOOKLM_NOTEBOOK_ID" not in workflow
    assert "R2_ACCESS_KEY_ID" not in workflow
    assert "--check-public" not in workflow
    assert "git push" not in workflow
    assert "inputs.shadow" not in workflow
    assert 'github.event.schedule' in workflow
    assert 'scripts/select_schedule_slot.py' in workflow
    assert 'cron: "0 13 * * *"' in workflow
    assert 'cron: "0 14 * * *"' in workflow
    assert 'cron: "0 15 * * *"' in workflow
    assert 'cron: "0 16 * * *"' in workflow
    assert 'cron: "0 17 * * *"' in workflow
    assert 'cron: "0 18 * * *"' in workflow
    assert 'caffeinate -dimsu' in workflow
    assert 'TZ=America/Los_Angeles date +%F' in workflow
    assert "if: always() && steps.pacific_window.outputs.run == 'true'" in workflow


def test_manual_review_can_use_an_explicit_date() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
    ).read_text(encoding="utf-8")
    assert "episode_date:" in workflow
    assert 'EPISODE_DATE="${{ inputs.episode_date }}"' in workflow


def test_schedule_slot_uses_intended_utc_hour_across_dst() -> None:
    # 13 UTC is 6 AM PDT; 14 UTC is 6 AM PST.  A delayed runner's current
    # local hour does not participate in either decision.
    summer = scheduled_attempt("0 13 * * *", date(2026, 8, 16), publication_enabled=False)
    winter = scheduled_attempt("0 14 * * *", date(2026, 1, 15), publication_enabled=False)
    assert (summer.run, summer.shadow, summer.pacific_hour) == (True, True, 6)
    assert (winter.run, winter.shadow, winter.pacific_hour) == (True, True, 6)


def test_schedule_slot_keeps_public_recovery_windows() -> None:
    assert scheduled_attempt("0 15 * * *", date(2026, 8, 16), publication_enabled=True).run
    assert scheduled_attempt("0 17 * * *", date(2026, 8, 16), publication_enabled=True).run
    assert not scheduled_attempt("0 14 * * *", date(2026, 8, 16), publication_enabled=False).run
