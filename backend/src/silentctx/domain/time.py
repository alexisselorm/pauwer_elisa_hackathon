"""The time rules R1-R10 (plan 001 §3.3) as pure functions. No I/O, no clock reads.

Conventions:
- Every instant is timezone-aware UTC (`UtcDatetime`). Validity windows are half-open:
  `valid_from <= t < valid_to`, and `valid_to = None` means "still believed true".
- A calendar date stated in text ("on 1 December", "until 20 November") means
  00:00 Europe/Helsinki on that day. As a `valid_to` it is exclusive: "until 20 November"
  ends when 20 November begins (eval/heldout H-03).
- `ingested_at` (wall-clock at ingestion) is deliberately absent from this module: it is
  never an input to temporal logic (R1).
"""

from __future__ import annotations

import email.utils
import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Self
from zoneinfo import ZoneInfo

from dateparser.date import DateDataParser
from dateparser.search import search_dates
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from silentctx.domain.models import AssertionStatus, Platform, UtcDatetime

HELSINKI = ZoneInfo("Europe/Helsinki")
PM_INTERVAL = timedelta(minutes=15)
_UTC_ADAPTER: TypeAdapter[datetime] = TypeAdapter(UtcDatetime)


class TemporalError(ValueError):
    """An operation would break a temporal invariant (R2-R7)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def ensure_utc(value: datetime) -> datetime:
    """Validate an aware datetime and normalise it to UTC. Naive datetimes are rejected."""
    return _UTC_ADAPTER.validate_python(value)


# ----------------------------------------------------------------------------------------
# R1: event_time comes from the platform


_SLACK_TS = re.compile(r"^(\d{9,11})\.(\d{6})$")


def slack_ts_to_datetime(ts: str) -> datetime:
    """Slack `ts` ("1760422928.123456") to UTC, keeping the microseconds exactly (no float)."""
    match = _SLACK_TS.match(ts)
    if not match:
        raise ValueError(f"not a Slack ts: {ts!r}")
    seconds, micros = match.groups()
    return datetime.fromtimestamp(int(seconds), tz=UTC) + timedelta(microseconds=int(micros))


def datetime_to_slack_ts(value: datetime) -> str:
    """Inverse of `slack_ts_to_datetime`."""
    utc = ensure_utc(value)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = utc - epoch
    return f"{delta.days * 86400 + delta.seconds}.{delta.microseconds:06d}"


def email_date_to_datetime(header: str) -> datetime:
    """RFC 5322 `Date:` header to UTC. A `-0000` zone (time is UTC, sender zone unknown)
    is taken as UTC, as RFC 5322 §3.3 defines it."""
    parsed = email.utils.parsedate_to_datetime(header)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def pm_interval_start(sample_time: datetime, interval: timedelta = PM_INTERVAL) -> datetime:
    """The interval a PM sample belongs to. Samples are stamped at the interval START."""
    utc = ensure_utc(sample_time)
    step = int(interval.total_seconds())
    epoch_s = int(utc.timestamp())
    return datetime.fromtimestamp(epoch_s - epoch_s % step, tz=UTC)


def last_complete_pm_interval(
    at: datetime, interval: timedelta = PM_INTERVAL
) -> tuple[datetime, datetime]:
    """The last PM interval that ENDED at or before `at` (gate V4). An interval that is still
    open may already contain post-fix traffic, so it is never used.
    06:21:30Z -> (06:00Z, 06:15Z)."""
    end = pm_interval_start(at, interval)
    return end - interval, end


class EventTime(_Frozen):
    """The single timestamp of an event (R1), with the platform's own sequence for ties (R7)."""

    event_id: str = Field(min_length=1)
    platform: Platform
    event_time: UtcDatetime
    # Platform order within one instant: Slack ts string, Teams created_iid, alarm seq.
    platform_seq: str | None = None


# ----------------------------------------------------------------------------------------
# R7: ordering


def event_order_key(event: EventTime) -> tuple[datetime, str, str]:
    """Order by event_time, then platform sequence, then id (total and deterministic).
    Slack ts strings have a fixed width until 2286, so they sort correctly as text."""
    return (event.event_time, event.platform_seq or "", event.event_id)


def sort_events(events: Iterable[EventTime]) -> list[EventTime]:
    return sorted(events, key=event_order_key)


def check_thread_order(events: Sequence[EventTime]) -> None:
    """Messages in a thread must be strictly increasing (R7). Raises TemporalError."""
    for earlier, later in itertools.pairwise(events):
        if event_order_key(later)[:2] <= event_order_key(earlier)[:2]:
            raise TemporalError(
                f"thread order broken: {later.event_id} is not after {earlier.event_id}"
            )


