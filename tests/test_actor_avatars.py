from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from actor_avatars import avatar_png, initials_for, is_solid_placeholder


def test_avatar_png_is_not_a_solid_fill(tmp_path: Path) -> None:
    one = avatar_png("苏畅")
    two = avatar_png("Riley Reid")
    assert one.startswith(b"\x89PNG")
    assert two.startswith(b"\x89PNG")
    assert one != two
    assert len(one) > 2000
    assert len(two) > 2000
    path = tmp_path / "a.png"
    path.write_bytes(one)
    assert not is_solid_placeholder(path)
    assert initials_for("苏畅") == "苏畅"
    assert initials_for("Riley Reid") == "RR"
