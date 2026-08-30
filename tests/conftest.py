import pytest


@pytest.fixture(autouse=True)
def disable_live_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_TRANSLATION_ENABLED", "false")
