# -*- coding: utf-8 -*-
"""Tests for _path_to_file_url in send_file module.

Verifies that non-ASCII characters (CJK, full-width punctuation, emoji)
and ASCII special characters are correctly percent-encoded, producing a
valid ASCII-only RFC 8089 file:// URL that round-trips through
file_url_to_local_path on POSIX systems.
"""

# pylint: disable=protected-access
import os

import pytest

from qwenpaw.agents.tools.send_file import _path_to_file_url
from qwenpaw.app.channels.utils import file_url_to_local_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


# ---------------------------------------------------------------------------
# _path_to_file_url: basic sanity
# ---------------------------------------------------------------------------


class TestPathToFileUrl:
    """Tests for _path_to_file_url."""

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/plain.txt",
            "/tmp/my file.txt",
            "/tmp/file#1.txt",
            "/tmp/100%.txt",
            "/tmp/Q&A.txt",
            "/tmp/a;b.txt",
            "/tmp/C++ primer.pdf",
            "/tmp/data[2026].csv",
        ],
    )
    def test_ascii_paths_produce_valid_url(self, path: str) -> None:
        url = _path_to_file_url(path)
        assert url.startswith(
            "file://",
        ), f"Expected file:// scheme, got {url!r}"
        assert _is_ascii(url), f"URL is not ASCII-safe: {url!r}"

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/文件，测试。.txt",
            "/tmp/報告（最終版）.docx",
            "/tmp/テスト.txt",
            "/tmp/테스트.txt",
            "/tmp/📊report.xlsx",
        ],
    )
    def test_non_ascii_paths_produce_valid_url(self, path: str) -> None:
        """Non-ASCII file names must be percent-encoded, not embedded raw."""
        url = _path_to_file_url(path)
        assert url.startswith(
            "file://",
        ), f"Expected file:// scheme, got {url!r}"
        assert _is_ascii(url), f"URL contains non-ASCII characters: {url!r}"
        # The raw non-ASCII characters must NOT appear in the URL
        for ch in path:
            if ord(ch) > 127:
                assert (
                    ch not in url
                ), f"Raw non-ASCII char {ch!r} found unencoded in URL: {url!r}"

    def test_percent_in_filename_not_double_encoded(self) -> None:
        """A literal '%' in a filename must be encoded as %25 exactly once."""
        url = _path_to_file_url("/tmp/100%.txt")
        assert "%25" in url, f"Expected %25 in URL, got {url!r}"
        # Must not become %2525 (double-encoding)
        assert "%2525" not in url, f"Double-encoding detected in URL: {url!r}"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX only")
    def test_posix_url_format(self) -> None:
        """On POSIX, result must be file:///absolute/path (three slashes)."""
        url = _path_to_file_url("/tmp/test.txt")
        assert url == "file:///tmp/test.txt", f"Unexpected URL: {url!r}"


# ---------------------------------------------------------------------------
# Round-trip: _path_to_file_url → file_url_to_local_path (POSIX only)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX round-trip test")
class TestRoundTripPosix:
    """Verify encoding then decoding produces the original absolute path."""

    @pytest.mark.parametrize(
        "path",
        [
            "/tmp/plain.txt",
            "/tmp/my file.txt",
            "/tmp/file#1.txt",
            "/tmp/100%.txt",
            "/tmp/Q&A.txt",
            "/tmp/文件，测试。.txt",
            "/tmp/報告（最終版）.docx",
            "/tmp/テスト.txt",
            "/tmp/테스트.txt",
            "/tmp/📊report.xlsx",
        ],
    )
    def test_roundtrip(self, path: str) -> None:
        url = _path_to_file_url(path)
        recovered = file_url_to_local_path(url)
        assert recovered == os.path.abspath(path), (
            f"Round-trip failed for {path!r}: "
            f"url={url!r}, recovered={recovered!r}"
        )
