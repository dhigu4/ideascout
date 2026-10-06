"""READ-ONLY integrity audit of the active Taste holdout (`audit-holdout-integrity`).

Three groups are kept separate after the training checkpoint:

  OFFICIAL      -- exactly the rows db.get_eligible_feedback_after returns,
                   the same predicate taste-status uses to count the holdout.
                   Only these count toward 20/20, uniqueness, and the verdict.
  CANDIDATES    -- BLIND_REVIEW rows that look like holdout judgments but fail
                   the official predicate (holdout_eligible=0, excluded, or
                   not PARSED). They make the holdout NOT CLEAN.
  OTHER         -- every remaining post-checkpoint feedback row (SCREEN_REVIEW,
                   DIGEST_REPLY, ...). Reported for reconciliation only; never
                   a holdout member, never a duplicate, never a verdict input.

Nothing here writes. Every query is a SELECT; nothing calls an LLM or the
network.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import blind_review, db, digest_reply, screen_review, taste

SMOKE_TEST_MARKERS = frozenset({"TESTCO", "XYZ"})
NAMED_CHECK_TICKERS = ("CPRT",)

_LEVEL_NAMES = {
    "A": "same blind-review assignment",
    "D": "source_name + external_id",
    "C": "screening_id",
    "E": "normalized ticker",
    "F": "normalized company",
}
_LEVEL_ORDER = ("A", "D", "C", "E", "F")

_BASE_SQL = """
SELECT f.feedback_id, f.feedback_origin, f.holdout_eligible, f.excluded_from_learning,
       f.verdict, f.ticker AS feedback_ticker, f.company AS feedback_company,
       f.created_at AS feedback_created_at, f.screening_id, f.source_id AS feedback_source_id,
       f.blind_review_assignment_id, mr.feedback_parse_status,
       bra.assigned_at, bra.judged_at,
       cs.source_id, cs.source_name, cs.external_id,
       cs.ticker AS source_ticker, cs.company AS source_company
FROM feedback f
JOIN messages_raw mr ON mr.message_id = f.message_id
LEFT JOIN blind_review_assignments bra ON bra.assignment_id = f.blind_review_assignment_id
LEFT JOIN collected_sources cs ON cs.source_id = COALESCE(bra.source_id, f.source_id)
"""

_USABLE_JUDGMENTS_SQL = """
SELECT COUNT(*) FROM feedback f
JOIN messages_raw mr ON mr.message_id = f.message_id
WHERE f.blind_review_assignment_id = ? AND f.excluded_from_learning = 0
  AND mr.feedback_parse_status = 'PARSED'
"""

_PRIOR_EXPOSURE_SQL = """
SELECT feedback_id, feedback_origin, created_at FROM feedback
WHERE feedback_origin IN ('DIGEST_REPLY', 'SCREEN_REVIEW')
  AND (source_id = ? OR screening_id IN (SELECT screening_id FROM source_screenings WHERE source_id = ?))
