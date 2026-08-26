"""Task U42: achievement catalog + award repository round-trips."""

from _helpers import NOW, make_db

from cradle.models import AchievementDef, AchievementSource, Rarity, RuleType
from cradle.repos.badges_repo import BadgesRepo

_DEF = AchievementDef(
    key="test.badge",
    name="Test Badge",
    description="A badge for testing.",
    rarity=Rarity.COMMON,
    rule_type=RuleType.COUNT,
    domain="feed",
    threshold=3,
    icon="🏆",
    source=AchievementSource.PREDEFINED,
)


def test_seed_predefined_is_idempotent() -> None:
    """Re-seeding on every app start must never insert a duplicate row nor
    raise, even after an award already exists against the key."""
    repo = BadgesRepo(make_db())
    repo.seed_predefined([_DEF])
    repo.seed_predefined([_DEF])
    defs = repo.list_definitions()
    assert [d.key for d in defs] == ["test.badge"]


def test_seed_predefined_never_overwrites_an_existing_award() -> None:
    repo = BadgesRepo(make_db())
    repo.seed_predefined([_DEF])
    repo.record_award(1, "test.badge", NOW)
    repo.seed_predefined([_DEF])  # simulates a second app start
    award = repo.get_award(1, "test.badge")
    assert award is not None
    assert award.count == 1


def test_list_definitions_round_trips_every_field() -> None:
    repo = BadgesRepo(make_db())
    d = AchievementDef(
        key="test.full",
        name="Full Badge",
        description="Every field set.",
        rarity=Rarity.LEGENDARY,
        rule_type=RuleType.SINGLE,
        domain="nappy",
        field="kind",
        match_value="dirty",
        threshold=5,
        repeatable=True,
        icon="🌟",
        source=AchievementSource.CUSTOM,
        celebrate_every=(5, 10),
    )
    repo.insert_custom(d)
    (got,) = repo.list_definitions()
    assert got == d


def test_get_definition_returns_none_for_unknown_key() -> None:
    repo = BadgesRepo(make_db())
    assert repo.get_definition("nope") is None


def test_insert_custom_rejects_duplicate_key() -> None:
    import sqlite3

    repo = BadgesRepo(make_db())
    repo.insert_custom(_DEF)
    try:
        repo.insert_custom(_DEF)
        raised = False
    except sqlite3.IntegrityError:
        raised = True
    assert raised, "a second custom achievement with the same key must be rejected"


def test_record_award_inserts_then_increments_the_same_row() -> None:
    """UNIQUE(baby_id, badge_key): the same badge can never produce a second
    award row - a repeat qualifying event updates count/last_awarded_at on
    the existing row instead."""
    repo = BadgesRepo(make_db())
    repo.seed_predefined([_DEF])

    first = repo.record_award(1, "test.badge", NOW)
    assert first.count == 1
    assert first.first_awarded_at == NOW
    assert first.last_awarded_at == NOW

    later = NOW.replace(hour=NOW.hour + 1) if NOW.hour < 23 else NOW
    second = repo.record_award(1, "test.badge", later)
    assert second.count == 2
    assert second.first_awarded_at == NOW, "first_awarded_at must never move"
    assert second.last_awarded_at == later

    assert len(repo.list_awards(1)) == 1, (
        "a repeat award must update the existing row, never insert a second"
    )


def test_record_award_increment_amount_is_configurable() -> None:
    repo = BadgesRepo(make_db())
    repo.seed_predefined([_DEF])
    repo.record_award(1, "test.badge", NOW, increment=1)
    award = repo.record_award(1, "test.badge", NOW, increment=1)
    assert award.count == 2


def test_get_award_returns_none_before_any_award() -> None:
    repo = BadgesRepo(make_db())
    repo.seed_predefined([_DEF])
    assert repo.get_award(1, "test.badge") is None


def test_list_awards_keys_by_badge_key() -> None:
    repo = BadgesRepo(make_db())
    other = AchievementDef(
        key="test.other",
        name="Other",
        description="",
        rarity=Rarity.COMMON,
        rule_type=RuleType.MANUAL,
    )
    repo.seed_predefined([_DEF, other])
    repo.record_award(1, "test.badge", NOW)
    repo.record_award(1, "test.other", NOW)
    awards = repo.list_awards(1)
    assert set(awards) == {"test.badge", "test.other"}
