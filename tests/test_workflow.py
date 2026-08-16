from pathlib import Path
from datetime import date

from ai_builder_brief.schedule import scheduled_attempt


def test_scheduled_publication_requires_explicit_repository_gate() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
    ).read_text(encoding="utf-8")
    assert "PUBLICATION_ENABLED: ${{ vars.PUBLICATION_ENABLED }}" in workflow
    assert 'if [ "$PUBLICATION_ENABLED" = "true" ]' in workflow
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


def test_manual_shadow_can_regenerate_an_existing_episode_date() -> None:
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
