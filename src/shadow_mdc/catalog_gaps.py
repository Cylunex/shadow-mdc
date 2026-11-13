"""Read-only checks against metadata recorded on catalog works."""

from enum import StrEnum

from sqlalchemy import ColumnElement, Integer, case, cast, exists, func, or_, select

from .catalog_duration import work_duration
from .db.models import Work, WorkTaxonomy


class MetadataGap(StrEnum):
    ANY = "any"
    PLOT = "plot"
    COVER = "cover"
    TAGS = "tags"
    YEAR = "year"
    DURATION = "duration"


GAP_NAMES = {
    MetadataGap.ANY: "任一资料缺项",
    MetadataGap.PLOT: "缺简介",
    MetadataGap.COVER: "缺封面记录",
    MetadataGap.TAGS: "缺标签",
    MetadataGap.YEAR: "缺发行年份",
    MetadataGap.DURATION: "缺时长",
}


def catalog_year() -> ColumnElement[int]:
    return func.coalesce(
        select(WorkTaxonomy.release_year).where(WorkTaxonomy.work_id == Work.id).scalar_subquery(),
        cast(func.strftime("%Y", Work.release_date), Integer),
    )


def gap_conditions() -> dict[MetadataGap, ColumnElement[bool]]:
    artwork = func.json_each(Work.artwork).table_valued("value", "type")
    # Legacy arrays can contain scalars. Only feed JSON objects to json_extract.
    item = case((artwork.c.type == "object", artwork.c.value), else_="{}")
    whitespace = " \t\n\r\u3000"
    has_cover = exists(
        select(1)
        .select_from(artwork)
        .where(
            func.lower(func.coalesce(func.json_extract(item, "$.kind"), "")) != "sample",
            or_(
                *(
                    func.trim(func.json_extract(item, path), whitespace) != ""
                    for path in ("$.url", "$.local_path")
                )
            ),
        )
    )
    tags = func.json_each(Work.tags).table_valued("value", "type")
    has_tag = exists(
        select(1)
        .select_from(tags)
        .where(
            tags.c.type == "text",
            func.trim(tags.c.value, whitespace) != "",
        )
    )
    conditions = {
        MetadataGap.PLOT: func.trim(func.coalesce(Work.plot, ""), whitespace) == "",
        MetadataGap.COVER: ~has_cover,
        MetadataGap.TAGS: ~has_tag,
        MetadataGap.YEAR: catalog_year().is_(None),
        MetadataGap.DURATION: work_duration().is_(None),
    }
    return {MetadataGap.ANY: or_(*conditions.values()), **conditions}
