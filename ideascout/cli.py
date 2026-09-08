"""Command-line entry points: check-mail, parse-mail, show-feedback, status.

This is the only file that argparse touches. Each cmd_* function also
returns a plain result value (not just prints to the screen), which is
what the tests exercise directly.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from email.utils import parseaddr

from . import agentmail_client, db, parser
from .config import Config, ConfigError, load_config
from .logger import get_logger, setup_logging


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def cmd_check_mail(config: Config) -> int:
    logger = get_logger()
    conn = db.connect(config.database_path)

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
    conn = db.connect(config.database_path)

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
    conn = db.connect(config.database_path)
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

        print(
            f"[{date}]{event_suffix} {label} | {row['event_type']} | "
            f"{verdict} | confidence={confidence_text}"
        )
        print(f'    "{comment}"')

    return 0


def cmd_status(config: Config) -> int:
    try:
        conn = db.connect(config.database_path)
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
    conn = db.connect(config.database_path)

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


def build_parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser(prog="run.py", description="IdeaScout")
    subparsers = arg_parser.add_subparsers(dest="command", required=True)
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
    return arg_parser


def main(argv: list[str] | None = None) -> int:
    arg_parser = build_parser()
    args = arg_parser.parse_args(argv)

    try:
        if args.command == "check-mail":
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
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
