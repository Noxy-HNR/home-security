"""The browser decodes ES modules as UTF-8 with no override: one stray byte renders as U+FFFD.

A bare Latin-1 `·` (0xB7) in util.js once put a `�` in the title of every sensor event, and
nothing failed — not the tests, not the build, not the page load. These checks make that loud.
"""

from __future__ import annotations

from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
TEXT_SUFFIXES = {".js", ".css", ".html", ".svg"}
FILES = sorted(p for p in FRONTEND.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)


def test_frontend_files_found():
    assert FILES, f"no frontend sources under {FRONTEND}"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_source_is_valid_utf8(path: Path):
    raw = path.read_bytes()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as err:
        line = raw[: err.start].count(b"\n") + 1
        pytest.fail(f"{path.relative_to(FRONTEND)} line {line}: byte {raw[err.start]:#04x} is not UTF-8 ({err.reason})")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_source_has_no_replacement_char(path: Path):
    """Catches a `�` that was *saved* as valid UTF-8 — already-lost characters, not just bad bytes."""
    text = path.read_text(encoding="utf-8")
    assert "�" not in text, f"{path.relative_to(FRONTEND)} contains U+FFFD, a character lost in an earlier save"
