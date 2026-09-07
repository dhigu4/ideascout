"""Command-line entry points: `check-mail` and `status`.

This is the only file that argparse touches. The functions cmd_check_mail
and cmd_status also each return a plain result value (not just print to the
screen), which is what the tests exercise directly.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from . import agentmail_client, db
from .config import Config, ConfigError, load_config
from .logger import get_logger, setup_logging


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        items = agentmail_client.list_all_message_items(client, config.agentmail_inbox_id)
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


def cmd_status(config: Config) -> int:
    try:
        conn = db.connect(config.database_path)
    except Exception as exc:
        print("Database reachable: NO")
        print(f"Error: {exc}")
        return 1

    total = db.count_total_messages(conn)
    unparsed = db.count_unparsed_messages(conn)
    last_check = db.get_meta(conn, "last_check_at") or "never"
    last_error = db.get_meta(conn, "last_error") or "none"
    conn.close()

    print("Database reachable: YES")
    print(f"Total raw messages: {total}")
    print(f"Unparsed messages: {unparsed}")
    print(f"Last successful inbox check: {last_check}")
    print(f"Last error: {last_error}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="run.py", description="IdeaScout Stage 1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check-mail", help="Fetch new messages from AgentMail into SQLite")
    subparsers.add_parser("status", help="Show database and last-check health")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "check-mail":
            config = load_config(require_agentmail=True)
            setup_logging(config.log_path)
            return cmd_check_mail(config)
        elif args.command == "status":
            config = load_config(require_agentmail=False)
            setup_logging(config.log_path)
            return cmd_status(config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}")
        return 2

    return 1


if __name__ == "__main__":
    sys.exit(main())
