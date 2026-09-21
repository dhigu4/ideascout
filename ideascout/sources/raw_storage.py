"""Deterministic, content-addressed raw-artifact storage for collected
website sources.

This is permanent provenance: a raw capture is never deleted or
overwritten, and identical content re-saved under the same external_id is
a guaranteed no-op (the filename IS the content hash), so re-running a
collector is always safe. A changed version of the same external_id gets
its own new file -- the old one is untouched, preserving history exactly
as CLAUDE.md's production-state rules and this stage's spec require.

raw_storage_dir is always passed in explicitly by the caller (from
Config.raw_storage_dir) -- this module never computes its own default,
matching the rest of the app's rule that only ideascout/config.py ever
decides what the real production paths are.
"""

from __future__ import annotations

import re
from pathlib import Path

_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize(component: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", component)
    return cleaned or "unknown"


def raw_path_for(source_name: str, external_id: str, content_hash: str, raw_storage_dir: Path) -> Path:
    return raw_storage_dir / _sanitize(source_name) / _sanitize(external_id) / f"{content_hash}.html"


def save_raw_html(source_name: str, external_id: str, content_hash: str, html: str, raw_storage_dir: Path) -> Path:
    """Write `html` to its deterministic, content-addressed path and
    return it. If a file already exists there (identical content,
    identical hash), nothing is re-written -- this is what makes
    re-collecting an already-known, unchanged source a true no-op.
    """
    path = raw_path_for(source_name, external_id, content_hash, raw_storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(html, encoding="utf-8", newline="")
    return path
