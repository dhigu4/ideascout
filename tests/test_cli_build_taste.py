"""Tests for build-taste / taste-status (Stage 3: Far View Taste v1).

These never call a real LLM -- taste generation is monkeypatched at the
ideascout.taste module boundary (build_client / generate_idea_taste_body /
generate_candidate_rules), so cli.py runs against fake, deterministic
output. Nothing here ever touches IdeaScoutLocal; every database and every
generated file lives under pytest's tmp_path (see tests/conftest.py for
the additional autouse safety net).
"""

from __future__ import annotations

import json
from pathlib import Path

from ideascout import cli, db, parser, taste
from ideascout.config import Config


def make_config(tmp_path) -> Config:
    config = Config(
        agentmail_api_key=None,
        agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",
        log_path=tmp_path / "app.log",
        anthropic_api_key="fake-anthropic-key",
    )
    db.connect(config.database_path).close()
    return config


def insert_raw_message(
    conn, message_id: str, subject: str = "An idea", body_raw: str = "LIKE.", sender: str = "brad@example.com"
) -> None:
    db.insert_message(
        conn,
        message_id=message_id,
        inbox_id="inbox_abc",
        thread_id="thread_abc",
        received_at="2026-01-01T12:00:00+00:00",
        sender=sender,
        recipients="ideas@yourdomain.agentmail.to",
        subject=subject,
        body_raw=body_raw,
        body_format="text",
        size_bytes=100,
        stored_at="2026-01-01T12:00:05+00:00",
        sender_authenticated="AUTHENTICATED",
    )


def insert_feedback_row(
    conn,
    message_id: str,
    event_index: int = 0,
    event_type: str = "FEEDBACK",
    verdict="LIKE",
    ticker="XYZ",
    company=None,
    user_comment: str = "Looks interesting.",
    extra_json: dict | None = None,
) -> int:
    db.insert_feedback(
        conn,
        message_id=message_id,
        event_index=event_index,
        event_type=event_type,
        verdict=verdict,
        ticker=ticker,
        company=company,
        novelty="UNKNOWN",
        user_comment=user_comment,
        parsed_json=json.dumps(extra_json or {}),
        parser_version=parser.PARSER_VERSION,
        model_name="claude-haiku-4-5",
        confidence=0.9,
        created_at="2026-01-01T00:00:00+00:00",
    )
    return db.get_feedback_events_for_message(conn, message_id)[event_index]["feedback_id"]


def make_parsed_message(conn, message_id: str, **feedback_kwargs) -> int:
    insert_raw_message(conn, message_id)
    feedback_id = insert_feedback_row(conn, message_id, **feedback_kwargs)
    db.set_feedback_parse_status(conn, message_id, "PARSED")
    return feedback_id


def make_needs_review_message(conn, message_id: str, **feedback_kwargs) -> int:
    insert_raw_message(conn, message_id)
    feedback_kwargs.setdefault("event_type", "UNCLEAR")
    feedback_kwargs.setdefault("verdict", None)
    feedback_id = insert_feedback_row(conn, message_id, **feedback_kwargs)
    db.set_feedback_parse_status(conn, message_id, "NEEDS_REVIEW")
    return feedback_id


def make_excluded_message(conn, message_id: str, **feedback_kwargs) -> int:
    feedback_id = make_parsed_message(conn, message_id, **feedback_kwargs)
    db.set_feedback_excluded_from_learning(conn, feedback_id, True)
    return feedback_id


def insert_n_eligible(conn, n: int, prefix: str = "msg") -> list[int]:
    return [make_parsed_message(conn, f"{prefix}_{i}", ticker=f"T{i}") for i in range(n)]


def patch_taste(monkeypatch, body: str = "## Strong Positive Signals\nFake.\n", rules=None):
    rules = rules if rules is not None else []
    monkeypatch.setattr(taste, "build_client", lambda api_key: object())
    monkeypatch.setattr(
        taste, "generate_idea_taste_body", lambda client, model, records, logger=None: body
    )
    monkeypatch.setattr(
        taste, "generate_candidate_rules", lambda client, model, records, logger=None: rules
    )


def make_rule(i: int, confidence: str = "HIGH") -> taste.CandidateRule:
    return taste.CandidateRule(rule=f"Rule {i}", evidence=f"Evidence {i}", reusability=f"Reuse {i}", confidence=confidence)


