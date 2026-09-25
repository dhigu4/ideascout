"""Command-line entry points: check-mail, parse-mail, show-feedback, status.

This is the only file that argparse touches. Each cmd_* function also
returns a plain result value (not just prints to the screen), which is
what the tests exercise directly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from . import (
    agentmail_client,
    db,
    digest_reply,
    idea_extraction,
    notifier,
    parser,
    shadow,
    source_isolation,
    structured_llm,
    taste,
)
from . import config as config_module
from .config import Config, ConfigError, load_config
from .logger import get_logger, setup_logging
from .sources import base as source_base
from .sources import browser as source_browser
from .sources import raw_storage, yellowbrick


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write(path, content: str) -> None:
    """Write `content` to `path` via a temp-file-then-rename, so a reader
    never sees a partially-written file and a failure mid-write never
    leaves `path` itself corrupted -- whatever `path` contained before
    (valid content, or nothing) is untouched until the rename succeeds.

    newline="" is required: without it, Path.write_text's default text
    mode silently translates "\\n" to "\\r\\n" on Windows, so the bytes
    landing on disk would no longer match a SHA-256 computed from the
    in-memory string -- exactly the kind of mismatch taste artifact
    hash-verification exists to catch.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    tmp.replace(path)


def _sender_email_address(raw_sender: str | None) -> str:
    """Pull the bare email address out of a From header, which AgentMail
    may give us as either "user@example.com" or "Display Name
    <user@example.com>". Uses the standard library's own header parser
    (email.utils.parseaddr) rather than ad-hoc string splitting.
    """
    if not raw_sender:
        return ""
    _, address = parseaddr(raw_sender)
    return address.strip().lower()


def _is_allowed_brad_sender(raw_sender: str | None, allowed_senders: frozenset[str]) -> bool:
    return _sender_email_address(raw_sender) in allowed_senders


def cmd_init(config: Config) -> int:
    """Create a brand-new production database. The ONLY command allowed to
    do so -- every other command refuses outright if the database is
    missing (see db.open_production_database). Refuses if a database
    already exists at the target path.
    """
    try:
        result = db.initialize_new_database(config.database_path)
    except db.DatabaseAlreadyInitializedError as exc:
        print(f"Refusing to initialize: {exc}")
        return 1

    state_dir = config.database_path.parent
    backups_dir = state_dir / "backups"

    print("Initialized a new production database.")
    print(f"State directory: {state_dir} ({'created' if result['state_dir_created'] else 'already existed'})")
    print(f"Backups directory: {backups_dir} ({'created' if result['backups_dir_created'] else 'already existed'})")
    print(f"Database file: {config.database_path} (created, schema version {result['schema_version']})")
    print("")
    print(f"If you haven't already, put your real credentials in: {state_dir / '.env'}")
    return 0


def cmd_check_mail(config: Config) -> int:
    logger = get_logger()
    conn = db.open_production_database(config.database_path)

    try:
        client = agentmail_client.build_client(config.agentmail_api_key)
    except Exception as exc:  # SDK constructor failed (bad key format, etc.)
        message = f"Could not create AgentMail client: {exc}"
        logger.exception(message)
        db.set_meta(conn, "last_error", f"{utcnow_iso()} {message}")
        conn.close()
        print("AgentMail check FAILED")
        print(f"Error: {message}")
        return 1

    try:
        # include_unauthenticated=True: Stage 1 preserves every inbound
        # message regardless of authentication result -- it's Stage 2's
        # job to decide what to do with that signal, not Stage 1's to
        # silently drop mail. A second, separately-filtered listing tells
        # us which of those message_ids AgentMail actually authenticated
        # (see fetch_authenticated_message_ids for why it has to be done
        # this way).
        items = agentmail_client.list_all_message_items(
            client, config.agentmail_inbox_id, include_unauthenticated=True
        )
        authenticated_ids = agentmail_client.fetch_authenticated_message_ids(
            client, config.agentmail_inbox_id
        )
    except Exception as exc:
        message = f"Could not list messages from AgentMail: {exc}"
        logger.exception(message)
        db.set_meta(conn, "last_error", f"{utcnow_iso()} {message}")
        conn.close()
        print("AgentMail check FAILED")
        print(f"Error: {message}")
        return 1

    existing_ids = db.get_all_message_ids(conn)
    new_items = [item for item in items if item.message_id not in existing_ids]

    stored = 0
    already_known = len(items) - len(new_items)
    errors = 0

    for item in new_items:
        try:
            message = agentmail_client.fetch_message(
                client, config.agentmail_inbox_id, item.message_id
            )
            body_raw, body_format = agentmail_client.extract_body(message)
            sender_authenticated = (
                "AUTHENTICATED" if message.message_id in authenticated_ids else "UNAUTHENTICATED"
            )
            inserted = db.insert_message(
                conn,
                message_id=message.message_id,
                inbox_id=message.inbox_id,
                thread_id=message.thread_id,
                received_at=message.timestamp.isoformat(),
                sender=message.from_,
                recipients=agentmail_client.format_recipients(message),
                subject=message.subject,
                body_raw=body_raw,
                body_format=body_format,
                size_bytes=message.size,
                stored_at=utcnow_iso(),
                sender_authenticated=sender_authenticated,
            )
            if inserted:
                stored += 1
            else:
                # Only possible if the message showed up twice in this same
                # listing pass; the database's UNIQUE constraint is what
                # actually prevents a duplicate row from ever being written.
                already_known += 1
        except Exception as exc:
            message_text = f"Failed to store message_id={item.message_id}: {exc}"
            logger.exception(message_text)
            db.set_meta(conn, "last_error", f"{utcnow_iso()} {message_text}")
            errors += 1

    db.set_meta(conn, "last_check_at", utcnow_iso())
    db.maybe_create_routine_backup(conn, config.database_path)
    conn.close()

    print("AgentMail check complete")
    print(f"New messages found: {len(new_items)}")
    print(f"Stored successfully: {stored}")
    print(f"Already known: {already_known}")
    print(f"Errors: {errors}")

    return 0 if errors == 0 else 1


def _events_need_review(events) -> bool:
    """True if any event (ParserResult or a feedback DB row -- both expose
    event_type/verdict/confidence) should route the whole message to
    NEEDS_REVIEW.
    """
    for event in events:
        reason = parser.determine_review_reason(event["event_type"], event["verdict"], event["confidence"])
        if reason is not None:
            return True
    return False


def cmd_parse_mail(config: Config) -> int:
    """Turn messages pending feedback parsing into structured feedback rows.

    A message is only ever marked PARSED after ALL of its feedback events
    have been committed to SQLite as a single all-or-nothing batch. If
    anything goes wrong, the message is left as one of:
      - UNPARSED still (a database-write failure -- safe to retry as-is),
      - RETRYABLE_ERROR (a transient API failure -- retried automatically
        next run), or
      - ERROR (a non-transient failure -- not retried automatically).
    Its raw email fields are never touched either way. A message is only
    ever sent to the LLM if BOTH its sender is an approved Brad address
    AND AgentMail confirmed the message as authenticated -- otherwise it
    is marked SKIPPED_NOT_BRAD or SKIPPED_UNAUTHENTICATED (respectively)
    and left otherwise untouched. A matching From address alone is
    trivially spoofable; requiring authentication too is what makes the
    sender check meaningful.
    """
    logger = get_logger()
    conn = db.open_production_database(config.database_path)

    try:
        client = parser.build_client(config.anthropic_api_key)
    except Exception as exc:
        message = f"Could not create LLM client: {exc}"
        logger.exception(message)
        db.set_meta(conn, "last_error", f"{utcnow_iso()} {message}")
        conn.close()
        print("Feedback parse FAILED")
        print(f"Error: {message}")
        return 1

    pending_rows = db.get_messages_pending_feedback_parse(conn)
    existing_feedback_ids = db.get_feedback_message_ids(conn)

    parsed = 0
    needs_review = 0
    no_feedback = 0
    retryable_errors = 0
    errors = 0
    skipped_not_brad = 0
    skipped_unauthenticated = 0

    for row in pending_rows:
        message_id = row["message_id"]

        if message_id in existing_feedback_ids:
            # Crash recovery: feedback events already exist for this
            # message (a previous run wrote them but was interrupted
            # before updating feedback_parse_status). Reuse them instead
            # of spending another LLM call -- this is what guarantees no
            # LLM call ever happens twice for the same message_id.
            existing_events = db.get_feedback_events_for_message(conn, message_id)
            status = "NEEDS_REVIEW" if _events_need_review(existing_events) else "PARSED"
            db.set_feedback_parse_status(conn, message_id, status)
            if status == "PARSED":
                parsed += 1
            else:
                needs_review += 1
            continue

        if not _is_allowed_brad_sender(row["sender"], config.brad_allowed_senders):
            # Not from an approved Brad address (e.g. a newsletter or
            # Substack that will land in this inbox in a later stage).
            # The raw email is kept exactly as stored; it is simply never
            # handed to the feedback parser.
            db.set_feedback_parse_status(conn, message_id, "SKIPPED_NOT_BRAD")
            skipped_not_brad += 1
            continue

        if row["sender_authenticated"] != "AUTHENTICATED":
            # Sender address matches, but AgentMail did not confirm
            # SPF/DKIM/DMARC for this message (or authentication status is
            # unknown -- e.g. a message stored before this check existed).
            # A matching From address is trivially spoofable on its own;
            # requiring authentication too is what makes the sender check
            # actually mean something. Unknown never gets the benefit of
            # the doubt -- it is treated the same as a confirmed failure,
            # not as a pass. This is a policy decision, not a parser
            # failure, so it is never treated as transient/retryable.
            db.set_feedback_parse_status(conn, message_id, "SKIPPED_UNAUTHENTICATED")
            skipped_unauthenticated += 1
            continue

        db.record_parse_attempt(conn, message_id, utcnow_iso())

        # Stage 9: a reply to a send-digest email is NEVER handed to the
        # LLM-based direct-feedback parser below -- it is a fundamentally
        # different case (Brad has already seen IdeaScout's own screen
        # result/rationale, so it can never be a clean holdout judgment;
        # see digest_reply.py's module docstring) and is routed through a
        # wholly separate, deterministic, no-LLM parser instead. The ONLY
        # trusted signal that a message is a digest reply is its
        # thread_id matching a recorded digest_deliveries row -- a
        # digest-reply-looking subject with no such match is a fail-closed
        # safety net (UNMATCHED_DIGEST_REPLY), never treated as a genuine
        # digest reply and never treated as ordinary direct feedback.
        digest_delivery = db.get_digest_delivery_by_thread_id(conn, row["thread_id"])

        if digest_delivery is not None:
            delivery_items = [
                digest_reply.DeliveryItem(
                    source_id=item_row["source_id"],
                    ticker=item_row["ticker"],
                    company=item_row["company"],
                    position=item_row["position"],
                )
                for item_row in db.get_digest_delivery_items_with_source_info(
                    conn, digest_delivery["delivery_id"]
                )
            ]
            parse_result = digest_reply.parse_digest_reply(row["body_raw"], delivery_items)

            if not parse_result.ok:
                message_text = (
                    f"Digest-reply parse failed for message_id={message_id}: {parse_result.reason}"
                )
                logger.warning(message_text)
                db.set_feedback_parse_status(
                    conn,
                    message_id,
                    "NEEDS_REVIEW",
                    error=f"DIGEST_REPLY_PARSE_FAILED: {parse_result.reason}",
                )
                needs_review += 1
                continue

            try:
                db.insert_feedback_events(
                    conn,
                    message_id=message_id,
                    events=[
                        {
                            "event_type": "FEEDBACK",
                            "verdict": block.verdict,
                            "ticker": block.item.ticker,
                            "company": block.item.company,
                            "novelty": None,
                            "user_comment": block.comment,
                            "parsed_json": "{}",
                            "parser_version": digest_reply.PARSER_VERSION,
                            "model_name": digest_reply.MODEL_NAME,
                            "confidence": None,
                            "created_at": utcnow_iso(),
                            "feedback_origin": "DIGEST_REPLY",
                            "holdout_eligible": False,
                            "source_id": block.item.source_id,
                            "digest_delivery_id": digest_delivery["delivery_id"],
                        }
                        for block in parse_result.blocks
                    ],
                )
            except Exception as exc:
                message_text = f"Failed to save digest-reply feedback for message_id={message_id}: {exc}"
                logger.exception(message_text)
                db.set_meta(conn, "last_error", f"{utcnow_iso()} {message_text}")
                errors += 1
                continue

            db.set_feedback_parse_status(conn, message_id, "PARSED")
            parsed += 1
            continue

        if digest_reply.looks_like_digest_reply_subject(row["subject"]):
            message_text = (
                f"Message {message_id} subject looks like a digest reply but no matching "
                "digest_delivery thread was found"
            )
            logger.warning(message_text)
            db.set_feedback_parse_status(
                conn,
                message_id,
                "NEEDS_REVIEW",
                error=(
                    "UNMATCHED_DIGEST_REPLY: subject looks like a reply to a digest email, but no "
                    "recorded digest delivery matches this message's thread. Refusing to guess -- "
                    "review manually."
                ),
            )
            needs_review += 1
            continue

        try:
            events = parser.parse_message(client, config.parser_model_name, row["subject"], row["body_raw"])
        except parser.RetryableParserError as exc:
            message_text = f"Transient failure parsing message_id={message_id}: {exc}"
            logger.exception(message_text)
            db.set_feedback_parse_status(conn, message_id, "RETRYABLE_ERROR", error=str(exc))
            db.set_meta(conn, "last_error", f"{utcnow_iso()} {message_text}")
            retryable_errors += 1
            continue
        except Exception as exc:
            message_text = f"Failed to parse message_id={message_id}: {exc}"
            logger.exception(message_text)
            db.set_feedback_parse_status(conn, message_id, "ERROR", error=str(exc))
            db.set_meta(conn, "last_error", f"{utcnow_iso()} {message_text}")
            errors += 1
            continue

        if not events:
            # A successful parse that found nothing to report (e.g. a
            # purely informational email) must NOT be marked PARSED --
            # PARSED means at least one feedback row was committed for
            # this message, full stop. NO_FEEDBACK is its own terminal
            # status: not an error, not something to retry (there is
            # nothing more the LLM would find by trying again), and not a
            # review case (the parser wasn't uncertain -- it was sure
            # there was nothing to extract).
            db.set_feedback_parse_status(conn, message_id, "NO_FEEDBACK")
            no_feedback += 1
            continue

        try:
            db.insert_feedback_events(
                conn,
                message_id=message_id,
                events=[
                    {
                        "event_type": result.event_type,
                        "verdict": result.verdict,
                        "ticker": result.ticker,
                        "company": result.company,
                        "novelty": result.novelty,
                        "user_comment": result.user_comment,
                        "parsed_json": result.parsed_json,
                        "parser_version": parser.PARSER_VERSION,
                        "model_name": config.parser_model_name,
                        "confidence": result.confidence,
                        "created_at": utcnow_iso(),
                    }
                    for result in events
                ],
            )
        except Exception as exc:
            # None of this message's events were committed -- per spec,
            # the message must not be marked PARSED. Leave its status
            # exactly as it was (UNPARSED or RETRYABLE_ERROR) so a
            # transient database problem heals itself on the next run.
            message_text = f"Failed to save feedback for message_id={message_id}: {exc}"
            logger.exception(message_text)
            db.set_meta(conn, "last_error", f"{utcnow_iso()} {message_text}")
            errors += 1
            continue

        status = "NEEDS_REVIEW" if any(result.needs_review for result in events) else "PARSED"
        db.set_feedback_parse_status(conn, message_id, status)
        if status == "PARSED":
            parsed += 1
        else:
            needs_review += 1

    db.set_meta(conn, "last_parse_at", utcnow_iso())
    db.maybe_create_routine_backup(conn, config.database_path)
    conn.close()

    print("Feedback parse complete")
    print(f"Messages considered: {len(pending_rows)}")
    print(f"Parsed successfully: {parsed}")
    print(f"No feedback found: {no_feedback}")
    print(f"Needs review: {needs_review}")
    print(f"Retryable errors: {retryable_errors}")
    print(f"Errors: {errors}")
    print(f"Skipped (not a Brad sender): {skipped_not_brad}")
    print(f"Skipped (unauthenticated): {skipped_unauthenticated}")

    return 0 if (errors == 0 and retryable_errors == 0) else 1


