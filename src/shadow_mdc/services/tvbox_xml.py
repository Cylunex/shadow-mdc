"""Normalize the legacy MacCMS XML dialect to the TVBox catalog model."""

import re
from xml.etree import ElementTree

from .tvbox_http import TvBoxError

_VIDEO_FIELDS = {
    "id": "vod_id",
    "name": "vod_name",
    "tid": "type_id",
    "type": "type_name",
    "pic": "vod_pic",
    "des": "vod_content",
    "year": "vod_year",
    "note": "vod_remarks",
}


def normalize_xml(body: bytes) -> dict[str, object]:
    text: str
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("gb18030", errors="replace")
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.IGNORECASE) or "\x00" in text:
        raise TvBoxError("XML 目录包含不支持的声明")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise TvBoxError("上游未返回有效的 CMS XML") from exc
    listing = root.find("list")
    categories = root.findall("class/ty")
    if root.tag not in ("rss", "xml") or (listing is None and not categories):
        raise TvBoxError("上游 XML 缺少 CMS 目录")
    records: list[dict[str, str]] = []
    if listing is not None:
        for video in listing.findall("video")[:120]:
            row = {field: video.findtext(key, default="") for key, field in _VIDEO_FIELDS.items()}
            tracks = video.findall("dl/dd")
            row["vod_play_from"] = "$$$".join(track.get("flag", "") for track in tracks)
            row["vod_play_url"] = "$$$".join(track.text or "" for track in tracks)
            records.append(row)
    return {
        "class": [{"type_id": cat.get("id", ""), "type_name": cat.text or ""} for cat in categories[:200]],
        "list": records,
        "pagecount": listing.get("pagecount", "1") if listing is not None else "1",
    }
