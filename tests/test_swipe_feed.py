"""Library swipe-feed entry route (SPA tab lives at ?view=swipe)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app


def test_swipe_feed_entry_returns_html(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'swipe.db'}")

    with TestClient(app) as client:
        for path in ("/swipe", "/swipe/"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 200, path
            assert "text/html" in response.headers.get("content-type", "")
            body = response.text
            assert "view=swipe" in body
            assert "刷流" in body
