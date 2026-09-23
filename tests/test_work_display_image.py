from pathlib import Path

from shadow_mdc.db.models import Work
from shadow_mdc.media.artwork import resolve_work_display_image


def _work(work_id: str, artwork: list[dict]) -> Work:
    return Work(
        id=work_id,
        title="fixture",
        family="jav",
        category="Japan",
        artwork=artwork,
    )


def test_samples_do_not_claim_missing_poster(tmp_path: Path) -> None:
    work_id = "work-samples-only"
    root = tmp_path / "artwork" / work_id
    samples = root / "samples"
    samples.mkdir(parents=True)
    fanart = root / "fanart.jpg"
    fanart.write_bytes(b"fanart")
    sample = samples / "sample_abc.jpg"
    sample.write_bytes(b"sample")
    thumb = root / "thumb.jpg"
    thumb.write_bytes(b"thumb")
    work = _work(
        work_id,
        [
            {
                "url": "https://example.com/fanart.jpg",
                "kind": "fanart",
                "local_path": str(fanart),
            },
            {
                "url": "https://example.com/s1.jpg",
                "kind": "sample",
                "local_path": str(sample),
            },
            {"url": "https://example.com/poster.jpg", "kind": "poster"},
        ],
    )
    assert resolve_work_display_image(work) == f"/api/works/{work_id}/artwork/thumb"
    assert resolve_work_display_image(work, data_dir=tmp_path) == f"/api/works/{work_id}/artwork/thumb"

    thumb.unlink()
    assert resolve_work_display_image(work, data_dir=tmp_path) == f"/api/works/{work_id}/artwork/fanart"

    fanart.unlink()
    assert resolve_work_display_image(work, data_dir=tmp_path) == "https://example.com/poster.jpg"


def test_fanart_kind_ignores_samples(tmp_path: Path) -> None:
    work_id = "work-fanart"
    root = tmp_path / "artwork" / work_id
    root.mkdir(parents=True)
    (root / "samples").mkdir()
    fanart = root / "fanart.jpg"
    fanart.write_bytes(b"x")
    sample = root / "samples" / "sample_1.jpg"
    sample.write_bytes(b"s")
    work = _work(
        work_id,
        [
            {"url": "https://example.com/f.jpg", "kind": "fanart", "local_path": str(fanart)},
            {"url": "https://example.com/s.jpg", "kind": "sample", "local_path": str(sample)},
        ],
    )
    assert resolve_work_display_image(work, "fanart", data_dir=tmp_path) == (
        f"/api/works/{work_id}/artwork/fanart"
    )
