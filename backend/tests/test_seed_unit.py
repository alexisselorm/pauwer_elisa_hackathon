"""Seed loader without Neo4j: the files, the profile rules and the demo exclusions."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from kaiku.seed.models import DeviceEventRow
from kaiku.seed.profiles import Profile, demo_exclusions, thread_subject
from kaiku.seed.service import SeedRequest
from kaiku.seed.sources import MockData

MOCK = Path(__file__).parents[2] / "mock_data"
ELISA_THREAD = "19:71fe3c060dcfcf4e0874a4bf@thread.v2"
RECAP_EMAIL = "<BE1A9D274C68A253.nfurufochrhq@volga.example>"


@pytest.fixture(scope="module")
def data() -> MockData:
    return MockData(MOCK)


def test_every_source_file_loads_and_validates(data: MockData) -> None:
    assert len(data.entities()) == 166
    assert len(data.devices()) == len(data.nodes_running()) == 76
    assert data.segments()
    assert data.links()
    assert data.snapshots()
    assert data.device_events()
    assert data.alarms()
    assert len(data.gold_assertions()) == 24
    assert data.access_register()
    assert data.model_aliases()["tnc1x"] == "TNC-1X"


def test_not_a_mock_data_dir(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        MockData(tmp_path)


def test_device_events_obey_record_time_equals_event_time() -> None:
    row = {
        "event_id": "EVT-1",
        "device_id": "TRN-001",
        "type": "field_visit",
        "event_time": "2026-01-01T10:00:00Z",
        "record_time": "2026-01-01T10:05:00Z",
    }
    with pytest.raises(ValidationError, match="record_time"):
        DeviceEventRow.model_validate(row)


@pytest.mark.parametrize(
    ("request_kwargs", "message"),
    [
        ({}, "nothing to do"),
        ({"gold": True, "wipe_gold": True, "profile": Profile.FULL}, "make no sense"),
    ],
)
def test_seed_request_rules(request_kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SeedRequest.model_validate(request_kwargs).validate_rules()


def test_seed_request_allowed_combinations() -> None:
    SeedRequest(network=True).validate_rules()
    SeedRequest(network=True, gold=True, profile=Profile.FULL).validate_rules()
    SeedRequest(wipe_gold=True).validate_rules()


@pytest.mark.parametrize(
    ("subject", "key"),
    [
        ("RE: TRN-042 traffic not recovering", "trn-042 traffic not recovering"),
        ("Fwd: RE:  TRN-042 traffic   not recovering", "trn-042 traffic not recovering"),
        ("VS: Kemi", "kemi"),
        ("Kemi site", "kemi site"),
    ],
)
def test_thread_subject(subject: str, key: str) -> None:
    assert thread_subject(subject) == key


def test_demo_profile_excludes_every_forwarding_stall_source(data: MockData) -> None:
    x = demo_exclusions(data)
    assert x.gold_assertion_ids == {
        "ASR-0001",  # Elisa's thread, message 8
        "ASR-0002",  # Erkki's recap email
        "ASR-0005",  # "these old units", same thread
        "ASR-0006",  # 2026-03 Gen2 scope widening
        "ASR-0016",
        "ASR-0017",
        "ASR-0019",
        "ASR-0020",
        "ASR-0021",  # "same thing again" messages
    }
    assert ELISA_THREAD in x.teams_conversation_ids
    assert x.excludes_teams("any-later-reply", ELISA_THREAD)
    assert x.excludes_email(RECAP_EMAIL, "RE: TRN-042 traffic not recovering after SFP replacement")
    # A reply in the same email chain is excluded by subject even with a new message id.
    assert x.excludes_email("<new@x>", "Re: TRN-042 traffic not recovering after SFP replacement")


def test_demo_profile_keeps_corroboration_and_contradiction(data: MockData) -> None:
    x = demo_exclusions(data)
    for kept in ("ASR-0003", "ASR-0004", "ASR-0030"):  # 2019 PDF, its forwarding email, ITSM KB
        assert kept not in x.gold_assertion_ids
    gold = {a.assertion_id: a for a in data.gold_assertions()}
    assert not x.excludes_email(
        gold["ASR-0004"].source_ref, "Fwd: old upgrade notes for the TRN boxes"
    )
    # The TRN-006 "SFP was loose, not the known issue" message is a control: it stays.
    reseat = next(
        m for m in data.teams_messages() if m["message_id"] == gold["ASR-0018"].source_ref
    )
    assert not x.excludes_teams(reseat["message_id"], reseat["conversation_id"])