def cmd_show_feedback(config: Config, limit: int = 10) -> int:
    conn = db.open_production_database(config.database_path)
    rows = db.get_latest_feedback(conn, limit=limit)
    conn.close()

    if not rows:
        print("No feedback records yet. Run 'python run.py parse-mail' first.")
        return 0

    for row in rows:
        date = row["received_at"] or row["created_at"]
        label = row["ticker"] or row["company"] or "(no ticker/company)"
        verdict = row["verdict"] or "-"
        confidence = row["confidence"]
        confidence_text = f"{confidence:.2f}" if confidence is not None else "n/a"
        comment = row["user_comment"] or "(no comment)"
        # event_index > 0 means this email produced more than one event
        # (e.g. Brad replying to a digest with feedback on several ideas).
        event_suffix = f" (event {row['event_index'] + 1})" if row["event_index"] else ""
        excluded_marker = " [EXCLUDED FROM LEARNING]" if row["excluded_from_learning"] else ""

        print(
            f"[{date}]{event_suffix} {label} | {row['event_type']} | "
            f"{verdict} | confidence={confidence_text}{excluded_marker}"
        )
        print(f'    "{comment}"')
        # Stage 9: DIGEST_REPLY feedback is learning-eligible but never a
        # clean holdout judgment (Brad already saw IdeaScout's screen
        # result before replying) -- always shown explicitly so this is
        # never mistaken for a blind DIRECT judgment.
        if row["feedback_origin"] == "DIGEST_REPLY":
            source_label = row["origin_source_name"] or "unknown"
            if row["ticker"]:
                source_label = f"{source_label}/{row['ticker']}"
            print(
                f"    Origin: DIGEST_REPLY | Holdout eligible: "
                f"{'YES' if row['holdout_eligible'] else 'NO'} | Source: {source_label}"
            )

    return 0


def _describe_feedback_row(row) -> str:
    label = row["ticker"] or row["company"] or "(no ticker/company)"
    verdict = row["verdict"] or "-"
    comment = row["user_comment"] or "(no comment)"
    return (
        f"feedback_id={row['feedback_id']} | {label} | {row['event_type']} | "
        f'verdict={verdict} | "{comment}"'
    )


def _ticker_company_label(row) -> str:
    return f"{row['ticker'] or '-'} / {row['company'] or '-'}"


def _set_feedback_exclusion(config: Config, ticker: str, excluded: bool) -> int:
    """Shared logic for exclude-feedback / include-feedback (ticker/company
    targeting): find every feedback row whose ticker or company matches,
    show it, and flip its excluded_from_learning flag. Never touches the
    raw email, never touches any other feedback row, and never deletes
    anything -- only this one boolean column on the matched row(s)
    changes. Unchanged from before --feedback-id targeting was added.
    """
    conn = db.open_production_database(config.database_path)
    matches = db.get_feedback_by_ticker_or_company(conn, ticker)

    if not matches:
        conn.close()
        print(f"No feedback records found matching {ticker!r}. Nothing changed.")
        return 1

    action = "Excluding from learning" if excluded else "Re-including in learning"
    print(f"{action} -- {len(matches)} matching feedback record(s) for {ticker!r}:")

    changed = 0
    for row in matches:
        already_set = bool(row["excluded_from_learning"]) == excluded
        note = "  (already set, no change)" if already_set else ""
        print(f"  {_describe_feedback_row(row)}{note}")
        db.set_feedback_excluded_from_learning(conn, row["feedback_id"], excluded)
        if not already_set:
            changed += 1

    conn.close()

    verb = "excluded from" if excluded else "included back into"
    print(f"Done: {len(matches)} record(s) now {verb} learning ({changed} changed).")
    return 0


def _set_feedback_exclusion_by_id(config: Config, feedback_id: int, excluded: bool) -> int:
    """Same guarantees as _set_feedback_exclusion, but targets EXACTLY ONE
    feedback row by primary key -- added so one duplicate/follow-up
    feedback event (e.g. a later reply/addendum judging the same idea a
    second time) can be excluded from learning without also excluding
    another, still-eligible feedback row for the same ticker/company.
    Never deletes anything; only that one row's excluded_from_learning
    column ever changes. A nonexistent feedback_id makes zero DB changes
    and fails clearly.
    """
    conn = db.open_production_database(config.database_path)
    row = db.get_feedback_by_id(conn, feedback_id)

    if row is None:
        conn.close()
        print(f"No feedback record found with feedback_id={feedback_id}. Nothing changed.")
        return 1

    already_set = bool(row["excluded_from_learning"]) == excluded
    action = "Excluding from learning" if excluded else "Re-including in learning"
    print(f"{action} -- feedback_id={feedback_id}:")
    note = "  (already set, no change)" if already_set else ""
    print(f"  {_describe_feedback_row(row)}{note}")

    db.set_feedback_excluded_from_learning(conn, feedback_id, excluded)
    conn.close()

    changed = 0 if already_set else 1
    verb = "excluded from" if excluded else "included back into"
    print(f"Feedback records {'excluded' if excluded else 'included'}: 1")
    print(f"feedback_id: {feedback_id}")
    print(f"ticker/company: {_ticker_company_label(row)}")
    print(f"Done: 1 record now {verb} learning ({changed} changed).")
    return 0


def cmd_exclude_feedback(config: Config, ticker: str | None, *, feedback_id: int | None = None) -> int:
    """Mark feedback record(s) as excluded_from_learning -- e.g. artificial
    smoke-test records, or one duplicate/follow-up judgment, that must
    stay in the database for audit/history but must never be used when a
    later stage learns Brad's preferences. The raw email and the feedback
    row itself are never deleted or otherwise modified.

    Exactly one targeting mode is used: `ticker` (by ticker or company,
    case-insensitive, may match several rows) OR `feedback_id` (exactly
    one row, by primary key) -- enforced by build_parser()'s mutually
    exclusive, required argument group, never by this function itself.
    """
    if feedback_id is not None:
        return _set_feedback_exclusion_by_id(config, feedback_id, excluded=True)
    return _set_feedback_exclusion(config, ticker, excluded=True)


def cmd_include_feedback(config: Config, ticker: str | None, *, feedback_id: int | None = None) -> int:
    """Reverse an accidental exclude-feedback: clears excluded_from_learning
    for the targeted feedback record(s) -- see cmd_exclude_feedback for the
    exactly-one-targeting-mode contract.
    """
    if feedback_id is not None:
        return _set_feedback_exclusion_by_id(config, feedback_id, excluded=False)
    return _set_feedback_exclusion(config, ticker, excluded=False)


def cmd_status(config: Config) -> int:
    try:
        conn = db.open_production_database(config.database_path)
    except db.ProductionDatabaseMissingError as exc:
        print("Database reachable: NO")
        print(f"Error: {exc}")
        return 3
    except Exception as exc:
        print("Database reachable: NO")
        print(f"Error: {exc}")
        return 1

    total = db.count_total_messages(conn)
    pending = db.count_feedback_pending_messages(conn)
    parsed = db.count_feedback_parsed_messages(conn)
    no_feedback = db.count_feedback_no_feedback_messages(conn)
    needs_review = db.count_feedback_needs_review_messages(conn)
    retryable_errors = db.count_feedback_retryable_error_messages(conn)
    error_count = db.count_feedback_error_messages(conn)
    skipped_not_brad = db.count_feedback_skipped_not_brad_messages(conn)
    skipped_unauthenticated = db.count_feedback_skipped_unauthenticated_messages(conn)
    feedback_records = db.count_feedback_records(conn)
    last_check = db.get_meta(conn, "last_check_at") or "never"
    last_parse = db.get_meta(conn, "last_parse_at") or "never"
    last_error = db.get_meta(conn, "last_error") or "none"
    violations = db.find_feedback_invariant_violations(conn)
    conn.close()

    print("Database reachable: YES")
    print(f"Total raw messages: {total}")
    print(f"Unparsed messages: {pending}")
    print(f"Parsed messages: {parsed}")
    print(f"No-feedback messages: {no_feedback}")
    print(f"Needs review: {needs_review}")
    print(f"Retryable errors: {retryable_errors}")
    print(f"Error messages: {error_count}")
    print(f"Skipped (not a Brad sender): {skipped_not_brad}")
    print(f"Skipped (unauthenticated): {skipped_unauthenticated}")
    print(f"Feedback records: {feedback_records}")
    print(f"Last successful inbox check: {last_check}")
    print(f"Last successful parse run: {last_parse}")
    print(f"Last error: {last_error}")
    print(f"Data integrity warnings: {len(violations)}")
    for violation in violations:
        print(f"  - {violation}")

    return 0 if not violations else 1


# Statuses eligible for requeue-feedback. PARSED is excluded (already
# successfully done); UNPARSED/RETRYABLE_ERROR are excluded because they
# are already pending and will be picked up by the next parse-mail run on
# their own; SKIPPED_NOT_BRAD and SKIPPED_UNAUTHENTICATED are excluded
# because requeuing either changes nothing on its own -- the sender still
# won't be approved, and authentication is a fact about the original
# message that a retry cannot alter.
REQUEUE_ELIGIBLE_STATUSES = ("ERROR", "NEEDS_REVIEW")


