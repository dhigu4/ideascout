"""Unit tests for ideascout/sources/raw_storage.py -- deterministic,
content-addressed raw HTML storage. Pure filesystem operations under
tmp_path only.
"""

from __future__ import annotations

from ideascout.sources import raw_storage


def test_raw_path_for_is_deterministic_from_source_external_id_and_hash(tmp_path):
    path1 = raw_storage.raw_path_for("yellowbrick", "abc123", "deadbeef", tmp_path)
    path2 = raw_storage.raw_path_for("yellowbrick", "abc123", "deadbeef", tmp_path)
    assert path1 == path2
    assert path1.is_relative_to(tmp_path)


def test_save_raw_html_writes_file_and_returns_its_path(tmp_path):
    path = raw_storage.save_raw_html("yellowbrick", "abc123", "deadbeef", "<html>hello</html>", tmp_path)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == "<html>hello</html>"


def test_save_raw_html_identical_content_is_a_no_op(tmp_path):
    path1 = raw_storage.save_raw_html("yellowbrick", "abc123", "deadbeef", "<html>v1</html>", tmp_path)
    mtime1 = path1.stat().st_mtime

    path2 = raw_storage.save_raw_html("yellowbrick", "abc123", "deadbeef", "<html>v1</html>", tmp_path)
    mtime2 = path2.stat().st_mtime

    assert path1 == path2
    assert mtime1 == mtime2  # never rewritten


def test_save_raw_html_different_hash_creates_a_new_file_old_one_untouched(tmp_path):
    old_path = raw_storage.save_raw_html("yellowbrick", "abc123", "oldhash", "<html>old</html>", tmp_path)
    new_path = raw_storage.save_raw_html("yellowbrick", "abc123", "newhash", "<html>new</html>", tmp_path)

    assert old_path != new_path
    assert old_path.exists()
    assert old_path.read_text(encoding="utf-8") == "<html>old</html>"
    assert new_path.read_text(encoding="utf-8") == "<html>new</html>"


def test_raw_path_sanitizes_unsafe_external_id_characters(tmp_path):
    path = raw_storage.raw_path_for("yellowbrick", "../../etc/passwd", "deadbeef", tmp_path)
    assert path.is_relative_to(tmp_path)
    assert ".." not in path.relative_to(tmp_path).parts
