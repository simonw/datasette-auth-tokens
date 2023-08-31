from datasette_auth_tokens.migrations import migration
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
    assert db["_datasette_auth_tokens"].columns_dict["ended_timestamp"] == int
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
