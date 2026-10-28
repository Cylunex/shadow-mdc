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


def test_neighbor_codes_and_titles_are_not_subtitle_owners(tmp_path: Path) -> None:
    for name in ("ABC-1230.zh.srt", "XABC-123.zh.srt", "ABC-123-CD2.zh.srt", "Filmography.en.srt"):
        (tmp_path / name).write_text("fixture")
    assert find_subtitles(tmp_path / "ABC-123.mp4", "ABC-123") == ()
    assert find_subtitles(tmp_path / "Film.mp4", None) == ()


def test_code_fallback_preserves_language_and_accessibility_per_part(tmp_path: Path) -> None:
    first = tmp_path / "ABC-123.1080p-CD1.strm"
    second = tmp_path / "ABC-123.1080p-CD2.strm"
    first.write_text("https://example.com/one")
    second.write_text("https://example.com/two")
    subtitles = [
        tmp_path / name
        for name in (
            "ABC_123-CD1.zh-Hans.forced.srt",
            "ABC-123-part2.en.sdh.ass",
            "ABC-123.zh.srt",
        )
    ]
    for subtitle in subtitles:
        subtitle.write_text("fixture")
    assert find_subtitles(first, "ABC-123") == (subtitles[0],)
    assert find_subtitles(second, "ABC-123") == (subtitles[1],)
    assert (
        subtitle_destination(subtitles[0], first, tmp_path / "New-CD1.strm").name
        == "New-CD1.zh-Hans.forced.srt"
    )
    assert subtitle_destination(subtitles[1], second, tmp_path / "New-CD2.strm").name == "New-CD2.en.sdh.ass"


def test_ambiguous_versions_keep_generic_subtitle_in_place(tmp_path: Path) -> None:
    media = [tmp_path / name for name in ("ABC-123.1080p.mp4", "ABC-123.2160p.mp4")]
    for item in media:
        item.touch()
    generic = tmp_path / "ABC-123.zh.srt"
    exact = tmp_path / "ABC-123.2160p.zh.srt"
    generic.touch()
    exact.touch()
    assert find_subtitles(media[0], "ABC-123") == ()
    assert find_subtitles(media[1], "ABC-123") == (exact,)


def test_exact_version_owns_subtitle_and_new_peer_invalidates_fallback(tmp_path: Path) -> None:
    video = tmp_path / "ABC-123.1080p.mp4"
    video.touch()
    subtitle = tmp_path / "ABC-123.zh.srt"
    subtitle.touch()
    assert find_subtitles(video, "ABC-123") == (subtitle,)
    peer = tmp_path / "ABC-123.mp4"
    peer.touch()
    assert find_subtitles(video, "ABC-123") == ()
    assert find_subtitles(peer, "ABC-123") == (subtitle,)


def test_duplicate_media_stems_and_subtitle_symlinks_are_ambiguous(tmp_path: Path) -> None:
    video = tmp_path / "Film.mp4"
    video.touch()
    (tmp_path / "Film.strm").touch()
    subtitle = tmp_path / "Film.zh.srt"
    subtitle.touch()
    assert find_subtitles(video, None) == ()
    (tmp_path / "Film.strm").unlink()
    subtitle.unlink()
    external = tmp_path / "external.txt"
    external.write_text("untouched")
    subtitle.symlink_to(external)
    assert find_subtitles(video, None) == ()
    assert external.read_text() == "untouched"


def test_unicode_names_and_unrecognized_suffix_are_conservative(tmp_path: Path) -> None:
    subtitle = tmp_path / "Café.zh.srt"
    subtitle.touch()
    (tmp_path / "Café.directors-cut.srt").touch()
    assert find_subtitles(tmp_path / "Cafe\u0301.mp4", None) == (subtitle,)


def test_language_like_media_suffix_is_part_of_exact_identity(tmp_path: Path) -> None:
    media = tmp_path / "Film.en.mp4"
    media.touch()
    plain = tmp_path / "Film.en.srt"
    tagged = tmp_path / "Film.en.zh.srt"
    plain.touch()
    tagged.touch()
    shorter = tmp_path / "Film.mp4"
    shorter.touch()
    assert find_subtitles(shorter, None) == ()
    assert find_subtitles(media, None) == (plain, tagged)
    assert subtitle_destination(plain, media, tmp_path / "New.mp4").name == "New.srt"
    assert subtitle_destination(tagged, media, tmp_path / "New.mp4").name == "New.zh.srt"


def test_code_fallback_does_not_claim_unlabelled_sibling_version(tmp_path: Path) -> None:
    media = tmp_path / "ABC-123.1080p.mp4"
    media.touch()
    (tmp_path / "video.mp4").touch()
    (tmp_path / "ABC-123.zh.srt").touch()
    assert find_subtitles(media, "ABC-123") == ()
    assert find_subtitles(tmp_path / "video.mp4", "ABC-123") == ()
