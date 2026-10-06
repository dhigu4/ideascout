"""READ-ONLY formal evaluation of a frozen Taste version against its clean
blind holdout (`evaluate-taste --version N`).

Scope: this module only READS. It never calls an LLM, never sets build_unlocked,
never writes approval, never creates a taste version, and never changes
feedback or screenings. Every number and excerpt comes from stored rows.

The holdout definition is holdout_audit.audit_holdout's OFFICIAL group -- the
same predicate taste-status counts -- so there is exactly one holdout definition.
Superseded duplicates and questionable candidates are never evaluated.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field

from . import blind_review, db, holdout_audit, screen_review, taste

INVESTIGATE_NOW = "INVESTIGATE_NOW"
WATCH = "WATCH"
PASS = "PASS"
INSUFFICIENT = "INSUFFICIENT_INFORMATION"

ACTUAL_CLASSES = (INVESTIGATE_NOW, WATCH, PASS)
PREDICTED_CLASSES = (INVESTIGATE_NOW, WATCH, PASS, INSUFFICIENT)

BRAD_LABEL_TO_CLASS = {
    "STRONG LIKE": INVESTIGATE_NOW,
    "LIKE": INVESTIGATE_NOW,
    "MAYBE": WATCH,
    "PASS": PASS,
    "STRONG PASS": PASS,
}

DEGENERATE_PASS_SHARE = 0.9
IMBALANCE_LOW = 0.25
IMBALANCE_HIGH = 0.75

DISCLAIMER = (
    "This diagnostic identifies possible overlearning. It does not prove that "
    "the 3x language caused the prediction."
)

HURDLE_PATTERNS = (
    ("3x-style multiple", re.compile(r"(?<![\w.])~?\s*(?:2\s*-\s*)?3\s*x(?![a-z])", re.IGNORECASE)),
    ("three-bagger", re.compile(r"three[\s-]+bagger", re.IGNORECASE)),
    ("effective hurdle", re.compile(r"effective\s+hurdle", re.IGNORECASE)),
)
PERMANENT_RULE_FRAMING = re.compile(r"100\s*%", re.IGNORECASE)

THEME_PATTERNS = (
    ("no clear reason for mispricing", (r"no clear reason", r"no (?:specific |clear )?reason (?:for|why)", r"unclear (?:why|reason)")),
    ("large cap / well covered", (r"large[\s-]cap", r"well[\s-]covered", r"widely (?:followed|covered)")),
    ("relative value", (r"relative[\s-]value",)),
    ("NAV / book discount", (r"\bNAV\b", r"book value", r"(?:discount|trades?) (?:to|below) (?:book|NAV)")),
    ("insufficient upside", (r"insufficient upside", r"limited upside", r"upside (?:is )?(?:too )?(?:small|limited|insufficient)")),
    ("no edge", (r"\bno (?:informational |variant |clear )?edge\b",)),
    ("cyclical", (r"\bcyclical",)),
    ("takeout dependent", (r"take-?out",)),
)


def normalize_label(verdict: str | None) -> str:
    return (verdict or "").replace("_", " ").strip().upper()


@dataclass
class EvalRow:
    ref: str
    feedback_id: int
    company: str | None
    ticker: str | None
    source: str
    sr_ref: str
    prediction: str
    label: str
    actual: str
    screening: sqlite3.Row
    judged_at: str

    @property
    def is_actual_positive(self) -> bool:
        return self.actual in (INVESTIGATE_NOW, WATCH)

    @property
    def is_predicted_positive(self) -> bool:
        return self.prediction in (INVESTIGATE_NOW, WATCH)

    @property
    def kind(self) -> str:
        if self.prediction == INSUFFICIENT:
            return "UNSCORED (INSUFFICIENT_INFORMATION)"
        if self.prediction == self.actual:
            return "correct"
        if self.is_actual_positive and self.prediction == PASS:
            return "false negative"
        if not self.is_actual_positive and self.is_predicted_positive:
            return "false positive"
        return "severity mismatch"

    @property
    def error_bucket(self) -> int:
        """1-7 cost ordering from the spec; 8 = correct (listed last)."""
        if self.kind == "correct":
            return 8
        if self.prediction == PASS:
            return {"STRONG LIKE": 1, "LIKE": 2, "MAYBE": 3}.get(self.label, 7)
        if self.label in ("STRONG LIKE", "LIKE") and self.prediction == WATCH:
            return 4
        if self.label in ("STRONG PASS", "PASS") and self.prediction == INVESTIGATE_NOW:
            return 5
        if self.label in ("STRONG PASS", "PASS") and self.prediction == WATCH:
            return 6
        return 7


# --- gate -------------------------------------------------------------------------------


@dataclass
class GateResult:
    checks: list[tuple[str, bool, str]]
    audit: holdout_audit.HoldoutAudit
    rows: list[EvalRow]

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def _screening_before_judgment(conn, source_id: int, taste_version: int, judged_at: str):
    return conn.execute(
        """
        SELECT * FROM source_screenings
        WHERE source_id = ? AND taste_version = ? AND created_at <= ?
        ORDER BY created_at DESC, screening_id DESC
        LIMIT 1
        """,
        (source_id, taste_version, judged_at),
    ).fetchone()


def check_gate(conn: sqlite3.Connection, latest_taste: sqlite3.Row, requested_version: int) -> GateResult:
    """SELECT-only. Every condition the evaluation requires, each reported."""
    audit = holdout_audit.audit_holdout(conn, latest_taste)
    version = latest_taste["version_number"]
    checks: list[tuple[str, bool, str]] = []

    checks.append((
        f"Taste version {requested_version} is the latest version",
        version == requested_version,
        f"latest is v{version}",
    ))
    try:
        taste.verify_taste_version_integrity(latest_taste)
        integrity = (True, "artifact hash matches")
    except taste.TasteIntegrityError as exc:
        integrity = (False, str(exc))
    checks.append(("Taste artifact integrity OK", integrity[0], integrity[1]))
    checks.append((
        "Taste version frozen (build_unlocked = 0)",
        latest_taste["build_unlocked"] == 0,
        f"build_unlocked={latest_taste['build_unlocked']}",
    ))
    checks.append((
        f"Holdout complete: {taste.HOLDOUT_SIZE} official members",
        audit.official_count == taste.HOLDOUT_SIZE,
        f"{audit.official_count} official",
    ))
    checks.append((
        f"{taste.HOLDOUT_SIZE} unique valid ideas",
        audit.valid_unique_count == taste.HOLDOUT_SIZE and audit.unique_count == taste.HOLDOUT_SIZE,
        f"{audit.valid_unique_count} valid unique, {audit.unique_count} unique",
    ))
    checks.append((
        "Zero questionable blind candidates",
        not audit.candidates,
        f"{len(audit.candidates)} candidate(s)",
    ))
    checks.append((
        "Zero duplicate groups",
        not audit.duplicate_groups,
        f"{len(audit.duplicate_groups)} group(s)",
    ))
    checks.append((
        "No integrity problems on holdout members",
        not any(r.problems for r in audit.official),
        f"{sum(1 for r in audit.official if r.problems)} member(s) with problems",
    ))

    rows: list[EvalRow] = []
    missing: list[str] = []
    for member in audit.official:
        label = normalize_label(member.verdict)
        screening = _screening_before_judgment(conn, member.source_id, version, member.feedback_created_at)
        if label not in BRAD_LABEL_TO_CLASS:
            missing.append(f"{member.ref}: unrecognized label {member.verdict!r}")
            continue
        if screening is None:
            missing.append(f"{member.ref}: no Taste v{version} screening created before the judgment")
            continue
        prediction = screening["overall_prediction"]
        if prediction not in (INVESTIGATE_NOW, WATCH, PASS, INSUFFICIENT):
            missing.append(f"{member.ref}: unrecognized prediction {prediction!r}")
            continue
        rows.append(EvalRow(
            ref=member.ref,
            feedback_id=member.feedback_id,
            company=member.company,
            ticker=member.ticker,
            source=f"{member.source_name}/{member.external_id}",
            sr_ref=screen_review.format_review_ref(screening["screening_id"]),
            prediction=prediction,
            label=label,
            actual=BRAD_LABEL_TO_CLASS.get(label, ""),
            screening=screening,
            judged_at=member.feedback_created_at,
        ))
    checks.append((
        f"Taste v{version} prediction existed before each judgment",
        not missing,
        "all present" if not missing else "; ".join(missing),
    ))
    rows.sort(key=lambda r: r.feedback_id)
    return GateResult(checks=checks, audit=audit, rows=rows)


# --- metrics ------------------------------------------------------------------------------


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else numerator / denominator


def _fmt(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


@dataclass
class Metrics:
    n: int
    exact_correct: int
    n_scored: int
    exact_correct_scored: int
    matrix: dict[str, dict[str, int]]
    tp: int
    fp: int
    tn: int
    fn: int
    precision: float | None
    recall: float | None
    specificity: float | None
    balanced_accuracy: float | None
    f1: float | None
    hc_actual: int
    hc_hit: int
    hc_recall: float | None
    pass_pred_share: float | None
    actual_pass_share: float | None
    actual_positive_share: float | None
    insufficient_rows: list[EvalRow] = field(default_factory=list)


def compute_metrics(rows: list[EvalRow]) -> Metrics:
    matrix = {a: {p: 0 for p in PREDICTED_CLASSES} for a in ACTUAL_CLASSES}
    for r in rows:
        matrix[r.actual][r.prediction] += 1
    scored = [r for r in rows if r.prediction != INSUFFICIENT]
    tp = sum(1 for r in scored if r.is_actual_positive and r.is_predicted_positive)
    fp = sum(1 for r in scored if not r.is_actual_positive and r.is_predicted_positive)
    tn = sum(1 for r in scored if not r.is_actual_positive and not r.is_predicted_positive)
    fn = sum(1 for r in scored if r.is_actual_positive and not r.is_predicted_positive)
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    specificity = _ratio(tn, tn + fp)
    balanced = None if recall is None or specificity is None else (recall + specificity) / 2
    f1_den = 2 * tp + fp + fn
    f1 = None if f1_den == 0 else 2 * tp / f1_den
    hc = [r for r in rows if r.actual == INVESTIGATE_NOW]
    hc_hit = [r for r in hc if r.prediction == INVESTIGATE_NOW]
    n = len(rows)
    return Metrics(
        n=n,
        exact_correct=sum(1 for r in rows if r.prediction == r.actual),
        n_scored=len(scored),
        exact_correct_scored=sum(1 for r in scored if r.prediction == r.actual),
        matrix=matrix,
        tp=tp, fp=fp, tn=tn, fn=fn,
        precision=precision, recall=recall, specificity=specificity,
        balanced_accuracy=balanced, f1=f1,
        hc_actual=len(hc), hc_hit=len(hc_hit), hc_recall=_ratio(len(hc_hit), len(hc)),
        pass_pred_share=_ratio(sum(1 for r in rows if r.prediction == PASS), n),
        actual_pass_share=_ratio(sum(1 for r in rows if r.actual == PASS), n),
        actual_positive_share=_ratio(sum(1 for r in rows if r.is_actual_positive), n),
        insufficient_rows=[r for r in rows if r.prediction == INSUFFICIENT],
    )


# --- stored-text diagnostics --------------------------------------------------------------


def _stored_text_items(screening) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for field_name, column in (
        ("key reasons", "key_reasons_json"),
        ("key concerns", "key_concerns_json"),
        ("critical questions", "critical_questions_json"),
    ):
        for text in json.loads(screening[column] or "[]"):
            if isinstance(text, str) and text.strip():
                items.append((field_name, text))
    return items


def _excerpt(text: str, start: int, end: int, radius: int = 80, limit: int = 180) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = " ".join(text[left:right].split())
    if left > 0:
        snippet = "…" + snippet
    if right < len(text):
        snippet = snippet + "…"
    return snippet[:limit]


@dataclass
class HurdleHit:
    row: EvalRow
    categories: list[str]
    field_name: str
    excerpt: str
    permanent_framing: bool


def hurdle_hits(rows: list[EvalRow]) -> list[HurdleHit]:
    hits: list[HurdleHit] = []
    for row in rows:
        items = _stored_text_items(row.screening)
        categories: list[str] = []
        first: tuple[str, str] | None = None
        for field_name, text in items:
            for category, pattern in HURDLE_PATTERNS:
                match = pattern.search(text)
                if match:
                    if category not in categories:
                        categories.append(category)
                    if first is None:
                        first = (field_name, _excerpt(text, match.start(), match.end()))
        if categories and first is not None:
            framing = any(PERMANENT_RULE_FRAMING.search(text) for _, text in items)
            hits.append(HurdleHit(row, categories, first[0], first[1], framing))
    return hits


def theme_counts(rows: list[EvalRow]) -> list[tuple[str, list[str]]]:
    results = []
    for theme, patterns in THEME_PATTERNS:
        regexes = [re.compile(p, re.IGNORECASE) for p in patterns]
        refs = [
            row.ref for row in rows
            if any(rx.search(text) for _, text in _stored_text_items(row.screening) for rx in regexes)
        ]
        results.append((theme, refs))
    return results


def rule_excerpt(rules_text: str) -> list[str]:
    lines = [line.strip() for line in rules_text.splitlines() if line.strip()]
    matched = [line for line in lines if re.search(r"\d+\s*%", line)]
    return [line[:220] for line in matched[:6]]


# --- rendering ----------------------------------------------------------------------------


def _dims(row: EvalRow) -> str:
    s = row.screening
    return (
        f"mispricing={s['mispricing']} | variant={s['variant_perception']} | upside={s['upside']} | "
        f"business={s['business_quality']} | downside={s['downside']} | confidence={s['confidence']}"
    )


def _reasoning_lines(row: EvalRow) -> list[str]:
    s = row.screening
    lines = [f"    dimensions: {_dims(row)}"]
    for label, column in (
        ("key reasons", "key_reasons_json"),
        ("key concerns", "key_concerns_json"),
        ("critical questions", "critical_questions_json"),
    ):
        values = [v for v in json.loads(s[column] or "[]") if isinstance(v, str) and v.strip()]
        lines.append(f"    {label}: " + (" | ".join(values) if values else "(none stored)"))
    return lines


def _row_line(row: EvalRow) -> str:
    return (
        f"  {row.ref} | feedback {row.feedback_id} | {row.company or '-'} ({row.ticker or '-'}) | "
        f"source {row.source} | {row.sr_ref} | model {row.prediction} | Brad {row.label} -> {row.actual} | "
        f"{row.kind}"
    )


def render_report(
    *,
    version_label: str,
    checkpoint_feedback_id: int,
    rules_sha256: str,
    gate: GateResult,
    rules_text: str,
) -> list[str]:
    rows = gate.rows
    m = compute_metrics(rows)
    out: list[str] = []
    out.append(f"TASTE EVALUATION -- {version_label} (checkpoint feedback_id {checkpoint_feedback_id})")
    out.append(f"Screening rules sha256 at evaluation: {rules_sha256}")
    out.append("")
    out.append("HOLDOUT GATE: PASSED")
    for name, ok, detail in gate.checks:
        out.append(f"  [{'ok' if ok else 'FAIL'}] {name} ({detail})")
    out.append("")
    out.append("LABEL MAPPING (Brad label -> actual class; model prediction used as-is)")
    out.append("  STRONG LIKE -> INVESTIGATE_NOW | LIKE -> INVESTIGATE_NOW | MAYBE -> WATCH")
    out.append("  PASS -> PASS | STRONG PASS -> PASS")
    out.append("  Model: INVESTIGATE_NOW / WATCH / PASS as-is; INSUFFICIENT_INFORMATION reported separately, never as PASS")
    out.append("")
    out.append("CONFUSION MATRIX (rows = actual class, columns = model prediction)")
    header = "  " + "actual \\ predicted".ljust(22) + "".join(p.ljust(26) for p in PREDICTED_CLASSES)
    out.append(header)
    for actual in ACTUAL_CLASSES:
        out.append(
            "  " + actual.ljust(22) + "".join(str(m.matrix[actual][p]).ljust(26) for p in PREDICTED_CLASSES)
        )
    out.append("")
    out.append(
        f"OVERALL ACCURACY: exact class {m.exact_correct}/{m.n} = {_fmt(_ratio(m.exact_correct, m.n))} "
        f"(INSUFFICIENT_INFORMATION counted as wrong); on scored rows {m.exact_correct_scored}/{m.n_scored} = "
        f"{_fmt(_ratio(m.exact_correct_scored, m.n_scored))}"
    )
    out.append("")
    out.append("WORTH-AT-LEAST-WATCHING BINARY VIEW (scored rows only)")
    out.append("  actual positive: STRONG LIKE / LIKE / MAYBE | predicted positive: INVESTIGATE_NOW / WATCH")
    out.append(f"  TP {m.tp} | FP {m.fp} | TN {m.tn} | FN {m.fn}")
    out.append(f"  precision {_fmt(m.precision)} | recall {_fmt(m.recall)} | specificity {_fmt(m.specificity)}")
    out.append(f"  balanced accuracy {_fmt(m.balanced_accuracy)} | F1 {_fmt(m.f1)}")
    out.append(
        f"  INSUFFICIENT_INFORMATION rows excluded from this view: {len(m.insufficient_rows)} "
        f"({sum(1 for r in m.insufficient_rows if r.is_actual_positive)} of them actual positive)"
    )
    out.append("")
    out.append("HIGH-CONVICTION DISCOVERY (actual LIKE / STRONG LIKE predicted INVESTIGATE_NOW)")
    out.append(
        f"  recall {m.hc_hit}/{m.hc_actual} = {_fmt(m.hc_recall)} "
        "(denominator includes every actual high-conviction row)"
    )
    out.append("")
    out.append("CLASS BALANCE")
    out.append(
        f"  actual positive share {_fmt(m.actual_positive_share)} | actual PASS share {_fmt(m.actual_pass_share)} | "
        f"predicted PASS share {_fmt(m.pass_pred_share)}"
    )
    if m.pass_pred_share is not None and m.pass_pred_share >= DEGENERATE_PASS_SHARE:
        out.append(
            f"  WARNING: Degenerate all-PASS classifier behavior -- {_fmt(m.pass_pred_share)} of predictions are PASS. "
            f"Raw accuracy {_fmt(_ratio(m.exact_correct, m.n))} mostly reflects the PASS base rate. "
            "Judge by recall and balanced accuracy, not raw accuracy."
        )
    if m.actual_positive_share is not None and not (IMBALANCE_LOW <= m.actual_positive_share <= IMBALANCE_HIGH):
        out.append(
            f"  WARNING: Holdout is materially imbalanced ({_fmt(m.actual_positive_share)} actual positive). "
            "Do not let raw accuracy obscure discovery recall."
        )
    out.append("")
    out.append("ERROR TABLE (costliest misses first)")
    for row in sorted(rows, key=lambda r: (r.error_bucket, r.feedback_id)):
        out.append(_row_line(row))
    out.append("")
    out.append("FALSE NEGATIVES: actual LIKE / STRONG LIKE / MAYBE predicted PASS (stored rationale, no LLM)")
    fns = sorted((r for r in rows if r.is_actual_positive and r.prediction == PASS), key=lambda r: r.feedback_id)
    if not fns:
        out.append("  (none)")
    for row in fns:
        out.append(_row_line(row))
        out.extend(_reasoning_lines(row))
    out.append("")
    out.append("FALSE POSITIVES: actual PASS / STRONG PASS predicted WATCH or INVESTIGATE_NOW (stored rationale)")
    fps = sorted((r for r in rows if r.kind == "false positive"), key=lambda r: r.feedback_id)
    if not fps:
        out.append("  (none)")
    for row in fps:
        out.append(_row_line(row))
        out.extend(_reasoning_lines(row))
    out.append("")
    out.append("3x HURDLE DIAGNOSTIC (stored screening text only)")
    out.append(f"  {DISCLAIMER}")
    out.append("  Canonical permanent rule (quoted from the screening rules file, not interpreted):")
    excerpts = rule_excerpt(rules_text)
    if excerpts:
        for line in excerpts:
            out.append(f"    > {line}")
    else:
        out.append("    (no rule lines mentioning 100 or upside were found)")
    hits = hurdle_hits(rows)
    if not hits:
        out.append("  No holdout screening uses 3x, three-bagger, or effective-hurdle language.")
    for hit in hits:
        outcome = hit.row.kind
        in_fn = "IN a false negative" if hit.row.kind == "false negative" else f"outcome: {outcome}"
        out.append(
            f"  {hit.row.ref} | model {hit.row.prediction} | Brad {hit.row.label} | {in_fn} | "
            f"categories: {', '.join(hit.categories)} | in {hit.field_name}"
        )
        out.append(f"    excerpt: \"{hit.excerpt}\"")
        if hit.permanent_framing:
            out.append("    note: the same stored text also cites 100%-style framing")
    fn_hits = [h for h in hits if h.row.kind == "false negative"]
    out.append(
        f"  Summary: {len(hits)} holdout screening(s) use 3x-style language; "
        f"{len(fn_hits)} of them are false negatives."
    )
    out.append("")
    out.append("OTHER PATTERNS IN FALSE NEGATIVES (simple phrase matching on stored text)")
    fn_rows = [r for r in rows if r.is_actual_positive and r.prediction == PASS]
    for theme, refs in theme_counts(fn_rows):
        if refs:
            out.append(f"  {theme}: {len(refs)} ({', '.join(refs)})")
    if not fn_rows:
        out.append("  (no false negatives)")
    out.append("")
    out.append("EVALUATION ONLY — Taste remains frozen.")
    out.append("Brad review/approval is required before any unlock or new Taste build.")
    return out
