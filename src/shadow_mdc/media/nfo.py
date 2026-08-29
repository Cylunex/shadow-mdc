import os
import tempfile
from pathlib import Path
from xml.etree.ElementTree import Element, SubElement, tostring

from ..db.models import ExternalIdentity, Work


def build_nfo(work: Work, identities: list[ExternalIdentity]) -> str:
    movie = Element("movie")
    SubElement(movie, "title").text = work.title
    if work.original_title:
        SubElement(movie, "originaltitle").text = work.original_title
    if work.primary_code:
        SubElement(movie, "id").text = work.primary_code
    if work.plot:
        SubElement(movie, "plot").text = work.plot
    if work.release_date:
        SubElement(movie, "premiered").text = work.release_date.isoformat()
        SubElement(movie, "year").text = str(work.release_date.year)
    if work.runtime_seconds:
        SubElement(movie, "runtime").text = str(round(work.runtime_seconds / 60))
    if work.studio:
        SubElement(movie, "studio").text = work.studio
    if work.category and work.category != "Other":
        SubElement(movie, "country").text = work.category
    if work.series:
        SubElement(movie, "set").text = work.series
    for actor_name in work.actors:
        actor = SubElement(movie, "actor")
        SubElement(actor, "name").text = actor_name
    for director in work.directors:
        SubElement(movie, "director").text = director
    for tag in work.tags:
        SubElement(movie, "tag").text = tag
        SubElement(movie, "genre").text = tag
    for identity in identities:
        unique = SubElement(movie, "uniqueid", {"type": identity.provider})
        unique.text = identity.value
    for item in work.artwork:
        url = item.get("url")
        if item.get("kind") == "thumb" and isinstance(url, str):
            SubElement(movie, "thumb").text = url
    body = tostring(movie, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + body + "\n"


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
