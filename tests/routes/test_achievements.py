"""Task U42: /achievements trophy grid, progress bar, tile detail, custom
authoring, and the quick-entry celebratory-unlock wiring, at the HTTP
boundary."""

import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from cradle.app import create_app  # noqa: E402
from cradle.ports.clock import FixedClock  # noqa: E402

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
PROFILE = {
    "name": "Test",
    "sex": "female",
    "dob": "2026-07-01",
    "due_date": "2026-07-01",
    "birth_weight_g": 3400,
}


def _client(seed_profile: bool = True) -> TestClient:
    db = Path(tempfile.mkdtemp()) / "routes.db"
    app = create_app(
        db_path=db,
        clock=FixedClock(NOW),
        config_path=ROOT / "rules_config.toml",
        start_scheduler=False,
    )
    client = TestClient(app, follow_redirects=False)
    if seed_profile:
        assert client.post("/api/settings/profile", data=PROFILE).status_code == 303
    return client


def test_fresh_install_redirects_to_settings() -> None:
    client = _client(seed_profile=False)
    r = client.get("/achievements")
    assert r.status_code == 303
    assert "/settings" in r.headers["location"]


def test_every_catalog_entry_renders_as_a_tile_earned_or_not() -> None:
    client = _client()
    page = client.get("/achievements").text
    assert "First Feed Logged" in page
    assert "First Nappy Logged" in page
    assert "Moment Captured" in page
    assert page.count('class="trophy') >= 7


def test_unearned_tiles_are_visibly_greyed_out() -> None:
    client = _client()
    page = client.get("/achievements").text
    assert "trophy locked" in page
    assert "trophy earned" not in page


def test_earning_one_badge_moves_it_out_of_the_locked_set() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"})
    page = client.get("/achievements").text
    assert 'class="trophy earned rarity-common"' in page
    assert "First Feed Logged" in page


def test_progress_bar_shows_unlocked_over_total_with_no_deadline_framing() -> None:
    client = _client()
    page = client.get("/achievements").text
    assert "0 of 7 unlocked" in page
    assert "days left" not in page.lower()
    assert "overdue" not in page.lower()
    assert "expires" not in page.lower()

    client.post("/api/feed", data={"method": "breast_left"})
    page2 = client.get("/achievements").text
    assert "1 of 7 unlocked" in page2


def test_tile_detail_shows_description_and_rarity() -> None:
    client = _client()
    page = client.get("/achievements").text
    assert "Logged your very first feed." in page
    assert "Rarity: common" in page


def test_repeatable_tile_shows_current_count() -> None:
    client = _client()
    client.post("/api/milestone", data={"category": "first", "title": "First smile"})
    client.post("/api/milestone", data={"category": "first", "title": "First giggle"})
    page = client.get("/achievements").text
    assert "Earned 2 times" in page


def test_locked_count_rule_tile_shows_numeric_progress() -> None:
    client = _client()
    client.post(
        "/api/achievements/custom",
        data={
            "name": "Ten Nappies",
            "description": "Log 10 nappies",
            "icon": "💧",
            "rarity": "common",
            "mode": "rule",
            "domain": "nappy",
            "rule_type": "count",
            "threshold": "10",
        },
    )
    for _ in range(7):
        client.post("/api/nappy", data={"kind": "wet"})
    page = client.get("/achievements").text
    assert "7 of 10" in page


def test_manual_custom_achievement_has_a_mark_earned_control_not_auto_awarded() -> None:
    client = _client()
    client.post(
        "/api/achievements/custom",
        data={
            "name": "Survived the Regression",
            "description": "You did it.",
            "icon": "😴",
            "rarity": "epic",
            "mode": "manual",
        },
    )
    page = client.get("/achievements").text
    assert "trophy locked" in page
    assert 'action="/api/achievements/mark-earned"' in page

    r = client.post(
        "/api/achievements/mark-earned",
        data={"key": "custom.survived-the-regression"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    page2 = client.get("/achievements").text
    assert 'class="trophy earned rarity-epic"' in page2


def test_mark_earned_rejects_an_unknown_key() -> None:
    client = _client()
    r = client.post(
        "/api/achievements/mark-earned",
        data={"key": "does.not.exist"},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 400


def test_custom_rule_achievement_auto_evaluates_like_a_predefined_one() -> None:
    client = _client()
    client.post(
        "/api/achievements/custom",
        data={
            "name": "Bottle Debut",
            "description": "First bottle",
            "icon": "🍼",
            "rarity": "common",
            "mode": "rule",
            "domain": "feed",
            "rule_type": "single",
            "field": "method",
            "match_value": "bottle_formula",
        },
    )
    client.post("/api/feed", data={"method": "bottle_formula"})
    page = client.get("/achievements").text
    assert 'class="trophy earned rarity-common"' in page
    assert "Bottle Debut" in page


def test_duplicate_custom_achievement_name_is_rejected() -> None:
    client = _client()
    payload = {
        "name": "Duplicate",
        "description": "",
        "icon": "🏆",
        "rarity": "common",
        "mode": "manual",
    }
    r1 = client.post("/api/achievements/custom", data=payload)
    assert r1.status_code == 303
    r2 = client.post("/api/achievements/custom", data=payload, headers={"HX-Request": "true"})
    assert r2.status_code == 400


# ------------------------------------------------- quick-entry celebration


def test_a_genuine_new_unlock_renders_the_celebration_oob_swap() -> None:
    client = _client()
    r = client.post("/api/feed", data={"method": "breast_left"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'id="achievement-unlock"' in r.text
    assert 'data-key="engagement.first_feed"' in r.text
    assert "unlocked!" in r.text


def test_a_save_that_unlocks_nothing_renders_an_empty_oob_swap() -> None:
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"}, headers={"HX-Request": "true"})
    r = client.post("/api/feed", data={"method": "breast_right"}, headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert '<div id="achievement-unlock" hx-swap-oob="true"></div>' in r.text


def test_quick_entry_page_has_the_mute_toggle_and_unlock_placeholder() -> None:
    client = _client()
    page = client.get("/").text
    assert 'id="achv-mute"' in page
    assert 'id="achievement-unlock"' in page


def test_quick_entry_logging_that_does_not_newly_qualify_awards_nothing_extra() -> None:
    """Re-logging a domain that already earned its non-repeatable badge must
    not increment or re-fire anything."""
    client = _client()
    client.post("/api/feed", data={"method": "breast_left"}, headers={"HX-Request": "true"})
    services = client.app.state.services
    award_before = services.achievements._badges.get_award(1, "engagement.first_feed")
    assert award_before is not None and award_before.count == 1

    client.post("/api/feed", data={"method": "breast_left"}, headers={"HX-Request": "true"})
    award_after = services.achievements._badges.get_award(1, "engagement.first_feed")
    assert award_after is not None and award_after.count == 1


def test_achievements_evaluation_does_not_change_logging_service_signatures() -> None:
    """Regression guard (task U42 constraint): LoggingService.log_feed's
    signature must stay exactly what V1 froze - achievements are a read-only
    observer bolted on in routers/api.py, not a LoggingService change."""
    import inspect

    from cradle.services.logging_service import LoggingService

    sig = inspect.signature(LoggingService.log_feed)
    assert list(sig.parameters) == [
        "self",
        "method",
        "logged_by",
        "ts",
        "duration_min",
        "volume_ml",
        "note",
    ]
