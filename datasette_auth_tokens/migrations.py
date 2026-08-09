from sqlite_migrate import Migrations
import time

migration = Migrations("datasette_auth_tokens")


# Use this decorator against functions that implement migrations
@migration()
def m001_create_table(db):
    # If the table exists already, this will be a no-op
    db.execute("""
    CREATE TABLE IF NOT EXISTS _datasette_auth_tokens (
        id INTEGER PRIMARY KEY,
        token_status TEXT DEFAULT 'A', -- [A]ctive, [R]evoked, [E]xpired
        description TEXT,
        actor_id TEXT,
        permissions TEXT,
        created_timestamp INTEGER,
        last_used_timestamp INTEGER,
        expires_after_seconds INTEGER,
        secret_version INTEGER DEFAULT 0
    );
    """)


@migration()
def m002_rename_live_to_active(db):
    # In case anything is left over - I made this change before
    # I introduced migrations
    db["_datasette_auth_tokens"].transform(defaults={"token_status": "A"})
    db.execute("""
        update _datasette_auth_tokens
        set token_status = 'A'
        where token_status = 'L'
        """)


@migration()
def m003_add_ended_timestamp(db):
    db["_datasette_auth_tokens"].add_column("ended_timestamp", int)
    # Switch order around
    db["_datasette_auth_tokens"].transform(
        column_order=[
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
    )
    # Set it to now for any revoked tokens
    db.execute(
        "update _datasette_auth_tokens set ended_timestamp = :now where token_status = 'R'",
        {"now": int(time.time())},
    )
    # Set it to created_timestamp + expires_after_seconds for any expired tokens
    db.execute("""
        update _datasette_auth_tokens
        set ended_timestamp = created_timestamp + expires_after_seconds
        where token_status = 'E'
        """)


@migration()
def m004_create_usage_table(db):
    # Records which permission checks each token was used for, so we can offer
    # a "lock this token down to what it actually used" feature.
    db.execute("""
    CREATE TABLE IF NOT EXISTS auth_tokens_usage (
        id INTEGER PRIMARY KEY,
        token_id INTEGER,        -- references _datasette_auth_tokens.id
        when_iso TEXT,           -- PermissionCheck.when (ISO, microsecond)
        created_ms INTEGER,      -- derived epoch ms, for range queries / pruning
        action TEXT,
        parent TEXT,             -- database, nullable
        child TEXT,              -- table/resource, nullable
        result INTEGER           -- 1 / 0
    );
    """)
    db.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_auth_tokens_usage_dedup
        ON auth_tokens_usage (token_id, when_iso, action, parent, child);
    """)
    db.execute("""
    CREATE INDEX IF NOT EXISTS idx_auth_tokens_usage_token_time
        ON auth_tokens_usage (token_id, created_ms);
    """)
