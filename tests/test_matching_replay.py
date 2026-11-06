"""Human confirmations can be replayed locally without transmitting source links."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Library, MatchFeedback, MediaAsset
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import IdentityHints, ProviderRecord
from shadow_mdc.enums import QueryMode
from shadow_mdc.matching import rank_candidates


def test_manual_candidate_captures_scoring_inputs_and_reports_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'replay.db'}")
    source = "https://example.com/lookup?token=fixture"
    hints = IdentityHints(term="Shared title", title="Shared title", mode=QueryMode.TEXT, source_url=source)
    records = [
        ProviderRecord(provider="source", external_id="auto", title="Shared title", source_url=source),
        ProviderRecord(provider="source", external_id="manual", title="Shared title"),
    ]
    with TestClient(app) as client:
        runtime = app.state.runtime
        with runtime.database.session() as session:
            library = Library(name="Test", root_path=str(tmp_path))
            session.add(library)
            session.flush()
            asset = MediaAsset(library_id=library.id, path=str(tmp_path / "file.mp4"), size=0,
                               hints=hints.model_dump(mode="json"))
            session.add(asset)
            session.flush()
            asset_id = asset.id
            candidates = Repository(session).save_candidates(asset, rank_candidates(hints, records))
            chosen = next(row.id for row in candidates if row.external_id == "manual")
        assert client.post(f"/api/candidates/{chosen}/accept").status_code == 200
        assert client.get("/api/matching/replay").json()["without_code"] == {
            "samples": 1, "manual_review": 0, "automatic": 1, "disagreed": 1,
        }
        with runtime.database.session() as session:
            feedback = session.get(MatchFeedback, asset_id)
            assert feedback is not None
            assert "file_path" not in feedback.hints
            assert feedback.hints["source_url"] != source
            assert "token=fixture" not in repr(feedback.candidates)