def _requeue_one_message(conn, message_id: str) -> tuple[bool, str]:
    """Attempt to requeue a single message. Returns (succeeded, a
    human-readable explanation of what happened or why it was refused).

    Never touches messages_raw's raw fields (subject/body_raw/sender/...)
    -- the email itself stays byte-for-byte authoritative. A PARSED
    message is refused outright (see the status check below), so its
    confirmed feedback can never be reached by this function.

    For ERROR: there are never any feedback rows yet (the LLM call itself
    failed before anything was saved), so resetting to UNPARSED alone is
    enough for the next parse-mail run to make a genuine fresh attempt.

    For NEEDS_REVIEW: feedback rows already exist -- that's what triggered
    the review. Those rows are derived, regenerable parser output (never
    Brad's raw email), specifically the ambiguous/low-confidence result
    that needs a second opinion. Leaving them in place would make
    parse-mail's own "never re-call the LLM for a message that already has
    feedback" rule silently no-op the requeue. So they are deleted here --
    and ONLY here, ONLY for a NEEDS_REVIEW message, which structurally can
    never be a PARSED one -- which is what makes the next parse-mail run
    genuinely re-parse it instead of just resyncing stale status.
    """
    row = db.get_message(conn, message_id)
    if row is None:
        return False, f"No message found with message_id={message_id!r}."

    status = row["feedback_parse_status"]

    if status not in REQUEUE_ELIGIBLE_STATUSES:
        if status == "PARSED":
            return False, (
                f"message_id={message_id!r} already has successfully parsed feedback "
                "(status=PARSED) -- refusing to requeue it."
            )
        if status == "SKIPPED_NOT_BRAD":
            return False, (
                f"message_id={message_id!r} was skipped because its sender is not an "
                "approved Brad address (status=SKIPPED_NOT_BRAD) -- requeuing would not "
                "change that outcome. Update BRAD_ALLOWED_SENDERS if this is wrong."
            )
        if status == "SKIPPED_UNAUTHENTICATED":
            return False, (
                f"message_id={message_id!r} was skipped because AgentMail did not confirm "
                "it as authenticated (status=SKIPPED_UNAUTHENTICATED) -- requeuing it would "
                "not change that outcome, since authentication is a property of the "
                "original message, not something a retry can fix."
            )
        if status == "NO_FEEDBACK":
            return False, (
                f"message_id={message_id!r} was parsed successfully but the parser found no "
                "feedback, new idea, or missed idea to record (status=NO_FEEDBACK) -- this is "
                "not a failure, so it is not eligible for automatic or manual requeue."
            )
        # UNPARSED or RETRYABLE_ERROR
        return False, (
            f"message_id={message_id!r} is already pending (status={status}) -- it will "
            "be picked up automatically on the next 'parse-mail' run."
        )

    if status == "NEEDS_REVIEW":
        superseded_count = db.delete_feedback_for_message(conn, message_id)
        db.set_feedback_parse_status(conn, message_id, "UNPARSED", error=None)
        return True, (
            f"Requeued message_id={message_id!r} (was NEEDS_REVIEW) to UNPARSED. "
            f"Removed {superseded_count} superseded (unconfirmed) feedback event(s) -- "
            "the raw email is unchanged, and the next 'parse-mail' run will call the LLM "
            "fresh for this message."
        )

    # status == "ERROR": no feedback rows exist yet, nothing to remove.
    db.set_feedback_parse_status(conn, message_id, "UNPARSED", error=None)
    return True, f"Requeued message_id={message_id!r} (was ERROR) to UNPARSED."


def cmd_requeue_feedback(config: Config, message_id: str | None, all_errors: bool) -> int:
    logger = get_logger()
    conn = db.open_production_database(config.database_path)

    if all_errors:
        error_ids = db.get_message_ids_by_feedback_status(conn, "ERROR")
        if not error_ids:
            conn.close()
            print("No messages are currently in ERROR state. Nothing to requeue.")
            return 0

        requeued = 0
        for one_message_id in error_ids:
            success, detail = _requeue_one_message(conn, one_message_id)
            print(detail)
            logger.info(detail)
            if success:
                requeued += 1
        conn.close()
        print(f"Requeued {requeued} of {len(error_ids)} ERROR message(s).")
        return 0

    success, detail = _requeue_one_message(conn, message_id)
    conn.close()
    print(detail)
    if success:
        logger.info(detail)
    else:
        logger.warning(detail)
    return 0 if success else 1


def _describe_feedback_event(event) -> str:
    label = event["ticker"] or event["company"] or "(no ticker/company)"
    verdict = event["verdict"] or "-"
    confidence = event["confidence"]
    confidence_text = f"{confidence:.2f}" if confidence is not None else "n/a"
    comment = event["user_comment"] or "(no comment)"
    return (
        f"[{event['event_index']}] {label} | {event['event_type']} | "
        f'verdict={verdict} | confidence={confidence_text}\n      "{comment}"'
    )


def cmd_show_review(config: Config) -> int:
    """Show every message currently awaiting human review, with its
    derived feedback event(s), so Brad can decide whether to approve them
    (approve-review) or send them back for a fresh parse (requeue-feedback).
    Purely a read: never changes any status or data.
    """
    conn = db.open_production_database(config.database_path)
    messages = db.get_messages_by_feedback_status(conn, "NEEDS_REVIEW")

    if not messages:
        conn.close()
        print("No messages currently need review.")
        return 0

    print(f"{len(messages)} message(s) need review -- NOT YET APPROVED for learning:")
    print()
    for message in messages:
        events = db.get_feedback_events_for_message(conn, message["message_id"])
        print(
            f"message_id={message['message_id']} | subject={message['subject']!r} | "
            f"received_at={message['received_at']}"
        )
        if message["error"]:
            print(f"  reason: {message['error']}")
        for event in events:
            print(f"  {_describe_feedback_event(event)}")
        if not events:
            # Stage 9: UNMATCHED_DIGEST_REPLY and DIGEST_REPLY_PARSE_FAILED
            # are the first-ever NEEDS_REVIEW cases with zero feedback
            # events -- there is nothing here for approve-review to
            # promote, so it must be requeued (or ignored) instead.
            print(
                f"  -> No feedback events were extracted. Cannot be approved. Use "
                f"requeue-feedback {message['message_id']!r} to force a fresh parse, or leave as-is."
            )
        else:
            print(
                f"  -> NOT YET APPROVED. Approve with: python run.py approve-review "
                f"{message['message_id']}"
            )
        print()

    conn.close()
    return 0


def cmd_approve_review(config: Config, message_id: str) -> int:
    """Approve a NEEDS_REVIEW message's feedback exactly as extracted.

    Changes ONLY feedback_parse_status (NEEDS_REVIEW -> PARSED). Never
    touches the raw email, and never touches the feedback row(s)
    themselves -- approving doesn't correct or re-derive anything, it
    just says "yes, this extraction is fine to use." Once PARSED, its
    feedback becomes eligible for learning (see
    db.get_feedback_eligible_for_learning); it never was while
    NEEDS_REVIEW, no matter how confident it looked.

    If the extracted feedback is actually wrong, use requeue-feedback
    instead -- that deletes the superseded event(s) and forces a fresh
    LLM parse, rather than approving what's already there.
    """
    conn = db.open_production_database(config.database_path)
    row = db.get_message(conn, message_id)

    if row is None:
        conn.close()
        print(f"No message found with message_id={message_id!r}.")
        return 1

    status = row["feedback_parse_status"]
    if status != "NEEDS_REVIEW":
        conn.close()
        print(
            f"message_id={message_id!r} is not eligible for approval "
            f"(status={status}, not NEEDS_REVIEW) -- refusing."
        )
        return 1

    events = db.get_feedback_events_for_message(conn, message_id)
    if not events:
        # Stage 9: UNMATCHED_DIGEST_REPLY and DIGEST_REPLY_PARSE_FAILED are
        # the first-ever NEEDS_REVIEW cases with zero feedback events.
        # Flipping straight to PARSED here would recreate exactly the
        # "PARSED with nothing to show for it" state Migrations 4 and 6
        # exist to repair -- refuse instead, and point at requeue-feedback,
        # which is the only path that can still do something useful (force
        # a fresh parse attempt).
        conn.close()
        print(
            f"message_id={message_id!r} has NO feedback events to approve (reason: "
            f"{row['error'] or 'unknown'}) -- refusing. Use requeue-feedback {message_id!r} "
            "to force a fresh parse instead."
        )
        return 1

    db.set_feedback_parse_status(conn, message_id, "PARSED", error=None)
    conn.close()

    print(f"Approved message_id={message_id!r}: feedback_parse_status NEEDS_REVIEW -> PARSED.")
    print(f"{len(events)} feedback event(s), unchanged, are now eligible for learning:")
    for event in events:
        print(f"  {_describe_feedback_event(event)}")
    return 0


def cmd_build_taste(config: Config) -> int:
    """Generate a new Far View Idea Taste version from Brad's accumulated
    eligible feedback (see db.get_feedback_eligible_for_learning -- the
    ONE canonical eligibility query; this command never recreates that
    logic). Requires at least taste.MIN_TRAINING_RECORDS eligible records,
    and refuses to regenerate while the previous version's time-forward
    holdout has fewer than taste.HOLDOUT_SIZE judgments collected.

    Every version's real, authoritative artifacts are immutable files
    under IdeaScoutLocal\\taste-versions\\vN\\ -- written, renamed into
    place, and hash-verified BEFORE the taste_versions row is committed.
    That DB commit is the one and only thing that makes a version
    "active"; nothing here ever decides the current version by looking at
    files on disk. A failure at any point before that commit -- including
    a process crash -- can leave orphaned vN files behind, but can never
    alter whichever version was previously active (v1's own row/files, or
    -- for a v1 built before this scheme existed -- the canonical files
    directly, since that is what v1's row already points at).

    idea-taste.md and candidate-permanent-rules.md directly under
    IdeaScoutLocal are refreshed as convenience copies only, AFTER the new
    version is already active -- if that refresh fails, the active
    version and its authoritative artifacts are unaffected; this prints a
    warning rather than failing or rolling anything back.

    Never touches IDEA_SCREEN_RULES.md -- that file is Brad's own
    permanent, manually curated rules and nothing here ever writes to it.
    """
    logger = get_logger()
    conn = db.open_production_database(config.database_path)

    eligible = db.get_feedback_eligible_for_learning(conn)
    print(f"Eligible training records: {len(eligible)}")

    if len(eligible) < taste.MIN_TRAINING_RECORDS:
        conn.close()
        print(
            f"Need at least {taste.MIN_TRAINING_RECORDS} eligible feedback records to "
            f"build a taste model; only {len(eligible)} available right now."
        )
        return 1

    latest = db.get_latest_taste_version(conn)
    if latest is not None:
        holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
        if len(holdout) < taste.HOLDOUT_SIZE:
            conn.close()
            print(
                f"Taste {latest['version_label']} is frozen for time-forward evaluation: "
                f"{len(holdout)}/{taste.HOLDOUT_SIZE} holdout judgments collected so far. "
                "Refusing to regenerate until all of them have accumulated -- see "
                "'python run.py taste-status'."
            )
            return 1

    try:
        client = taste.build_client(config.anthropic_api_key)
    except Exception as exc:
        message = f"Could not create LLM client: {exc}"
        logger.exception(message)
        conn.close()
        print(f"build-taste FAILED: {message}")
        return 1

    records = taste.training_records_from_rows(eligible)

    # Both generations happen entirely in memory before anything touches
    # disk or the database -- a failure here (even after some internal
    # retries inside taste.py) leaves no trace anywhere.
    try:
        body = taste.generate_idea_taste_body(client, config.taste_model_name, records, logger=logger)
        rules = taste.generate_candidate_rules(client, config.taste_model_name, records, logger=logger)
    except taste.TasteGenerationError as exc:
        logger.exception(str(exc))
        conn.close()
        print(f"build-taste FAILED: {exc}")
        return 1

    version_number = (latest["version_number"] + 1) if latest is not None else 1
    version_label = f"v{version_number}"
    generated_date = date.today().isoformat()
    generated_at = utcnow_iso()

    idea_taste_content = taste.render_idea_taste_document(
        version_label=version_label,
        generated_date=generated_date,
        training_count=len(eligible),
        body=body,
    )
    idea_taste_sha256 = taste.compute_content_sha256(idea_taste_content)
    candidate_rules_content = taste.render_candidate_rules_document(
        version_label=version_label,
        generated_date=generated_date,
        training_count=len(eligible),
        rules=rules,
    )

    training_ids = sorted(row["feedback_id"] for row in eligible)
    checkpoint_feedback_id = training_ids[-1]

    # Immutable, version-specific staging directory. Every future build
    # writes its complete artifacts here FIRST, at a path unique to this
    # version number -- never at the canonical idea-taste.md /
    # candidate-permanent-rules.md paths directly. Nothing below is
    # "active" until the taste_versions row is committed at the end: a
    # crash or failure at any point up to and including that commit call
    # leaves these as harmless orphan files and never touches whichever
    # version was previously active (an older version's own immutable
    # directory, or -- for a v1 built before this scheme existed -- the
    # canonical files directly, since that is what v1's row points at).
    version_dir = config.database_path.parent / "taste-versions" / version_label
    version_dir.mkdir(parents=True, exist_ok=True)

    idea_taste_version_path = version_dir / "idea-taste.md"
    candidate_rules_version_path = version_dir / "candidate-permanent-rules.md"
    idea_taste_version_tmp = idea_taste_version_path.with_suffix(idea_taste_version_path.suffix + ".tmp")
    candidate_rules_version_tmp = candidate_rules_version_path.with_suffix(
        candidate_rules_version_path.suffix + ".tmp"
    )

    try:
        # 1-3: documents already exist as validated strings in memory;
        # write each to a temp file inside this version's own directory
        # and atomically rename it into its immutable, version-specific
        # final filename.
        _atomic_write(idea_taste_version_path, idea_taste_content)
        _atomic_write(candidate_rules_version_path, candidate_rules_content)

        # 4: verify hashes. The bytes now sitting at the immutable paths
        # must be exactly what was generated in memory before any of this
        # is allowed to become active -- this is what would catch, e.g., a
        # filesystem-level write that silently truncated or corrupted the
        # file despite the rename succeeding.
        taste.verify_file_sha256(idea_taste_version_path, idea_taste_sha256, label="idea-taste.md")
        if candidate_rules_version_path.read_text(encoding="utf-8") != candidate_rules_content:
            raise taste.TasteIntegrityError(
                f"candidate-permanent-rules.md content mismatch after write: {candidate_rules_version_path}"
            )

        # 5: only now, with complete and verified immutable artifacts
        # already on disk, commit the taste_versions row. This single
        # commit is the ENTIRE definition of "active" -- taste-status and
        # all future screening logic read only this row, never file
        # presence, to determine the current version.
        db.insert_taste_version(
            conn,
            version_number=version_number,
            version_label=version_label,
            generated_at=generated_at,
            model_name=config.taste_model_name,
            training_count=len(eligible),
            training_feedback_ids_json=json.dumps(training_ids),
            checkpoint_feedback_id=checkpoint_feedback_id,
            idea_taste_path=str(idea_taste_version_path),
            idea_taste_sha256=idea_taste_sha256,
            candidate_rules_path=str(candidate_rules_version_path),
            created_at=generated_at,
        )
    except Exception as exc:
        message = f"Failed to finalize taste {version_label}: {exc}"
        logger.exception(message)
        # Best-effort tidy-up of this not-yet-active version's staging
        # files. Purely cosmetic: whether or not this succeeds, nothing
        # here was ever active (that is decided solely by the
        # taste_versions row, which was never committed), and whatever
        # version was previously active -- rows and files alike -- was
        # never touched by any of the code above.
        for stray in (
            idea_taste_version_tmp,
            candidate_rules_version_tmp,
            idea_taste_version_path,
            candidate_rules_version_path,
        ):
            try:
                stray.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            version_dir.rmdir()  # only succeeds if now empty
        except OSError:
            pass
        conn.close()
        print(f"build-taste FAILED: {message}")
        return 1

    # From here on {version_label} IS the active version -- the DB row is
    # committed -- no matter what happens next. Refreshing the canonical
    # convenience copies is a nicety for easy reading, not part of
    # activation, so its failure is reported as a warning, never as a
    # command failure or a reason to touch the row/artifacts above.
    canonical_idea_taste_path = config.database_path.parent / "idea-taste.md"
    canonical_candidate_rules_path = config.database_path.parent / "candidate-permanent-rules.md"
    try:
        _atomic_write(canonical_idea_taste_path, idea_taste_content)
        _atomic_write(canonical_candidate_rules_path, candidate_rules_content)
    except Exception as exc:
        logger.exception(f"Failed to refresh canonical convenience copies for {version_label}: {exc}")
        print(
            f"WARNING: taste {version_label} is active and its authoritative artifacts are "
            f"valid, but refreshing the convenience copies at {canonical_idea_taste_path} and "
            f"{canonical_candidate_rules_path} failed: {exc}"
        )

    conn.close()

    print(f"Generated taste {version_label} from {len(eligible)} eligible feedback record(s).")
    print(f"  {idea_taste_version_path}")
    print(f"  {candidate_rules_version_path}")
    print(f"{len(rules)} candidate permanent rule(s) proposed -- not applied automatically.")
    print(
        f"Holdout for {version_label} starts now: the next {taste.HOLDOUT_SIZE} eligible "
        "judgments must accumulate before this can be regenerated."
    )
    return 0


