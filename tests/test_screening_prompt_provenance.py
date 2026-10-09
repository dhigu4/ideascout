"""Screening-prompt provenance.

Every source_screenings row records the prompt version that produced it. That
version is part of a screening's identity, so the legacy peer-authority prompt
and the authority-ordered prompt can never be mistaken for one another. The
evaluator refuses a holdout whose predictions mix prompt versions. Every
database lives under pytest's tmp_path. No real LLM, network, or production
state is touched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ideascout import db, screening_prompt, shadow
from tests.test_narrative_repair_diagnostics import _snapshot_all_tables
from tests.test_taste_evaluation import _evaluate, _setup_v2, _standard_holdout


def _insert_source(conn) -> int:
    conn.execute(
        "INSERT INTO collected_sources (source_name, external_id, canonical_url, discovered_at, content_hash, "
        "raw_html_path, metadata_json, created_at) VALUES ('yellowbrick', '1', 'u', 'd', 'h', 'p', '{}', 'd')"
    )
    conn.commit()
    return conn.execute("SELECT source_id FROM collected_sources ORDER BY source_id DESC LIMIT 1").fetchone()[0]


def _screening(conn, source_id, *, prediction="WATCH", prompt_version=None, rules_sha="rules-sha",
               content_hash="hash-1"):
    kwargs = dict(
        source_id=source_id,
        created_at="2026-01-01T00:00:00+00:00",
        taste_version=2,
        taste_sha256="v2-sha",
        screen_rules_path="IDEA_SCREEN_RULES.md",
        screen_rules_sha256=rules_sha,
        content_hash=content_hash,
        model_name="fake-model",
        overall_prediction=prediction,
        mispricing="Plausible",
        variant_perception="Weak",
        upside="Potentially sufficient",
        business_quality="Plausible",
        downside="Acceptable",
        key_reasons_json='["r"]',
        key_concerns_json='["c"]',
        critical_questions_json='["q"]',
        confidence="MEDIUM",
    )
    if prompt_version is not None:
        kwargs["screening_prompt_version"] = prompt_version
    return db.insert_source_screening(conn, **kwargs)


def _build_pre_migration_18(path):
    """A real database as it stood before migration 18: schema 17, one legacy
    screening, and a narrative-repair row that references that screening.
    """
    conn = sqlite3.connect(str(path))
    for sql in db.MIGRATIONS[:17]:
        conn.executescript(sql)
    conn.execute("PRAGMA user_version = 17")
    conn.execute(
        "INSERT INTO collected_sources (source_name, external_id, canonical_url, discovered_at, content_hash, "
        "raw_html_path, metadata_json, created_at) VALUES ('yellowbrick', '1', 'u', 'd', 'h', 'p', '{}', 'd')"
    )
    conn.execute(
        "INSERT INTO source_screenings (source_id, created_at, taste_version, taste_sha256, screen_rules_path, "
        "screen_rules_sha256, content_hash, model_name, overall_prediction, mispricing, variant_perception, upside, "
        "business_quality, downside, key_reasons_json, key_concerns_json, critical_questions_json, confidence) "
        "VALUES (1, '2026-01-01', 2, 'tsha', 'rp', 'rsha', 'h', 'm', 'WATCH', 'Plausible', 'Plausible', "
        "'Potentially sufficient', 'Plausible', 'Acceptable', '[\"r\"]', '[\"c\"]', '[\"q\"]', 'MEDIUM')"
    )
    conn.execute(
        "INSERT INTO source_screening_narrative_repairs (screening_id, attempted_at, model_name, status, "
        "missing_fields_json, key_reasons_json, key_concerns_json, critical_questions_json) "
        "VALUES (1, 'd', 'm', 'REPAIRED', '[]', '[]', '[]', '[]')"
    )
    conn.commit()
    conn.close()


# --- constants and migration ---------------------------------------------------------------


def test_legacy_and_current_prompt_versions_are_distinct_constants():
    assert screening_prompt.LEGACY_SCREENING_PROMPT_VERSION == 1
    assert screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION == 2
    assert screening_prompt.CURRENT_SCREENING_PROMPT_VERSION == 3
    assert set(screening_prompt.SCREENING_PROMPT_DESCRIPTIONS) == {1, 2, 3}


def test_prompt_v1_and_v2_historical_identities_remain_intact():
    # v1 (legacy) and v2 (authority-ordered, pre-WATCH/PASS-guidance) are both
    # historical only now -- neither value or description changed by the v3 bump.
    assert screening_prompt.SCREENING_PROMPT_DESCRIPTIONS[1] == "legacy peer-authority prompt"
    assert screening_prompt.SCREENING_PROMPT_DESCRIPTIONS[2] == "authority-ordered prompt (permanent rules outrank taste)"
    assert screening_prompt.SCREENING_PROMPT_DESCRIPTIONS[3] == "authority-ordered prompt + explicit WATCH/PASS semantics"


def test_legacy_prompt_text_is_preserved_and_differs_from_current():
    assert shadow.LEGACY_SYSTEM_PROMPT_TEMPLATE != shadow.SYSTEM_PROMPT_TEMPLATE
    legacy = shadow.LEGACY_SYSTEM_PROMPT_TEMPLATE.format(idea_taste_body="TASTE", screen_rules_body="RULES")
    assert "AUTHORITATIVE" not in legacy
    assert legacy.index("TASTE") < legacy.index("RULES")


def test_migration_18_labels_every_historical_row_legacy_and_keeps_references_intact(tmp_path):
    database = tmp_path / "ideas.db"
    _build_pre_migration_18(database)

    conn = db.connect(database)

    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    rows = conn.execute("SELECT screening_id, overall_prediction, screening_prompt_version FROM source_screenings").fetchall()
    assert [tuple(r) for r in rows] == [(1, "WATCH", 1)]
    repair = conn.execute("SELECT screening_id, status FROM source_screening_narrative_repairs").fetchone()
    assert tuple(repair) == (1, "REPAIRED")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_migration_18_creates_a_premigration_backup(tmp_path):
    database = tmp_path / "ideas.db"
    _build_pre_migration_18(database)

    db.connect(database).close()

    backups = list((tmp_path / "backups").glob("*premigration*.db"))
    assert len(backups) == 1


# --- identity, dedupe, and current lookup -------------------------------------------------


def test_new_screenings_record_the_current_prompt_version(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)

    screening_id = _screening(conn, source_id)

    row = conn.execute("SELECT screening_prompt_version FROM source_screenings WHERE screening_id = ?", (screening_id,)).fetchone()
    assert row[0] == screening_prompt.CURRENT_SCREENING_PROMPT_VERSION


def test_same_taste_and_rules_under_a_different_prompt_is_not_deduped(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    legacy_id = _screening(conn, source_id, prompt_version=1, prediction="PASS")

    current_id = _screening(conn, source_id, prompt_version=2, prediction="WATCH")

    assert legacy_id != current_id
    assert db.get_source_screening(conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha",
                                   screening_prompt_version=1)["screening_id"] == legacy_id
    assert db.get_source_screening(conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha",
                                   screening_prompt_version=2)["screening_id"] == current_id


def test_identical_identity_including_prompt_version_is_still_rejected(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    _screening(conn, source_id, prompt_version=2)

    with pytest.raises(sqlite3.IntegrityError):
        _screening(conn, source_id, prompt_version=2, prediction="PASS")


def test_current_lookup_returns_the_current_prompt_screening_by_default(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    _screening(conn, source_id, prompt_version=screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION, prediction="PASS")
    current_id = _screening(conn, source_id, prompt_version=screening_prompt.CURRENT_SCREENING_PROMPT_VERSION, prediction="WATCH")

    found = db.get_source_screening(conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha")

    assert found["screening_id"] == current_id
    assert found["screening_prompt_version"] == screening_prompt.CURRENT_SCREENING_PROMPT_VERSION


def test_current_lookup_defaults_to_requesting_prompt_v3(tmp_path):
    """Where code asks for THE current prompt version without saying which one,
    it must now mean v3 -- not v2 (v2 is historical only after the WATCH/PASS
    guidance was added).
    """
    assert screening_prompt.CURRENT_SCREENING_PROMPT_VERSION == 3
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    screening_id = _screening(conn, source_id)  # no prompt_version override

    row = conn.execute(
        "SELECT screening_prompt_version FROM source_screenings WHERE screening_id = ?", (screening_id,)
    ).fetchone()
    assert row[0] == 3
    assert db.get_source_screening(
        conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha"
    )["screening_id"] == screening_id


def test_latest_lookup_is_prompt_agnostic_for_repairs(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    _screening(conn, source_id, prompt_version=1, prediction="PASS")
    newest = _screening(conn, source_id, prompt_version=2, prediction="WATCH")

    found = db.get_latest_screening_for_taste_and_rules(
        conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha"
    )

    assert found["screening_id"] == newest


def test_historical_rows_are_untouched_by_new_screenings(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    _screening(conn, source_id, prompt_version=1, prediction="PASS")
    before = [tuple(r) for r in conn.execute("SELECT * FROM source_screenings WHERE screening_prompt_version = 1")]

    _screening(conn, source_id, prompt_version=2, prediction="WATCH", content_hash="hash-2")

    after = [tuple(r) for r in conn.execute("SELECT * FROM source_screenings WHERE screening_prompt_version = 1")]
    assert after == before


# --- evaluate-taste provenance ------------------------------------------------------------


def _mark_legacy(conn, assignment_id):
    conn.execute(
        "UPDATE source_screenings SET screening_prompt_version = 1 WHERE source_id = "
        "(SELECT source_id FROM blind_review_assignments WHERE assignment_id = ?)",
        (assignment_id,),
    )
    conn.commit()


def test_evaluate_reports_the_prompt_version_of_a_legacy_holdout(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.execute("UPDATE source_screenings SET screening_prompt_version = 1")
    conn.commit()
    conn.close()

    code, output = _evaluate(config)

    assert code == 0
    assert "Screening prompt version used by every holdout prediction: v1 (legacy peer-authority prompt)" in output
    assert "[ok] Single screening prompt version across the holdout" in output


def test_evaluate_refuses_a_holdout_that_mixes_prompt_versions(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    first_assignment = conn.execute("SELECT MIN(assignment_id) FROM blind_review_assignments").fetchone()[0]
    _mark_legacy(conn, first_assignment)
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "[FAIL] Single screening prompt version across the holdout (mixed: v1 x1, v3 x19)" in output
    assert "TASTE EVALUATION --" not in output


def test_evaluate_refuses_a_member_with_two_prompt_versions_before_judgment(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    member_source = conn.execute(
        "SELECT source_id FROM blind_review_assignments ORDER BY assignment_id LIMIT 1"
    ).fetchone()[0]
    _screening(conn, member_source, prompt_version=1, prediction="PASS", content_hash="hash-legacy-extra")
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "mixed prompt versions" in output


def test_evaluate_leaves_every_historical_row_unchanged(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.execute("UPDATE source_screenings SET screening_prompt_version = 1")
    conn.commit()
    conn.close()
    before = _snapshot_all_tables(config)

    code, _ = _evaluate(config)

    assert code == 0
    assert _snapshot_all_tables(config) == before


def test_no_migration_was_added_for_the_v3_prompt_bump(tmp_path):
    """The prompt-version bump is a Python constant change: the existing
    screening_prompt_version INTEGER column already stores any integer, so no
    schema change (and no new MIGRATIONS entry) is needed to introduce v3.
    """
    assert len(db.MIGRATIONS) == 19


def test_old_prompt_v2_screenings_remain_unchanged_after_the_v3_bump(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    historical_source = _insert_source(conn)
    historical_id = _screening(
        conn, historical_source, prompt_version=screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION,
        prediction="PASS",
    )
    before = tuple(conn.execute("SELECT * FROM source_screenings WHERE screening_id = ?", (historical_id,)).fetchone())

    # New screening activity on a genuinely different source, recording the new
    # current prompt (v3) -- must not touch the historical v2 row above.
    conn.execute(
        "INSERT INTO collected_sources (source_name, external_id, canonical_url, discovered_at, content_hash, "
        "raw_html_path, metadata_json, created_at) VALUES ('yellowbrick', '2', 'u', 'd', 'h2', 'p', '{}', 'd')"
    )
    new_source = conn.execute("SELECT source_id FROM collected_sources WHERE external_id = '2'").fetchone()[0]
    _screening(conn, new_source, prompt_version=screening_prompt.CURRENT_SCREENING_PROMPT_VERSION, content_hash="hash-new")

    after = tuple(conn.execute("SELECT * FROM source_screenings WHERE screening_id = ?", (historical_id,)).fetchone())
    assert after == before


def test_same_source_taste_and_rules_distinguishes_v2_from_v3_by_provenance(tmp_path):
    conn = db.connect(tmp_path / "ideas.db")
    source_id = _insert_source(conn)
    v2_id = _screening(
        conn, source_id, prompt_version=screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION, prediction="PASS"
    )
    v3_id = _screening(
        conn, source_id, prompt_version=screening_prompt.CURRENT_SCREENING_PROMPT_VERSION, prediction="WATCH"
    )

    assert v2_id != v3_id
    found_v2 = db.get_source_screening(
        conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha",
        screening_prompt_version=screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION,
    )
    found_v3 = db.get_source_screening(
        conn, source_id=source_id, taste_version=2, screen_rules_sha256="rules-sha",
        screening_prompt_version=screening_prompt.CURRENT_SCREENING_PROMPT_VERSION,
    )
    assert found_v2["screening_id"] == v2_id and found_v2["overall_prediction"] == "PASS"
    assert found_v3["screening_id"] == v3_id and found_v3["overall_prediction"] == "WATCH"


def test_evaluate_accepts_the_completed_v3_holdouts_historical_all_v2_predictions(tmp_path, monkeypatch):
    """The already-completed Taste v3 holdout evaluation was produced entirely
    under prompt v2 (authority-ordered, pre-WATCH/PASS-guidance). It must
    remain evaluable, unchanged, exactly as it ran.
    """
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.execute(
        "UPDATE source_screenings SET screening_prompt_version = ?",
        (screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION,),
    )
    conn.commit()
    conn.close()

    code, output = _evaluate(config)

    assert code == 0
    assert (
        "Screening prompt version used by every holdout prediction: v2 "
        "(authority-ordered prompt (permanent rules outrank taste))"
    ) in output
    assert "[ok] Single screening prompt version across the holdout" in output


def test_evaluate_accepts_a_future_all_v3_holdout(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)  # default screenings already record CURRENT (v3)
    conn.close()

    code, output = _evaluate(config)

    assert code == 0
    assert (
        "Screening prompt version used by every holdout prediction: v3 "
        "(authority-ordered prompt + explicit WATCH/PASS semantics)"
    ) in output


def test_evaluate_refuses_a_holdout_that_mixes_v2_and_v3(tmp_path, monkeypatch):
    config, _ = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)  # all default to v3
    first_source = conn.execute(
        "SELECT source_id FROM blind_review_assignments ORDER BY assignment_id LIMIT 1"
    ).fetchone()[0]
    conn.execute(
        "UPDATE source_screenings SET screening_prompt_version = ? WHERE source_id = ?",
        (screening_prompt.AUTHORITY_ORDERED_SCREENING_PROMPT_VERSION, first_source),
    )
    conn.commit()
    conn.close()

    code, output = _evaluate(config)

    assert code == 1
    assert "[FAIL] Single screening prompt version across the holdout (mixed: v2 x1, v3 x19)" in output
    assert "TASTE EVALUATION --" not in output


def test_frozen_v2_artifact_is_untouched_by_provenance_checks(tmp_path, monkeypatch):
    config, latest = _setup_v2(tmp_path, monkeypatch)
    conn = db.connect(config.database_path)
    _standard_holdout(conn)
    conn.close()
    artifact = latest["idea_taste_path"]
    artifact_bytes = Path(artifact).read_bytes()

    _evaluate(config)

    assert Path(artifact).read_bytes() == artifact_bytes
