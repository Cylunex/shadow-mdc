"""Offline fixtures for coserslove homepage discovery."""

from __future__ import annotations

from pathlib import Path

from shadow_mdc.services.coserslove_catalog import parse_home_html

FIXTURES = Path(__file__).parent / "fixtures" / "coserslove"


def test_parse_home_albums_and_cosers() -> None:
    html = (FIXTURES / "home.html").read_text(encoding="utf-8")
    snap = parse_home_html(html)
    assert len(snap.albums) >= 8
    assert snap.albums[0].coser_name
    assert snap.albums[0].title
    assert snap.albums[0].album_id
    assert len(snap.cosers) >= 3
    assert "蠢沫沫" in snap.coser_names or any("沫" in n for n in snap.coser_names)
    # Album-derived names should expand beyond the featured strip.
    assert len(snap.coser_names) >= len(snap.cosers)


def test_challenge_home_empty() -> None:
    snap = parse_home_html("<html><title>Just a moment...</title></html>")
    assert snap.albums == ()
    assert snap.coser_names == ()