def cmd_taste_status(config: Config) -> int:
    conn = db.open_production_database(config.database_path)
    latest = db.get_latest_taste_version(conn)

    if latest is None:
        conn.close()
        print("No taste model has been generated yet.")
        print(
            f"Run 'python run.py build-taste' once at least {taste.MIN_TRAINING_RECORDS} "
            "eligible feedback records exist."
        )
        return 0

    holdout = db.get_eligible_feedback_after(conn, latest["checkpoint_feedback_id"])
    digest_reply_count = db.count_feedback_by_origin(conn, "DIGEST_REPLY")
    conn.close()

    frozen = len(holdout) < taste.HOLDOUT_SIZE

    print(f"Taste version: {latest['version_label']}")
    print(f"Training judgments: {latest['training_count']}")
    print(f"Holdout judgments collected: {len(holdout)} / {taste.HOLDOUT_SIZE}")
    print(f"Taste frozen: {'YES' if frozen else 'NO'}")
    if digest_reply_count:
        # Stage 9: learning-eligible but NEVER counted in "Holdout
        # judgments collected" above -- see db.get_eligible_feedback_after.
        print(f"Digest-reply feedback records: {digest_reply_count}")

    # Fail loudly rather than silently trusting a file that may have been
    # edited, truncated, or lost since this version was activated. This
    # checks the authoritative artifact the DB row points at -- for a
    # legacy v1 built before immutable per-version directories existed,
    # that is the canonical idea-taste.md itself; for every version since,
    # it is the immutable taste-versions/vN/idea-taste.md.
    try:
        taste.verify_taste_version_integrity(latest)
    except taste.TasteIntegrityError as exc:
        print(f"Artifact integrity: FAILED -- {exc}")
        return 1

    print("Artifact integrity: OK")
    return 0


# --- Stage 4: blind shadow screening for the Taste v1 holdout ---------------
#
# SHADOW MODE ONLY. Nothing in this section ever regenerates or modifies
# Taste v1, changes the holdout checkpoint, or writes to
# IDEA_SCREEN_RULES.md. Nothing here ever shows Brad a prediction before
# his own eligible feedback for that idea already exists in the database
# (see cmd_show_shadow_results) -- shadow-status shows aggregate counts
# only, never an individual idea's prediction.


def cmd_shadow_score(config: Config) -> int:
    """Blind shadow-screen every post-Taste-v1 holdout idea whose source
    material can be reliably isolated from Brad's own commentary.

    Three cost-tiered steps run per eligible holdout message, each
    idempotent and skipped if already done:
      Level 0 (free, local, no LLM): isolate source material from Brad's
        commentary (source_isolation.isolate_source). If it can't be done
        reliably, the message is recorded UNSCORABLE_SOURCE with a reason
        and permanently skipped -- never guessed at, never retried.
      Level 1 (cheap model): extract a compact, source-only idea record
        (idea_extraction.extract_idea). Left PENDING (not a terminal
        error) on failure, so a future run retries automatically.
      Level 2 (capable model): screen that compact record against the
        frozen Taste v1 artifact and canonical IDEA_SCREEN_RULES.md
        (shadow.screen_idea), recording a new, immutable prediction --
        unless an identical (idea, taste version, rules hash) prediction
        already exists, in which case it is skipped, never overwritten.

    Uses db.get_eligible_feedback_after -- the SAME canonical holdout
    query taste-status uses -- so shadow-score can never disagree with
    the holdout bookkeeping about which ideas are in scope. It never
    touches taste_versions, never touches the holdout checkpoint, and
    never reads candidate-permanent-rules.md.
    """
    logger = get_logger()
    conn = db.open_production_database(config.database_path)

    latest_taste = db.get_latest_taste_version(conn)
    if latest_taste is None:
        conn.close()
        print("No taste model has been generated yet. Run 'python run.py build-taste' first.")
        return 1

    try:
        taste.verify_taste_version_integrity(latest_taste)
    except taste.TasteIntegrityError as exc:
        conn.close()
        print(f"shadow-score FAILED: taste {latest_taste['version_label']} artifact integrity check failed: {exc}")
        return 1

    if not config.screen_rules_path.exists():
        conn.close()
        print(
            f"shadow-score FAILED: canonical screening rules file not found at "
            f"{config.screen_rules_path}. Set SCREEN_RULES_PATH in .env if it lives elsewhere."
        )
        return 1

    idea_taste_content = Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8")
    screen_rules_content = config.screen_rules_path.read_text(encoding="utf-8")
    screen_rules_sha256 = taste.compute_content_sha256(screen_rules_content)

    holdout_rows = db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"])
    # Dedupe by message_id: multiple feedback events on the same message
    # (e.g. one email covering several ideas) share one source and one
    # idea record -- see idea_records.message_id UNIQUE constraint. Order
    # preserved (dict.fromkeys), oldest first, matching holdout order.
    message_ids = list(dict.fromkeys(row["message_id"] for row in holdout_rows))
    print(f"Holdout messages to consider: {len(message_ids)}")

    # The already-parsed feedback event's own user_comment, keyed by
    # message_id -- used ONLY by source_isolation's inline-pasted-source
    # fallback as a delimiter (Stage 5.13), never as source material
    # itself. setdefault keeps the FIRST feedback row's comment when a
    # message produced more than one event.
    comment_by_message_id: dict[str, str | None] = {}
    for row in holdout_rows:
        comment_by_message_id.setdefault(row["message_id"], row["user_comment"])

    isolated_now = 0
    unscorable_now = 0
    reisolation_attempts = 0
    reisolated_now = 0
    extracted_now = 0
    predictions_now = 0
    already_predicted = 0

    idea_extraction_client = None  # built lazily -- only if actually needed
    shadow_client = None

    for message_id in message_ids:
        idea_record = db.get_idea_record_by_message_id(conn, message_id)

        if idea_record is None:
            message = db.get_message(conn, message_id)
            isolation = source_isolation.isolate_source(
                message["body_raw"] if message is not None else None,
                user_comment=comment_by_message_id.get(message_id),
            )
            isolated_at = utcnow_iso()
            if not isolation.scorable:
                db.insert_idea_record_unscorable(
                    conn,
                    message_id=message_id,
                    unscorable_reason=isolation.unscorable_reason,
                    source_isolated_at=isolated_at,
                )
                unscorable_now += 1
                continue
            source_hash = taste.compute_content_sha256(isolation.source_text)
            db.insert_idea_record_isolated(
                conn,
                message_id=message_id,
                source_type=isolation.source_type,
                source_text=isolation.source_text,
                source_hash=source_hash,
                source_isolated_at=isolated_at,
            )
            isolated_now += 1
            idea_record = db.get_idea_record_by_message_id(conn, message_id)

        elif idea_record["source_status"] == "UNSCORABLE_SOURCE":
            # Stage 5.13 robustness fix: the deterministic isolator may
            # have improved since this row was marked unscorable (e.g.
            # the new inline-pasted-source fallback). Safe to retry with
            # the CURRENT isolator on every run -- it's pure/free/no-LLM
            # -- since a genuinely still-ambiguous message just fails the
            # same way again, cheaply, with zero DB writes. Never touches
            # a row that has already reached ISOLATED/EXTRACTED.
            reisolation_attempts += 1
            message = db.get_message(conn, message_id)
            isolation = source_isolation.isolate_source(
                message["body_raw"] if message is not None else None,
                user_comment=comment_by_message_id.get(message_id),
            )
            if isolation.scorable:
                source_hash = taste.compute_content_sha256(isolation.source_text)
                db.update_idea_record_reisolated(
                    conn,
                    idea_id=idea_record["idea_id"],
                    source_type=isolation.source_type,
                    source_text=isolation.source_text,
                    source_hash=source_hash,
                    source_isolated_at=utcnow_iso(),
                )
                reisolated_now += 1
                idea_record = db.get_idea_record_by_message_id(conn, message_id)
            # else: still unscorable -- leave the row exactly as it is,
            # with no write at all, so timestamps/reason text are never
            # churned when nothing has actually changed.

        if idea_record["source_status"] != "ISOLATED":
            continue  # still UNSCORABLE_SOURCE -- never guessed at, never force-processed

        if idea_record["extraction_status"] != "EXTRACTED":
            if idea_extraction_client is None:
                idea_extraction_client = idea_extraction.build_client(config.anthropic_api_key)
            try:
                extracted = idea_extraction.extract_idea(
                    idea_extraction_client, config.parser_model_name, idea_record["source_text"], logger=logger
                )
            except structured_llm.StructuredGenerationError as exc:
                logger.exception(str(exc))
                print(f"  Idea extraction failed for message {message_id}: {exc} (will retry on a future run)")
                continue
            db.update_idea_record_extracted(
                conn,
                idea_id=idea_record["idea_id"],
                company=extracted.company or None,
                ticker=extracted.ticker or None,
                # ExtractedIdea no longer extracts title/date (see
                # idea_extraction.py) -- idea_records has no discovery-
                # time source for these either, so they simply stay
                # whatever they already were (None, for a first extraction).
                source_title=idea_record["source_title"],
                source_date=idea_record["source_date"],
                business_summary=extracted.business_summary,
                core_thesis=extracted.core_thesis,
                why_mispriced=extracted.why_mispriced,
                future_earnings_change=extracted.future_earnings_change,
                upside_case=extracted.upside_case,
                downside_or_key_risks=extracted.downside_or_key_risks,
                catalysts=extracted.catalysts,
                what_must_be_true=extracted.what_must_be_true,
                evidence_of_market_misunderstanding=extracted.evidence_of_market_misunderstanding,
                known_unknowns=extracted.known_unknowns,
                extraction_model_name=config.parser_model_name,
                extracted_at=utcnow_iso(),
            )
            extracted_now += 1
            idea_record = db.get_idea_record_by_message_id(conn, message_id)

        existing_prediction = db.get_shadow_prediction(
            conn,
            idea_id=idea_record["idea_id"],
            taste_version=latest_taste["version_number"],
            screen_rules_sha256=screen_rules_sha256,
        )
        if existing_prediction is not None:
            already_predicted += 1
            continue

        if shadow_client is None:
            shadow_client = shadow.build_client(config.anthropic_api_key)
        try:
            prediction = shadow.screen_idea(
                shadow_client,
                config.taste_model_name,
                idea_taste_body=idea_taste_content,
                screen_rules_body=screen_rules_content,
                idea_record=idea_record,
                logger=logger,
            )
        except structured_llm.StructuredGenerationError as exc:
            logger.exception(str(exc))
            print(f"  Shadow screening failed for message {message_id}: {exc} (will retry on a future run)")
            continue

        db.insert_shadow_prediction(
            conn,
            idea_id=idea_record["idea_id"],
            created_at=utcnow_iso(),
            taste_version=latest_taste["version_number"],
            taste_sha256=latest_taste["idea_taste_sha256"],
            screen_rules_path=str(config.screen_rules_path),
            screen_rules_sha256=screen_rules_sha256,
            source_hash=idea_record["source_hash"],
            model_name=config.taste_model_name,
            overall_prediction=prediction.overall_prediction,
            mispricing=prediction.mispricing,
            variant_perception=prediction.variant_perception,
            upside=prediction.upside,
            business_quality=prediction.business_quality,
            downside=prediction.downside,
            key_reasons_json=json.dumps(prediction.key_reasons),
            key_concerns_json=json.dumps(prediction.key_concerns),
            critical_questions_json=json.dumps(prediction.critical_questions),
            confidence=prediction.confidence,
        )
        predictions_now += 1

    conn.close()

    print(f"Sources isolated this run: {isolated_now}")
    print(f"Unscorable sources this run: {unscorable_now}")
    if reisolation_attempts:
        print(f"Previously-unscorable sources retried this run: {reisolation_attempts}")
        print(f"Previously-unscorable sources now isolated: {reisolated_now}")
    print(f"Ideas extracted this run: {extracted_now}")
    print(f"Shadow predictions created this run: {predictions_now}")
    if already_predicted:
        print(f"Already had a prediction for this taste/rules version: {already_predicted}")
    return 0


