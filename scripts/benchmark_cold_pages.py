"""Offline cold-page benchmark; uses a temporary synthetic library, never live data."""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import event, insert

from shadow_mdc.api import Runtime, app
from shadow_mdc.config import Settings
from shadow_mdc.db.models import Actor, Work, WorkActor
from shadow_mdc.db.repository import Repository
from shadow_mdc.media import directory_index
from shadow_mdc.services.actor_catalog import sync_actor_catalog_from_relations
from shadow_mdc.services.non_jav_actor_catalog import NonJavActorCatalog, NonJavActorProfile


def benchmark(work_count: int, repeats: int) -> None:
    with tempfile.TemporaryDirectory(prefix="shadow-mdc-cold-") as directory:
        root = Path(directory)
        settings = Settings(
            data_dir=root,
            database_url=f"sqlite:///{root / 'benchmark.db'}",
            redis_url="",
            auto_seed_non_jav_works=False,
            translation_enabled=False,
        )
        with patch("shadow_mdc.api.Settings", return_value=settings), TestClient(app) as client:
            runtime: Runtime = app.state.runtime
            actor_count = max(1, work_count // 20)
            with runtime.database.session() as session:
                session.execute(
                    insert(Actor),
                    [
                        {"id": f"actor-{i}", "name": f"Actor {i}", "normalized_name": f"actor {i}"}
                        for i in range(actor_count)
                    ],
                )
                session.execute(
                    insert(Work),
                    [
                        {
                            "id": f"work-{i}",
                            "title": f"Fixture movie {i}",
                            "primary_code": f"ABC-{i + 1:05d}" if i % 4 else None,
                            "family": "jav" if i % 4 else "western",
                            "category": "Japan" if i % 4 else "Europe",
                            "actors": [f"Actor {(i + n) % actor_count}" for n in range(3)],
                            "tags": ["Tag A", f"Tag {i % 20}"],
                            "studio": f"Studio {i % 50}",
                            "release_date": date(2020 + i % 6, 1 + i % 12, 1),
                            "plot": "Synthetic description. " * 500,
                            "reviews": [{"text": "Synthetic review. " * 100}],
                            "artwork": [{"kind": "poster", "url": f"https://example.com/cover/{i}.jpg"}],
                        }
                        for i in range(work_count)
                    ],
                )
                session.execute(
                    insert(WorkActor),
                    [
                        {"work_id": f"work-{i}", "actor_id": f"actor-{(i + n) % actor_count}", "position": n}
                        for i in range(work_count)
                        for n in range(3)
                    ],
                )
            with runtime.database.session() as session:
                sync_actor_catalog_from_relations(
                    runtime.actor_store,
                    Repository(session).list_actor_work_relations(lean=True),
                    runtime.alias_store.load(),
                    data_dir=root,
                )
            queries = 0
            runtime.non_jav_actor_store.save(NonJavActorCatalog(actors=tuple(
                NonJavActorProfile(name=f"Actor {i}", match_names=(f"Actor {i}",))
                for i in range(actor_count)
            )))

            def counted(*args: object) -> None:
                nonlocal queries
                queries += 1

            event.listen(runtime.database.engine, "before_cursor_execute", counted)
            print(
                f"synthetic works={work_count}; actors={actor_count}; "
                "response cache cleared before EVERY request"
            )
            for url in (
                "/api/works",
                "/api/works?compact=true",
                "/api/actors",
                "/api/non-jav-actors",
                "/api/works/work-0/related",
                "/api/categories",
            ):
                durations: list[float] = []
                query_counts: list[int] = []
                size = 0
                for _ in range(repeats):
                    runtime.response_cache.clear()
                    directory_index.clear()
                    queries = 0
                    started = time.perf_counter()
                    response = client.get(url)
                    response.raise_for_status()
                    durations.append(time.perf_counter() - started)
                    query_counts.append(queries)
                    size = len(response.content)
                print(
                    json.dumps(
                        {
                            "path": url,
                            "median_ms": round(statistics.median(durations) * 1000, 1),
                            "queries": query_counts,
                            "response_bytes": size,
                        }
                    )
                )
            event.remove(runtime.database.engine, "before_cursor_execute", counted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--works", type=int, default=5000)
    parser.add_argument("--repeats", type=int, default=3)
    options = parser.parse_args()
    if options.works < 20 or options.repeats < 1:
        parser.error("--works must be at least 20 and --repeats positive")
    benchmark(options.works, options.repeats)
