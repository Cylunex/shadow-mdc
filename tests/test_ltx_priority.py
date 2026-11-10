"""One selection rule is shared by intake, seeded batches and offline recovery."""

from shadow_mdc.media.magnets import MagnetLink, rank_magnets


def test_ltx_full_release_beats_ordinary_hd_but_not_identity_conflicts_or_samples() -> None:
    names = ["SSIS-123 HD 2160p Chinese", "SSIS-123-LTX", "SSIS-999-LTX", "SSIS-123 LTX sample"]
    magnets = [
        MagnetLink(
            provider="fixture",
            info_hash=f"{index:040x}",
            uri=f"magnet:?xt=urn:btih:{index:040x}",
            name=name,
            size_bytes=2 * 1024**3,
            hd=True,
        )
        for index, name in enumerate(names)
    ]
    ranked = rank_magnets(magnets, expected_code="SSIS-123")
    assert ranked[0].name == "SSIS-123-LTX"
    assert ranked[-1].name == "SSIS-999-LTX"
    assert ranked[1].name == "SSIS-123 HD 2160p Chinese"
