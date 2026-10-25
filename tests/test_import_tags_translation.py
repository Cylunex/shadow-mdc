from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily
from shadow_mdc.services.catalog_import import _import_formal_works_from_portable_db
from shadow_mdc.services.post_ingest import (
    translate_after_ingest,
    translate_pending_works,
    work_needs_translation,
)
from shadow_mdc.services.translation import GoogleTitleTranslator, TranslationCache

JA_TITLE = "隣人のゴミ部屋"
JA_PLOT = "これはあらすじです"


def _record(code: str = "ABC-001") -> ProviderRecord:
    return ProviderRecord(
        provider="fixture",
        external_id=code.lower(),
        code=code,
        title=JA_TITLE,
        plot=JA_PLOT,
        tags=["巨乳"],
        family=ContentFamily.JAV,
        language="ja",
    )


def _make_source_db(path: Path) -> None:
    db = Database(f"sqlite:///{path}")
    db.initialize()
    with db.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(_record(), overwrite=True)
        work.tags = ["巨乳", "评分:7", "boss-rating"]
        repo.apply_title_translation(work, source=JA_TITLE, translated="邻居的垃圾房", provider="google")
        repo.apply_plot_translation(work, source=JA_PLOT, translated="这是剧情", provider="google")


def _import(repo: Repository, src: Path, tmp: Path) -> int:
    return _import_formal_works_from_portable_db(
        repo=repo, portable_db=src, source_data_dir=tmp / "srcdata", target_artwork_dir=tmp / "art", dry_run=False
    )


def test_import_merges_custom_tags_and_translations_idempotently(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src)
    target = Database(f"sqlite:///{tmp_path / 'dst.db'}")
    target.initialize()
    with target.session() as session:
        repo = Repository(session)
        existing = repo.upsert_provider_record(_record(), overwrite=True)
        existing.tags = ["巨乳", "nas-only"]
    for _ in range(2):
        with target.session() as session:
            _import(Repository(session), src, tmp_path)
    with target.session() as session:
        work = Repository(session).find_work_by_code("ABC-001") if hasattr(Repository, "find_work_by_code") else None
        if work is None:
            from sqlalchemy import select

            from shadow_mdc.db.models import Work

            work = session.scalars(select(Work)).one()
        assert work.tags == ["巨乳", "nas-only", "评分:7", "boss-rating"]
        assert work.title == "邻居的垃圾房"
        assert work.original_title == JA_TITLE
        assert work.plot == "这是剧情"
        assert work.original_plot == JA_PLOT
        assert work.field_sources["title"] == "translation:google"
        assert not work_needs_translation(work)


def test_import_respects_locks_and_manual_edits(tmp_path: Path) -> None:
    src = tmp_path / "src.db"
    _make_source_db(src)
    target = Database(f"sqlite:///{tmp_path / 'dst.db'}")
    target.initialize()
    with target.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(_record(), overwrite=True)
        repo.update_work_fields(work, title="我的标题", tags=["mine"])
        work.field_locks = [*work.field_locks, "plot"]
    with target.session() as session:
        _import(Repository(session), src, tmp_path)
    with target.session() as session:
        from sqlalchemy import select

        from shadow_mdc.db.models import Work

        work = session.scalars(select(Work)).one()
        assert work.title == "我的标题"
        assert work.plot == JA_PLOT
        assert work.tags == ["mine"]  # tags locked by manual edit


def _translator(client: httpx.AsyncClient, tmp: Path) -> GoogleTitleTranslator:
    return GoogleTitleTranslator(
        client,
        TranslationCache(tmp / "tr.db"),
        enabled=True,
        endpoint="https://translate.example/translate_a/single",
        target_language="zh-CN",
    )


@pytest.mark.asyncio
async def test_post_ingest_failure_does_not_raise(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'w.db'}")
    db.initialize()

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as client:
        with db.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(_record(), overwrite=True)
            status = await translate_after_ingest(repo, work, _translator(client, tmp_path))
            assert status in {"failed"}
            assert work.title == JA_TITLE

        class Raises:
            async def translate_work(self, repo, work):  # noqa: ANN001
                raise RuntimeError("x")

        with db.session() as session:
            repo = Repository(session)
            assert await translate_after_ingest(repo, repo.upsert_provider_record(_record(), overwrite=False), Raises()) == "failed"


@pytest.mark.asyncio
async def test_backfill_translates_bounded_and_resumable(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'w.db'}")
    db.initialize()
    with db.session() as session:
        repo = Repository(session)
        for i in range(3):
            repo.upsert_provider_record(_record(f"ABC-00{i}"), overwrite=True)

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[["中文", "x", None, None]], None, "ja"])

    ckpt = tmp_path / "ckpt.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as client:
        tr = _translator(client, tmp_path)
        first = await translate_pending_works(db, tr, limit=2, checkpoint=ckpt)
        assert first.scanned == 2 and first.translated == 2 and first.remaining_estimate == 1
        assert ckpt.is_file()
        second = await translate_pending_works(db, tr, limit=2, checkpoint=ckpt)
        assert second.scanned == 1 and second.translated == 1
        assert not ckpt.exists()
        third = await translate_pending_works(db, tr, limit=10, checkpoint=ckpt)
        assert third.scanned == 0


def test_seed_script_wrapper_runs_hook_and_preserves_exit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import shadow_mdc.services.post_ingest as pi

    calls: list[object] = []

    async def fake(settings, database, *, since=None, **_):  # noqa: ANN001
        calls.append(since)
        raise RuntimeError("translation down")

    monkeypatch.setattr(pi, "run_post_ingest_translation", fake)
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'x.db'}")

    def main() -> None:
        raise SystemExit(3)

    with pytest.raises(SystemExit) as exc:
        pi.run_script_with_post_ingest(main, argv=["--database-url", f"sqlite:///{tmp_path / 'x.db'}"])
    assert exc.value.code == 3
    assert len(calls) == 1