def cmd_shadow_status(config: Config) -> int:
    """Aggregate counts ONLY. Must never reveal an individual idea's
    prediction -- see cmd_show_shadow_results for the (gated) command
    that does that.
    """
    conn = db.open_production_database(config.database_path)
    latest_taste = db.get_latest_taste_version(conn)

    if latest_taste is None:
        conn.close()
        print("No taste model has been generated yet.")
        return 0

    holdout = db.get_eligible_feedback_after(conn, latest_taste["checkpoint_feedback_id"])
    isolated = db.count_idea_records_by_source_status(conn, "ISOLATED")
    unscorable = db.count_idea_records_by_source_status(conn, "UNSCORABLE_SOURCE")
    predictions = db.count_shadow_predictions_for_taste_version(conn, latest_taste["version_number"])
    conn.close()

    print(f"Taste version: {latest_taste['version_label']}")
    print(f"Holdout judgments collected: {len(holdout)} / {taste.HOLDOUT_SIZE}")
    print(f"Sources isolated: {isolated}")
    print(f"Unscorable sources: {unscorable}")
    print(f"Shadow predictions generated: {predictions}")
    return 0


def cmd_show_shadow_results(config: Config) -> int:
    """Reveal a shadow prediction next to Brad's actual verdict, but ONLY
    for ideas where Brad's own eligible feedback already exists. This is
    checked fresh per idea (db.get_eligible_feedback_for_message), never
    assumed from holdout membership, so the no-leakage/no-influence
    guarantee holds even as the system grows beyond the holdout-only case
    this stage covers. Never updates taste or anything else -- read-only.
    """
    conn = db.open_production_database(config.database_path)

    idea_ids = db.get_idea_ids_with_shadow_predictions(conn)
    shown = 0
    for idea_id in idea_ids:
        idea_record = db.get_idea_record(conn, idea_id)
        judged_feedback = db.get_eligible_feedback_for_message(conn, idea_record["message_id"])
        if not judged_feedback:
            continue  # Brad has not (yet) supplied an eligible judgment -- never reveal

        prediction = db.get_latest_shadow_prediction_for_idea(conn, idea_id)
        label = idea_record["company"] or idea_record["ticker"] or idea_record["message_id"]
        ticker_suffix = f" ({idea_record['ticker']})" if idea_record["ticker"] and idea_record["ticker"] != label else ""

        for feedback_row in judged_feedback:
            print(f"=== {label}{ticker_suffix} ===")
            print(f"Shadow prediction: {prediction['overall_prediction']} (confidence: {prediction['confidence']})")
            print(f"Brad's actual verdict: {feedback_row['verdict'] or feedback_row['event_type']}")
            print(f"Prediction rationale: {'; '.join(json.loads(prediction['key_reasons_json'])) or '(none given)'}")
            print(f"Brad's actual reason: {feedback_row['user_comment'] or '(no comment)'}")
            print()
            shown += 1

    if shown == 0:
        print("No judged, scorable shadow predictions to show yet.")
    conn.close()
    return 0


# --- Stage 5: authenticated website-source collection (first: Yellowbrick) --
#
# The intelligence/screening layer (idea_extraction.py, shadow.py) has no
# Yellowbrick-specific logic at all -- everything site-specific lives
# behind ideascout/sources/yellowbrick.py's adapter interface. preview-digest
# only ever prints to the terminal and never imports notifier/agentmail_client
# at all. Real outbound delivery (Stage 6: send-digest) exists but is gated
# by Config.alerts_enabled (default false) -- see cmd_send_digest.

SOURCE_ADAPTERS = {
    yellowbrick.SOURCE_NAME: yellowbrick,
}

# collect-source --external-id (Stage 5.8): a generous discovery cap used
# ONLY when targeting one specific document by id, so the single already-
# loaded feed page is scanned in full rather than truncated at the normal
# --limit default -- never used for the ordinary feed-sweep path, and never
# a second page load or any pagination/scrolling.
EXTERNAL_ID_DISCOVERY_LIMIT = 500


class TasteOrRulesUnavailableError(RuntimeError):
    """Raised by _load_frozen_taste_and_rules when there's no active taste
    version, its artifact fails integrity verification, or the canonical
    IDEA_SCREEN_RULES.md is missing. Screening simply can't happen yet --
    this is not the same as a screening/extraction failure.
    """


def _load_frozen_taste_and_rules(conn, config: Config) -> tuple:
    """Shared by collect-source's screening sweep: load the active taste
    version (hash-verified) and canonical IDEA_SCREEN_RULES.md. Reuses the
    SAME taste.verify_taste_version_integrity/compute_content_sha256 Stage
    3/4 already rely on -- never re-derives that logic. Returns
    (latest_taste_row, idea_taste_content, screen_rules_content, screen_rules_sha256).
    """
    latest_taste = db.get_latest_taste_version(conn)
    if latest_taste is None:
        raise TasteOrRulesUnavailableError("No taste model has been generated yet.")
    try:
        taste.verify_taste_version_integrity(latest_taste)
    except taste.TasteIntegrityError as exc:
        raise TasteOrRulesUnavailableError(
            f"taste {latest_taste['version_label']} artifact integrity check failed: {exc}"
        ) from exc
    if not config.screen_rules_path.exists():
        raise TasteOrRulesUnavailableError(
            f"canonical screening rules file not found at {config.screen_rules_path}"
        )
    idea_taste_content = Path(latest_taste["idea_taste_path"]).read_text(encoding="utf-8")
    screen_rules_content = config.screen_rules_path.read_text(encoding="utf-8")
    screen_rules_sha256 = taste.compute_content_sha256(screen_rules_content)
    return latest_taste, idea_taste_content, screen_rules_content, screen_rules_sha256


def _read_source_raw_text(adapter, row) -> str:
    """Re-derive plain text from a source's permanently-saved raw HTML.
    Text is never stored separately in the database -- the raw HTML is
    the one permanent artifact; text is just a cheap, deterministic view
    of it, recomputed on demand for extraction.
    """
    html = Path(row["raw_html_path"]).read_text(encoding="utf-8")
    return adapter.html_to_text(html)


def cmd_source_login(config: Config, source_name: str) -> int:
    """Manual, one-time (per session-expiry) authentication for a
    website-source adapter. Opens a VISIBLE Chrome window using a
    DEDICATED persistent profile (never Brad's normal Chrome profile --
    see ideascout/sources/browser.py), waits for Brad to log in by hand,
    verifies access, then closes Chrome. The profile itself is preserved
    for future runs; closing Chrome here does not log Brad out. No
    credentials or cookies are ever read, printed, or stored by this code
    -- Chrome's own profile storage handles the actual session.
    """
    adapter = SOURCE_ADAPTERS.get(source_name)
    if adapter is None:
        print(f"Unknown source: {source_name!r}. Known sources: {', '.join(SOURCE_ADAPTERS)}")
        return 2

    profile = source_browser.profile_dir(source_name, config.browser_profiles_dir)
    print(f"Opening Chrome with the dedicated {source_name} profile at:\n  {profile}")
    print(f"Navigating to {adapter.HOME_URL} ...")

    with source_browser.persistent_chrome_context(source_name, config.browser_profiles_dir, headless=False) as context:
        page = context.new_page()
        page.goto(adapter.HOME_URL)
        print(
            f"\nIf you are not already logged in, please log in to {source_name} "
            "manually in the Chrome window now."
        )
        input("Press Enter here once you are logged in (or already were)... ")

        try:
            adapter.discover(page, limit=1)
        except source_base.AuthRequiredError:
            print("AUTH_REQUIRED: could not verify an authenticated session. Please try again.")
            return 1

    print(f"Authenticated. Chrome closed -- the {source_name} session profile is preserved for future runs.")
    return 0


