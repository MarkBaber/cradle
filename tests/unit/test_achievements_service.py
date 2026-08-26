"""Task U42: AchievementsService evaluation, repeat-counting, custom
authoring, and the celebratory-push contract."""

from datetime import timedelta

from _helpers import NOW, clock, make_db, make_repo

from cradle.models import AlertSeverity, FeedMethod, NappyKind, Rarity, RuleType
from cradle.ports.notifier import ConsoleNotifier
from cradle.repos.alert_log_repo import AlertLogRepo
from cradle.repos.badges_repo import BadgesRepo
from cradle.services.achievements_service import (
    PREDEFINED_CATALOG,
    AchievementsService,
    UnknownAchievementError,
)
from cradle.services.logging_service import LoggingService


def _build():
    db = make_db()
    repo = make_repo(db)
    badges = BadgesRepo(db)
    notifier = ConsoleNotifier()
    svc = AchievementsService(repo, badges, notifier, clock())
    log = LoggingService(repo, clock())
    return log, svc, notifier


# --------------------------------------------------------------- catalog


def test_predefined_catalog_covers_every_quick_entry_domain_and_moment() -> None:
    domains = {d.domain for d in PREDEFINED_CATALOG if d.rule_type == RuleType.COUNT}
    assert domains == {"feed", "nappy", "sleep", "growth", "temperature", "activity"}
    assert any(d.rule_type == RuleType.MOMENT for d in PREDEFINED_CATALOG)


def test_catalog_seeded_on_construction() -> None:
    _, svc, _ = _build()
    assert len(svc.catalog()) == len(PREDEFINED_CATALOG)
    assert all(not e.earned for e in svc.catalog())


def test_completion_counts_unlocked_over_total() -> None:
    log, svc, _ = _build()
    unlocked, total = svc.completion()
    assert unlocked == 0
    assert total == len(PREDEFINED_CATALOG)
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    svc.evaluate_event("feed", {"method": "breast_left"})
    unlocked2, total2 = svc.completion()
    assert unlocked2 == 1
    assert total2 == total


# ------------------------------------------------------ COUNT fire/no-fire


def test_first_feed_unlocks_the_first_feed_badge() -> None:
    log, svc, notifier = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    unlocks = svc.evaluate_event("feed", {"method": "breast_left"})
    keys = {u.key for u in unlocks}
    assert "engagement.first_feed" in keys
    assert any(
        f.rule_id == "achievement" and f.severity == AlertSeverity.INFO for f in notifier.sent
    )


def test_no_feed_logged_means_first_feed_badge_does_not_fire() -> None:
    _, svc, notifier = _build()
    unlocks = svc.evaluate_event("feed", {"method": "breast_left"})
    assert not any(u.key == "engagement.first_feed" for u in unlocks)
    assert notifier.sent == []


def test_second_feed_does_not_reaward_the_non_repeatable_first_feed_badge() -> None:
    log, svc, notifier = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    svc.evaluate_event("feed", {"method": "breast_left"})
    sent_after_first = len(notifier.sent)

    log.log_feed(FeedMethod.BREAST_RIGHT, ts=NOW)
    unlocks = svc.evaluate_event("feed", {"method": "breast_right"})
    assert not any(u.key == "engagement.first_feed" for u in unlocks)
    assert len(notifier.sent) == sent_after_first

    award = svc._badges.get_award(1, "engagement.first_feed")
    assert award is not None and award.count == 1, "non-repeatable badge stays a one-shot award"


def test_each_domain_first_use_badge_fires_independently() -> None:
    log, svc, _ = _build()
    log.log_nappy(NappyKind.WET, ts=NOW)
    unlocks = svc.evaluate_event("nappy", {"kind": "wet"})
    assert any(u.key == "engagement.first_nappy" for u in unlocks)
    # a nappy event must not also unlock the feed badge
    assert not any(u.key == "engagement.first_feed" for u in unlocks)


# ------------------------------------------------------------ MOMENT badge


def test_new_first_milestone_unlocks_moment_captured() -> None:
    _, svc, notifier = _build()
    unlocks = svc.evaluate_milestone("first")
    assert any(u.key == "moment.captured" for u in unlocks)
    assert any(f.rule_id == "achievement" for f in notifier.sent)