# ----------------------------------------------------------------------------------------
# R2: recorded_at is derived from the event stream


def recorded_at(source_events: Iterable[EventTime]) -> datetime:
    """The event_time of the event that completes the assertion: the last one it derives from."""
    ordered = sort_events(source_events)
    if not ordered:
        raise TemporalError("an assertion needs at least one source event")
    return ordered[-1].event_time


# ----------------------------------------------------------------------------------------
# R3 / R4a: dates stated in the text


class Tense(StrEnum):
    """Which way an underspecified date points from the message ("Monday", "3 August").
    Given by extraction from the wording: "installed on 3 August" is past, "from Monday" future."""

    PAST = "past"
    FUTURE = "future"


class StatedTime(_Frozen):
    """A verbatim time expression quoted from the source, e.g. "from Monday"."""

    phrase: str = Field(min_length=1)
    tense: Tense


class Resolution(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"  # no date found in the phrase
    AMBIGUOUS = "ambiguous"  # more than one date found


class ResolvedTime(_Frozen):
    resolution: Resolution
    value: UtcDatetime | None = None
    matched_text: str | None = None
    # True when the phrase named a time of day ("Monday 08:00"); else 00:00 local was used.
    has_time_of_day: bool = False


# Finnish short dates ("1.12.") read as clock times otherwise; "D.M." always has a trailing dot.
_FINNISH_DAY_MONTH = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(?!\d)")
_LANGUAGES = ["en", "fi"]


def _parser_settings(base_local: datetime, tense: Tense) -> dict[str, Any]:
    return {
        "RELATIVE_BASE": base_local,
        "PREFER_DATES_FROM": tense.value,
        "RETURN_TIME_AS_PERIOD": True,
        "DATE_ORDER": "DMY",
        "PREFER_DAY_OF_MONTH": "first",
        "RETURN_AS_TIMEZONE_AWARE": False,
    }


def resolve_stated_time(phrase: str, event_time: datetime, tense: Tense) -> ResolvedTime:
    """Resolve a stated date against the source's event_time in Europe/Helsinki (R3, R4a).

    Uses `dateparser`: `search_dates` finds the date inside the phrase ("from Monday the
    Pori room"), then `DateDataParser` re-parses that match to learn whether a time of day
    was given. A date without a time of day means 00:00 Helsinki on that day.
    """
    base_local = ensure_utc(event_time).astimezone(HELSINKI).replace(tzinfo=None)
    settings = _parser_settings(base_local, tense)
    text = _FINNISH_DAY_MONTH.sub(r"\1/\2", phrase)

    found = search_dates(text, languages=_LANGUAGES, settings=settings) or []
    if not found:
        return ResolvedTime(resolution=Resolution.UNRESOLVED)
    if len(found) > 1:
        return ResolvedTime(resolution=Resolution.AMBIGUOUS, matched_text=text)

    matched, _ = found[0]
    data = DateDataParser(languages=_LANGUAGES, settings=settings).get_date_data(matched)
    if data.date_obj is None:  # pragma: no cover - search_dates just parsed the same text
        return ResolvedTime(resolution=Resolution.UNRESOLVED)

    has_time = data.period == "time"
    local = data.date_obj if has_time else datetime.combine(data.date_obj.date(), time.min)
    value = local.replace(tzinfo=HELSINKI).astimezone(UTC)
    return ResolvedTime(
        resolution=Resolution.RESOLVED,
        value=value,
        matched_text=matched,
        has_time_of_day=has_time,
    )


class ValidFrom(_Frozen):
    valid_from: UtcDatetime
    from_stated_time: bool  # True: the text gave the start; False: = recorded_at
    clamped: bool = False  # moved up to the bound entity's own valid_from
    unresolved_phrase: str | None = None  # a phrase was given but no single date was found


def resolve_valid_from(
    recorded: datetime,
    stated: StatedTime | None = None,
    *,
    event_time: datetime | None = None,
    entity_valid_from: datetime | None = None,
) -> ValidFrom:
    """R3. `valid_from` = the stated start, resolved against the source's `event_time`
    (defaults to `recorded`), else `recorded_at`. Never extraction time. A start before the
    bound entity existed is clamped to the entity's `valid_from` and flagged."""
    recorded = ensure_utc(recorded)
    value = recorded
    from_stated = False
    unresolved: str | None = None
    if stated is not None:
        resolved = resolve_stated_time(stated.phrase, event_time or recorded, stated.tense)
        if resolved.value is not None:
            value, from_stated = resolved.value, True
        else:
            unresolved = stated.phrase

    clamped = False
    if entity_valid_from is not None and value < ensure_utc(entity_valid_from):
        value, clamped = ensure_utc(entity_valid_from), True
    return ValidFrom(
        valid_from=value,
        from_stated_time=from_stated,
        clamped=clamped,
        unresolved_phrase=unresolved,
    )


def resolve_stated_expiry(
    stated: StatedTime, event_time: datetime, valid_from: datetime
) -> datetime:
    """R4a. "until 20 November" -> 00:00 Helsinki on 20 November (exclusive end).
    Raises TemporalError when no single date is found or it doesn't end after valid_from."""
    resolved = resolve_stated_time(stated.phrase, event_time, stated.tense)
    if resolved.value is None:
        raise TemporalError(f"no single date in expiry phrase {stated.phrase!r}")
    if resolved.value <= ensure_utc(valid_from):
        raise TemporalError(
            f"expiry {resolved.value.isoformat()} is not after valid_from {valid_from.isoformat()}"
        )
    return resolved.value


# ----------------------------------------------------------------------------------------
# The temporal state of an assertion version, and R4-R6 transitions


class TemporalState(_Frozen):
    """The time-bearing fields of one assertion version (plan 001 §5)."""

    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None
    recorded_at: UtcDatetime
    retracted_at: UtcDatetime | None = None
    status: AssertionStatus = AssertionStatus.UNVERIFIED
    review_due: bool = False

    @model_validator(mode="after")
    def _invariants(self) -> Self:
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from (half-open window)")
        if self.retracted_at is not None and self.retracted_at < self.recorded_at:
            raise ValueError("retracted_at can't be before recorded_at")
        if self.status is AssertionStatus.SUPERSEDED and self.valid_to is None:
            raise ValueError("a superseded version needs valid_to = its successor's valid_from")
        if self.status is AssertionStatus.EXPIRED and self.valid_to is None:
            raise ValueError("an expired version needs valid_to")
        return self

    def replace(self, **changes: Any) -> TemporalState:
        """A validated copy (pydantic's model_copy(update=...) skips validation)."""
        return TemporalState.model_validate({**self.model_dump(), **changes})


def supersede(predecessor: TemporalState, successor: TemporalState) -> TemporalState:
    """R4b / R6(i), a change in the world: pred.valid_to = succ.valid_from ("superseded
    timestamp"), status superseded. Returns the new predecessor version."""
    start = successor.valid_from
    if start <= predecessor.valid_from:
        raise TemporalError(
            "a successor must start after its predecessor; a fix to the same window is a "
            "correction (R6 ii), not a supersession"
        )
    if predecessor.valid_to is not None and predecessor.valid_to < start:
        raise TemporalError("the predecessor had already ended; nothing to supersede")
    return predecessor.replace(valid_to=start, status=AssertionStatus.SUPERSEDED)


def correct(wrong: TemporalState, correction_time: datetime) -> tuple[TemporalState, TemporalState]:
    """R6(ii), an extraction mistake corrected by a person. The wrong version is retracted at
    the correction's event_time; the fixed version KEEPS the original valid_from (and
    valid_to) and is recorded at the correction. Returns (retracted, fixed)."""
    at = ensure_utc(correction_time)
    if wrong.retracted_at is not None:
        raise TemporalError("this version is already retracted")
    retracted = wrong.replace(retracted_at=at)
    fixed = TemporalState(
        valid_from=wrong.valid_from,
        valid_to=wrong.valid_to,
        recorded_at=at,
        status=wrong.status,
        review_due=False,
    )
    return retracted, fixed


class Expiry(_Frozen):
    valid_to: UtcDatetime | None
    expired: bool


def decommission_expiry(
    state: TemporalState, observed_entity_ends: Sequence[datetime | None]
) -> Expiry:
    """R4c. If EVERY observed entity the assertion is bound to has ended, it ends when the
    last of them did, and becomes expired. Model- or class-level bindings are not passed
    here: they don't end with one device (plan 001 §6.3)."""
    if not observed_entity_ends or any(end is None for end in observed_entity_ends):
        return Expiry(valid_to=state.valid_to, expired=False)
    last_end = max(ensure_utc(end) for end in observed_entity_ends if end is not None)
    new_end = last_end if state.valid_to is None else min(state.valid_to, last_end)
    return Expiry(valid_to=new_end, expired=True)


def apply_doubt(
    state: TemporalState,
    *,
    status: AssertionStatus | None = None,
    review_due: bool | None = None,
) -> TemporalState:
    """R5. Doubt (a failed fix, a disagreeing expert, a firmware change) changes status or
    review_due, never the validity window. Superseded/expired are not doubt statuses."""
    if status in (AssertionStatus.SUPERSEDED, AssertionStatus.EXPIRED):
        raise TemporalError(f"{status} is a validity change, not doubt; use supersede/expiry")
    changes: dict[str, Any] = {}
    if status is not None:
        changes["status"] = status
    if review_due is not None:
        changes["review_due"] = review_due
    return state.replace(**changes)


# ----------------------------------------------------------------------------------------
# R8: the Elisa example backfill

ELISA_BACKFILL_DATE = date(2025, 10, 14)
# Elisa's chat gives HH:MM only. Synthetic seconds keep messages in the same minute ordered;
# they are the values used in mock_data (teams/messages/chan-noc-transport.jsonl).
ELISA_SYNTHETIC_SECONDS: Mapping[int, int] = MappingProxyType(
    {1: 5, 2: 12, 3: 41, 4: 3, 5: 37, 6: 22, 7: 30, 8: 8, 9: 4, 10: 39, 11: 15, 12: 2}
)
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def elisa_backfill_time(seq: int, local_hhmm: str) -> datetime:
    """R8. Message `seq` of Elisa's example at local "HH:MM" -> UTC on 2025-10-14
    (Europe/Helsinki, EEST). Message 8 at 09:22 -> 2025-10-14T06:22:08Z."""
    match = _HHMM.match(local_hhmm)
    if not match:
        raise ValueError(f"expected HH:MM, got {local_hhmm!r}")
    if seq not in ELISA_SYNTHETIC_SECONDS:
        raise ValueError(f"Elisa's example has messages 1-12, not {seq}")
    hour, minute = int(match.group(1)), int(match.group(2))
    local = datetime.combine(
        ELISA_BACKFILL_DATE, time(hour, minute, ELISA_SYNTHETIC_SECONDS[seq]), tzinfo=HELSINKI
    )
    return local.astimezone(UTC)


# ----------------------------------------------------------------------------------------
# R9 / R10: as-of queries


class EntityWindow(_Frozen):
    """An entity's existence window in the design/as-built/as-running layers."""

    entity_id: str = Field(min_length=1)
    valid_from: UtcDatetime
    valid_to: UtcDatetime | None = None


def window_contains(valid_from: datetime, valid_to: datetime | None, t: datetime) -> bool:
    """valid_from <= t < coalesce(valid_to, infinity)."""
    t = ensure_utc(t)
    return ensure_utc(valid_from) <= t and (valid_to is None or t < ensure_utc(valid_to))


def known_at(state: TemporalState, known: datetime) -> bool:
    """R10 (our extension). The version was on record at `known` and not yet retracted."""
    k = ensure_utc(known)
    return state.recorded_at <= k and (state.retracted_at is None or state.retracted_at > k)


def qualifiers_match(
    required: Mapping[str, str | Sequence[str]], snapshot: Mapping[str, str]
) -> bool:
    """Every qualifier the assertion requires (model, generation, firmware) equals the
    as-running snapshot's value, or one of them for a list (case-insensitive). No
    requirements -> matches."""
    for key, value in required.items():
        allowed = {value} if isinstance(value, str) else set(value)
        if key not in snapshot or snapshot[key].casefold() not in {
            str(v).casefold() for v in allowed
        }:
            return False
    return True


def in_scope_at(
    state: TemporalState,
    t: datetime,
    entities: Sequence[EntityWindow],
    *,
    required_qualifiers: Mapping[str, str] | None = None,
    snapshot: Mapping[str, str] | None = None,
    known: datetime | None = None,
) -> bool:
    """R9: in scope at t <=> valid_from <= t < coalesce(valid_to, inf), a bound entity exists
    at t, and the qualifiers match the as-running snapshot at t.
    R10: with `known`, only versions on record at `known` count, and a version retracted
    later still counts (it was believed then). Without `known`, retracted versions never count.
    """
    if known is None:
        if state.retracted_at is not None:
            return False
    elif not known_at(state, known):
        return False
    if not window_contains(state.valid_from, state.valid_to, t):
        return False
    if not any(window_contains(e.valid_from, e.valid_to, t) for e in entities):
        return False
    return qualifiers_match(required_qualifiers or {}, snapshot or {})
