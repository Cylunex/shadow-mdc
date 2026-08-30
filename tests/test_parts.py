from pathlib import Path

from shadow_mdc.media.parts import detect_media_part, find_subtitles, subtitle_destination


def test_detects_explicit_and_numbered_jav_parts_without_splitting_no_code_series(
    tmp_path: Path,
) -> None:
    cd_part = detect_media_part(tmp_path / "SONE-118-CD2.mp4", "SONE-118")
    version_part = detect_media_part(tmp_path / "SONE-118-v3.mp4", "SONE-118")
    letter_part = detect_media_part(tmp_path / "SONE-118-B.mp4", "SONE-118")
    assert cd_part is not None and cd_part.index == 2
    assert version_part is not None and version_part.index == 3
    assert letter_part is not None and letter_part.index == 2
    assert detect_media_part(tmp_path / "DragonLLLLL_1.mp4", None) is None


def test_subtitles_match_their_media_part_and_keep_language_suffix(tmp_path: Path) -> None:
    media_one = tmp_path / "SONE-118-CD1.mp4"
    media_two = tmp_path / "SONE-118-CD2.mp4"
    media_one.write_bytes(b"one")
    media_two.write_bytes(b"two")
    first_subtitle = tmp_path / "SONE-118-CD1.zh.srt"
    second_subtitle = tmp_path / "SONE-118-CD2.ass"
    first_subtitle.write_text("one", encoding="utf-8")
    second_subtitle.write_text("two", encoding="utf-8")

    assert find_subtitles(media_one, "SONE-118") == (first_subtitle,)
    assert find_subtitles(media_two, "SONE-118") == (second_subtitle,)
    destination = tmp_path / "out" / "SONE-118-CD1.mp4"
    assert subtitle_destination(first_subtitle, media_one, destination).name == "SONE-118-CD1.zh.srt"
