"""Time rules R1-R10 (plan 001 §3.3). Test names carry the rule number; plan 003 cites them."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from silentctx.domain.models import AssertionStatus, Platform
from silentctx.domain.time import (
    ELISA_SYNTHETIC_SECONDS,
    EntityWindow,
    EventTime,
    Resolution,
    StatedTime,
    TemporalError,
    TemporalState,
    Tense,
    apply_doubt,
    check_thread_order,
    correct,
    datetime_to_slack_ts,
    decommission_expiry,
    elisa_backfill_time,
    email_date_to_datetime,
    ensure_utc,
    in_scope_at,
    known_at,
    last_complete_pm_interval,
    pm_interval_start,
    qualifiers_match,
    recorded_at,
    resolve_stated_expiry,
    resolve_stated_time,
    resolve_valid_from,
    slack_ts_to_datetime,
    sort_events,
    supersede,
)

REPO = Path(__file__).parents[3]
ELISA_CHAT = REPO / "Elisa_SinceAI2026" / "ELISA_HIDDEN_KNOWLEDGE_EXAMPLE_2026" / "teams_chat.json"
CORPUS_CHAT = REPO / "mock_data" / "teams" / "messages" / "chan-noc-transport.jsonl"
ELISA_THREAD = "19:71fe3c060dcfcf4e0874a4bf@thread.v2"


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ev(event_id: str, t: str, seq: str | None = None) -> EventTime:
    return EventTime(
        event_id=event_id, platform=Platform.SLACK, event_time=utc(t), platform_seq=seq
    )


# --- R1 -----------------------------------------------------------------------------------


def test_r1_slack_ts_keeps_microseconds_exactly() -> None:
    t = slack_ts_to_datetime("1760422928.000123")
    assert t == datetime(2025, 10, 14, 6, 22, 8, 123, tzinfo=UTC)
    assert datetime_to_slack_ts(t) == "1760422928.000123"


@pytest.mark.parametrize("bad", ["1760422928", "1760422928.12", "abc.123456", "", "1.5e9"])
def test_r1_rejects_malformed_slack_ts(bad: str) -> None:
    with pytest.raises(ValueError, match="Slack ts"):
        slack_ts_to_datetime(bad)


def test_r1_email_date_header_to_utc() -> None:
    # The recap email: local Helsinki time with +0300 (plan 001 §3.4).
    assert email_date_to_datetime("Tue, 14 Oct 2025 11:40:00 +0300") == utc("2025-10-14T08:40:00Z")
    assert email_date_to_datetime("Tue, 14 Oct 2025 08:40:00 -0000") == utc("2025-10-14T08:40:00Z")


def test_r1_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ensure_utc(datetime(2025, 10, 14, 6, 22))
    with pytest.raises(ValidationError):
        EventTime(event_id="e", platform=Platform.EMAIL, event_time=datetime(2025, 1, 1))


def test_r1_offsets_are_normalised_to_utc() -> None:
    helsinki_summer = timezone(timedelta(hours=3))
    e = EventTime(
        event_id="e",
        platform=Platform.EMAIL,
        event_time=datetime(2025, 10, 14, 9, 22, 8, tzinfo=helsinki_summer),
    )
    assert e.event_time == utc("2025-10-14T06:22:08Z")
    assert e.event_time.tzinfo is UTC


def test_r1_pm_samples_are_stamped_at_interval_start() -> None:
    assert pm_interval_start(utc("2025-10-14T06:21:30Z")) == utc("2025-10-14T06:15:00Z")
    assert pm_interval_start(utc("2025-10-14T06:15:00Z")) == utc("2025-10-14T06:15:00Z")


def test_v4_uses_the_last_complete_interval_not_the_open_one() -> None:
    # "Counters basically zero" at 06:21:30Z is checked against 06:00-06:15, not the open
    # 06:15 bin, which already has post-bounce traffic (plan 001 §6.1 V4).
    assert last_complete_pm_interval(utc("2025-10-14T06:21:30Z")) == (
        utc("2025-10-14T06:00:00Z"),
        utc("2025-10-14T06:15:00Z"),
    )
    assert last_complete_pm_interval(utc("2025-10-14T06:15:00Z")) == (
        utc("2025-10-14T06:00:00Z"),
        utc("2025-10-14T06:15:00Z"),
    )


# --- R2 -----------------------------------------------------------------------------------


def test_r2_recorded_at_is_the_completing_events_time() -> None:
    events = [
        ev("m6", "2025-10-14T06:19:22Z"),
        ev("m8", "2025-10-14T06:22:08Z"),
        ev("m4", "2025-10-14T06:18:03Z"),
    ]
    assert recorded_at(events) == utc("2025-10-14T06:22:08Z")


def test_r2_needs_a_source_event() -> None:
    with pytest.raises(TemporalError):
        recorded_at([])


# --- R3 -----------------------------------------------------------------------------------


def test_r3_elisa_message_8_without_stated_start() -> None:
    vf = resolve_valid_from(elisa_backfill_time(8, "09:22"))
    assert vf.valid_from == utc("2025-10-14T06:22:08Z")
    assert not vf.from_stated_time
    assert not vf.clamped


def test_r3_kemi_installed_3_august_in_an_email_of_18_august() -> None:
    # 00:00 Helsinki (EEST, UTC+3) on 3 August = 2026-08-02T21:00Z; matches gold ASR-0024.
    vf = resolve_valid_from(
        utc("2026-08-18T10:20:40Z"), StatedTime(phrase="on 3 August", tense=Tense.PAST)
    )
    assert vf.valid_from == utc("2026-08-02T21:00:00Z")
    assert vf.from_stated_time


def test_r3_h08_from_monday_sent_wednesday_after_dst_end() -> None:
    # Wed 2026-11-04; EET (UTC+2) since 2026-10-25. Monday 2026-11-09 00:00 local.
    vf = resolve_valid_from(
        utc("2026-11-04T15:30:00Z"), StatedTime(phrase="From Monday", tense=Tense.FUTURE)
    )
    assert vf.valid_from == utc("2026-11-08T22:00:00Z")


def test_r3_h02_on_1_december_announced_in_advance() -> None:
    vf = resolve_valid_from(
        utc("2026-10-28T12:06:40Z"), StatedTime(phrase="on 1 December", tense=Tense.FUTURE)
    )
    assert vf.valid_from == utc("2026-11-30T22:00:00Z")
    assert vf.valid_from > utc("2026-10-28T12:06:40Z")  # recorded before valid


@pytest.mark.parametrize(
    ("phrase", "tense", "sent", "expected"),
    [
        ("since 3 August", Tense.PAST, "2026-08-18T10:20:40Z", "2026-08-02T21:00:00Z"),
        ("from 1 December", Tense.FUTURE, "2026-10-28T12:06:40Z", "2026-11-30T22:00:00Z"),
        ("starting Monday", Tense.FUTURE, "2026-11-04T15:30:00Z", "2026-11-08T22:00:00Z"),
        ("from next Monday", Tense.FUTURE, "2026-11-04T15:30:00Z", "2026-11-08T22:00:00Z"),
        ("1.12. alkaen", Tense.FUTURE, "2026-10-28T12:06:40Z", "2026-11-30T22:00:00Z"),
        ("Monday 08:00", Tense.FUTURE, "2026-11-04T15:30:00Z", "2026-11-09T06:00:00Z"),
        ("tomorrow", Tense.FUTURE, "2026-11-04T15:30:00Z", "2026-11-04T22:00:00Z"),
        # The date is taken in Helsinki: 23:30Z on 4 Nov is already 5 Nov locally.
        ("tomorrow", Tense.FUTURE, "2026-11-04T23:30:00Z", "2026-11-05T22:00:00Z"),
        ("from 20 Dec", Tense.FUTURE, "2025-12-15T09:30:29Z", "2025-12-19T22:00:00Z"),
    ],
)
def test_r3_stated_start_variants(phrase: str, tense: Tense, sent: str, expected: str) -> None:
    r = resolve_stated_time(phrase, utc(sent), tense)
    assert r.resolution is Resolution.RESOLVED
    assert r.value == utc(expected)


def test_r3_time_of_day_is_kept_when_stated() -> None:
    r = resolve_stated_time("Monday 08:00", utc("2026-11-04T15:30:00Z"), Tense.FUTURE)
    assert r.has_time_of_day
    r = resolve_stated_time("Monday", utc("2026-11-04T15:30:00Z"), Tense.FUTURE)
    assert not r.has_time_of_day


def test_r3_tense_decides_the_year_of_an_underspecified_date() -> None:
    sent = utc("2026-08-18T10:20:40Z")
    past = resolve_stated_time("3 August", sent, Tense.PAST)
    future = resolve_stated_time("3 August", sent, Tense.FUTURE)
    assert past.value == utc("2026-08-02T21:00:00Z")
    assert future.value == utc("2027-08-02T21:00:00Z")


def test_r3_unresolvable_phrase_falls_back_to_recorded_at_and_is_flagged() -> None:
    sent = utc("2026-11-04T15:30:00Z")
    vf = resolve_valid_from(sent, StatedTime(phrase="once the parts arrive", tense=Tense.FUTURE))
    assert vf.valid_from == sent
    assert not vf.from_stated_time
    assert vf.unresolved_phrase == "once the parts arrive"


def test_r3_two_dates_in_one_phrase_is_ambiguous() -> None:
    r = resolve_stated_time("from 20 Dec to 5 Jan", utc("2025-12-15T09:30:29Z"), Tense.FUTURE)
    assert r.resolution is Resolution.AMBIGUOUS
    assert r.value is None


def test_r3_start_before_the_entity_existed_is_clamped_and_flagged() -> None:
    vf = resolve_valid_from(
        utc("2019-05-01T08:00:00Z"),
        StatedTime(phrase="since 3 March", tense=Tense.PAST),
        entity_valid_from=utc("2019-04-18T00:00:00Z"),  # TRN-042 commissioned
    )
    assert vf.valid_from == utc("2019-04-18T00:00:00Z")
    assert vf.clamped


def test_r3_resolves_against_the_source_event_not_recorded_at() -> None:
    # The stated start is relative to the message that says it, even when the assertion
    # was completed by a later message.
    vf = resolve_valid_from(
        utc("2026-11-10T09:00:00Z"),
        StatedTime(phrase="From Monday", tense=Tense.FUTURE),
        event_time=utc("2026-11-04T15:30:00Z"),
    )
    assert vf.valid_from == utc("2026-11-08T22:00:00Z")


# --- R4 -----------------------------------------------------------------------------------


def test_r4a_h03_until_20_november_is_an_exclusive_end() -> None:
    sent = utc("2026-11-02T06:30:00Z")
    end = resolve_stated_expiry(
        StatedTime(phrase="until 20 November", tense=Tense.FUTURE), sent, sent
    )
    assert end == utc("2026-11-19T22:00:00Z")


def test_r4a_sales_freeze_window_matches_gold_asr_0026() -> None:
    sent = utc("2025-12-15T09:30:29Z")
    start = resolve_valid_from(sent, StatedTime(phrase="from 20 Dec", tense=Tense.FUTURE))
    end = resolve_stated_expiry(
        StatedTime(phrase="to 5 Jan", tense=Tense.FUTURE), sent, start.valid_from
    )
    assert (start.valid_from, end) == (utc("2025-12-19T22:00:00Z"), utc("2026-01-04T22:00:00Z"))


def test_r4a_expiry_must_follow_valid_from() -> None:
    sent = utc("2026-11-02T06:30:00Z")
    with pytest.raises(TemporalError, match="not after"):
        resolve_stated_expiry(StatedTime(phrase="until 1 November", tense=Tense.PAST), sent, sent)
    with pytest.raises(TemporalError, match="no single date"):
        resolve_stated_expiry(
            StatedTime(phrase="until further notice", tense=Tense.FUTURE), sent, sent
        )


def _state(vf: str, vt: str | None = None, rec: str | None = None, **kw: object) -> TemporalState:
    return TemporalState.model_validate(
        {
            "valid_from": utc(vf),
            "valid_to": utc(vt) if vt else None,
            "recorded_at": utc(rec or vf),
            **kw,
        }
    )


def test_r4b_h02_supersession_sets_the_superseded_timestamp() -> None:
    padlock = _state("2026-10-28T12:05:00Z", status=AssertionStatus.BELIEVED_TRUE)
    code_lock = _state("2026-11-30T22:00:00Z", rec="2026-10-28T12:06:40Z")
    old = supersede(padlock, code_lock)
    assert old.valid_to == code_lock.valid_from == utc("2026-11-30T22:00:00Z")
    assert old.status is AssertionStatus.SUPERSEDED
    assert padlock.valid_to is None  # versions are immutable (R6)


def test_r4b_successor_must_start_later() -> None:
    a = _state("2026-10-28T12:05:00Z")
    with pytest.raises(TemporalError, match="correction"):
        supersede(a, _state("2026-10-28T12:05:00Z"))


def test_r4b_cannot_supersede_an_already_ended_window() -> None:
    closed = _state("2026-11-02T06:30:00Z", "2026-11-19T22:00:00Z")
    with pytest.raises(TemporalError, match="already ended"):
        supersede(closed, _state("2026-12-01T00:00:00Z"))


def test_r4c_decommissioning_ends_assertions_bound_only_to_ended_devices() -> None:
    s = _state("2025-10-14T06:22:08Z")
    out = decommission_expiry(s, [utc("2026-05-01T00:00:00Z")])
    assert out.expired
    assert out.valid_to == utc("2026-05-01T00:00:00Z")
    # The last of several devices to end decides.
    out = decommission_expiry(s, [utc("2026-05-01T00:00:00Z"), utc("2026-07-01T00:00:00Z")])
    assert out.valid_to == utc("2026-07-01T00:00:00Z")
    # One device still alive: nothing changes.
    out = decommission_expiry(s, [utc("2026-05-01T00:00:00Z"), None])
    assert not out.expired
    assert out.valid_to is None
    # No observed bindings (class-level only): nothing changes.
    assert not decommission_expiry(s, []).expired


def test_r4c_an_earlier_stated_expiry_wins() -> None:
    s = _state("2026-11-02T06:30:00Z", "2026-11-19T22:00:00Z")
    out = decommission_expiry(s, [utc("2027-01-01T00:00:00Z")])
    assert out.valid_to == utc("2026-11-19T22:00:00Z")


# --- R5 -----------------------------------------------------------------------------------


def test_r5_doubt_changes_status_never_validity() -> None:
    s = _state("2025-10-14T06:22:08Z", status=AssertionStatus.BELIEVED_TRUE)
    doubted = apply_doubt(s, status=AssertionStatus.DISPUTED, review_due=True)
    assert doubted.status is AssertionStatus.DISPUTED
    assert doubted.review_due
    assert (doubted.valid_from, doubted.valid_to) == (s.valid_from, s.valid_to)


@pytest.mark.parametrize("status", [AssertionStatus.SUPERSEDED, AssertionStatus.EXPIRED])
def test_r5_validity_statuses_are_not_doubt(status: AssertionStatus) -> None:
    with pytest.raises(TemporalError):
        apply_doubt(_state("2025-10-14T06:22:08Z"), status=status)


# --- R6 -----------------------------------------------------------------------------------


def test_r6_correction_retracts_and_keeps_valid_from() -> None:
    wrong = _state("2025-10-14T06:22:08Z", status=AssertionStatus.UNVERIFIED)
    retracted, fixed = correct(wrong, utc("2025-10-14T07:00:00Z"))
    assert retracted.retracted_at == utc("2025-10-14T07:00:00Z")
    assert fixed.valid_from == wrong.valid_from
    assert fixed.recorded_at == utc("2025-10-14T07:00:00Z")
    assert fixed.retracted_at is None


def test_r6_a_retracted_version_cant_be_corrected_twice() -> None:
    retracted, _ = correct(_state("2025-10-14T06:22:08Z"), utc("2025-10-14T07:00:00Z"))
    with pytest.raises(TemporalError):
        correct(retracted, utc("2025-10-14T08:00:00Z"))


@pytest.mark.parametrize(
    "fields",
    [
        {"valid_to": "2025-10-14T06:22:08Z"},  # empty window
        {"retracted_at": "2025-10-14T06:00:00Z"},  # retracted before recorded
        {"status": "superseded"},  # superseded without valid_to
        {"status": "expired"},
    ],
)
def test_temporal_state_invariants(fields: dict[str, str]) -> None:
    base = {"valid_from": "2025-10-14T06:22:08Z", "recorded_at": "2025-10-14T06:22:08Z"}
    with pytest.raises(ValidationError):
        TemporalState.model_validate({**base, **fields})


# --- R7 -----------------------------------------------------------------------------------


def test_r7_order_by_event_time_then_platform_sequence() -> None:
    a = ev("a", "2025-10-14T06:16:00Z", "1760422560.000200")
    b = ev("b", "2025-10-14T06:16:00Z", "1760422560.000100")
    c = ev("c", "2025-10-14T06:15:00Z")
    assert [e.event_id for e in sort_events([a, b, c])] == ["c", "b", "a"]


def test_r7_thread_must_be_strictly_increasing() -> None:
    check_thread_order([ev("a", "2025-10-14T06:16:12Z"), ev("b", "2025-10-14T06:16:41Z")])
    with pytest.raises(TemporalError):
        check_thread_order([ev("a", "2025-10-14T06:16:00Z"), ev("b", "2025-10-14T06:16:00Z")])


# --- R8 -----------------------------------------------------------------------------------


def test_r8_elisa_message_8() -> None:
    assert elisa_backfill_time(8, "09:22") == utc("2025-10-14T06:22:08Z")


def test_r8_matches_elisas_file_and_the_corpus_and_is_strictly_ordered() -> None:
    chat = json.loads(ELISA_CHAT.read_text(encoding="utf-8"))
    ours = [elisa_backfill_time(m["seq"], m["time"]) for m in chat["messages"]]
    assert ours == sorted(ours)
    assert len(set(ours)) == len(ours)
    assert len(ours) == len(ELISA_SYNTHETIC_SECONDS) == 12

    corpus = [
        utc(m["created_at"])
        for m in map(json.loads, CORPUS_CHAT.read_text(encoding="utf-8").splitlines())
        if m["conversation_id"] == ELISA_THREAD and m["created_at"].startswith("2025-10-14T06")
    ]
    assert corpus == ours


def test_r8_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        elisa_backfill_time(8, "9:22")
    with pytest.raises(ValueError, match="1-12"):
        elisa_backfill_time(13, "09:30")


# --- R9 / R10 -------------------------------------------------------------------------------

TRN_042 = EntityWindow(entity_id="TRN-042", valid_from=utc("2019-04-18T00:00:00Z"))
GEN1 = {"hw_model": "TNC-1X"}


def test_r9_half_open_window() -> None:
    s = _state("2026-10-28T12:05:00Z", "2026-11-30T22:00:00Z", status=AssertionStatus.SUPERSEDED)
    assert in_scope_at(s, utc("2026-10-28T12:05:00Z"), [TRN_042])
    assert in_scope_at(s, utc("2026-11-30T21:59:59Z"), [TRN_042])
    assert not in_scope_at(s, utc("2026-11-30T22:00:00Z"), [TRN_042])
    assert not in_scope_at(s, utc("2026-10-28T12:04:59Z"), [TRN_042])


def test_r9_needs_a_bound_entity_that_exists_at_t() -> None:
    s = _state("2025-10-14T06:22:08Z")
    gone = EntityWindow(
        entity_id="TRN-099",
        valid_from=utc("2019-01-01T00:00:00Z"),
        valid_to=utc("2026-01-01T00:00:00Z"),
    )
    assert in_scope_at(s, utc("2025-12-01T00:00:00Z"), [gone])
    assert not in_scope_at(s, utc("2026-02-01T00:00:00Z"), [gone])
    assert in_scope_at(s, utc("2026-02-01T00:00:00Z"), [gone, TRN_042])
    assert not in_scope_at(s, utc("2026-02-01T00:00:00Z"), [])


def test_r9_qualifiers_must_match_the_as_running_snapshot() -> None:
    s = _state("2025-10-14T06:22:08Z")
    t = utc("2026-01-01T00:00:00Z")
    assert in_scope_at(s, t, [TRN_042], required_qualifiers=GEN1, snapshot={"hw_model": "tnc-1x"})
    assert not in_scope_at(
        s, t, [TRN_042], required_qualifiers=GEN1, snapshot={"hw_model": "TNC-2X"}
    )
    assert not in_scope_at(s, t, [TRN_042], required_qualifiers=GEN1, snapshot={})
    assert qualifiers_match({}, {})
    assert qualifiers_match({"generation": ["Gen1", "Gen2"]}, {"generation": "gen2"})
    assert not qualifiers_match({"generation": ["Gen1", "Gen2"]}, {"generation": "Gen3"})


def test_r9_retracted_versions_are_never_in_scope_without_known_at() -> None:
    retracted, fixed = correct(_state("2025-10-14T06:22:08Z"), utc("2025-10-14T07:00:00Z"))
    t = utc("2025-10-14T06:30:00Z")
    assert not in_scope_at(retracted, t, [TRN_042])
    assert in_scope_at(fixed, t, [TRN_042])  # the fix keeps valid_from (R6)


def test_r10_known_at_kemi_dispatch_before_the_email() -> None:
    # The code lock is valid from 2026-08-02T21:00Z but only recorded on 2026-08-18.
    kemi_seg = EntityWindow(entity_id="SEG-042", valid_from=utc("2019-04-18T00:00:00Z"))
    padlock = _state(
        "2025-10-20T09:19:38Z", "2026-08-02T21:00:00Z", status=AssertionStatus.SUPERSEDED
    )
    code_lock = _state("2026-08-02T21:00:00Z", rec="2026-08-18T10:20:40Z")
    dispatch = utc("2026-08-10T06:00:00Z")

    # As of the dispatch, with today's knowledge: the code lock.
    assert in_scope_at(code_lock, dispatch, [kemi_seg])
    assert not in_scope_at(padlock, dispatch, [kemi_seg])
    # As known at the dispatch: nothing about the code lock yet. Why the AI went without it.
    assert not in_scope_at(code_lock, dispatch, [kemi_seg], known=dispatch)
    assert known_at(padlock, dispatch)


def test_r10_a_later_retraction_still_counts_as_known_before_it() -> None:
    retracted, fixed = correct(_state("2025-10-14T06:22:08Z"), utc("2025-10-14T07:00:00Z"))
    t = utc("2025-10-14T06:30:00Z")
    assert in_scope_at(retracted, t, [TRN_042], known=utc("2025-10-14T06:45:00Z"))
    assert not in_scope_at(fixed, t, [TRN_042], known=utc("2025-10-14T06:45:00Z"))
    assert not in_scope_at(retracted, t, [TRN_042], known=utc("2025-10-14T07:00:00Z"))


# --- The gold layer obeys the same model ------------------------------------------------------

GOLD = REPO / "mock_data" / "knowledge_graph" / "assertions.jsonl"


def test_every_gold_assertion_is_a_valid_temporal_state() -> None:
    rows = [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines()]
    assert rows
    fields = set(TemporalState.model_fields)
    by_id = {
        r["assertion_id"]: TemporalState.model_validate({k: v for k, v in r.items() if k in fields})
        for r in rows
    }
    # Supersession links agree with R4b: pred.valid_to == succ.valid_from.
    for r in rows:
        if r.get("superseded_by"):
            assert by_id[r["assertion_id"]].valid_to == by_id[r["superseded_by"]].valid_from