def cmd_collect_source(
    config: Config, source_name: str, *, dry_run: bool, limit: int, external_id: str | None = None
) -> int:
    """Discover, and (unless --dry-run) fetch/save/extract/screen, recent
    items from one website source.

    --dry-run: authenticate, discover at most `limit` items, print what
    would be collected, and stop -- NO database changes, NO LLM calls.

    external_id (Stage 5.8, mutually exclusive with limit at the CLI
    level): narrows the SAME pipeline below to exactly one logical
    document, instead of sweeping the recent feed. Discovery still runs
    exactly once against the already-loaded feed page (via
    EXTERNAL_ID_DISCOVERY_LIMIT, never a second page load or pagination),
    then discovered_items is filtered down to (at most) the one matching
    item BEFORE the dry-run branch and before any fetch/DB/LLM activity,
    so "not found" fails clearly with no writes and no LLM calls. Every
    later phase (fetch, version detection, extraction, screening) is the
    same code, unmodified, only ever seeing that one item -- there is
    deliberately no separate ad-hoc single-document code path.

    Otherwise, three phases, each idempotent and skipped where already
    done (so re-running this is always safe and never repeats work):
      1. Discover recent items; for each NOT already known (by
         external_id) or whose cheap discovery_title differs from what
         was seen before, fetch its full page, save the raw HTML (before
         any AI processing), and record its metadata. A changed item gets
         a NEW row (previous_version_source_id links back) -- the prior
         row and its raw artifact are never touched.
      2. Sweep every still-PENDING source for this adapter (not just ones
         just fetched) and attempt Level-1 compact idea extraction.
      3. If a frozen taste version and the canonical IDEA_SCREEN_RULES.md
         are both available, sweep every EXTRACTED source lacking a
         screening for that exact (taste, rules) combination and screen
         it (Level 2, reusing shadow.screen_idea -- no Yellowbrick-
         specific logic there). If not available yet, screening is
         skipped for this run and retried automatically on a later one.
    """
    adapter = SOURCE_ADAPTERS.get(source_name)
    if adapter is None:
        print(f"Unknown source: {source_name!r}. Known sources: {', '.join(SOURCE_ADAPTERS)}")
        return 2

    logger = get_logger()
    discover_limit = EXTERNAL_ID_DISCOVERY_LIMIT if external_id is not None else limit

    with source_browser.persistent_chrome_context(source_name, config.browser_profiles_dir, headless=True) as context:
        page = context.new_page()
        try:
            discovered_items = adapter.discover(page, limit=discover_limit)
        except source_base.AuthRequiredError:
            print(f"AUTH_REQUIRED: run 'python run.py source-login {source_name}' first.")
            return 1

        if external_id is not None:
            discovered_items = [item for item in discovered_items if item.external_id == external_id]
            if not discovered_items:
                print(
                    f"NOT_FOUND: no item with external_id={external_id!r} was discovered for source "
                    f"{source_name!r}. No database changes made. No LLM calls made."
                )
                return 1

        if dry_run:
            print(f"[DRY RUN] Discovered: {len(discovered_items)}")
            for item in discovered_items:
                print(adapter.format_discovered_item_for_dry_run(item))
            print("[DRY RUN] No database changes made. No LLM calls made.")
            return 0

        conn = db.open_production_database(config.database_path)
        stats = {"already_known": 0, "new_saved": 0, "changed": 0, "auth_required": 0, "errors": 0, "incomplete": 0}

        for item in discovered_items:
            existing = db.get_latest_collected_source(conn, source_name, item.external_id)
            # A prior INCOMPLETE_CONTENT capture (Stage 5.7) must never be
            # treated as "already known" purely because its discovery
            # title looks unchanged -- otherwise a thin summary-only
            # capture would never be retried.
            looks_unchanged = (
                existing is not None
                and existing["collection_status"] == "COLLECTED"
                and (item.title is None or existing["discovery_title"] == item.title)
            )
            if looks_unchanged:
                stats["already_known"] += 1
                continue

            try:
                fetched = adapter.fetch(page, item, discovered_at=utcnow_iso())
            except source_base.AuthRequiredError:
                stats["auth_required"] += 1
                continue
            except Exception as exc:
                stats["errors"] += 1
                logger.exception(f"Error fetching {item.canonical_url}: {exc}")
                continue

            if existing is not None and existing["content_hash"] == fetched.content_hash:
                stats["already_known"] += 1
                continue

            raw_path = raw_storage.save_raw_html(
                source_name, fetched.external_id, fetched.content_hash, fetched.raw_html, config.raw_storage_dir
            )
            collection_status = "COLLECTED" if fetched.content_complete else "INCOMPLETE_CONTENT"
            db.insert_collected_source(
                conn,
                source_name=source_name,
                external_id=fetched.external_id,
                canonical_url=fetched.canonical_url,
                discovered_at=fetched.discovered_at,
                discovery_title=fetched.title,
                source_date=fetched.published_at,
                source_title=fetched.title,
                author=fetched.author,
                ticker=fetched.ticker,
                company=fetched.company,
                source_type=fetched.source_type,
                content_hash=fetched.content_hash,
                raw_capture_hash=fetched.raw_capture_hash,
                raw_html_path=str(raw_path),
                metadata_json=json.dumps(fetched.metadata),
                previous_version_source_id=existing["source_id"] if existing is not None else None,
                created_at=utcnow_iso(),
                collection_status=collection_status,
            )
            stats["changed" if existing is not None else "new_saved"] += 1
            if not fetched.content_complete:
                stats["incomplete"] += 1
                print(
                    f"INCOMPLETE_CONTENT: {fetched.external_id} was saved for provenance but the page still "
                    f"signals more substantive content exists (e.g. an un-expanded 'Show full summary' control). "
                    f"Skipping extraction/screening for this version -- it will be retried on the next collection run."
                )
            time.sleep(adapter.POLITE_DELAY_SECONDS)

    # Level 1: sweep every PENDING source for this adapter, not just ones
    # fetched just now -- a prior missing API key or transient failure
    # self-heals here on a later run, with no manual requeue needed.
    extracted_now = 0
    if config.anthropic_api_key:
        extraction_client = None
        for row in db.get_pending_extraction_sources(conn, source_name):
            if extraction_client is None:
                extraction_client = idea_extraction.build_client(config.anthropic_api_key)
            try:
                source_text = _read_source_raw_text(adapter, row)
                extracted = idea_extraction.extract_idea(
                    extraction_client, config.parser_model_name, source_text, logger=logger
                )
            except structured_llm.StructuredGenerationError as exc:
                logger.exception(str(exc))
                continue
            db.update_collected_source_extracted(
                conn,
                source_id=row["source_id"],
                company=extracted.company or None,
                ticker=extracted.ticker or None,
                # ExtractedIdea no longer extracts title/date (see
                # idea_extraction.py) -- carry through whatever discovery
                # already found (Yellowbrick's own card/page parsing is
                # more reliable for these than an LLM guess anyway).
                source_title=row["source_title"],
                source_date=row["source_date"],
                business_summary=extracted.business_summary,
                core_thesis=extracted.core_thesis,
                why_mispriced=extracted.why_mispriced,
                future_earnings_change=extracted.future_earnings_change,
                upside_case=extracted.upside_case,
                downside_or_key_risks=extracted.downside_or_key_risks,
                catalysts=extracted.catalysts,
                what_must_be_true=extracted.what_must_be_true,
                evidence_of_market_misunderstanding=extracted.evidence_of_market_misunderstanding,
                known_unknowns=extracted.known_unknowns,
                extraction_model_name=config.parser_model_name,
                extracted_at=utcnow_iso(),
            )
            extracted_now += 1

    # Level 2: sweep every EXTRACTED source lacking a screening for the
    # CURRENT (taste, rules) combination. Skipped cleanly (not a failure)
    # if taste/rules aren't ready yet -- raw capture and extraction never
    # depend on screening being possible.
    screened_now = 0
    screening_note = None
    if config.anthropic_api_key:
        try:
            latest_taste, idea_taste_content, screen_rules_content, screen_rules_sha256 = (
                _load_frozen_taste_and_rules(conn, config)
            )
        except TasteOrRulesUnavailableError as exc:
            screening_note = str(exc)
        else:
            shadow_client = None
            for row in db.get_extracted_sources_missing_screening(
                conn, source_name, taste_version=latest_taste["version_number"], screen_rules_sha256=screen_rules_sha256
            ):
                if shadow_client is None:
                    shadow_client = shadow.build_client(config.anthropic_api_key)
                try:
                    prediction = shadow.screen_idea(
                        shadow_client,
                        config.taste_model_name,
                        idea_taste_body=idea_taste_content,
                        screen_rules_body=screen_rules_content,
                        idea_record=row,
                        logger=logger,
                    )
                except structured_llm.StructuredGenerationError as exc:
                    logger.exception(str(exc))
                    continue
                db.insert_source_screening(
                    conn,
                    source_id=row["source_id"],
                    created_at=utcnow_iso(),
                    taste_version=latest_taste["version_number"],
                    taste_sha256=latest_taste["idea_taste_sha256"],
                    screen_rules_path=str(config.screen_rules_path),
                    screen_rules_sha256=screen_rules_sha256,
                    content_hash=row["content_hash"],
                    model_name=config.taste_model_name,
                    overall_prediction=prediction.overall_prediction,
                    mispricing=prediction.mispricing,
                    variant_perception=prediction.variant_perception,
                    upside=prediction.upside,
                    business_quality=prediction.business_quality,
                    downside=prediction.downside,
                    key_reasons_json=json.dumps(prediction.key_reasons),
                    key_concerns_json=json.dumps(prediction.key_concerns),
                    critical_questions_json=json.dumps(prediction.critical_questions),
                    confidence=prediction.confidence,
                )
                screened_now += 1

    db.set_meta(conn, f"source:{source_name}:last_collected_at", utcnow_iso())
    db.set_meta(conn, f"source:{source_name}:last_collection_errors", str(stats["errors"]))
    conn.close()

    print("Discovered:", len(discovered_items))
    print("Already known:", stats["already_known"])
    print("New sources saved:", stats["new_saved"])
    print("Changed sources:", stats["changed"])
    print("Authentication required:", stats["auth_required"])
    print("Errors:", stats["errors"])
    print("Incomplete content (saved, not extracted/screened):", stats["incomplete"])
    print(f"Ideas extracted this run: {extracted_now}")
    if screening_note:
        print(f"Screening skipped this run: {screening_note}")
    else:
        print(f"Ideas screened this run: {screened_now}")
    return 0


def cmd_source_status(config: Config, source_name: str) -> int:
    """"Known documents" (distinct external_id) is deliberately reported
    separately from "Source versions" (total collected_sources rows,
    including any legitimate additional version created by a genuine
    content change) -- this is what makes "Known documents: 1 /
    Extracted: 2" legible instead of mysterious (see Stage 5.4).

    "Pending extraction versions" (Stage 5.12 fix) uses the EXACT same
    eligibility as the real extraction sweep (db.get_pending_extraction_
    sources): PENDING extraction_status AND COLLECTED collection_status.
    A row stuck at extraction_status='PENDING' only because it's an
    intentionally retained collection_status='INCOMPLETE_CONTENT' version
    (Stage 5.7) is never actionable work and must never inflate this
    count -- it's reported separately as "Incomplete content versions"
    instead, so Brad can tell "there's real pending work" apart from
    "there's a historical incomplete capture that isn't going anywhere."
    """
    adapter = SOURCE_ADAPTERS.get(source_name)
    if adapter is None:
        print(f"Unknown source: {source_name!r}. Known sources: {', '.join(SOURCE_ADAPTERS)}")
        return 2

    profile_present = source_browser.profile_dir(source_name, config.browser_profiles_dir).exists()

    conn = db.open_production_database(config.database_path)
    last_collected_at = db.get_meta(conn, f"source:{source_name}:last_collected_at")
    known = db.count_collected_sources(conn, source_name)
    versions = db.count_collected_source_versions(conn, source_name)
    newest_source_date = db.get_newest_source_date(conn, source_name)
    pending_extraction = db.count_pending_extraction_sources(conn, source_name)
    incomplete_content = db.count_collected_sources_by_collection_status(conn, source_name, "INCOMPLETE_CONTENT")
    extracted = db.count_collected_sources_by_extraction_status(conn, source_name, "EXTRACTED")
    screened = db.count_screened_sources(conn, source_name)
    collection_errors = db.get_meta(conn, f"source:{source_name}:last_collection_errors") or "0"
    conn.close()

    print(f"Authentication profile present: {'YES' if profile_present else 'NO'}")
    print(f"Last successful collection: {last_collected_at or 'never'}")
    print(f"Known documents: {known}")
    print(f"Source versions: {versions}")
    print(f"Newest published date: {newest_source_date or 'unknown'}")
    print(f"Pending extraction versions: {pending_extraction}")
    print(f"Incomplete content versions: {incomplete_content}")
    print(f"Extracted versions: {extracted}")
    print(f"Screened versions: {screened}")
    print(f"Collection errors: {collection_errors}")
    return 0


def cmd_diagnose_source_live(config: Config, source_name: str, external_id: str) -> int:
    """READ-ONLY (Stage 5.9): launches the existing authenticated browser
    profile for `source_name`, discovers just enough of the feed to locate
    exactly one pitch by external_id, and reports direct DOM/network
    evidence about its "Show full summary" control -- see
    yellowbrick.diagnose_live for exactly what is gathered and why.

    Deliberately makes NO database connection at all (there is no
    db.open_production_database call anywhere in this function), NO raw
    artifact writes, and NO LLM calls -- this exists purely to gather
    ground truth before attempting another capture-logic fix, never to
    collect, extract, or screen anything.
    """
    adapter = SOURCE_ADAPTERS.get(source_name)
    if adapter is None:
        print(f"Unknown source: {source_name!r}. Known sources: {', '.join(SOURCE_ADAPTERS)}")
        return 2

    if not hasattr(adapter, "diagnose_live"):
        print(f"Source {source_name!r} does not support the live diagnostic yet.")
        return 2

    with source_browser.persistent_chrome_context(source_name, config.browser_profiles_dir, headless=True) as context:
        page = context.new_page()
        try:
            discovered_items = adapter.discover(page, limit=EXTERNAL_ID_DISCOVERY_LIMIT)
        except source_base.AuthRequiredError:
            print(f"AUTH_REQUIRED: run 'python run.py source-login {source_name}' first.")
            return 1

        matches = [item for item in discovered_items if item.external_id == external_id]
        if not matches:
            print(f"NOT_FOUND: no item with external_id={external_id!r} was discovered for source {source_name!r}.")
            return 1

        try:
            result = adapter.diagnose_live(page, context, matches[0])
        except source_base.AuthRequiredError:
            print(f"AUTH_REQUIRED: run 'python run.py source-login {source_name}' first.")
            return 1

    print(adapter.format_live_diagnostic_report(result))
    return 0


