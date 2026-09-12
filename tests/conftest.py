import pytest


@pytest.fixture(autouse=True)
def disable_live_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_TRANSLATION_ENABLED", "false")


@pytest.fixture(autouse=True)
def disable_auto_non_jav_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API unit tests isolated from the large curated non-JAV seed catalog."""

    monkeypatch.setenv("SHADOW_MDC_AUTO_SEED_NON_JAV_WORKS", "false")