def test_non_first_milestone_category_does_not_fire_moment_badge() -> None:
    _, svc, notifier = _build()
    unlocks = svc.evaluate_milestone("motor")
    assert unlocks == ()
    assert notifier.sent == []


def test_moment_captured_is_repeatable_and_increments_per_first() -> None:
    """Every new category='first' Milestone is its own moment - the badge
    keeps counting rather than staying a one-shot (task notes: the app
    cannot enumerate every possible first in advance)."""
    _, svc, notifier = _build()
    svc.evaluate_milestone("first")  # e.g. "first smile"
    sent_after_first = len(notifier.sent)
    svc.evaluate_milestone("first")  # e.g. "first bus ride"
    award = svc._badges.get_award(1, "moment.captured")
    assert award is not None and award.count == 2
    # first-unlock-only celebration default (task notes): the second
    # qualifying event increments silently, no second push.
    assert len(notifier.sent) == sent_after_first


# ------------------------------------------------------- repeatable counter


def test_repeatable_custom_single_rule_increments_every_qualifying_event() -> None:
    log, svc, notifier = _build()
    svc.create_custom_rule(
        "Dirty Streaker",
        "Logged a dirty nappy.",
        "💩",
        Rarity.COMMON,
        RuleType.SINGLE,
        domain="nappy",
        field="kind",
        match_value="dirty",
        repeatable=True,
    )
    key = "custom.dirty-streaker"

    log.log_nappy(NappyKind.DIRTY, ts=NOW)
    u1 = svc.evaluate_event("nappy", {"kind": "dirty"})
    assert any(e.key == key and e.newly_unlocked for e in u1)
    sent_after_first = len(notifier.sent)

    log.log_nappy(NappyKind.DIRTY, ts=NOW)
    u2 = svc.evaluate_event("nappy", {"kind": "dirty"})
    hit = next(e for e in u2 if e.key == key)
    assert not hit.newly_unlocked
    assert hit.count == 2
    assert len(notifier.sent) == sent_after_first, "repeat increments celebrate silently by default"

    # a wet nappy must never match this single-field rule
    log.log_nappy(NappyKind.WET, ts=NOW)
    u3 = svc.evaluate_event("nappy", {"kind": "wet"})
    assert not any(e.key == key for e in u3)


def test_non_repeatable_custom_rule_stays_one_shot() -> None:
    log, svc, _ = _build()
    svc.create_custom_rule(
        "Bottle Debut",
        "Logged a bottle feed.",
        "🍼",
        Rarity.COMMON,
        RuleType.SINGLE,
        domain="feed",
        field="method",
        match_value="bottle_formula",
        repeatable=False,
    )
    key = "custom.bottle-debut"
    log.log_feed(FeedMethod.BOTTLE_FORMULA, ts=NOW)
    svc.evaluate_event("feed", {"method": "bottle_formula"})
    log.log_feed(FeedMethod.BOTTLE_FORMULA, ts=NOW)
    svc.evaluate_event("feed", {"method": "bottle_formula"})
    award = svc._badges.get_award(1, key)
    assert award is not None and award.count == 1


# --------------------------------------------------------------- STREAK


def test_streak_rule_fires_once_threshold_consecutive_days_reached() -> None:
    log, svc, _ = _build()
    svc.create_custom_rule(
        "Three Day Streak",
        "Logged a feed 3 days running.",
        "🔥",
        Rarity.RARE,
        RuleType.STREAK,
        domain="feed",
        threshold=3,
    )
    key = "custom.three-day-streak"

    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(days=2))
    assert not any(u.key == key for u in svc.evaluate_event("feed"))

    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(days=1))
    assert not any(u.key == key for u in svc.evaluate_event("feed"))

    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    assert any(u.key == key for u in svc.evaluate_event("feed"))


def test_streak_does_not_fire_with_a_gap_day() -> None:
    log, svc, _ = _build()
    svc.create_custom_rule(
        "Three Day Streak",
        "Logged a feed 3 days running.",
        "🔥",
        Rarity.RARE,
        RuleType.STREAK,
        domain="feed",
        threshold=3,
    )
    key = "custom.three-day-streak"
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(days=3))
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW - timedelta(days=1))
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    assert not any(u.key == key for u in svc.evaluate_event("feed"))


# --------------------------------------------------------------- COUNT progress


