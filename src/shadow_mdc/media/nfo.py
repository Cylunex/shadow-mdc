import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, fromstring, indent, tostring

from ..db.models import ExternalIdentity, MediaAsset, Work
from ..domain import MediaTechnicalInfo


def build_nfo(
    work: Work,
    identities: list[ExternalIdentity],
    asset: MediaAsset | None = None,
    *,
    local_artwork: Mapping[str, str] | None = None,
) -> str:
    """Build a Kodi/Emby movie.nfo.

    Artwork thumb/fanart entries reference **local sibling files** only
    (``poster.jpg`` / ``fanart.jpg`` next to the NFO/STRM). Remote http(s)
    URLs are never written — Emby should use the copied local images.
    """
    movie = Element("movie")
    title = _display_title(work.title, work.primary_code)
    SubElement(movie, "title").text = title
    if work.original_title:
        SubElement(movie, "originaltitle").text = work.original_title
    if work.primary_code:
        SubElement(movie, "id").text = work.primary_code
    if work.plot:
        SubElement(movie, "plot").text = work.plot
    if work.release_date:
        SubElement(movie, "premiered").text = work.release_date.isoformat()
        SubElement(movie, "year").text = str(work.release_date.year)
    media_info = _media_info(asset)
    actual_runtime = media_info.duration_seconds or work.runtime_seconds
    if actual_runtime:
        SubElement(movie, "runtime").text = str(round(actual_runtime / 60))
    SubElement(movie, "mpaa").text = "NC-17"
    if work.studio:
        SubElement(movie, "studio").text = work.studio
    if work.category and work.category != "Other":
        SubElement(movie, "country").text = {
            "Japan": "日本",
            "China": "中国",
            "Korea": "韩国",
            "Europe": "欧美",
        }.get(work.category, work.category)
    if work.series:
        SubElement(movie, "set").text = work.series
    for actor_name in work.actors:
        actor = SubElement(movie, "actor")
        SubElement(actor, "name").text = actor_name
    for director in work.directors:
        SubElement(movie, "director").text = director
    for tag in _nfo_tags(work):
        SubElement(movie, "tag").text = tag
        SubElement(movie, "genre").text = tag
    if work.primary_code:
        unique = SubElement(movie, "uniqueid", {"type": "num", "default": "true"})
        unique.text = work.primary_code
    for identity in identities:
        if identity.provider == "global" and identity.value == work.primary_code:
            continue
        unique = SubElement(movie, "uniqueid", {"type": identity.provider})
        unique.text = identity.value
    poster_ref, fanart_ref = _nfo_local_artwork_refs(work, local_artwork)
    if poster_ref:
        SubElement(movie, "thumb", {"aspect": "poster"}).text = poster_ref
    if fanart_ref:
        fanart = SubElement(movie, "fanart")
        SubElement(fanart, "thumb").text = fanart_ref
    _append_file_info(movie, media_info)
    indent(movie, space="  ")
    body = tostring(movie, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>\n' + body + "\n"



def _nfo_local_artwork_refs(
    work: Work,
    local_artwork: Mapping[str, str] | None,
) -> tuple[str | None, str | None]:
    """Return (poster, fanart) relative filenames for NFO — never remote URLs.

    Prefer explicit ``local_artwork`` (kind → filename written beside the NFO).
    Else derive conventional ``poster.*`` / ``fanart.*`` names from cached
    ``local_path`` entries that exist on disk. Remote ``url`` values are ignored.
    """

    if local_artwork:
        poster = local_artwork.get("poster") or local_artwork.get("fanart")
        fanart = local_artwork.get("fanart") or local_artwork.get("poster")
        return _nonempty(poster), _nonempty(fanart)

    poster: str | None = None
    fanart: str | None = None
    for item in work.artwork or []:
        if not isinstance(item, dict):
            continue
        raw_path = item.get("local_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        path = Path(raw_path)
        if not path.is_file():
            continue
        kind = str(item.get("kind", "thumb")).casefold()
        # Export/organize always rename to poster.* / fanart.*; mirror that here
        # so NFO paths match siblings even when the cache file is poster.jpg.
        extension = path.suffix if path.suffix else ".jpg"
        if extension.casefold() in {".jpeg"}:
            extension = ".jpg"
        if kind in {"fanart", "background", "backdrop"}:
            fanart = fanart or f"fanart{extension.casefold()}"
        elif kind != "sample":
            poster = poster or f"poster{extension.casefold()}"
    poster = poster or fanart
    fanart = fanart or poster
    return poster, fanart


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _media_info(asset: MediaAsset | None) -> MediaTechnicalInfo:
    if asset is None:
        return MediaTechnicalInfo()
    return MediaTechnicalInfo.model_validate(asset.media_info or {})


def _append_file_info(movie: Element, info: MediaTechnicalInfo) -> None:
    if not any(
        (
            info.video_codec,
            info.audio_codec,
            info.width,
            info.height,
            info.frame_rate,
            info.overall_bitrate,
        )
    ):
        return
    file_info = SubElement(movie, "fileinfo")
    details = SubElement(file_info, "streamdetails")
    if any((info.video_codec, info.width, info.height, info.frame_rate, info.video_bitrate)):
        video = SubElement(details, "video")
        _element(video, "codec", info.video_codec)
        _element(video, "width", info.width)
        _element(video, "height", info.height)
        _element(video, "aspect", _aspect_ratio(info.width, info.height))
        _element(video, "framerate", _decimal(info.frame_rate))
        _element(video, "bitrate", info.video_bitrate)
        _element(video, "bitdepth", info.bit_depth)
        _element(video, "hdrtype", info.hdr_format)
    if any((info.audio_codec, info.audio_channels, info.audio_sample_rate, info.audio_bitrate)):
        audio = SubElement(details, "audio")
        _element(audio, "codec", info.audio_codec)
        _element(audio, "channels", info.audio_channels)
        _element(audio, "samplingrate", info.audio_sample_rate)
        _element(audio, "bitrate", info.audio_bitrate)


def _element(parent: Element, name: str, value: object | None) -> None:
    if value not in {None, ""}:
        SubElement(parent, name).text = str(value)


def _aspect_ratio(width: int | None, height: int | None) -> str | None:
    if not width or not height:
        return None
    return f"{width / height:.3f}".rstrip("0").rstrip(".")


def _decimal(value: float | None) -> str | None:
    return f"{value:.3f}".rstrip("0").rstrip(".") if value is not None else None


def _display_title(title: str, code: str | None) -> str:
    cleaned = title.strip()
    if not code:
        return cleaned
    normalized_code = "".join(character for character in code.casefold() if character.isalnum())
    normalized_title = "".join(character for character in cleaned.casefold() if character.isalnum())
    return cleaned if normalized_title.startswith(normalized_code) else f"{code} {cleaned}"


def _nfo_tags(work: Work) -> tuple[str, ...]:
    values: list[str] = []
    if work.category == "Japan":
        code = (work.primary_code or "").upper()
        text = " ".join((work.title, *work.tags)).casefold()
        if code.startswith("FC2-"):
            values.extend(("JAV", "FC2", "无码"))
        elif any(
            marker in text or code.startswith(marker.upper())
            for marker in ("无码", "無碼", "uncensored", "heyzo", "1pondo", "carib", "10musume")
        ):
            values.extend(("JAV", "无码"))
        else:
            values.extend(("JAV", "有码"))
    elif work.category == "China":
        values.append("国产")
    elif work.category == "Korea":
        values.append("韩国")
    elif work.category == "Europe":
        values.append("欧美")
    values.extend(work.tags)
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def write_nfo(path: str | Path, content: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=destination.name, suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def parse_nfo(path: str | Path) -> dict[str, object]:
    """Parse a Kodi/Emby-style movie.nfo into a flat field dict for reverse import."""

    text = Path(path).read_text(encoding="utf-8-sig")
    root = fromstring(text)
    if root.tag.lower() != "movie":
        movie = root.find("movie")
        if movie is None:
            raise ValueError("nfo root is not <movie>")
        root = movie

    def text_of(tag: str) -> str | None:
        node = root.find(tag)
        if node is None or node.text is None:
            return None
        value = node.text.strip()
        return value or None

    actors: list[str] = []
    for actor in root.findall("actor"):
        name = actor.findtext("name")
        if name and name.strip():
            actors.append(name.strip())
    tags = [node.text.strip() for node in root.findall("tag") if node.text and node.text.strip()]
    genres = [node.text.strip() for node in root.findall("genre") if node.text and node.text.strip()]
    return {
        "title": text_of("title"),
        "original_title": text_of("originaltitle"),
        "plot": text_of("plot"),
        "studio": text_of("studio"),
        "label": text_of("label"),
        "series": text_of("set") or text_of("series"),
        "premiered": text_of("premiered") or text_of("releasedate"),
        "runtime": text_of("runtime"),
        "code": text_of("num") or text_of("id"),
        "actors": actors,
        "tags": list(dict.fromkeys([*tags, *genres])),
    }