def cmd_diagnose_source_versions(config: Config, source_name: str, external_id: str) -> int:
    """READ-ONLY diagnostic: for one external_id, list every stored
    version's hashes/paths and report whether their substantive pitch
    text is actually identical -- recomputed fresh from each version's
    own saved raw HTML file (never assumed from a possibly-stale
    content_hash, in case this is being run to sanity-check versions
    captured before the Stage 5.4 fix). Makes NO writes of any kind: no
    DB writes, no file writes, no backups, no re-fetching.
    """
    adapter = SOURCE_ADAPTERS.get(source_name)
    if adapter is None:
        print(f"Unknown source: {source_name!r}. Known sources: {', '.join(SOURCE_ADAPTERS)}")
        return 2

    conn = db.open_production_database(config.database_path)
    rows = db.get_collected_source_versions(conn, source_name, external_id)
    conn.close()

    if not rows:
        print(f"No stored versions found for {source_name}/{external_id}.")
        return 0

    print(f"{len(rows)} version(s) stored for {source_name}/{external_id}:")
    canonical_texts = []
    for row in rows:
        print(f"  source_id={row['source_id']}")
        print(f"    content_hash:      {row['content_hash']}")
        print(f"    raw_capture_hash:  {row['raw_capture_hash'] or '(not recorded -- captured before this column existed)'}")
        print(f"    raw_html_path:     {row['raw_html_path']}")
        print(f"    created_at:        {row['created_at']}")
        print(f"    collection_status: {row['collection_status']}")
        try:
            html = Path(row["raw_html_path"]).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"    (could not read raw artifact to compare substantive text: {exc})")
            continue
        canonical_text = adapter.build_canonical_source_text(html)
        canonical_texts.append(canonical_text)
        print(f"    raw HTML chars:    {len(html)}")
        print(f"    canonical chars:   {len(canonical_text)}")
        print(f"    canonical text (first ~1000 chars):")
        print(f"      {canonical_text[:1000]!r}")
        print(f"    canonical text (last ~1000 chars):")
        print(f"      {canonical_text[-1000:]!r}")

    if len(canonical_texts) < len(rows):
        print("Could not read every raw artifact -- substantive-text comparison is incomplete.")
    elif len(set(canonical_texts)) == 1:
        print("Substantive pitch text is IDENTICAL across all stored versions.")
    else:
        print("Substantive pitch text DIFFERS across stored versions.")
    return 0


DIGEST_MAX_IDEAS = 5

_LEADING_DOLLAR_PATTERN = re.compile(r"^\$")
_COMPANY_PUNCTUATION_PATTERN = re.compile(r"[^\w\s]")


def _normalize_ticker(raw: str | None) -> str | None:
    """Trim whitespace, uppercase, drop one optional leading "$" -- e.g.
    " $bbgi " and "BBGI" both normalize to "BBGI". Deliberately NOT more
    aggressive than this (no exchange-suffix stripping, no share-class
    normalization, ...): those could incorrectly merge different
    international securities, which this suppression must never risk.
    Returns None for missing/blank input, never an empty string.
    """
    if not raw:
        return None
    normalized = _LEADING_DOLLAR_PATTERN.sub("", raw.strip()).upper()
    return normalized or None


def _normalize_company_name(raw: str | None) -> str | None:
    """Lowercase, trim/collapse whitespace, and strip basic punctuation
    (periods, commas, etc.) only -- e.g. "Beasley Broadcast Group, Inc."
    and "Beasley Broadcast Group Inc" normalize the same. Deliberately
    does NOT strip corporate suffixes ("Inc"/"Corp"/"Ltd"/...), abbreviate,
    or otherwise fuzzy-match: two textually-different company names must
    never be treated as the same one. Returns None for missing/blank
    input, never an empty string.
    """
    if not raw:
        return None
    stripped_punctuation = _COMPANY_PUNCTUATION_PATTERN.sub("", raw)
    normalized = " ".join(stripped_punctuation.split()).lower()
    return normalized or None


def _judged_identity_sets(conn) -> tuple[set[str], set[str]]:
    """Normalized (ticker, company) identities from every genuinely
    learning-eligible feedback judgment -- reuses
    db.get_feedback_eligible_for_learning, the ONE canonical eligibility
    query (excluded_from_learning=0 AND feedback_parse_status='PARSED'),
    rather than reimplementing that filter here. Used ONLY to suppress a
    website-source digest candidate Brad has already judged via email;
    never inspects verdict/user_comment content -- the mere presence of
    an eligible judgment is enough.
    """
    eligible = db.get_feedback_eligible_for_learning(conn)
    judged_tickers: set[str] = set()
    judged_companies: set[str] = set()
    for row in eligible:
        ticker = _normalize_ticker(row["ticker"])
        if ticker:
            judged_tickers.add(ticker)
        company = _normalize_company_name(row["company"])
        if company:
            judged_companies.add(company)
    return judged_tickers, judged_companies


def _is_already_judged_by_brad(candidate, judged_tickers: set[str], judged_companies: set[str]) -> bool:
    """Primary match: normalized ticker exact match. Fallback ONLY when
    the CANDIDATE has no usable ticker of its own: normalized company-
    name exact match. A candidate WITH a ticker that simply doesn't match
    anything never falls through to a company-name check -- that would
    risk matching a different judged company under a different ticker
    entirely. Never fuzzy in either direction.
    """
    candidate_ticker = _normalize_ticker(candidate["ticker"])
    if candidate_ticker:
        return candidate_ticker in judged_tickers
    candidate_company = _normalize_company_name(candidate["company"])
    if candidate_company:
        return candidate_company in judged_companies
    return False


def select_digest_candidates(conn, taste_version: int) -> tuple[list, int]:
    """THE ONE candidate-selection function preview-digest AND send-digest
    both use -- do not reimplement this elsewhere. Returns (selected,
    already_judged_suppressed_count).

    The underlying query (db.get_unshown_screened_sources_for_taste_
    version) already applies latest-version-only, non-PASS, and not-yet-
    shown filtering at the SQL level. On top of that, a candidate whose
    ticker or (ticker-absent fallback) company name matches a genuinely
    learning-eligible feedback judgment is suppressed here -- Brad has
    already judged it via email, so a website-source digest must not
    present it as if it were a new discovery, even from a materially
    changed later source version (see module docstring / task notes;
    explicit re-surfacing logic is intentionally not built yet). A
    suppressed candidate is simply excluded, never marked shown.
    Remaining candidates: INVESTIGATE_NOW first, then WATCH, capped at
    DIGEST_MAX_IDEAS. No LLM calls, no DB writes anywhere in this
    function.
    """
    candidates = db.get_unshown_screened_sources_for_taste_version(conn, taste_version)
    judged_tickers, judged_companies = _judged_identity_sets(conn)

    eligible = []
    suppressed_count = 0
    for candidate in candidates:
        if _is_already_judged_by_brad(candidate, judged_tickers, judged_companies):
            suppressed_count += 1
        else:
            eligible.append(candidate)

    investigate_now = [c for c in eligible if c["overall_prediction"] == "INVESTIGATE_NOW"]
    watch = [c for c in eligible if c["overall_prediction"] == "WATCH"]
    return (investigate_now + watch)[:DIGEST_MAX_IDEAS], suppressed_count


def cmd_preview_digest(config: Config) -> int:
    """LOCAL PREVIEW ONLY -- prints at most 5 new, worth-Brad's-attention
    ideas to the terminal and marks them shown (so they're never presented
    as "new" again). Never sends anything anywhere -- this function never
    imports or calls ideascout.notifier or agentmail_client at all; works
    regardless of Config.alerts_enabled, since printing to a terminal Brad
    explicitly ran is not an alert.
    """
    conn = db.open_production_database(config.database_path)
    latest_taste = db.get_latest_taste_version(conn)
    if latest_taste is None:
        conn.close()
        print("No taste model has been generated yet.")
        return 0

    selected, suppressed_count = select_digest_candidates(conn, latest_taste["version_number"])
    if suppressed_count:
        print(f"Already-judged ideas suppressed: {suppressed_count}")

    if not selected:
        print("No new ideas to show right now.")
        conn.close()
        return 0

    for item in selected:
        label = item["company"] or item["ticker"] or item["external_id"]
        ticker_suffix = f" ({item['ticker']})" if item["ticker"] and item["ticker"] != label else ""
        key_concerns = json.loads(item["key_concerns_json"])

        print(f"=== {label}{ticker_suffix} ===")
        print(f"Source: {item['source_name']}")
        print(f"Why surfaced: {item['overall_prediction']}")
        print(f"Why potentially mispriced: {item['why_mispriced'] or '(not stated)'}")
        print(f"Upside framing: {item['upside_case'] or '(not stated)'}")
        print(f"Main concern: {key_concerns[0] if key_concerns else '(none noted)'}")
        print(f"Original source: {item['canonical_url']}")
        print()

        db.mark_source_shown_in_digest(conn, item["source_id"], utcnow_iso())

    conn.close()
    return 0


def _digest_item_from_row(row) -> notifier.DigestItem:
    """Converts one select_digest_candidates() row into a DigestItem --
    only ever copies already-stored extraction/screening fields, applies
    the required 2/2/1 caps, and never invents or rewrites anything.
    """
    return notifier.DigestItem(
        company=row["company"],
        ticker=row["ticker"],
        source_name=row["source_name"],
        canonical_url=row["canonical_url"],
        overall_prediction=row["overall_prediction"],
        confidence=row["confidence"],
        mispricing=row["mispricing"],
        variant_perception=row["variant_perception"],
        upside=row["upside"],
        business_quality=row["business_quality"],
        downside=row["downside"],
        key_reasons=json.loads(row["key_reasons_json"])[:2],
        key_concerns=json.loads(row["key_concerns_json"])[:2],
        critical_questions=json.loads(row["critical_questions_json"])[:1],
    )


def cmd_send_digest(config: Config, *, dry_run: bool) -> int:
    """Production email delivery for the SAME candidate pool preview-digest
    shows (select_digest_candidates) -- never a separate ranking. Uses
    ONLY stored extraction/screening fields; no LLM call is made anywhere
    in this function.

    Delivery safety (the one thing this function most needs to get
    right): digest_shown_sources is written ONLY after AgentMail confirms
    the send (no exception raised). A send failure marks nothing and
    returns nonzero, leaving every candidate eligible for the next run
    unchanged. A send success is followed by ONE atomic DB-marking call
    (db.mark_sources_shown_in_digest) for exactly the source_ids that were
    actually included; if THAT fails after a successful send, this prints
    a loud, explicit warning (a rerun could resend the same ideas) and
    returns nonzero rather than silently hiding the inconsistency.

    --dry-run selects exactly what a real send would select, prints the
    rendered email, and makes zero network calls and zero DB writes --
    works regardless of Config.alerts_enabled, exactly like preview-digest.
    A real send additionally requires Config.alerts_enabled=True (the
    explicit kill switch) and a configured recipient/AgentMail credentials.
    """
    conn = db.open_production_database(config.database_path)
    latest_taste = db.get_latest_taste_version(conn)
    if latest_taste is None:
        conn.close()
        print("No taste model has been generated yet.")
        return 0

    selected, suppressed_count = select_digest_candidates(conn, latest_taste["version_number"])
    print(f"Already-judged ideas suppressed: {suppressed_count}")
    print(f"Candidates selected: {len(selected)}")

    if not selected:
        conn.close()
        print("No new ideas to send.")
        if dry_run:
            print("DRY RUN — no email sent and no database changes")
        return 0

    items = [_digest_item_from_row(row) for row in selected]
    source_ids = [row["source_id"] for row in selected]
    sent_date = utcnow_iso()[:10]
    subject = notifier.digest_subject(sent_date)
    body = notifier.render_digest_body(items)

    if dry_run:
        print(f"Subject: {subject}")
        print()
        print(body)
        print()
        print("Email sent: NO")
        print("Sources marked shown: 0")
        print("DRY RUN — no email sent and no database changes")
        conn.close()
        return 0

    if not config.alerts_enabled:
        conn.close()
        print("Email sent: NO")
        print("Sources marked shown: 0")
        print(
            "send-digest FAILED: ALERTS_ENABLED is not true, so a real send is refused. "
            "Run 'send-digest --dry-run' to review the email, then set ALERTS_ENABLED=true "
            f"in {config_module.ENV_PATH} once ready."
        )
        return 1

    missing = [
        name
        for name, value in (
            ("AGENTMAIL_API_KEY", config.agentmail_api_key),
            ("AGENTMAIL_INBOX_ID", config.agentmail_inbox_id),
            ("DIGEST_RECIPIENT_EMAIL", config.digest_recipient_email),
        )
        if not value
    ]
    if missing:
        conn.close()
        print("Email sent: NO")
        print("Sources marked shown: 0")
        print(
            f"send-digest FAILED: missing required setting(s): {', '.join(missing)}. "
            f"Add them to {config_module.ENV_PATH} (see .env.example)."
        )
        return 1

    logger = get_logger()
    idempotency_key = notifier.digest_idempotency_key(sent_date, source_ids)

    try:
        client = agentmail_client.build_client(config.agentmail_api_key)
        send_response = notifier.send_digest_email(
            client,
            inbox_id=config.agentmail_inbox_id,
            recipient_email=config.digest_recipient_email,
            subject=subject,
            body=body,
            idempotency_key=idempotency_key,
        )
    except Exception as exc:
        logger.exception(f"send-digest: email send failed: {exc}")
        conn.close()
        print("Email sent: NO")
        print("Sources marked shown: 0")
        print(f"send-digest FAILED: could not send email: {exc}")
        print("No database changes were made -- these candidates remain eligible for the next run.")
        return 1

    print("Email sent: YES")

    # Stage 9: records digest_deliveries + digest_delivery_items + the
    # digest_shown_sources marks in ONE atomic transaction -- this is what
    # later lets a reply to this exact email be correlated back to these
    # exact source_ids via provider_thread_id (see
    # ideascout/digest_reply.py). provider_message_id/provider_thread_id
    # come straight from AgentMail's own send confirmation, never guessed.
    try:
        db.record_digest_delivery(
            conn,
            provider_message_id=getattr(send_response, "message_id", None),
            provider_thread_id=getattr(send_response, "thread_id", None),
            idempotency_key=idempotency_key,
            subject=subject,
            sent_at=utcnow_iso(),
            taste_version=latest_taste["version_number"],
            source_ids=source_ids,
        )
    except Exception as exc:
        logger.exception(f"send-digest: delivery/DB marking failed after a successful send: {exc}")
        conn.close()
        print("Sources marked shown: 0")
        print(
            "*** CRITICAL: the email WAS sent successfully, but recording the delivery and marking "
            "the sources as shown in the database FAILED. Rerunning send-digest now could send a "
            f"DUPLICATE email for the same ideas. Manual investigation is required before "
            f"rerunning. Error: {exc}"
        )
        return 1

    conn.close()
    print(f"Sources marked shown: {len(source_ids)}")
    return 0


