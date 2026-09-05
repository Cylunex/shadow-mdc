from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from actor_avatars import initials_for, is_solid_placeholder, notes_indicate_placeholder


def test_initials_and_placeholder_notes() -> None:
    assert initials_for("苏畅") == "苏畅"
    assert initials_for("Riley Reid") == "RR"


def test_default_seed_path_does_not_call_placeholder_generator() -> None:
    seed = (Path(__file__).resolve().parents[1] / "scripts" / "seed_non_jav_placeholders.py").read_text(
        encoding="utf-8"
    )
    assert "avatar_png" not in seed
    assert "fetch_real_portrait" in seed
    assert "Designed identicon" not in seed


def test_work_seed_does_not_generate_placeholder_avatars() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "src" / "shadow_mdc" / "services" / "non_jav_work_seed.py"
    ).read_text(encoding="utf-8")
    assert "avatar_png" not in source
    assert "never generate placeholder" in source.casefold() or "Reuse an existing real portrait" in source


def test_is_solid_placeholder_for_tiny_file(tmp_path: Path) -> None:
    path = tmp_path / "tiny.png"
    path.write_bytes(b"x" * 10)
    assert is_solid_placeholder(path)
    assert notes_indicate_placeholder("Designed identicon avatar; replace via actor library upload.")


def test_collapse_name_pinyin_spacing() -> None:
    from actor_avatars import collapse_name

    assert collapse_name("Xia Qing Zi") == collapse_name("Xia Qingzi")
    assert collapse_name("Su Chang") == "suchang"
