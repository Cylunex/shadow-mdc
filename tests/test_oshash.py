from pathlib import Path

from shadow_mdc.media.oshash import compute_oshash


def test_small_file_has_no_oshash(tmp_path: Path) -> None:
    path = tmp_path / "small.bin"
    path.write_bytes(b"x" * 1024)

    assert compute_oshash(path) is None


def test_oshash_is_stable_and_sensitive_to_tail(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    payload = bytearray(b"x" * (128 * 1024))
    first.write_bytes(payload)
    payload[-8:] = b"changed!"
    second.write_bytes(payload)

    assert compute_oshash(first) == compute_oshash(first)
    assert compute_oshash(first) != compute_oshash(second)