def _source_screening_label(row) -> str:
    """Ticker and/or company, pipe-joined; if NEITHER is known, falls
    back to source_title, then external_id -- always something, never
    blank.
    """
    parts = [part for part in (row["ticker"], row["company"]) if part]
    if not parts:
        parts = [row["source_title"] or row["external_id"]]
    return " | ".join(parts)


def cmd_show_source_screenings(config: Config, *, limit: int, all_versions: bool = False) -> int:
    """READ-ONLY audit view of recent website-source screening decisions,
    newest first -- lets Brad inspect them without raw SQLite queries.

    By default, one row per LOGICAL document (source_name, external_id):
    a document with more than one stored version -- a genuine content
    change, or a historical duplicate retained from before the Stage 5.4
    idempotency fix -- is shown once, using its single newest screening
    across all versions, so a retained old duplicate never makes the same
    document appear twice. --all-versions is the forensic escape hatch
    that shows every stored version/screening instead, exactly as before.
    --limit always applies to the final (deduplicated, unless
    --all-versions) result.

    Strictly read-only: only ever SELECTs from source_screenings/
    collected_sources. Never writes to digest_shown_sources or marks
    anything as seen (unlike preview-digest), never triggers extraction
    or screening, and never calls an LLM (unlike collect-source) -- this
    only ever displays decisions that were already made and stored.
    """
    conn = db.open_production_database(config.database_path)
    if all_versions:
        rows = db.get_recent_source_screenings(conn, limit)
    else:
        rows = db.get_recent_source_screenings_deduplicated(conn, limit)
    conn.close()

    if not rows:
        print("No source screenings recorded yet.")
        return 0

    for row in rows:
        key_reasons = json.loads(row["key_reasons_json"])[:2]
        key_concerns = json.loads(row["key_concerns_json"])[:2]
        critical_questions = json.loads(row["critical_questions_json"])[:2]

        print(f"{_source_screening_label(row)} | {row['overall_prediction']}")
        print(
            f"Mispricing: {row['mispricing']} | Variant: {row['variant_perception']} | "
            f"Upside: {row['upside']}"
        )
        print(
            f"Business quality: {row['business_quality']} | Downside: {row['downside']} | "
            f"Confidence: {row['confidence']}"
        )
        print()

        print("Why:")
        for reason in key_reasons:
            print(f"- {reason}")
        print()

        print("Concerns:")
        for concern in key_concerns:
            print(f"- {concern}")
        print()

        print("Questions:")
        for question in critical_questions:
            print(f"- {question}")
        print()

        print(f"URL: {row['canonical_url']}")
        print()

    return 0


def build_parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(prog="run.py", description="IdeaScout")
    subparsers = arg_parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "init",
        help="Create a brand-new production database (refuses if one already exists)",
    )
    subparsers.add_parser("check-mail", help="Fetch new messages from AgentMail into SQLite")
    subparsers.add_parser("parse-mail", help="Turn unparsed messages into structured feedback")
    show_feedback_parser = subparsers.add_parser(
        "show-feedback", help="Show the most recent structured feedback records"
    )
    show_feedback_parser.add_argument(
        "--limit", type=int, default=10, help="Number of records to show (default: 10)"
    )
    subparsers.add_parser("status", help="Show database and last-run health")
    requeue_parser = subparsers.add_parser(
        "requeue-feedback",
        help="Reset an ERROR or NEEDS_REVIEW message back to UNPARSED for reprocessing",
    )
    requeue_parser.add_argument(
        "message_id", nargs="?", default=None, help="message_id to requeue"
    )
    requeue_parser.add_argument(
        "--all-errors",
        action="store_true",
        help="Requeue every message currently in ERROR state (not NEEDS_REVIEW, not SKIPPED_NOT_BRAD)",
    )
    exclude_parser = subparsers.add_parser(
        "exclude-feedback",
        help="Mark feedback record(s) for a ticker/company (or one exact feedback_id) as excluded from learning",
    )
    exclude_group = exclude_parser.add_mutually_exclusive_group(required=True)
    exclude_group.add_argument("ticker", nargs="?", default=None, help="Ticker or company name to match")
    exclude_group.add_argument(
        "--feedback-id", type=int, default=None, help="Exact feedback_id to target (instead of ticker/company)"
    )
    include_parser = subparsers.add_parser(
        "include-feedback",
        help="Reverse exclude-feedback for a ticker/company (or one exact feedback_id)",
    )
    include_group = include_parser.add_mutually_exclusive_group(required=True)
    include_group.add_argument("ticker", nargs="?", default=None, help="Ticker or company name to match")
    include_group.add_argument(
        "--feedback-id", type=int, default=None, help="Exact feedback_id to target (instead of ticker/company)"
    )
    subparsers.add_parser(
        "show-review",
        help="Show every message awaiting human review, with its derived feedback event(s)",
    )
    approve_parser = subparsers.add_parser(
        "approve-review",
        help="Approve a NEEDS_REVIEW message's feedback exactly as extracted",
    )
    approve_parser.add_argument("message_id", help="message_id to approve")
    subparsers.add_parser(
        "build-taste",
        help="Generate a new Far View Idea Taste version from eligible feedback",
    )
    subparsers.add_parser(
        "taste-status",
        help="Show the current taste version and its holdout progress",
    )
    subparsers.add_parser(
        "shadow-score",
        help="Blind-screen post-Taste-v1 holdout ideas against frozen Taste v1 + IDEA_SCREEN_RULES.md",
    )
    subparsers.add_parser(
        "shadow-status",
        help="Show aggregate shadow-screening counts (never an individual prediction)",
    )
    subparsers.add_parser(
        "show-shadow-results",
        help="Show shadow predictions next to Brad's actual verdict, for already-judged ideas only",
    )
    source_login_parser = subparsers.add_parser(
        "source-login",
        help="Open a visible Chrome window to manually authenticate a website source",
    )
    source_login_parser.add_argument("source_name", help="e.g. yellowbrick")
    collect_source_parser = subparsers.add_parser(
        "collect-source",
        help="Discover and collect recent items from an authenticated website source",
    )
    collect_source_parser.add_argument("source_name", help="e.g. yellowbrick")
    collect_source_parser.add_argument(
        "--dry-run", action="store_true", help="Discover and print only -- no database changes, no LLM calls"
    )
    collect_source_target_group = collect_source_parser.add_mutually_exclusive_group()
    collect_source_target_group.add_argument(
        "--limit", type=int, default=10, help="Maximum number of recent items to discover (default: 10)"
    )
    collect_source_target_group.add_argument(
        "--external-id",
        default=None,
        help="Collect exactly one document by its external_id (e.g. 143618) through the normal "
        "discover/fetch/extract/screen pipeline, instead of sweeping the recent feed",
    )
    source_status_parser = subparsers.add_parser(
        "source-status",
        help="Show collection/extraction/screening status for a website source",
    )
    source_status_parser.add_argument("source_name", help="e.g. yellowbrick")
    diagnose_versions_parser = subparsers.add_parser(
        "diagnose-source-versions",
        help="READ-ONLY: show every stored version of one document and whether their text actually differs",
    )
    diagnose_versions_parser.add_argument("source_name", help="e.g. yellowbrick")
    diagnose_versions_parser.add_argument("external_id", help="e.g. 143411")
    diagnose_live_parser = subparsers.add_parser(
        "diagnose-source-live",
        help="READ-ONLY: launch the real authenticated browser and inspect one pitch's live DOM/network behavior",
    )
    diagnose_live_parser.add_argument("source_name", help="e.g. yellowbrick")
    diagnose_live_parser.add_argument("--external-id", required=True, help="e.g. 143618")
    subparsers.add_parser(
        "preview-digest",
        help="LOCAL PREVIEW ONLY: show up to 5 new worth-attention ideas from all sources",
    )
    send_digest_parser = subparsers.add_parser(
        "send-digest",
        help="Email up to 5 new worth-attention ideas to the configured recipient",
    )
    send_digest_parser.add_argument(
        "--dry-run", action="store_true", help="Select and render the digest, but send zero emails and change nothing"
    )
    show_source_screenings_parser = subparsers.add_parser(
        "show-source-screenings",
        help="READ-ONLY: show recent website-source screening decisions, newest first",
    )
    show_source_screenings_parser.add_argument(
        "--limit", type=int, default=10, help="Maximum number of screenings to show (default: 10)"
    )
    show_source_screenings_parser.add_argument(
        "--all-versions",
        action="store_true",
        help="Forensic: show every stored version/screening instead of one row per logical document",
    )
    return arg_parser


def main(argv: list[str] | None = None) -> int:
    arg_parser = build_parser()
    args = arg_parser.parse_args(argv)

    legacy_env = config_module.find_legacy_repo_env()
    if legacy_env is not None:
        print(
            f"Warning: found an old .env at {legacy_env}. IdeaScout now reads .env only "
            f"from {config_module.ENV_PATH}. Move it there manually -- this app will never "
            "do so automatically."
        )

    try:
        if args.command == "init":
            cfg = load_config()
            setup_logging(cfg.log_path)
            return cmd_init(cfg)
        elif args.command == "check-mail":
            config = load_config(require_agentmail=True)
            setup_logging(config.log_path)
            return cmd_check_mail(config)
        elif args.command == "parse-mail":
            config = load_config(require_llm=True)
            setup_logging(config.log_path)
            return cmd_parse_mail(config)
        elif args.command == "show-feedback":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_show_feedback(config, limit=args.limit)
        elif args.command == "status":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_status(config)
        elif args.command == "requeue-feedback":
            if args.all_errors and args.message_id:
                print("Specify either MESSAGE_ID or --all-errors, not both.")
                return 2
            if not args.all_errors and not args.message_id:
                print("Usage: python run.py requeue-feedback MESSAGE_ID | --all-errors")
                return 2
            config = load_config()
            setup_logging(config.log_path)
            return cmd_requeue_feedback(config, args.message_id, args.all_errors)
        elif args.command == "exclude-feedback":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_exclude_feedback(config, args.ticker, feedback_id=args.feedback_id)
        elif args.command == "include-feedback":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_include_feedback(config, args.ticker, feedback_id=args.feedback_id)
        elif args.command == "show-review":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_show_review(config)
        elif args.command == "approve-review":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_approve_review(config, args.message_id)
        elif args.command == "build-taste":
            config = load_config(require_taste=True)
            setup_logging(config.log_path)
            return cmd_build_taste(config)
        elif args.command == "taste-status":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_taste_status(config)
        elif args.command == "shadow-score":
            config = load_config(require_shadow=True)
            setup_logging(config.log_path)
            return cmd_shadow_score(config)
        elif args.command == "shadow-status":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_shadow_status(config)
        elif args.command == "show-shadow-results":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_show_shadow_results(config)
        elif args.command == "source-login":
            # No LLM calls happen here at all -- just a browser + manual
            # login -- so this does not require ANTHROPIC_API_KEY.
            config = load_config()
            setup_logging(config.log_path)
            return cmd_source_login(config, args.source_name)
        elif args.command == "collect-source":
            # --dry-run makes no LLM calls at all, so it must not require
            # ANTHROPIC_API_KEY -- only a real (non-dry-run) run does.
            config = load_config(require_source=not args.dry_run)
            setup_logging(config.log_path)
            return cmd_collect_source(
                config, args.source_name, dry_run=args.dry_run, limit=args.limit, external_id=args.external_id
            )
        elif args.command == "source-status":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_source_status(config, args.source_name)
        elif args.command == "diagnose-source-versions":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_diagnose_source_versions(config, args.source_name, args.external_id)
        elif args.command == "diagnose-source-live":
            # Read-only browser introspection, no LLM calls -- does not
            # require ANTHROPIC_API_KEY.
            config = load_config()
            setup_logging(config.log_path)
            return cmd_diagnose_source_live(config, args.source_name, args.external_id)
        elif args.command == "preview-digest":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_preview_digest(config)
        elif args.command == "send-digest":
            # --dry-run makes no network calls at all, so it must not
            # require AgentMail credentials -- only a real send does.
            config = load_config(require_agentmail=not args.dry_run)
            setup_logging(config.log_path)
            return cmd_send_digest(config, dry_run=args.dry_run)
        elif args.command == "show-source-screenings":
            config = load_config()
            setup_logging(config.log_path)
            return cmd_show_source_screenings(config, limit=args.limit, all_versions=args.all_versions)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
