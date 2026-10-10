"""NFO must reference local sibling poster/fanart files, never remote URLs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shadow_mdc.media.nfo import build_nfo
from shadow_mdc.services.pan import PanSettings, RemoteVideo
from shadow_mdc.services.strm_export import export_work


def _work(tmp_path: Path, *, with_local: bool) -> SimpleNamespace:
    art = tmp_path / "cache"
    art.mkdir(exist_ok=True)
    poster = art / "poster.jpg"
    poster.write_bytes(b"P" * 20)
    artwork = [
        {
            "kind": "poster",
            "url": "https://cdn.example/remote-poster.jpg",
            **({"local_path": str(poster)} if with_local else {}),
        }
    ]
    return SimpleNamespace(
        id="w1",
        title="Demo",
        original_title=None,
        primary_code="ABC-123",
        plot=None,
        release_date=None,
        runtime_seconds=None,
        studio="Studio",
        category="Japan",
        series=None,
        actors=(),
        directors=(),
        tags=(),
        artwork=artwork,
    )


def test_build_nfo_uses_explicit_local_artwork_names_not_urls(tmp_path: Path) -> None:
    work = _work(tmp_path, with_local=True)
    nfo = build_nfo(work, [], local_artwork={"poster": "poster.jpg", "fanart": "fanart.jpg"})  # type: ignore[arg-type]
    assert "https://cdn.example" not in nfo
    assert "poster.jpg" in nfo
    assert "fanart.jpg" in nfo


def test_build_nfo_derives_sibling_names_from_local_path(tmp_path: Path) -> None:
    work = _work(tmp_path, with_local=True)
    nfo = build_nfo(work, [])  # type: ignore[arg-type]
    assert "https://cdn.example" not in nfo
    assert "poster.jpg" in nfo


def test_build_nfo_omits_remote_only_artwork(tmp_path: Path) -> None:
    work = _work(tmp_path, with_local=False)
    nfo = build_nfo(work, [])  # type: ignore[arg-type]
    assert "https://cdn.example" not in nfo
    assert "<thumb" not in nfo
    assert "<fanart" not in nfo


def test_strm_export_nfo_references_copied_siblings(tmp_path: Path) -> None:
    work = _work(tmp_path, with_local=True)
    root = tmp_path / "strm"
    root.mkdir()
    settings = PanSettings(
        strm_enabled=True,
        strm_output_root=str(root),
        strm_mode="openlist",
        strm_url_prefix="http://openlist/d",
        strm_layout_template="{studio}/{code}",
    )
    result = export_work(
        settings=settings,
        code="ABC-123",
        videos=[
            RemoteVideo(
                file_id="f1",
                name="ABC-123.mp4",
                pick_code="p1",
                relative_path="ABC-123.mp4",
                size=1,
            )
        ],
        work=work,  # type: ignore[arg-type]
        identities=[],
    )
    assert result.nfo_path is not None
    nfo_text = result.nfo_path.read_text(encoding="utf-8")
    assert "https://cdn.example" not in nfo_text
    assert "poster.jpg" in nfo_text
    assert (result.directory / "poster.jpg").is_file()
