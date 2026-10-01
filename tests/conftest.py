import pytest


@pytest.fixture(autouse=True)
def disable_live_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_TRANSLATION_ENABLED", "false")


@pytest.fixture(autouse=True)
def disable_auto_non_jav_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep API unit tests isolated from the large curated non-JAV seed catalog."""

    monkeypatch.setenv("SHADOW_MDC_AUTO_SEED_NON_JAV_WORKS", "false")


@pytest.fixture(autouse=True)
def reset_pan_backoff_gates() -> None:
    """The 115 / OpenList Retry-After gates are process-wide; isolate tests."""

    from shadow_mdc.services.pan_common import reset_shared_gates

    reset_shared_gates()
