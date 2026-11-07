"""Static checks on graph/checks.cypher (no Neo4j needed). The behaviour of each check is
tested against a real graph in test_integration_checks.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from kaiku.graph.checks import CHECKS, CHECKS_FILE, load_checks

# The rules the checks port (plan 001 §3.3 and verify_temporal.py), by check name.
REQUIRED = {
    # validity windows
    "node_window_order",
    "relationship_window_order",
    "access_register_overlap",
    "bound_entity_exists_at_valid_from",
    # status vs time
    "open_status_past_valid_to",
    "expired_has_ended",
    # supersession and links
    "supersession_timestamp",
    "superseded_has_successor",
    "expired_has_no_successor",
    "superseded_by_mirrors_edge",
    "supersedes_is_asymmetric",
    "symmetric_link_stored_once",
    "disputed_has_contradiction",
    # record time = event time
    "recorded_at_is_source_event_time",  # R2
    "event_has_one_event_time",  # R1
    "event_has_no_validity",  # R1
    "thread_strictly_increasing",  # R7
}


def test_every_rule_has_a_check() -> None:
    assert not REQUIRED - set(CHECKS)


def test_scoped_checks_filter_on_ids_and_graph_checks_do_not() -> None:
    for check in CHECKS.values():
        if check.anchor == "graph":
            assert "$ids" not in check.query, check.name
        else:
            assert "$ids" in check.query, check.name
        assert "$now" not in check.query or check.anchor == "graph", check.name
        assert check.query.rstrip().splitlines()[-1].startswith("RETURN"), check.name
        assert " AS id" in check.query.splitlines()[-1], check.name


def test_the_file_has_no_statement_separators() -> None:
    # Each check is sent on its own; a ';' would end it early or split it.
    assert ";" not in CHECKS_FILE.read_text(encoding="utf-8")


def test_a_bad_header_is_refused(tmp_path: Path) -> None:
    bad = tmp_path / "bad.cypher"
    bad.write_text("// check: x [nowhere]\nMATCH (n) RETURN n AS id\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown anchor"):
        load_checks(bad)
    bad.write_text(
        "// check: x [graph]\nRETURN 1 AS id\n// check: x [graph]\nRETURN 2 AS id\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="defined twice"):
        load_checks(bad)
