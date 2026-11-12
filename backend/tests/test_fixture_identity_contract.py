"""Focused unit tests for the pure canonical fixture identity contract."""

from datetime import datetime, timedelta, timezone

import pytest

from backend.services.data.fixture_identity import (
    FixtureIdentityEvidence,
    FixtureSchedule,
    ProviderFixtureID,
    ScheduleRevision,
    append_schedule_revision,
    detect_fixture_duplicate,
    map_legacy_fixture_uid,
    validate_internal_fixture_id,
)


FIXTURE_A = "0190f4d0-7c00-7000-8000-000000000001"
FIXTURE_B = "0190f4d0-7c00-7000-8000-000000000002"
UTC = timezone.utc
T0 = datetime(2026, 8, 21, 19, 0, tzinfo=UTC)


def identity(
    fixture_id=None,
    *,
    provider_ids=(),
    day_offset=0,
    round_id="round-1",
    competition="eng.1",
    home_team_id="team-liv",
    away_team_id="team-ars",
):
    return FixtureIdentityEvidence(
        fixture_id=fixture_id,
        competition_id=competition,
        season_start_year=2026,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        round_id=round_id,
        kickoff_utc=T0 + timedelta(days=day_offset),
        provider_ids=tuple(provider_ids),
    )


def test_internal_fixture_id_requires_canonical_uuid():
    assert validate_internal_fixture_id(FIXTURE_A) == FIXTURE_A
    with pytest.raises(ValueError, match="UUID"):
        validate_internal_fixture_id("espn:12345")
    with pytest.raises(ValueError, match="canonical"):
        validate_internal_fixture_id(FIXTURE_A.upper())


@pytest.mark.parametrize(
    "parts",
    [
        ("ESPN", "eng.1", "123"),
        ("espn", "eng.1", ""),
        ("espn", "eng.1", "123 456"),
        ("espn", "", "123"),
    ],
)
def test_provider_fixture_ids_reject_invalid_namespace_or_value(parts):
    with pytest.raises(ValueError):
        ProviderFixtureID(*parts)


def test_provider_ids_are_namespaced_by_provider_and_competition():
    espn = ProviderFixtureID("espn", "eng.1", "123")
    fotmob = ProviderFixtureID("fotmob", "eng.1", "123")
    other_comp = ProviderFixtureID("espn", "esp.1", "123")
    assert len({espn, fotmob, other_comp}) == 3


def test_legacy_uid_maps_only_when_exactly_one_candidate_exists():
    assert map_legacy_fixture_uid("0123456789abcdef", [FIXTURE_A]).status == "matched"
    ambiguous = map_legacy_fixture_uid("0123456789abcdef", [FIXTURE_B, FIXTURE_A, FIXTURE_A])
    assert ambiguous.status == "ambiguous"
    assert ambiguous.fixture_id is None
    assert ambiguous.candidate_fixture_ids == (FIXTURE_A, FIXTURE_B)
    assert map_legacy_fixture_uid("0123456789abcdef", []).status == "unresolved"


def test_legacy_mapping_rejects_noncanonical_status_shapes():
    from backend.services.data.fixture_identity import LegacyFixtureUIDMapping

    with pytest.raises(ValueError, match="matched mapping"):
        LegacyFixtureUIDMapping("0123456789abcdef", "matched")
    with pytest.raises(ValueError, match="at least two"):
        LegacyFixtureUIDMapping(
            "0123456789abcdef", "ambiguous", candidate_fixture_ids=(FIXTURE_A,)
        )


def test_exact_provider_id_detects_duplicate_across_different_sources_only_if_namespace_matches():
    espn_id = ProviderFixtureID("espn", "eng.1", "evt-77")
    existing = identity(FIXTURE_A, provider_ids=(espn_id,))
    incoming_same = identity(provider_ids=(espn_id,))
    incoming_other_provider = identity(
        provider_ids=(ProviderFixtureID("fotmob", "eng.1", "evt-77"),),
        home_team_id="team-che",
    )
    assert detect_fixture_duplicate(incoming_same, [existing]).status == "duplicate"
    assert detect_fixture_duplicate(incoming_other_provider, [existing]).status == "distinct"


def test_matching_internal_and_provider_ids_remains_a_definite_duplicate():
    provider_id = ProviderFixtureID("espn", "eng.1", "evt-88")
    incoming = identity(FIXTURE_A, provider_ids=(provider_id,))
    existing = identity(FIXTURE_A, provider_ids=(provider_id,))

    result = detect_fixture_duplicate(incoming, [existing])

    assert result.status == "duplicate"
    assert result.fixture_ids == (FIXTURE_A,)


def test_matching_internal_id_with_provider_id_for_another_fixture_is_ambiguous():
    provider_id = ProviderFixtureID("espn", "eng.1", "evt-88")
    incoming = identity(FIXTURE_A, provider_ids=(provider_id,))
    existing = identity(FIXTURE_B, provider_ids=(provider_id,))

    result = detect_fixture_duplicate(incoming, [existing])

    assert result.status == "ambiguous"
    assert set(result.fixture_ids) == {FIXTURE_A, FIXTURE_B}
    assert result.reason == "provider ID conflicts with supplied fixture ID"


def test_matching_internal_id_with_unresolved_provider_mapping_is_ambiguous():
    provider_id = ProviderFixtureID("espn", "eng.1", "evt-88")
    incoming = identity(FIXTURE_A, provider_ids=(provider_id,))
    existing = [
        identity(FIXTURE_A),
        identity(None, provider_ids=(provider_id,)),
    ]

    result = detect_fixture_duplicate(incoming, existing)

    assert result.status == "ambiguous"
    assert result.fixture_ids == ()
    assert result.reason == "provider ID has an unresolved fixture mapping"


