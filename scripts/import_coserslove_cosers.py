#!/usr/bin/env python3
"""Import featured cosplay creator names from coserslove.com homepage.

Homepage SSR only (album/coser deep pages are often Cloudflare-blocked).
Upserts non-JAV actors with groups=(cosplay,) — names/aliases only, no bulk
image download unless --fetch-avatars is set for already-exposed avatar URLs.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings
from shadow_mdc.enums import MediaCategory
from shadow_mdc.services.coserslove_catalog import (
    BROWSER_UA,
    DEFAULT_BASE_URL,
    parse_home_html,
)
from shadow_mdc.services.non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    NonJavActorProfile,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()

    settings = Settings()
    if arguments.data_dir is not None:
        settings = settings.model_copy(update={"data_dir": arguments.data_dir})
    settings.ensure_directories()

    with httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        },
        proxy=settings.proxy_url,
    ) as client:
        response = client.get(arguments.base_url.rstrip("/") + "/")
        response.raise_for_status()
        html = response.text

    snapshot = parse_home_html(html, base_url=arguments.base_url)
    runs_dir = settings.data_dir / "coserslove-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_path = runs_dir / f"{date.today().isoformat()}.json"
    run_path.write_text(
        json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"coserslove home: albums={len(snapshot.albums)} "
        f"featured_cosers={len(snapshot.cosers)} names={len(snapshot.coser_names)} "
        f"log={run_path}"
    )

    if arguments.dry_run:
        for name in snapshot.coser_names:
            print(f"  would upsert: {name}")
        if arguments.json:
            print(json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2))
        return

    store = NonJavActorCatalogStore(settings.data_dir / "non-jav-actors.json")
    added = 0
    updated = 0
    for name in snapshot.coser_names:
        existing = store.get(name)
        if existing is None:
            profile = NonJavActorProfile(
                name=name,
                aliases=(),
                groups=("cosplay",),
                categories=(MediaCategory.OTHER,),
                notes="discovered from coserslove.com homepage cards",
            )
            store.upsert(profile)
            added += 1
            continue
        groups = tuple(dict.fromkeys([*existing.groups, "cosplay"]))
        if groups == existing.groups:
            continue
        store.upsert(existing.model_copy(update={"groups": groups}))
        updated += 1
    print(f"coserslove import: added={added} groups_updated={updated}")
    if arguments.json:
        print(
            json.dumps(
                {
                    "added": added,
                    "updated": updated,
                    "names": list(snapshot.coser_names),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    main()