ORDER BY feedback_id
"""

OFFICIAL = "official"
CANDIDATE = "candidate"
SUPERSEDED = "superseded"
OTHER = "other"
REFERENCE = "reference"


@dataclass
class HoldoutRow:
    feedback_id: int
    role: str
    origin: str
    holdout_eligible: bool
    excluded: bool
    parse_status: str | None
    verdict: str | None
    feedback_created_at: str
    assignment_id: int | None
    assigned_at: str | None
    source_id: int | None
    source_name: str | None
    external_id: str | None
    ticker: str | None
    company: str | None
    screening_id: int | None
    prediction: str | None
    usable_judgments: int = 0
    keys: list[tuple[str, str]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return blind_review.format_review_ref(self.assignment_id) if self.assignment_id is not None else "-"

    @property
    def screening_ref(self) -> str:
        return screen_review.format_review_ref(self.screening_id) if self.screening_id is not None else "-"


@dataclass(frozen=True)
class DuplicateGroup:
    members: tuple[HoldoutRow, ...]
    identity_level: str
    identity_value: str

    @property
    def refs(self) -> str:
        return " / ".join(f"{_row_label(m)} (feedback {m.feedback_id})" for m in self.members)


def _row_label(row: HoldoutRow) -> str:
    return row.ref if row.assignment_id is not None else f"FB {row.feedback_id}"


@dataclass
class HoldoutAudit:
    version_label: str
    checkpoint_feedback_id: int
    post_training_count: int
    official: list[HoldoutRow]
    candidates: list[HoldoutRow]
    superseded: list[HoldoutRow]
    superseded_records: dict[int, dict]
    other: list[HoldoutRow]
    unique_groups: list[list[HoldoutRow]]
    duplicate_groups: list[DuplicateGroup]
    taste_status_ids: frozenset[int]
    named_checks: dict[str, list[HoldoutRow]]

    @property
    def official_count(self) -> int:
        return len(self.official)

    @property
    def unique_count(self) -> int:
        return sum(1 for group in self.unique_groups if any(m.role == OFFICIAL for m in group))

    @property
    def valid_unique_count(self) -> int:
        return sum(
            1
            for group in self.unique_groups
            if len(group) == 1 and group[0].role == OFFICIAL and not group[0].problems
        )

    @property
    def reconciles(self) -> bool:
        return {r.feedback_id for r in self.official} == set(self.taste_status_ids)

    @property
    def reconciles_total(self) -> bool:
        return self.post_training_count == (
            self.official_count + len(self.candidates) + len(self.superseded) + len(self.other)
        )

    @property
    def clean(self) -> bool:
        return (
            self.official_count == taste.HOLDOUT_SIZE
            and self.valid_unique_count == taste.HOLDOUT_SIZE
            and not any(r.problems for r in self.official)
            and not self.candidates
        )


def _keys_for(row: HoldoutRow) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if row.assignment_id is not None:
        keys.append(("A", f"assignment:{row.assignment_id}"))
    if row.source_name and row.external_id:
        keys.append(("D", f"{row.source_name}:{row.external_id}"))
    if row.screening_id is not None:
        keys.append(("C", f"screening:{row.screening_id}"))
    ticker = digest_reply.normalize_ticker(row.ticker)
    if ticker:
        keys.append(("E", ticker))
    company = digest_reply.normalize_company_name(row.company)
    if company:
        keys.append(("F", company))
    return keys


def _build_row(conn: sqlite3.Connection, record: sqlite3.Row, role: str) -> HoldoutRow:
    source_id = record["source_id"]
    screening = None
    if record["screening_id"] is not None:
        screening = conn.execute(
            "SELECT * FROM source_screenings WHERE screening_id = ?", (record["screening_id"],)
        ).fetchone()
    elif source_id is not None:
        screening = db.get_latest_source_screening_for_source(conn, source_id)

    usable = 0
    if record["blind_review_assignment_id"] is not None:
        usable = conn.execute(_USABLE_JUDGMENTS_SQL, (record["blind_review_assignment_id"],)).fetchone()[0]

    row = HoldoutRow(
        feedback_id=record["feedback_id"],
        role=role,
        origin=record["feedback_origin"],
        holdout_eligible=bool(record["holdout_eligible"]),
        excluded=bool(record["excluded_from_learning"]),
        parse_status=record["feedback_parse_status"],
        verdict=record["verdict"],
        feedback_created_at=record["feedback_created_at"],
        assignment_id=record["blind_review_assignment_id"],
        assigned_at=record["assigned_at"],
        source_id=source_id,
        source_name=record["source_name"],
        external_id=record["external_id"],
        ticker=record["source_ticker"] or record["feedback_ticker"],
        company=record["source_company"] or record["feedback_company"],
        screening_id=screening["screening_id"] if screening is not None else None,
        prediction=screening["overall_prediction"] if screening is not None else None,
        usable_judgments=usable,
    )
    row.keys = _keys_for(row)
    if role in (OFFICIAL, CANDIDATE):
        row.problems = _validity_problems(conn, row, record, screening)
    return row


def _validity_problems(conn, row: HoldoutRow, record: sqlite3.Row, screening) -> list[str]:
    problems: list[str] = []
    if row.role == OFFICIAL and row.origin != "BLIND_REVIEW":
        problems.append(f"counted as an official holdout member but origin is {row.origin}")
    if row.origin == "BLIND_REVIEW" and row.assignment_id is None:
        problems.append("no blind-review assignment is linked")
    if row.origin == "BLIND_REVIEW" and record["screening_id"] is not None:
        problems.append(f"blind judgment is linked to SR-{record['screening_id']}")
    if not row.holdout_eligible:
        problems.append("holdout_eligible=0")
    if row.excluded:
        problems.append("excluded_from_learning=1")
    if row.parse_status != "PARSED":
        problems.append(f"feedback_parse_status is {row.parse_status}")
    if row.assignment_id is not None and row.usable_judgments != 1:
        problems.append(f"{row.usable_judgments} usable Brad judgments for this assignment (need exactly 1)")

    if record["source_id"] is None:
        problems.append("source not found")
    elif screening is None:
        problems.append("no screening recorded for this source")
    elif screening["created_at"] > row.feedback_created_at:
        problems.append(f"{row.screening_ref} was created after the judgment")

    if record["source_id"] is not None:
        shown = conn.execute(
            "SELECT shown_at FROM digest_shown_sources WHERE source_id = ?", (record["source_id"],)
        ).fetchone()
        if shown is not None and shown["shown_at"] < row.feedback_created_at:
            problems.append(f"source was shown in a digest at {shown['shown_at']}, before the judgment")
        for prior in conn.execute(_PRIOR_EXPOSURE_SQL, (record["source_id"], record["source_id"])).fetchall():
            if prior["created_at"] < row.feedback_created_at:
                problems.append(
                    f"{prior['feedback_origin']} feedback FB {prior['feedback_id']} on this idea came before "
                    "the judgment (prediction may have been seen)"
                )

    ticker = digest_reply.normalize_ticker(row.ticker)
    company = digest_reply.normalize_company_name(row.company)
    if ticker in SMOKE_TEST_MARKERS:
        problems.append(f"smoke-test marker {ticker}")
    if company in {marker.lower() for marker in SMOKE_TEST_MARKERS}:
        problems.append(f"smoke-test marker {company}")
    return problems


def _has_duplicate_exclusion_record(conn: sqlite3.Connection, feedback_id: int) -> bool:
    record = db.get_holdout_repair_record(conn, feedback_id)
    return record is not None and record.get("feedback_id") == feedback_id


def _load(conn: sqlite3.Connection, where_sql: str, params: tuple, role_for) -> list[HoldoutRow]:
    records = conn.execute(_BASE_SQL + where_sql, params).fetchall()
    return [_build_row(conn, record, role_for(record)) for record in records]


def _group(rows: list[HoldoutRow]) -> tuple[list[list[HoldoutRow]], list[DuplicateGroup]]:
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    first_index: dict[tuple[str, str], int] = {}
    for index, row in enumerate(rows):
        for key in row.keys:
            if key in first_index:
                parent[find(index)] = find(first_index[key])
            else:
                first_index[key] = index

    members_by_root: dict[int, list[HoldoutRow]] = {}
    for index, row in enumerate(rows):
        members_by_root.setdefault(find(index), []).append(row)
    unique = sorted(
        (sorted(members, key=lambda r: r.feedback_id) for members in members_by_root.values()),
        key=lambda members: members[0].feedback_id,
    )

    duplicates: list[DuplicateGroup] = []
    for members in unique:
        if len(members) >= 2:
            level, value = _strongest_shared_identity(members)
            duplicates.append(DuplicateGroup(tuple(members), level, value))
    return unique, duplicates


def _strongest_shared_identity(members: list[HoldoutRow]) -> tuple[str, str]:
    for level in _LEVEL_ORDER:
        counts: dict[str, int] = {}
        for row in members:
            for key_level, value in row.keys:
                if key_level == level:
                    counts[value] = counts.get(value, 0) + 1
        shared = sorted(value for value, count in counts.items() if count >= 2)
        if shared:
            return level, shared[0]
    return "-", "linked transitively"


def _training_overlaps(conn, latest_taste, holdout_rows: list[HoldoutRow]) -> dict[int, str]:
    training_ids = json.loads(latest_taste["training_feedback_ids_json"])
    if not training_ids:
        return {}
    placeholders = ",".join("?" for _ in training_ids)
    training_rows = _load(
        conn, f"WHERE f.feedback_id IN ({placeholders})", tuple(training_ids), lambda _record: REFERENCE
    )
    training_keys: dict[tuple[str, str], int] = {}
    for training in training_rows:
        for key in training.keys:
            if key[0] in ("D", "E", "F"):
                training_keys.setdefault(key, training.feedback_id)

    overlaps: dict[int, str] = {}
    for row in holdout_rows:
        for key in row.keys:
            if key[0] in ("D", "E", "F") and key in training_keys:
                overlaps[row.feedback_id] = (
                    f"{_LEVEL_NAMES[key[0]]} matches training judgment FB {training_keys[key]}"
                )
                break
    return overlaps


def audit_holdout(conn: sqlite3.Connection, latest_taste: sqlite3.Row) -> HoldoutAudit:
    """SELECT-only. Partitions post-checkpoint feedback into OFFICIAL members
    (the taste-status predicate, via db.get_eligible_feedback_after),
    questionable BLIND_REVIEW CANDIDATES, and OTHER feedback.
    """
    checkpoint = latest_taste["checkpoint_feedback_id"]
    official_ids = frozenset(r["feedback_id"] for r in db.get_eligible_feedback_after(conn, checkpoint))

    def role_for(record) -> str:
        if record["feedback_id"] in official_ids:
            return OFFICIAL
        if record["feedback_origin"] == "BLIND_REVIEW" or record["blind_review_assignment_id"] is not None:
            if record["excluded_from_learning"] and _has_duplicate_exclusion_record(conn, record["feedback_id"]):
                return SUPERSEDED
            return CANDIDATE
        return OTHER

    window = _load(conn, "WHERE f.feedback_id > ? ORDER BY f.feedback_id", (checkpoint,), role_for)
    official = [r for r in window if r.role == OFFICIAL]
    candidates = [r for r in window if r.role == CANDIDATE]
    superseded = [r for r in window if r.role == SUPERSEDED]
    other = [r for r in window if r.role == OTHER]

    holdout_rows = official + candidates
    for feedback_id, reason in _training_overlaps(conn, latest_taste, holdout_rows).items():
        row = next(r for r in holdout_rows if r.feedback_id == feedback_id)
        row.problems.append(reason)

    unique, duplicates = _group(holdout_rows)
    named = {
        ticker: [r for r in holdout_rows if digest_reply.normalize_ticker(r.ticker) == ticker]
        for ticker in NAMED_CHECK_TICKERS
    }
    return HoldoutAudit(
        version_label=latest_taste["version_label"],
        checkpoint_feedback_id=checkpoint,
        post_training_count=len(window),
        official=official,
        candidates=candidates,
        superseded=superseded,
        superseded_records={r.feedback_id: db.get_holdout_repair_record(conn, r.feedback_id) for r in superseded},
        other=other,
        unique_groups=unique,
        duplicate_groups=duplicates,
        taste_status_ids=official_ids,
        named_checks=named,
    )


def replacement_needed(audit: HoldoutAudit) -> int:
    return max(0, taste.HOLDOUT_SIZE - audit.official_count)


def remediation_lines(audit: HoldoutAudit) -> list[str]:
    lines: list[str] = []
    needed = replacement_needed(audit)
    if needed:
        lines.append(
            f"Replacement blind-review judgment required: {needed}. Run "
            f"'python run.py blind-review --limit {needed}', judge the new item(s) BLIND by reply, then re-run "
            "audit-holdout-integrity. Formal evaluation stays blocked until the holdout is 20 official rows."
        )
    elif audit.official_count != taste.HOLDOUT_SIZE:
        lines.append(
            f"The official holdout has {audit.official_count} row(s); expected exactly {taste.HOLDOUT_SIZE}."
        )
    for group in audit.duplicate_groups:
        lines.append(
            f"Duplicate: {_identity_display(group)} -- {group.refs}. "
            "Recommended action: exclude one duplicate from formal holdout evaluation and "
            "obtain one new blind-review judgment before evaluating Taste v2."
        )
    for row in audit.official:
        if row.problems:
            lines.append(
                f"Holdout row {_row_label(row)}: {'; '.join(row.problems)}. "
                "Recommended action: resolve manually before evaluating Taste v2 (no automatic change is made)."
            )
    for row in audit.candidates:
        lines.append(
            f"Questionable blind candidate {_row_label(row)} (feedback {row.feedback_id}): "
            f"{'; '.join(row.problems)}. It is not an official holdout member, but it makes the "
            "holdout not clean. Decide manually whether it stays excluded."
        )
    return lines


def _identity_display(group: DuplicateGroup) -> str:
    if group.identity_level == "A":
        return f"blind assignment BR-{group.identity_value.split(':', 1)[1]}"
    if group.identity_level == "D":
        return f"source {group.identity_value.replace(':', '/', 1)}"
    if group.identity_level == "E":
        return f"ticker {group.identity_value}"
    if group.identity_level == "F":
        return f"company {group.identity_value!r}"
    if group.identity_level == "C":
        return f"screening {group.identity_value.split(':', 1)[1]}"
    return group.identity_value


def render_lines(audit: HoldoutAudit) -> list[str]:
    out: list[str] = []
    out.append(f"Taste version: {audit.version_label}")
    out.append(f"Checkpoint feedback_id: {audit.checkpoint_feedback_id}")
    out.append("")
    out.append("RECONCILIATION")
    out.append(f"  Post-training feedback rows (feedback_id > checkpoint): {audit.post_training_count}")
    out.append(f"  Official blind holdout rows (taste-status predicate): {audit.official_count}")
    out.append(f"  Questionable blind holdout candidates: {len(audit.candidates)}")
    out.append(f"  Superseded duplicates (manually excluded, recorded repair): {len(audit.superseded)}")
    out.append(f"  Other post-checkpoint feedback (not part of holdout): {len(audit.other)}")
    out.append(
        f"  Totals reconcile: {'YES' if audit.reconciles_total else 'NO'}; "
        f"official rows match taste-status's eligible set: {'YES' if audit.reconciles else 'NO'} "
        f"({len(audit.taste_status_ids)} eligible)"
    )
    out.append(f"  Unique valid holdout ideas: {audit.valid_unique_count} / {taste.HOLDOUT_SIZE}")
    out.append("")

    out.append("OFFICIAL HOLDOUT MEMBERS")
    if not audit.official:
        out.append("  (none)")
    for row in audit.official:
        _append_row(out, row)

    out.append("")
    out.append("QUESTIONABLE BLIND HOLDOUT CANDIDATES (not official members; these make the holdout NOT CLEAN)")
    if not audit.candidates:
        out.append("  (none)")
    for row in audit.candidates:
        _append_row(out, row)

    out.append("")
    out.append("SUPERSEDED DUPLICATES (manually excluded by a recorded repair; not members, not a verdict input)")
    if not audit.superseded:
        out.append("  (none)")
    for row in audit.superseded:
        record = audit.superseded_records.get(row.feedback_id, {})
        out.append(
            f"  {_row_label(row)} | feedback {row.feedback_id} | {row.company or '-'} ({row.ticker or '-'}) | "
            f"retained: {record.get('retained_ref', '-')} (feedback {record.get('retained_feedback_id', '-')}) | "
            f"reason: {record.get('reason', '-')}"
        )

    out.append("")
    out.append("OTHER POST-CHECKPOINT FEEDBACK (not part of holdout; reported for reconciliation only)")
    if not audit.other:
        out.append("  (none)")
    for row in audit.other:
        out.append(
            f"  FB {row.feedback_id} | origin {row.origin} | {row.company or '-'} ({row.ticker or '-'}) | "
            f"holdout-eligible: {'YES' if row.holdout_eligible else 'NO'} | "
            f"excluded: {'YES' if row.excluded else 'NO'} | parse: {row.parse_status or '-'}"
        )

    out.append("")
    out.append("DUPLICATE DETECTION (official members and questionable blind candidates only)")
    out.append("  Checked levels: A same assignment, B feedback_id (primary key, cannot repeat), "
               "C screening_id, D source_name+external_id, E normalized ticker, F normalized company")
    for ticker, matches in audit.named_checks.items():
        refs = ", ".join(f"{_row_label(m)} (feedback {m.feedback_id})" for m in matches) or "none"
        out.append(f"  Named check {ticker}: {len(matches)} row(s) in holdout: {refs}")
    if not audit.duplicate_groups:
        out.append("  No duplicate groups.")
    for group in audit.duplicate_groups:
        labels = sorted({m.verdict or "-" for m in group.members})
        conflict = " | CONFLICTING LABELS" if len(labels) > 1 else ""
        out.append(
            f"  DUPLICATE ({_LEVEL_NAMES[group.identity_level]}: {_identity_display(group)}) -- "
            f"{group.refs}; labels: {', '.join(labels)}{conflict}"
        )

    out.append("")
    out.append("SUMMARY")
    out.append(f"  Total holdout rows: {audit.official_count}")
    out.append(f"  Unique ideas: {audit.unique_count}")
    out.append(f"  Duplicate groups: {len(audit.duplicate_groups)}")
    out.append(f"  Valid unique holdout judgments: {audit.valid_unique_count}")
    out.append(f"  Invalid/questionable rows: {_questionable_count(audit)}")
    if replacement_needed(audit):
        out.append(f"  REPLACEMENT BLIND-REVIEW JUDGMENT REQUIRED: {replacement_needed(audit)}")
    if audit.clean:
        out.append(
            f"  HOLDOUT CLEAN: {taste.HOLDOUT_SIZE} official rows, {taste.HOLDOUT_SIZE} unique valid ideas, "
            "no questionable blind candidates."
        )
    else:
        out.append(
            f"  HOLDOUT NOT CLEAN: {audit.official_count} official row(s), {audit.unique_count} unique ideas, "
            f"{_questionable_count(audit)} questionable row(s)."
        )
        out.append("")
        out.append("REMEDIATION PLAN (manual; nothing has been changed)")
        for line in remediation_lines(audit):
            out.append(f"  - {line}")
    return out


def _append_row(out: list[str], row: HoldoutRow) -> None:
    out.append(
        f"  {_row_label(row)} | feedback {row.feedback_id} | origin {row.origin} | "
        f"{row.company or '-'} ({row.ticker or '-'})"
    )
    out.append(
        f"    source: {row.source_name or '-'}/{row.external_id or '-'} | screening: {row.screening_ref} "
        f"| prediction: {row.prediction or '-'} | label: {row.verdict or '-'}"
    )
    out.append(
        f"    learning-eligible: {'YES' if _in_taste_status_predicate(row) else 'NO'} | "
        f"holdout-eligible: {'YES' if row.holdout_eligible else 'NO'} | "
        f"excluded: {'YES' if row.excluded else 'NO'} | parse: {row.parse_status or '-'}"
    )
    out.append(f"    assigned: {row.assigned_at or '-'} | judged (feedback): {row.feedback_created_at}")
    status = "VALID" if not row.problems else "QUESTIONABLE: " + "; ".join(row.problems)
    out.append(f"    validity: {status}")


def _in_taste_status_predicate(row: HoldoutRow) -> bool:
    return row.holdout_eligible and not row.excluded and row.parse_status == "PARSED"


def _questionable_count(audit: HoldoutAudit) -> int:
    in_duplicate = {m.feedback_id for g in audit.duplicate_groups for m in g.members}
    return sum(
        1
        for row in audit.official + audit.candidates
        if row.problems or row.feedback_id in in_duplicate
    )
