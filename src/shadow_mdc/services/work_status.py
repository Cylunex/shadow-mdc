"""Status triad helpers for work cards: 想看 / 已入库 / 有本地或 Emby."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkStatusFlags:
    want_list: bool
    in_catalog: bool
    has_local_or_emby: bool


def compute_work_status(
    *,
    want_list: bool,
    in_catalog: bool,
    has_local_media: bool,
    emby_linked: bool = False,
) -> WorkStatusFlags:
    return WorkStatusFlags(
        want_list=want_list,
        in_catalog=in_catalog,
        has_local_or_emby=has_local_media or emby_linked,
    )


def status_badges(flags: WorkStatusFlags) -> tuple[str, ...]:
    badges: list[str] = []
    if flags.want_list:
        badges.append("想看")
    if flags.in_catalog:
        badges.append("已入库")
    if flags.has_local_or_emby:
        badges.append("有本地或Emby")
    return tuple(badges)
