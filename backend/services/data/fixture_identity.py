"""Pure contracts for canonical football-fixture identity.

This module deliberately performs no I/O and has no warehouse/provider imports.
It validates identity evidence and returns reconciliation decisions; callers
remain responsible for persisting only reviewed mappings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID


LegacyMappingStatus = Literal["matched", "ambiguous", "unresolved"]
DuplicateStatus = Literal["duplicate", "ambiguous", "distinct"]
FixtureStatus = Literal[
    "scheduled", "postponed", "abandoned", "cancelled", "result_available", "unknown"
]

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9._-]{0,62}$")
_COMPETITION_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
_LEGACY_UID_RE = re.compile(r"^[0-9a-f]{16}$")
_SAFE_OPAQUE_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$")


def _require_text(value: object, label: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string")
    if pattern is not None and not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def validate_internal_fixture_id(value: str) -> str:
    """Return a canonical UUID string; reject names, provider IDs, and malformed UUIDs."""
    _require_text(value, "internal fixture ID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("internal fixture ID must be a UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("internal fixture ID must use canonical UUID formatting")
    return canonical


@dataclass(frozen=True, order=True)
class ProviderFixtureID:
    """A fixture ID scoped to its provider and competition namespace."""

    provider: str
    competition_id: str
    provider_id: str

    def __post_init__(self) -> None:
        _require_text(self.provider, "provider", _PROVIDER_RE)
        _require_text(self.competition_id, "competition ID", _COMPETITION_RE)
        _require_text(self.provider_id, "provider fixture ID", _SAFE_OPAQUE_ID_RE)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.provider, self.competition_id, self.provider_id


@dataclass(frozen=True)
class LegacyFixtureUIDMapping:
    """A non-destructive mapping from a legacy 16-hex UID to a canonical ID."""

    legacy_uid: str
    status: LegacyMappingStatus
    fixture_id: str | None = None
    candidate_fixture_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.legacy_uid, "legacy fixture UID", _LEGACY_UID_RE)
        candidates = tuple(validate_internal_fixture_id(x) for x in self.candidate_fixture_ids)
        if len(set(candidates)) != len(candidates):
            raise ValueError("candidate fixture IDs must be unique")
        object.__setattr__(self, "candidate_fixture_ids", candidates)
        mapped = (
            validate_internal_fixture_id(self.fixture_id)
            if self.fixture_id is not None
            else None
        )
        object.__setattr__(self, "fixture_id", mapped)

        if self.status == "matched":
            if mapped is None or candidates:
                raise ValueError("matched mapping requires one fixture_id and no candidates")
        elif self.status == "ambiguous":
            if mapped is not None or len(candidates) < 2:
                raise ValueError("ambiguous mapping requires at least two candidates and no fixture_id")
        elif self.status == "unresolved":
            if mapped is not None or candidates:
                raise ValueError("unresolved mapping cannot contain a fixture association")
        else:
            raise ValueError("unknown legacy mapping status")


def map_legacy_fixture_uid(
    legacy_uid: str, candidate_fixture_ids: tuple[str, ...] | list[str]
) -> LegacyFixtureUIDMapping:
    """Classify candidates without selecting among conflicts or mutating history."""
    candidates = tuple(sorted({validate_internal_fixture_id(x) for x in candidate_fixture_ids}))
    if len(candidates) == 1:
        return LegacyFixtureUIDMapping(legacy_uid, "matched", fixture_id=candidates[0])
    if len(candidates) > 1:
        return LegacyFixtureUIDMapping(
            legacy_uid, "ambiguous", candidate_fixture_ids=candidates
        )
    return LegacyFixtureUIDMapping(legacy_uid, "unresolved")


@dataclass(frozen=True)
class FixtureIdentityEvidence:
    """Identity facts used for conservative duplicate reconciliation."""

    fixture_id: str | None
    competition_id: str
    season_start_year: int
    home_team_id: str
    away_team_id: str
    round_id: str | None = None
    kickoff_utc: datetime | None = None
    provider_ids: tuple[ProviderFixtureID, ...] = ()

    def __post_init__(self) -> None:
        if self.fixture_id is not None:
            object.__setattr__(self, "fixture_id", validate_internal_fixture_id(self.fixture_id))
        _require_text(self.competition_id, "competition ID", _COMPETITION_RE)
        if type(self.season_start_year) is not int or not 1800 <= self.season_start_year <= 2200:
            raise ValueError("season_start_year must be an integer year")
        _require_text(self.home_team_id, "home team ID", _SAFE_OPAQUE_ID_RE)
        _require_text(self.away_team_id, "away team ID", _SAFE_OPAQUE_ID_RE)
        if self.home_team_id == self.away_team_id:
            raise ValueError("home and away team IDs must differ")
        if self.round_id is not None:
            _require_text(self.round_id, "round ID", _SAFE_OPAQUE_ID_RE)
        if self.kickoff_utc is not None:
            _require_utc(self.kickoff_utc, "kickoff_utc")
        if len(set(self.provider_ids)) != len(self.provider_ids):
            raise ValueError("provider IDs must be unique within fixture evidence")


@dataclass(frozen=True)
class DuplicateDecision:
    status: DuplicateStatus
    fixture_ids: tuple[str, ...] = ()
    reason: str = ""


def detect_fixture_duplicate(
    incoming: FixtureIdentityEvidence,
    existing: tuple[FixtureIdentityEvidence, ...] | list[FixtureIdentityEvidence],
    *,
    kickoff_tolerance: timedelta = timedelta(hours=24),
) -> DuplicateDecision:
    """Find a definite duplicate or fail closed when fixture identity is ambiguous.

    Exact provider-qualified IDs take precedence. Without them, a duplicate needs
    matching competition, season, ordered canonical teams, round and nearby UTC
    kickoff. A same-pair/same-round candidate with weak timing evidence is
    ambiguous rather than silently merged.
    """
    if not isinstance(kickoff_tolerance, timedelta) or kickoff_tolerance < timedelta(0):
        raise ValueError("kickoff_tolerance must be a non-negative timedelta")
    known = list(existing)
    provider_keys = {provider_id.key for provider_id in incoming.provider_ids}
    provider_matches = [
        item
        for item in known
        if provider_keys.intersection(provider_id.key for provider_id in item.provider_ids)
    ]
    if provider_matches:
        ids = tuple(sorted({item.fixture_id for item in provider_matches if item.fixture_id}))
        if any(item.fixture_id is None for item in provider_matches):
            return DuplicateDecision("ambiguous", ids, "provider ID has an unresolved fixture mapping")
        conflicting = [
            item
            for item in provider_matches
            if (item.competition_id, item.season_start_year, item.home_team_id, item.away_team_id)
            != (incoming.competition_id, incoming.season_start_year, incoming.home_team_id, incoming.away_team_id)
            or (item.round_id is not None and incoming.round_id is not None and item.round_id != incoming.round_id)
        ]
        if conflicting:
            return DuplicateDecision(
                "ambiguous", ids, "provider ID matches but fixture facts conflict"
            )
        if len(ids) == 1:
            if incoming.fixture_id is not None and incoming.fixture_id != ids[0]:
                return DuplicateDecision("ambiguous", ids + (incoming.fixture_id,), "provider ID conflicts with supplied fixture ID")
            return DuplicateDecision("duplicate", ids, "same provider-qualified fixture ID")
        return DuplicateDecision("ambiguous", ids, "provider-qualified ID points to multiple fixtures")

    # Provider identity has now had a chance to disprove the supplied internal
    # ID. Only use the internal ID as a duplicate signal when no provider mapping
    # contradicts or ambiguously qualifies it.
    same_internal_id = [
        item for item in known
        if incoming.fixture_id is not None and item.fixture_id == incoming.fixture_id
    ]
    if same_internal_id:
        ids = (incoming.fixture_id,)
        if any(
            (item.competition_id, item.season_start_year, item.home_team_id, item.away_team_id)
            != (incoming.competition_id, incoming.season_start_year, incoming.home_team_id, incoming.away_team_id)
            for item in same_internal_id
        ):
            return DuplicateDecision("ambiguous", ids, "internal fixture ID conflicts with fixture facts")
        return DuplicateDecision("duplicate", ids, "same internal fixture ID")

    candidate_hits: set[str] = set()
    weak_hits: set[str] = set()
    unresolved_weak_hit = False
    for item in known:
        if (
            item.competition_id != incoming.competition_id
            or item.season_start_year != incoming.season_start_year
            or item.home_team_id != incoming.home_team_id
            or item.away_team_id != incoming.away_team_id
        ):
            continue
        if incoming.round_id and item.round_id and incoming.round_id != item.round_id:
            continue
        if incoming.kickoff_utc is None or item.kickoff_utc is None:
            if item.fixture_id is None:
                unresolved_weak_hit = True
            else:
                weak_hits.add(item.fixture_id)
            continue
        if abs(incoming.kickoff_utc - item.kickoff_utc) <= kickoff_tolerance:
            if item.fixture_id is None:
                unresolved_weak_hit = True
            else:
                candidate_hits.add(item.fixture_id)
        else:
            # A large time gap could be either a reschedule or a repeat meeting.
            # Different explicit rounds were already excluded above.
            if item.fixture_id is None:
                unresolved_weak_hit = True
            else:
                weak_hits.add(item.fixture_id)

    if len(candidate_hits) == 1 and not weak_hits and not unresolved_weak_hit:
        hit = next(iter(candidate_hits))
        if incoming.fixture_id is not None and incoming.fixture_id != hit:
            return DuplicateDecision("ambiguous", tuple(sorted((hit, incoming.fixture_id))), "fixture signature conflicts with supplied fixture ID")
        return DuplicateDecision("duplicate", (hit,), "canonical fixture signature and kickoff agree")
    possible = candidate_hits | weak_hits
    if possible or unresolved_weak_hit:
        return DuplicateDecision("ambiguous", tuple(sorted(possible)), "fixture evidence is not unique")
    return DuplicateDecision("distinct", (), "no matching fixture evidence")


ScheduleStatus = FixtureStatus


@dataclass(frozen=True)
class ScheduleRevision:
    revision: int
    effective_at_utc: datetime
    kickoff_utc: datetime | None
    status: ScheduleStatus

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("schedule revision must be a positive integer")
        _require_utc(self.effective_at_utc, "effective_at_utc")
        if self.kickoff_utc is not None:
            _require_utc(self.kickoff_utc, "kickoff_utc")
        if self.status not in {"scheduled", "postponed", "abandoned", "cancelled", "result_available", "unknown"}:
            raise ValueError("invalid fixture status")
        if self.status == "scheduled" and self.kickoff_utc is None:
            raise ValueError("scheduled fixture requires kickoff_utc")


@dataclass(frozen=True)
class FixtureSchedule:
    fixture_id: str
    revisions: tuple[ScheduleRevision, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "fixture_id", validate_internal_fixture_id(self.fixture_id))
        revisions = tuple(self.revisions)
        for index, revision in enumerate(revisions, start=1):
            if revision.revision != index:
                raise ValueError("schedule revisions must be contiguous from 1")
            if index > 1 and revision.effective_at_utc <= revisions[index - 2].effective_at_utc:
                raise ValueError("schedule revision effective times must increase")
            if index > 1:
                previous = revisions[index - 2]
                if previous.status == "result_available" and revision.status != "result_available":
                    raise ValueError("a completed fixture cannot return to a non-result status")
                if previous.status == "cancelled" and revision.status != "cancelled":
                    raise ValueError("a cancelled fixture cannot be silently reopened")
                if previous.status == "abandoned" and revision.status not in {"abandoned", "result_available"}:
                    raise ValueError("an abandoned fixture can only remain abandoned or receive a result")
        object.__setattr__(self, "revisions", revisions)


def append_schedule_revision(
    schedule: FixtureSchedule,
    *,
    effective_at_utc: datetime,
    kickoff_utc: datetime | None,
    status: ScheduleStatus,
) -> FixtureSchedule:
    """Return an updated immutable schedule; exact repeated observations are idempotent."""
    _require_utc(effective_at_utc, "effective_at_utc")
    if schedule.revisions:
        latest = schedule.revisions[-1]
        if effective_at_utc <= latest.effective_at_utc:
            raise ValueError("schedule revision effective times must increase")
        if latest.kickoff_utc == kickoff_utc and latest.status == status:
            return schedule
    revision = ScheduleRevision(
        revision=len(schedule.revisions) + 1,
        effective_at_utc=effective_at_utc,
        kickoff_utc=kickoff_utc,
        status=status,
    )
    return FixtureSchedule(schedule.fixture_id, schedule.revisions + (revision,))


def _require_utc(value: datetime, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be timezone-aware UTC")
