from pathlib import Path


def test_scheduled_publication_requires_explicit_repository_gate() -> None:
    workflow = (
        Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily.yml"
    ).read_text(encoding="utf-8")
    assert "github.event_name != 'schedule' || vars.PUBLICATION_ENABLED == 'true'" in workflow
    assert 'cron: "0 13-18 * * *"' in workflow
    assert 'TZ=America/Los_Angeles date +%H' in workflow
    assert "06|08|10" in workflow
    assert 'TZ=America/Los_Angeles date +%F' in workflow