# --- canonical eligibility query ---------------------------------------------


def test_build_taste_uses_the_canonical_eligibility_query(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    calls = []
    original = db.get_feedback_eligible_for_learning

    def spy(conn):
        calls.append(1)
        return original(conn)

    monkeypatch.setattr(db, "get_feedback_eligible_for_learning", spy)
    patch_taste(monkeypatch)

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    assert len(calls) >= 1


def test_excluded_feedback_never_enters_training(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    excluded_id = make_excluded_message(conn, "msg_excluded", ticker="EXCLUDED")
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    assert "Eligible training records: 15" in capsys.readouterr().out

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    training_ids = json.loads(latest["training_feedback_ids_json"])
    conn.close()

    assert excluded_id not in training_ids
    assert len(training_ids) == 15


def test_needs_review_feedback_never_enters_training(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    review_id = make_needs_review_message(conn, "msg_review", ticker="REVIEW")
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    assert "Eligible training records: 15" in capsys.readouterr().out

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    training_ids = json.loads(latest["training_feedback_ids_json"])
    conn.close()

    assert review_id not in training_ids
    assert len(training_ids) == 15


def test_new_idea_can_contribute_learning_signal(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 14)
    new_idea_id = make_parsed_message(
        conn, "msg_new_idea", event_type="NEW_IDEA", verdict=None, ticker=None, company="TESTCO"
    )
    conn.close()

    patch_taste(monkeypatch)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    training_ids = json.loads(latest["training_feedback_ids_json"])
    conn.close()

    assert new_idea_id in training_ids
    assert len(training_ids) == 15


def test_missed_idea_can_contribute_learning_signal(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 14)
    missed_idea_id = make_parsed_message(
        conn, "msg_missed_idea", event_type="MISSED_IDEA", verdict=None, ticker=None, company="MISSEDCO"
    )
    conn.close()

    patch_taste(monkeypatch)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    training_ids = json.loads(latest["training_feedback_ids_json"])
    conn.close()

    assert missed_idea_id in training_ids


# --- minimum training record requirement ------------------------------------


def test_build_taste_refuses_below_minimum_records(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, taste.MIN_TRAINING_RECORDS - 1)
    conn.close()

    patch_taste(monkeypatch)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1

    output = capsys.readouterr().out
    assert f"Eligible training records: {taste.MIN_TRAINING_RECORDS - 1}" in output
    assert "at least" in output

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn) is None
    conn.close()
    assert not (tmp_path / "idea-taste.md").exists()
    assert not (tmp_path / "candidate-permanent-rules.md").exists()


def test_build_taste_succeeds_at_exactly_the_minimum(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, taste.MIN_TRAINING_RECORDS)
    conn.close()

    patch_taste(monkeypatch)
    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0


# --- generated files ----------------------------------------------------------


def test_build_taste_generates_idea_taste_md(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, body="## Strong Positive Signals\nA specific fake signal.\n")
    cli.cmd_build_taste(config)

    idea_taste_path = tmp_path / "idea-taste.md"
    assert idea_taste_path.exists()
    content = idea_taste_path.read_text(encoding="utf-8")
    assert "# Far View Idea Taste" in content
    assert "Version: v1" in content
    assert "Training judgments: 15" in content
    assert "A specific fake signal." in content


def test_build_taste_generates_candidate_permanent_rules_md(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, rules=[make_rule(1)])
    cli.cmd_build_taste(config)

    candidate_path = tmp_path / "candidate-permanent-rules.md"
    assert candidate_path.exists()
    content = candidate_path.read_text(encoding="utf-8")
    assert "# Candidate Permanent Rules" in content
    assert "Candidate rule:** Rule 1" in content
    assert "Evidence:** Evidence 1" in content
    assert "Confidence:** HIGH" in content
    assert "NOT been added to" in content  # proposals-only disclaimer


def test_max_five_candidate_rules_enforced_by_generate_candidate_rules():
    """The real (unmocked) taste.generate_candidate_rules must cap at 5,
    even if the LLM's structured output returns more.
    """

    batch_json = json.dumps(
        {
            "rules": [
                {"rule": f"Rule {i}", "evidence": f"Evidence {i}", "reusability": f"Reuse {i}", "confidence": "LOW"}
                for i in range(7)
            ]
        }
    )

    class FakeTextBlock:
        type = "text"
        text = batch_json

    class FakeResponse:
        stop_reason = "end_turn"
        content = [FakeTextBlock()]

    class FakeMessages:
        def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    rules = taste.generate_candidate_rules(FakeClient(), "fake-model", [])
    assert len(rules) == taste.MAX_CANDIDATE_RULES == 5


def test_build_taste_renders_no_more_than_five_candidates_end_to_end(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, rules=[make_rule(i) for i in range(5)])
    cli.cmd_build_taste(config)

    content = (tmp_path / "candidate-permanent-rules.md").read_text(encoding="utf-8")
    assert content.count("## Candidate ") == 5


# --- exact training IDs / version metadata / hash ----------------------------


def test_exact_training_feedback_ids_persisted(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    ids = insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    training_ids = json.loads(latest["training_feedback_ids_json"])
    assert sorted(training_ids) == sorted(ids)
    assert latest["training_count"] == 15


def test_version_metadata_recorded_correctly(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    assert latest["version_number"] == 1
    assert latest["version_label"] == "v1"
    assert latest["model_name"] == config.taste_model_name
    assert latest["training_count"] == 15
    assert latest["generated_at"]
    assert latest["created_at"]
    # The DB row's paths are the AUTHORITATIVE immutable artifacts, under
    # taste-versions/v1/ -- not the canonical convenience copies.
    assert latest["idea_taste_path"] == str(tmp_path / "taste-versions" / "v1" / "idea-taste.md")
    assert latest["candidate_rules_path"] == str(
        tmp_path / "taste-versions" / "v1" / "candidate-permanent-rules.md"
    )
    # The canonical convenience copies are still refreshed alongside them.
    assert (tmp_path / "idea-taste.md").exists()
    assert (tmp_path / "candidate-permanent-rules.md").exists()


def test_idea_taste_hash_matches_the_written_file(tmp_path, monkeypatch):
    import hashlib

    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    written_content = (tmp_path / "idea-taste.md").read_text(encoding="utf-8")
    expected_hash = hashlib.sha256(written_content.encode("utf-8")).hexdigest()
    assert latest["idea_taste_sha256"] == expected_hash


# --- holdout bookkeeping -------------------------------------------------------


def test_holdout_starts_only_after_taste_v1_checkpoint(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    conn.close()

    assert holdout == []  # nothing new has arrived yet


def test_earlier_feedback_never_counts_toward_holdout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    training_ids = insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    holdout_ids = {row["feedback_id"] for row in holdout}
    conn.close()

    assert holdout_ids.isdisjoint(training_ids)


def test_next_eligible_feedback_does_count_toward_holdout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    new_id = make_parsed_message(conn, "msg_new_after", ticker="NEWAFTER")
    latest = db.get_latest_taste_version(conn)
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    conn.close()

    assert len(holdout) == 1
    assert holdout[0]["feedback_id"] == new_id


def test_excluded_or_review_feedback_does_not_count_toward_holdout(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    make_excluded_message(conn, "msg_excluded_after", ticker="EXCAFTER")
    make_needs_review_message(conn, "msg_review_after", ticker="REVAFTER")
    genuinely_eligible_id = make_parsed_message(conn, "msg_real_after", ticker="REALAFTER")

    latest = db.get_latest_taste_version(conn)
    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    conn.close()

    assert len(holdout) == 1
    assert holdout[0]["feedback_id"] == genuinely_eligible_id


def test_regeneration_blocked_while_holdout_below_20(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    for i in range(taste.HOLDOUT_SIZE - 1):  # 19 -- one short
        make_parsed_message(conn, f"msg_holdout_{i}", ticker=f"H{i}")
    conn.close()

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "frozen" in output.lower()
    assert f"{taste.HOLDOUT_SIZE - 1}/{taste.HOLDOUT_SIZE}" in output

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn)["version_label"] == "v1"  # no v2 created
    conn.close()


def test_regeneration_allowed_once_holdout_reaches_20(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    for i in range(taste.HOLDOUT_SIZE):
        make_parsed_message(conn, f"msg_holdout_{i}", ticker=f"H{i}")
    conn.close()

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    assert latest["version_label"] == "v2"


# --- taste-status --------------------------------------------------------------


def test_taste_status_reports_no_taste_yet(tmp_path, capsys):
    config = make_config(tmp_path)
    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "No taste model has been generated yet" in output
    assert "build-taste" in output


def test_taste_status_reports_current_version_and_holdout(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    make_parsed_message(conn, "msg_extra", ticker="EXTRA")
    conn.close()

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Taste version: v1" in output
    assert "Training judgments: 15" in output
    assert f"Holdout judgments collected: 1 / {taste.HOLDOUT_SIZE}" in output
    assert "Taste frozen: YES" in output


# --- IDEA_SCREEN_RULES.md is never modified -----------------------------------


def test_idea_screen_rules_file_is_never_modified(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    rules_path = tmp_path / "IDEA_SCREEN_RULES.md"
    rules_path.write_text("# Brad's permanent approved rules\n\n- Rule one\n", encoding="utf-8")
    original_content = rules_path.read_text(encoding="utf-8")
    original_mtime = rules_path.stat().st_mtime

    patch_taste(monkeypatch, rules=[make_rule(1)])
    cli.cmd_build_taste(config)

    assert rules_path.read_text(encoding="utf-8") == original_content
    assert rules_path.stat().st_mtime == original_mtime


# --- production-state safety ---------------------------------------------------


def test_generated_files_never_land_under_real_idea_scout_local(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    real_state_dir = Path.home() / "IdeaScoutLocal"
    idea_taste_path = tmp_path / "idea-taste.md"
    candidate_rules_path = tmp_path / "candidate-permanent-rules.md"

    assert idea_taste_path.is_relative_to(tmp_path)
    assert candidate_rules_path.is_relative_to(tmp_path)
    assert real_state_dir not in idea_taste_path.parents
    assert real_state_dir not in candidate_rules_path.parents


def test_build_taste_refuses_when_production_database_missing(tmp_path, monkeypatch):
    config = Config(
        agentmail_api_key=None,
        agentmail_inbox_id=None,
        database_path=tmp_path / "ideas.db",  # deliberately never created
        log_path=tmp_path / "app.log",
        anthropic_api_key="fake-anthropic-key",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("the LLM must never be contacted when the DB is missing")

    monkeypatch.setattr(taste, "build_client", fail_if_called)

    try:
        cli.cmd_build_taste(config)
        assert False, "should have raised"
    except db.ProductionDatabaseMissingError:
        pass


# --- Robustness fix: truncation/retry, atomic build --------------------------


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResponse:
    def __init__(self, stop_reason: str = "end_turn", text: str = ""):
        self.stop_reason = stop_reason
        self.content = [_FakeTextBlock(text)]


class _ScriptedMessages:
    """Fake `client.messages` whose .create() replays a fixed script of
    responses (or raises, for a scripted exception) and records every
    call it received.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("ran out of scripted responses")
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _ScriptedClient:
    def __init__(self, responses):
        self.messages = _ScriptedMessages(responses)


def _valid_batch_json(n: int = 0) -> str:
    return json.dumps(
        {
            "rules": [
                {"rule": f"R{i}", "evidence": f"E{i}", "reusability": f"U{i}", "confidence": "LOW"}
                for i in range(n)
            ]
        }
    )


def test_successful_idea_taste_not_repeated_when_only_candidate_rules_needs_retry(tmp_path, monkeypatch):
    """taste.generate_idea_taste_body and taste.generate_candidate_rules
    each own their own internal retry loop -- cli.py calls each exactly
    once. Proves that when only the candidate-rules call needs an internal
    retry, the (already-succeeded) idea-taste call is never repeated.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    idea_taste_calls = []

    def fake_idea_taste_body(client, model, records, logger=None):
        idea_taste_calls.append(1)
        return "## Strong Positive Signals\nFake.\n"

    fake_client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),  # candidate rules: 1st attempt truncated
            _FakeResponse("end_turn", _valid_batch_json(0)),  # 2nd attempt succeeds
        ]
    )
    monkeypatch.setattr(taste, "build_client", lambda api_key: fake_client)
    monkeypatch.setattr(taste, "generate_idea_taste_body", fake_idea_taste_body)
    # generate_candidate_rules is left as the REAL function, exercising its
    # actual retry loop against fake_client.

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    assert len(idea_taste_calls) == 1  # never repeated
    assert len(fake_client.messages.calls) == 2  # candidate rules needed exactly one retry

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn)["version_label"] == "v1"
    conn.close()


def test_failed_build_creates_no_taste_versions_row(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    monkeypatch.setattr(
        taste,
        "generate_candidate_rules",
        lambda client, model, records, logger=None: (_ for _ in ()).throw(
            taste.TasteGenerationError("simulated exhausted retries")
        ),
    )

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn) is None
    conn.close()


def test_failed_build_creates_no_holdout_checkpoint(tmp_path, monkeypatch):
    """Same failure as above, checked from the holdout-tracking angle: with
    no taste version at all, there is no checkpoint, so every eligible
    record is just... eligible, not "holdout" of anything.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    monkeypatch.setattr(
        taste,
        "generate_candidate_rules",
        lambda client, model, records, logger=None: (_ for _ in ()).throw(
            taste.TasteGenerationError("simulated exhausted retries")
        ),
    )
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    assert db.get_latest_taste_version(conn) is None
    conn.close()

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0


def test_failed_build_leaves_no_misleading_output_files_on_first_attempt(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    monkeypatch.setattr(
        taste,
        "generate_candidate_rules",
        lambda client, model, records, logger=None: (_ for _ in ()).throw(
            taste.TasteGenerationError("simulated exhausted retries")
        ),
    )
    cli.cmd_build_taste(config)

    assert not (tmp_path / "idea-taste.md").exists()
    assert not (tmp_path / "candidate-permanent-rules.md").exists()
    # No stray temp files left behind either, anywhere under tmp_path
    # (including inside a taste-versions/vN/ staging directory).
    assert not list(tmp_path.rglob("*.tmp"))


def test_failed_regeneration_never_overwrites_a_previously_valid_version(tmp_path, monkeypatch):
    """The critical atomic-build guarantee: once v1 exists and is valid, a
    later FAILED regeneration attempt must leave v1's files exactly as
    they were -- byte for byte -- not half-overwritten.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, body="## Strong Positive Signals\nOriginal v1 content.\n")
    cli.cmd_build_taste(config)

    original_idea_taste = (tmp_path / "idea-taste.md").read_text(encoding="utf-8")
    original_candidate_rules = (tmp_path / "candidate-permanent-rules.md").read_text(encoding="utf-8")

    conn = db.connect(config.database_path)
    for i in range(taste.HOLDOUT_SIZE):
        make_parsed_message(conn, f"msg_holdout_{i}", ticker=f"H{i}")
    conn.close()

    # Now attempt v2, but make candidate-rules generation fail completely.
    patch_taste(monkeypatch, body="## Strong Positive Signals\nThis should never be written.\n")
    monkeypatch.setattr(
        taste,
        "generate_candidate_rules",
        lambda client, model, records, logger=None: (_ for _ in ()).throw(
            taste.TasteGenerationError("simulated exhausted retries")
        ),
    )

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1

    assert (tmp_path / "idea-taste.md").read_text(encoding="utf-8") == original_idea_taste
    assert (tmp_path / "candidate-permanent-rules.md").read_text(encoding="utf-8") == original_candidate_rules

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    assert latest["version_label"] == "v1"  # still v1 -- v2 never got recorded


def test_successful_retry_creates_exactly_one_taste_v1(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    fake_client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),
            _FakeResponse("end_turn", _valid_batch_json(1)),
        ]
    )
    monkeypatch.setattr(taste, "build_client", lambda api_key: fake_client)
    monkeypatch.setattr(
        taste, "generate_idea_taste_body", lambda client, model, records, logger=None: "## Strong Positive Signals\nx\n"
    )

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    all_versions = conn.execute("SELECT version_label FROM taste_versions").fetchall()
    conn.close()

    assert len(all_versions) == 1
    assert all_versions[0]["version_label"] == "v1"


def test_training_ids_identical_across_internal_retries(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    expected_ids = insert_n_eligible(conn, 15)
    conn.close()

    fake_client = _ScriptedClient(
        [
            _FakeResponse("max_tokens", ""),  # candidate rules truncated once
            _FakeResponse("end_turn", _valid_batch_json(1)),  # then succeeds
        ]
    )
    monkeypatch.setattr(taste, "build_client", lambda api_key: fake_client)
    monkeypatch.setattr(
        taste, "generate_idea_taste_body", lambda client, model, records, logger=None: "## Strong Positive Signals\nx\n"
    )

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    training_ids = json.loads(latest["training_feedback_ids_json"])
    assert sorted(training_ids) == sorted(expected_ids)
    assert latest["training_count"] == 15


# --- Immutable versioned artifacts / atomic finalization ---------------------
#
# These prove the taste-artifact-finalization fix: writing a new version's
# complete artifacts to an immutable, version-specific staging directory
# and hash-verifying them BEFORE the taste_versions row commits, so that a
# DB failure (or a crash) between "files written" and "row committed" can
# never alter whichever version was previously active.


def test_version_specific_artifacts_exist_and_are_correct_before_db_activation(tmp_path, monkeypatch):
    """Proves step ordering: by the time db.insert_taste_version is
    called, the immutable version-specific artifact it is about to be
    recorded as pointing to already exists on disk with the exact final
    content -- nothing about it is provisional at that point.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, body="## Strong Positive Signals\nComplete before activation.\n")

    seen = {}
    original_insert = db.insert_taste_version

    def spy_insert(conn, **kwargs):
        version_path = Path(kwargs["idea_taste_path"])
        seen["existed"] = version_path.exists()
        seen["content"] = version_path.read_text(encoding="utf-8") if version_path.exists() else None
        seen["hash_matches"] = taste.compute_file_sha256(version_path) == kwargs["idea_taste_sha256"]
        return original_insert(conn, **kwargs)

    monkeypatch.setattr(db, "insert_taste_version", spy_insert)

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0
    assert seen["existed"] is True
    assert "Complete before activation." in seen["content"]
    assert seen["hash_matches"] is True


def test_db_insert_failure_preserves_previously_active_version_and_files(tmp_path, monkeypatch):
    """The core bug fix: a DB failure AFTER artifacts are fully generated
    and written must not alter the previously active version's row, its
    immutable artifacts, or the canonical convenience copies -- byte for
    byte.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, body="## Strong Positive Signals\nOriginal v1.\n")
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    original_row = dict(db.get_latest_taste_version(conn))
    for i in range(taste.HOLDOUT_SIZE):
        make_parsed_message(conn, f"msg_h_{i}", ticker=f"H{i}")
    conn.close()

    original_canonical_idea_taste = (tmp_path / "idea-taste.md").read_text(encoding="utf-8")
    original_canonical_rules = (tmp_path / "candidate-permanent-rules.md").read_text(encoding="utf-8")
    original_version_idea_taste = Path(original_row["idea_taste_path"]).read_text(encoding="utf-8")

    patch_taste(monkeypatch, body="## Strong Positive Signals\nShould never become active.\n")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(db, "insert_taste_version", boom)

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 1

    conn = db.connect(config.database_path)
    latest = dict(db.get_latest_taste_version(conn))
    conn.close()
    assert latest == original_row  # unchanged -- still v1, byte-for-byte same row

    assert (tmp_path / "idea-taste.md").read_text(encoding="utf-8") == original_canonical_idea_taste
    assert (tmp_path / "candidate-permanent-rules.md").read_text(encoding="utf-8") == original_canonical_rules
    assert Path(original_row["idea_taste_path"]).read_text(encoding="utf-8") == original_version_idea_taste


def test_orphan_version_directory_does_not_count_as_active(tmp_path, monkeypatch, capsys):
    """Simulates a crash that left a fully-written but never-committed
    v2 staging directory behind. Its presence must not change what
    get_latest_taste_version or taste-status report as active.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)  # commits v1

    orphan_dir = tmp_path / "taste-versions" / "v2"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "idea-taste.md").write_text("# Orphan, never activated\n", encoding="utf-8")
    (orphan_dir / "candidate-permanent-rules.md").write_text("# Orphan\n", encoding="utf-8")

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    assert latest["version_label"] == "v1"

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Taste version: v1" in output
    assert "Artifact integrity: OK" in output


def test_successful_build_creates_exactly_one_active_version_even_with_orphan_present(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    # A stray, never-activated v2 staging directory from some earlier
    # crashed attempt (e.g. a build that failed after writing files but
    # before the DB commit).
    orphan_dir = tmp_path / "taste-versions" / "v2"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "idea-taste.md").write_text("# Orphan\n", encoding="utf-8")
    (orphan_dir / "candidate-permanent-rules.md").write_text("# Orphan\n", encoding="utf-8")

    conn = db.connect(config.database_path)
    for i in range(taste.HOLDOUT_SIZE):
        make_parsed_message(conn, f"msg_h_{i}", ticker=f"H{i}")
    conn.close()

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0

    conn = db.connect(config.database_path)
    all_versions = conn.execute("SELECT version_label FROM taste_versions").fetchall()
    latest = db.get_latest_taste_version(conn)
    conn.close()

    assert sorted(row["version_label"] for row in all_versions) == ["v1", "v2"]
    assert latest["version_label"] == "v2"
    # The real v2 build overwrote the orphan's placeholder content with its
    # own real, hash-verified content.
    assert "# Orphan" not in Path(latest["idea_taste_path"]).read_text(encoding="utf-8")


def test_canonical_convenience_copy_failure_does_not_invalidate_committed_version(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch, body="## Strong Positive Signals\nAuthoritative content.\n")

    real_atomic_write = cli._atomic_write

    def boom_only_for_canonical_copy(path, content):
        # Only the canonical convenience copy (directly under tmp_path)
        # should fail -- the authoritative version-specific write must
        # still go through normally, exactly like a real disk hiccup that
        # happens to hit the second, non-critical write.
        if path.parent == tmp_path:
            raise OSError("simulated disk failure writing convenience copy")
        return real_atomic_write(path, content)

    monkeypatch.setattr(cli, "_atomic_write", boom_only_for_canonical_copy)

    exit_code = cli.cmd_build_taste(config)
    assert exit_code == 0  # the version is still valid and active
    assert "WARNING" in capsys.readouterr().out

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()
    assert latest["version_label"] == "v1"

    version_content = Path(latest["idea_taste_path"]).read_text(encoding="utf-8")
    assert "Authoritative content." in version_content
    taste.verify_taste_version_integrity(latest)  # does not raise


def test_stored_sha256_matches_authoritative_artifact(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    taste.verify_taste_version_integrity(latest)  # must not raise
    assert latest["idea_taste_sha256"] == taste.compute_file_sha256(Path(latest["idea_taste_path"]))


def test_backward_compatible_with_legacy_v1_row_pointing_directly_at_canonical_file(tmp_path, capsys):
    """Production's real taste v1 was built before this fix existed: its
    taste_versions row points idea_taste_path directly at the canonical
    idea-taste.md, with no taste-versions/v1/ directory at all. The
    integrity check and taste-status must keep working against that shape
    with no migration and no rewrite of v1.
    """
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)

    legacy_idea_taste_path = tmp_path / "idea-taste.md"
    legacy_content = "# Far View Idea Taste\n\nVersion: v1\nTraining judgments: 15\n"
    # newline="" avoids Windows' default text-mode "\n" -> "\r\n"
    # translation, matching how production's actual legacy v1 file's hash
    # was made consistent with its on-disk bytes.
    legacy_idea_taste_path.write_text(legacy_content, encoding="utf-8", newline="")
    legacy_hash = taste.compute_content_sha256(legacy_content)

    db.insert_taste_version(
        conn,
        version_number=1,
        version_label="v1",
        generated_at="2026-09-10T18:22:26+00:00",
        model_name="claude-opus-5",
        training_count=15,
        training_feedback_ids_json="[1, 2, 3]",
        checkpoint_feedback_id=15,
        idea_taste_path=str(legacy_idea_taste_path),
        idea_taste_sha256=legacy_hash,
        candidate_rules_path=str(tmp_path / "candidate-permanent-rules.md"),
        created_at="2026-09-10T18:22:26+00:00",
    )
    conn.close()

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Taste version: v1" in output
    assert "Artifact integrity: OK" in output


def test_taste_status_reports_artifact_integrity_ok(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 0
    assert "Artifact integrity: OK" in capsys.readouterr().out


def test_taste_status_detects_hash_mismatch(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    # Tamper with the authoritative artifact after the fact.
    Path(latest["idea_taste_path"]).write_text("tampered content", encoding="utf-8")

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 1
    output = capsys.readouterr().out
    assert "Artifact integrity: FAILED" in output


def test_taste_status_detects_missing_authoritative_artifact(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    conn = db.connect(config.database_path)
    insert_n_eligible(conn, 15)
    conn.close()

    patch_taste(monkeypatch)
    cli.cmd_build_taste(config)

    conn = db.connect(config.database_path)
    latest = db.get_latest_taste_version(conn)
    conn.close()

    Path(latest["idea_taste_path"]).unlink()

    exit_code = cli.cmd_taste_status(config)
    assert exit_code == 1
    assert "Artifact integrity: FAILED" in capsys.readouterr().out
