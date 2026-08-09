from datasette_auth_tokens.migrations import migration
from datasette_auth_tokens.utils import prune_token_usage
import sqlite_utils

OLD_CREATE_TABLES_SQL = """
CREATE TABLE _datasette_auth_tokens (
    id INTEGER PRIMARY KEY,
    token_status TEXT DEFAULT 'L', -- [L]ive, [R]evoked, [E]xpired
    description TEXT,
    actor_id TEXT,
    permissions TEXT,
    created_timestamp INTEGER,
    last_used_timestamp INTEGER,
    expires_after_seconds INTEGER,
    secret_version INTEGER DEFAULT 0
);
"""


def test_migrate_from_original():
    db = sqlite_utils.Database(memory=True)
    db.execute(OLD_CREATE_TABLES_SQL)
    assert db["_datasette_auth_tokens"].columns_dict == {
        "id": int,
        "token_status": str,
        "description": str,
        "actor_id": str,
        "permissions": str,
        "created_timestamp": int,
        "last_used_timestamp": int,
        "expires_after_seconds": int,
        "secret_version": int,
    }

    # Default token_status should be L
    def get_col():
        return [
            col
            for col in db["_datasette_auth_tokens"].columns
            if col.name == "token_status"
        ][0]

    assert get_col().default_value == "'L'"
    migration.apply(db)
    assert db["_datasette_auth_tokens"].columns_dict["ended_timestamp"] is int
    # Should have updated token default
    assert get_col().default_value == "'A'"
    # Confirm column order is correct
    column_order = [col.name for col in db["_datasette_auth_tokens"].columns]
    assert column_order == [
        "id",
        "token_status",
        "description",
        "actor_id",
        "permissions",
        "created_timestamp",
        "last_used_timestamp",
        "expires_after_seconds",
        "ended_timestamp",
        "secret_version",
    ]


def test_usage_table_created():
    db = sqlite_utils.Database(memory=True)
    migration.apply(db)
    assert "auth_tokens_usage" in db.table_names()
    assert db["auth_tokens_usage"].columns_dict == {
        "id": int,
        "token_id": int,
        "when_iso": str,
        "created_ms": int,
        "action": str,
        "parent": str,
        "child": str,
        "result": int,
    }
    index_names = {index.name for index in db["auth_tokens_usage"].indexes}
    assert "idx_auth_tokens_usage_dedup" in index_names
    assert "idx_auth_tokens_usage_token_time" in index_names


def _insert_usage(db, token_id, count, base_ms, step=1):
    db["auth_tokens_usage"].insert_all(
        [
            {
                "token_id": token_id,
                "when_iso": str(base_ms + i * step),
                "created_ms": base_ms + i * step,
                "action": "view-table",
                "parent": "demo",
                "child": "foo",
                "result": 1,
            }
            for i in range(count)
        ]
    )


def test_prune_caps_at_1000_recent():
    db = sqlite_utils.Database(memory=True)
    migration.apply(db)
    now_ms = 10**12
    # 1500 rows all within the 5 minute window
    _insert_usage(db, token_id=1, count=1500, base_ms=now_ms)
    prune_token_usage(db.conn, 1, now_ms + 2000)
    rows = list(db["auth_tokens_usage"].rows)
    assert len(rows) == 1000
    # The newest 1000 (largest ids) should be kept
    assert min(r["id"] for r in rows) == 501


def test_prune_keeps_newest_200_when_old():
    db = sqlite_utils.Database(memory=True)
    migration.apply(db)
    now_ms = 10**12
    old = now_ms - 10**9  # well over 5 minutes old
    _insert_usage(db, token_id=1, count=350, base_ms=old)
    prune_token_usage(db.conn, 1, now_ms)
    rows = list(db["auth_tokens_usage"].rows)
    assert len(rows) == 200
    assert min(r["id"] for r in rows) == 151


def test_prune_only_touches_given_token():
    db = sqlite_utils.Database(memory=True)
    migration.apply(db)
    now_ms = 10**12
    old = now_ms - 10**9
    _insert_usage(db, token_id=1, count=350, base_ms=old)
    _insert_usage(db, token_id=2, count=10, base_ms=old)
    prune_token_usage(db.conn, 1, now_ms)
    assert db["auth_tokens_usage"].count_where("token_id = 1") == 200
    assert db["auth_tokens_usage"].count_where("token_id = 2") == 10
