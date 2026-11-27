from pathlib import Path

import pytest
from sqlalchemy import create_engine

from shadow_mdc.db.tag_index import initialize_tag_index


def test_existing_tags_and_transactional_changes(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'tags.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE works (id TEXT PRIMARY KEY, tags JSON)")
        conn.exec_driver_sql("""INSERT INTO works VALUES ('a', '["甲","甲","乙"]')""")
    with engine.begin() as conn:
        initialize_tag_index(conn)
    with engine.begin() as conn:
        initialize_tag_index(conn)
        query = "SELECT work_id, raw FROM mdc_work_tag_values ORDER BY work_id, raw"
        assert [tuple(row) for row in conn.exec_driver_sql(query)] == [("a", "乙"), ("a", "甲")]
        conn.exec_driver_sql("""UPDATE works SET tags='["乙","丙"]'""")
        conn.exec_driver_sql("""INSERT INTO works VALUES ('b', '["甲"]')""")
        conn.exec_driver_sql("""INSERT OR REPLACE INTO works VALUES ('a', '["丁"]')""")
        assert [tuple(row) for row in conn.exec_driver_sql(query)] == [("a", "丁"), ("b", "甲")]
    with pytest.raises(RuntimeError), engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM works")
        assert not conn.exec_driver_sql(query).all()
        raise RuntimeError("roll back")
    with engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM works WHERE id='a'")
        assert [tuple(row) for row in conn.exec_driver_sql(query)] == [("b", "甲")]
    engine.dispose()
