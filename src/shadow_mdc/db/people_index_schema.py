"""Additive SQLite schema for the people search projection."""

from sqlalchemy import Connection


def init_people_index(connection: Connection) -> None:
    for ddl in (
        "CREATE TABLE IF NOT EXISTS mdc_people_dirty (kind TEXT, id TEXT, PRIMARY KEY(kind,id)) WITHOUT ROWID",
        "CREATE TABLE IF NOT EXISTS mdc_people_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS mdc_people_work_names (work_id TEXT, name TEXT, PRIMARY KEY(work_id,name)) WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS mdc_people_name_lookup ON mdc_people_work_names(name,work_id)",
        "CREATE TABLE IF NOT EXISTS mdc_people_cards (id TEXT PRIMARY KEY, name TEXT, sort_name TEXT,"
        " aliases TEXT, search TEXT, works INTEGER, revision INTEGER, portrait_revision TEXT, portrait_url TEXT)",
        "CREATE INDEX IF NOT EXISTS mdc_people_card_sort ON mdc_people_cards(works DESC, sort_name,id)",
        "CREATE INDEX IF NOT EXISTS mdc_people_card_name ON mdc_people_cards(sort_name,id)",
        "CREATE INDEX IF NOT EXISTS mdc_people_card_same_name ON mdc_people_cards(name)",
        "CREATE TABLE IF NOT EXISTS mdc_people_card_works (person_id TEXT, work_id TEXT,"
        " PRIMARY KEY(person_id,work_id)) WITHOUT ROWID",
        "CREATE INDEX IF NOT EXISTS mdc_people_card_work ON mdc_people_card_works(work_id,person_id)",
    ):
        connection.exec_driver_sql(ddl)
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(mdc_people_cards)")}
    if "portrait_url" not in columns:
        connection.exec_driver_sql("ALTER TABLE mdc_people_cards ADD COLUMN portrait_url TEXT")
        connection.exec_driver_sql(
            "INSERT OR IGNORE INTO mdc_people_state VALUES ('portrait_backfill', '1')"
        )
    for table, kind, column in (
        ("actors", "actor", "id"), ("work_actors", "actor", "actor_id"),
        ("works", "work", "id"), ("catalog_people", "card", "id"),
        ("catalog_person_references", "card", "person_id"), ("catalog_person_credits", "card", "person_id"),
    ):
        for operation, references in (
            ("INSERT", ("NEW",)), ("UPDATE", ("OLD", "NEW")), ("DELETE", ("OLD",)),
        ):
            expressions = [f"{ref}.{column}" for ref in references]
            if table == "catalog_people":
                expressions.extend(f"{ref}.merged_into" for ref in references)
            body = " ".join(
                f"INSERT INTO mdc_people_dirty SELECT '{kind}', {value} WHERE {value} IS NOT NULL "
                "ON CONFLICT(kind,id) DO NOTHING;"
                for value in expressions
            )
            clause = " OF actors" if table == "works" and operation == "UPDATE" else ""
            guard = " WHEN OLD.actors IS NOT NEW.actors" if clause else ""
            connection.exec_driver_sql(
                f"CREATE TRIGGER IF NOT EXISTS mdc_people_change_{table}_{operation.lower()} "
                f"AFTER {operation}{clause} ON {table}{guard} BEGIN {body} END"
            )
    connection.exec_driver_sql("""
        CREATE TRIGGER IF NOT EXISTS mdc_people_card_deleted_work AFTER DELETE ON works BEGIN
            INSERT INTO mdc_people_dirty
                SELECT 'card', person_id FROM mdc_people_card_works WHERE work_id=OLD.id
                ON CONFLICT(kind,id) DO NOTHING;
        END
    """)