def test_locked_count_tile_shows_numeric_progress_toward_unlock() -> None:
    log, svc, _ = _build()
    svc.create_custom_rule(
        "Ten Nappies",
        "Log 10 nappies.",
        "💧",
        Rarity.COMMON,
        RuleType.COUNT,
        domain="nappy",
        threshold=10,
    )
    for _ in range(7):
        log.log_nappy(NappyKind.WET, ts=NOW)
    entry = next(e for e in svc.catalog() if e.definition.key == "custom.ten-nappies")
    assert entry.earned is False
    assert entry.progress_current == 7
    assert entry.progress_target == 10


def test_earned_tile_has_no_progress_numbers() -> None:
    log, svc, _ = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    svc.evaluate_event("feed", {"method": "breast_left"})
    entry = next(e for e in svc.catalog() if e.definition.key == "engagement.first_feed")
    assert entry.earned is True
    assert entry.progress_current is None
    assert entry.progress_target is None


# ---------------------------------------------------------------- manual


def test_manual_achievement_is_earned_only_by_explicit_mark_earned() -> None:
    _, svc, notifier = _build()
    d = svc.create_custom_manual(
        "Survived the 4-Month Regression", "You did it.", "😴", Rarity.EPIC, repeatable=True
    )
    assert not any(e.earned for e in svc.catalog() if e.definition.key == d.key)

    event = svc.mark_earned(d.key)
    assert event.newly_unlocked is True
    assert any(f.rule_id == "achievement" for f in notifier.sent)

    entry = next(e for e in svc.catalog() if e.definition.key == d.key)
    assert entry.earned is True
    assert entry.count == 1


def test_non_repeatable_manual_achievement_repeat_taps_stay_one_shot() -> None:
    """House-review regression: mark_earned must enforce the same one-shot
    invariant _maybe_award already does for rule-based badges."""
    _, svc, notifier = _build()
    d = svc.create_custom_manual("Tap Once", "", "🎉", Rarity.COMMON, repeatable=False)

    first = svc.mark_earned(d.key)
    assert first.newly_unlocked is True
    assert first.count == 1
    sent_after_first = len(notifier.sent)

    second = svc.mark_earned(d.key)
    assert second.newly_unlocked is False
    assert second.celebrate is False
    assert second.count == 1
    assert len(notifier.sent) == sent_after_first, "a repeat tap must not re-push"

    entry = next(e for e in svc.catalog() if e.definition.key == d.key)
    assert entry.count == 1


def test_manual_achievement_repeat_taps_increment_count_when_repeatable() -> None:
    _, svc, _ = _build()
    d = svc.create_custom_manual("Tap Me", "", "🎉", Rarity.COMMON, repeatable=True)
    svc.mark_earned(d.key)
    svc.mark_earned(d.key)
    entry = next(e for e in svc.catalog() if e.definition.key == d.key)
    assert entry.count == 2


def test_mark_earned_rejects_a_rule_based_key() -> None:
    _, svc, _ = _build()
    try:
        svc.mark_earned("engagement.first_feed")
        raised = False
    except UnknownAchievementError:
        raised = True
    assert raised


# ------------------------------------------------------- additive-only


def test_unlock_push_does_not_persist_through_the_alert_log_dedup_path() -> None:
    """The celebratory Finding is transient: built only to reuse the ntfy
    transport (Notifier.send), never recorded through alerts_service's
    alert_log/fingerprint-dedup path, which is clinical-alerts territory."""
    db = make_db()
    repo = make_repo(db)
    badges = BadgesRepo(db)
    notifier = ConsoleNotifier()
    svc = AchievementsService(repo, badges, notifier, clock())
    log = LoggingService(repo, clock())
    alert_log = AlertLogRepo(db)

    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    svc.evaluate_event("feed", {"method": "breast_left"})

    assert notifier.sent, "the achievement push must still go through Notifier.send"
    assert alert_log.all() == [], "an achievement Finding must never land in alert_log"


def test_no_award_is_ever_removed_or_decremented() -> None:
    log, svc, _ = _build()
    log.log_feed(FeedMethod.BREAST_LEFT, ts=NOW)
    svc.evaluate_event("feed", {"method": "breast_left"})
    before = svc._badges.get_award(1, "engagement.first_feed")
    assert before is not None
    log.undo("feed", 1)
    svc.evaluate_event("feed", {"method": "breast_left"})
    after = svc._badges.get_award(1, "engagement.first_feed")
    assert after is not None
    assert after.count >= before.count
