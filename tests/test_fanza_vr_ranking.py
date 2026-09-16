"""Offline tests for FANZA VR ranking list names and floor fallback."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from shadow_mdc.providers.fanza import FanzaProvider

FIXTURES = Path(__file__).parent / "fixtures" / "weekly_vr"


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("POST", "https://api.video.dmm.co.jp/graphql")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status={self.status_code}",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request, json=self._payload),
            )

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.mark.asyncio
async def test_fetch_ranking_vr_falls_back_to_av_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    mixed = json.loads((FIXTURES / "fanza_ranking_mixed.json").read_text(encoding="utf-8"))
    calls: list[dict[str, Any]] = []

    async def fake_post(url: str, **kwargs: Any) -> _FakeResponse:
        payload = kwargs.get("json") or {}
        variables = payload.get("variables") or {}
        filt = variables.get("filter") or {}
        calls.append(filt)
        # First VR attempt → 422
        weekly = filt.get("weekly") if isinstance(filt, dict) else None
        if isinstance(weekly, dict) and weekly.get("floor") == "VR":
            return _FakeResponse(
                422,
                {
                    "errors": [
                        {
                            "message": "VR is not a valid PPVFloor",
                            "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
                        }
                    ]
                },
            )
        return _FakeResponse(200, mixed)

    client = httpx.AsyncClient()
    monkeypatch.setattr(client, "post", fake_post)
    provider = FanzaProvider(client, "https://www.dmm.co.jp", retries=0)
    try:
        items = await provider.fetch_ranking("rankings_weekly_vr", limit=10, floor="VR")
    finally:
        await client.aclose()

    assert any(isinstance(c.get("weekly"), dict) and c["weekly"].get("floor") == "VR" for c in calls)
    assert any(isinstance(c.get("weekly"), dict) and c["weekly"].get("floor") == "AV" for c in calls)
    assert [item.code for item in items] == ["SAVR-1135", "SIVR-483", "MDVR-390"]
    assert items[0].rank == 1
