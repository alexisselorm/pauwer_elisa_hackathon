"""Static checks on the migration files (no Neo4j needed)."""

from __future__ import annotations

import re

from neo4j_python_migrations.loader import load

from silentctx.graph.schema import MIGRATIONS_DIR


def test_every_migration_file_is_loaded() -> None:
    files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.cypher"))
    assert files, "no migrations found"
    assert len(load(MIGRATIONS_DIR)) == len(files)  # a misnamed file would be skipped silently


def test_no_semicolons_in_comments() -> None:
    # The tool splits a file on every ';', even inside a // comment.
    for path in MIGRATIONS_DIR.glob("*.cypher"):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "//" in line:
                assert ";" not in line.split("//", 1)[1], f"{path.name}:{n}"


def test_vector_indexes_use_the_default_embedding_dim() -> None:
    text = (MIGRATIONS_DIR / "V0001__schema.cypher").read_text(encoding="utf-8")
    assert re.findall(r"`vector\.dimensions`: (\d+)", text) == ["1024", "1024"]
