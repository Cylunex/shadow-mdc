"""Index the original tag spellings; resolve the editable dictionary at query time."""

from sqlalchemy import String, column, table
from sqlalchemy.engine import Connection

work_tags = table("mdc_work_tag_values", column("work_id", String), column("raw", String))


def initialize_tag_index(connection: Connection) -> None:
    # SQLite's legacy transaction mode does not begin a transaction for DDL.
    # Serialize the first backfill with changes to works and publish it atomically.
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mdc_work_tag_values'"
    ).first():
        return
    connection.exec_driver_sql(
        "CREATE TABLE mdc_work_tag_values (work_id TEXT NOT NULL, raw TEXT NOT NULL, "
        "PRIMARY KEY(work_id, raw)) WITHOUT ROWID"
    )
    connection.exec_driver_sql(
        "INSERT INTO mdc_work_tag_values SELECT DISTINCT w.id, j.value FROM works w, json_each(w.tags) j "
        "WHERE j.type='text'"
    )
    connection.exec_driver_sql("CREATE INDEX mdc_work_tag_raw ON mdc_work_tag_values(raw, work_id)")
    add = ("INSERT INTO mdc_work_tag_values SELECT DISTINCT new.id, value FROM json_each(new.tags) "
           "WHERE type='text';")
    remove = "DELETE FROM mdc_work_tag_values WHERE work_id=old.id;"
    for suffix, event, body in (
        ("insert", "INSERT", "DELETE FROM mdc_work_tag_values WHERE work_id=new.id;" + add),
        ("delete", "DELETE", remove),
        ("update", "UPDATE OF tags, id", remove + add),
    ):
        connection.exec_driver_sql(
            f"CREATE TRIGGER mdc_work_tag_{suffix} AFTER {event} ON works BEGIN {body} END"
        )