def test_matching_internal_id_with_ambiguous_provider_mapping_is_ambiguous():
    provider_id = ProviderFixtureID("espn", "eng.1", "evt-88")
    incoming = identity(FIXTURE_A, provider_ids=(provider_id,))
    existing = [
        identity(FIXTURE_A, provider_ids=(provider_id,)),
        identity(FIXTURE_B, provider_ids=(provider_id,)),
    ]

    result = detect_fixture_duplicate(incoming, existing)

    assert result.status == "ambiguous"
    assert result.fixture_ids == (FIXTURE_A, FIXTURE_B)
    assert result.reason == "provider-qualified ID points to multiple fixtures"


def test_provider_id_reused_in_same_namespace_for_different_fixtures_is_ambiguous():
    shared_id = ProviderFixtureID("espn", "eng.1", "evt-77")
    result = detect_fixture_duplicate(
        identity(provider_ids=(shared_id,)),
        [
            identity(FIXTURE_A, provider_ids=(shared_id,)),
            identity(FIXTURE_B, provider_ids=(shared_id,), home_team_id="team-che"),
        ],
    )
    assert result.status == "ambiguous"
    assert result.fixture_ids == (FIXTURE_A, FIXTURE_B)


def test_matching_canonical_fixture_signature_and_nearby_kickoff_is_duplicate():
    result = detect_fixture_duplicate(identity(), [identity(FIXTURE_A)])
    assert result.status == "duplicate"
    assert result.fixture_ids == (FIXTURE_A,)


def test_competition_and_round_separate_distinct_fixtures():
    assert detect_fixture_duplicate(identity(competition="eng.cup"), [identity(FIXTURE_A)]).status == "distinct"
    later_round = identity(day_offset=21, round_id="round-2")
    assert detect_fixture_duplicate(later_round, [identity(FIXTURE_A)]).status == "distinct"


def test_same_fixture_signature_with_weak_or_conflicting_evidence_is_ambiguous():
    no_time = FixtureIdentityEvidence(
        fixture_id=None,
        competition_id="eng.1",
        season_start_year=2026,
        home_team_id="team-liv",
        away_team_id="team-ars",
        round_id="round-1",
        kickoff_utc=None,
    )
    assert detect_fixture_duplicate(no_time, [identity(FIXTURE_A)]).status == "ambiguous"
    far_date = identity(day_offset=50)
    assert detect_fixture_duplicate(far_date, [identity(FIXTURE_A)]).status == "ambiguous"
    conflicting_id = identity(FIXTURE_B)
    assert detect_fixture_duplicate(conflicting_id, [identity(FIXTURE_A)]).status == "ambiguous"


def test_distinct_provider_id_does_not_override_inconsistent_teams():
    other_pair = FixtureIdentityEvidence(
        fixture_id=FIXTURE_A,
        competition_id="eng.1",
        season_start_year=2026,
        home_team_id="team-che",
        away_team_id="team-ars",
        provider_ids=(ProviderFixtureID("espn", "eng.1", "evt-77"),),
    )
    incoming = identity(provider_ids=(ProviderFixtureID("espn", "eng.1", "evt-77"),))
    result = detect_fixture_duplicate(incoming, [other_pair])
    assert result.status == "ambiguous"
    assert result.reason == "provider ID matches but fixture facts conflict"


def test_schedule_revisions_are_immutable_ordered_and_retain_old_kickoff():
    schedule = FixtureSchedule(FIXTURE_A)
    first = append_schedule_revision(
        schedule, effective_at_utc=T0 - timedelta(days=30), kickoff_utc=T0, status="scheduled"
    )
    moved_kickoff = T0 + timedelta(days=7)
    second = append_schedule_revision(
        first,
        effective_at_utc=T0 - timedelta(days=2),
        kickoff_utc=moved_kickoff,
        status="scheduled",
    )
    assert len(first.revisions) == 1
    assert second.revisions[0].kickoff_utc == T0
    assert second.revisions[1].kickoff_utc == moved_kickoff
    assert second.revisions[1].revision == 2


def test_schedule_revision_idempotence_and_terminal_status_rules():
    schedule = FixtureSchedule(FIXTURE_A)
    first = append_schedule_revision(
        schedule, effective_at_utc=T0, kickoff_utc=T0 + timedelta(days=1), status="scheduled"
    )
    assert append_schedule_revision(
        first,
        effective_at_utc=T0 + timedelta(days=1),
        kickoff_utc=T0 + timedelta(days=1),
        status="scheduled",
    ) is first
    cancelled = append_schedule_revision(
        first, effective_at_utc=T0 + timedelta(days=1), kickoff_utc=None, status="cancelled"
    )
    with pytest.raises(ValueError, match="cancelled fixture"):
        append_schedule_revision(
            cancelled, effective_at_utc=T0 + timedelta(days=2), kickoff_utc=T0, status="scheduled"
        )


def test_schedule_contract_rejects_non_utc_or_nonmonotonic_revisions():
    with pytest.raises(ValueError, match="UTC"):
        ScheduleRevision(1, T0.replace(tzinfo=None), T0, "scheduled")
    first = append_schedule_revision(
        FixtureSchedule(FIXTURE_A), effective_at_utc=T0, kickoff_utc=T0, status="scheduled"
    )
    with pytest.raises(ValueError, match="increase"):
        append_schedule_revision(
            first, effective_at_utc=T0, kickoff_utc=T0 + timedelta(days=1), status="postponed"
        )
