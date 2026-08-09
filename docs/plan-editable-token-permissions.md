# Plan: Edit token permissions + "lock down to recently-used actions"

## Context

`datasette-auth-tokens` managed (database-backed) tokens currently support
**create**, **view**, and **revoke** — but a token's permissions are fixed at
creation time. The token details page (`/-/api/tokens/{id}`) shows restrictions
read-only.

A token's *effective* permissions are not baked into the token string. The token
is just a signed row id (`dsatok_<signed id>`); on every request
`_actor_from_managed()` loads the row's `permissions` JSON column into
`actor["_r"]` (`datasette_auth_tokens/__init__.py:267-269`). **This means
editing the `permissions` column changes the token's powers immediately, with no
re-issuing.** That property makes this feature natural to build, and enables the
second goal below.

This plan delivers two things:

1. **Edit the permissions of an existing token** via a new edit page.
2. **A "lock it down" workflow**: issue a wide token, exercise it via the API,
   then the edit page surfaces *"in the last 10 minutes this token used these
   exact actions — restrict it to only those?"*

Decisions confirmed with the user:
- **Usage capture** = at the end of every request, an `asgi_wrapper` scans
  Datasette's in-memory `datasette._permission_checks` deque for checks whose
  actor carries a `token_id` and **persists them to a new plugin-managed table**
  (`auth_tokens_usage`). On by default, disableable via a config
  setting. This makes usage durable (survives restarts, shared across workers via
  the shared SQLite table) — the deque is only the per-request source. Retention:
  per token, keep the **larger** of {last 5 minutes of data, 200 most recent
  records}, with a **hard cap of 1000 rows per token**.
- **Edit authorization** = a new dedicated action (named `auth-tokens-edit-all`
  to match the existing `-all` convention — owner can always edit their own
  token; this action lets an admin edit *others'* tokens, mirroring
  `auth-tokens-revoke-all`).

## How Datasette records permission checks (key fact)

In Datasette 1.0a20+ the old `permission_allowed` hook is gone. Every
`await datasette.allowed(action=, resource=, actor=)` call appends a
`PermissionCheck(when, actor, action, parent, child, result)` to
`datasette._permission_checks` (a `collections.deque(maxlen=200)`):
- `when` — ISO timestamp
- `actor` — the actor dict at check time (for our tokens this includes
  `token_id`, set in `__init__.py:262-266`)
- `action` — e.g. `view-table`
- `parent` / `child` — database / table (either may be `None`)
- `result` — bool

So checks made on requests authenticated by a managed token are attributable via
`check.actor.get("token_id")`, and `parent`/`child` give db/table granularity.

## Implementation workflow
- **Red/green TDD**: for each unit of behavior, write a failing test first, run
  `uv run pytest` to see it fail (red), implement the minimum to pass (green),
  then refactor. Work in this order: migration/table → asgi_wrapper capture
  (insert → dedup → prune) → read helpers → edit route → templates.
- **Frequent small commits** on `claude/database-token-permissions-fkky63`, each
  with a multi-line commit message (subject line + body explaining the change).
  Push at the end.

## Changes

### 1. New action `auth-tokens-edit-all`
`datasette_auth_tokens/__init__.py` — add to `register_actions()` (alongside the
existing three, `__init__.py:159-176`):
```python
Action(name="auth-tokens-edit-all", abbr=None,
       description="Edit permissions of any API tokens"),
```

### 2. Authorization helper
`datasette_auth_tokens/views.py` — add `actor_can_edit(datasette, actor,
token_actor_id)` modeled exactly on `actor_can_revoke` (`views.py:365-372`):
owner (`actor.id == token.actor_id`) OR `auth-tokens-edit-all`.

### 3. Refactor restriction parsing (reuse, don't duplicate)
The checkbox-name → `TokenRestrictions` parsing currently lives inline in
`create_api_token` (`views.py:50-66`). Extract it to a reusable helper, e.g.
`parse_restrictions_from_post(datasette, post) -> TokenRestrictions`, and call it
from both create and edit. Keeps the `all:` / `database:<db>:` /
`resource:<db>:<table>:` name format and `tilde_decode` handling identical.

### 4. Reverse mapping: stored permissions → pre-checked boxes
Add a helper to turn the stored **abbreviated** `permissions` dict back into the
set of checkbox `name` strings, so the edit form pre-checks current settings.
Reuse the abbr→name map that `format_permissions` already builds
(`utils.py:50-53`) — factor it into a small `abbr_to_name(datasette)` helper in
`utils.py` and use it in both places.
```
{"a":[abbr]}            -> all:<name>
{"d":{db:[abbr]}}       -> database:<tilde_encode(db)>:<name>
{"r":{db:{tbl:[abbr]}}} -> resource:<tilde_encode(db)>:<tilde_encode(tbl)>:<name>
```

### 5. Persist usage: new table + asgi_wrapper (feature 2 capture layer)

**Migration** — add `m004_create_usage_table` to `datasette_auth_tokens/migrations.py`
(applied by the existing `startup` hook, `__init__.py:128-143`):
```sql
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
-- dedup backstop:
CREATE UNIQUE INDEX IF NOT EXISTS idx_usage_dedup
    ON auth_tokens_usage (token_id, when_iso, action, parent, child);
CREATE INDEX IF NOT EXISTS idx_usage_token_time
    ON auth_tokens_usage (token_id, created_ms);
```

**`asgi_wrapper(datasette)` hook** in `__init__.py` (gated on `Config.enabled`
and a new `log_token_usage` setting defaulting to `True`):
- Wrap the app; for `scope["type"] == "http"`, after `await app(...)` returns,
  drain new token checks. Source = `datasette._permission_checks`.
- **Dedup**: keep a per-process high-water `when_iso`; process only deque entries
  with `when > high_water` and `actor and actor.get("token_id")`; advance the
  high-water. `INSERT OR IGNORE` (unique index) is the backstop for restarts /
  concurrency.
- Insert each as a row (parse `when` → `created_ms`). Skip the write entirely
  when there are no new token checks.
- **Prune** only the token_ids just touched (cheap). Per token_id, keep the union
  of {`created_ms >= now-5min`} and {newest 200 by `id`}, then cap that union at
  the newest 1000, deleting the rest:
  ```sql
  DELETE FROM auth_tokens_usage
  WHERE token_id = :tid AND id NOT IN (
    SELECT id FROM auth_tokens_usage
    WHERE token_id = :tid AND (
        created_ms >= :five_min_ago
        OR id IN (SELECT id FROM auth_tokens_usage
                  WHERE token_id = :tid ORDER BY id DESC LIMIT 200)
    )
    ORDER BY id DESC LIMIT 1000
  );
  ```
- Writes go through `Config.db` via `execute_write` / `execute_write_fn`.

### 6. Usage aggregation helper (feature 2 read layer)
Add `recent_token_usage(datasette, token_id, within_seconds=300)` in `utils.py`,
querying `auth_tokens_usage` (not the deque):
- `SELECT DISTINCT action, parent, child` for `token_id`, `result = 1`,
  `created_ms >= now - within_seconds` (only actions the token *successfully*
  used — denied attempts are excluded from the suggestion).
- Map each to a checkbox name (same vocabulary as the form):
  - `child` set → `resource:<db>:<table>:<action>`
  - `parent` only → `database:<db>:<action>`
  - neither → `all:<action>` (instance-level actions; document this mapping)
- Return the set of suggested checkbox names plus a human-readable list.
- Also expose a `recent_token_checks(datasette, token_id, limit)` for the
  details page that returns *all* recent rows (incl. denied) for transparency.

### 7. New edit route + view
`__init__.py` `register_routes()` (`__init__.py:151-155`) — add:
```python
(r"^/-/api/tokens/(?P<id>\d+)/edit$", token_edit),
```
`views.py` — add `token_edit(request, datasette)`:
- Fetch row; `NotFound` if missing; require `actor_can_edit`; only allow editing
  **Active** tokens.
- **GET**: build `_shared()` context (`views.py:118` — reuses the same
  database/table/permission lists as create), add `checked_names` (helper #4) and
  `suggested` usage names + display (helper #5), render `edit_api_token.html`.
- **POST**: `parse_restrictions_from_post` (helper #3) →
  `_abbreviate_restrictions(datasette, restrictions)` (`__init__.py:28`) →
  `UPDATE _datasette_auth_tokens SET permissions=:permissions WHERE id=:id`,
  then redirect to the details page. Restrictions only ever *narrow* what the
  owning actor can already do, so no extra privilege check on the chosen set is
  needed.

### 8. Templates
- New `templates/edit_api_token.html`: clone the checkbox sections + summary JS
  from `create_api_token.html` (`templates/create_api_token.html:67-99,103-159`),
  but: pre-check inputs whose `name` is in `checked_names`; drop the
  expiry/description and one-time-token issuance blocks; POST to the edit URL.
  Add a **"Used in the last 5 minutes"** panel listing `suggested` actions with
  an **"Apply these restrictions"** button — clicking it sets the checkboxes to
  exactly the suggested set (small JS using the existing names), so the user can
  review before saving. Reuse the existing `updateTokenSummary()` JS.
- `templates/token_details.html`: add an **"Edit permissions"** link to
  `{id}/edit` shown when `token_status == "Active"` and the actor can edit
  (pass a `can_edit` flag from `token_details`, `views.py:289`), plus a
  **"Recent usage"** list (from `recent_token_checks`) showing the last actions
  this token was checked against, including denied ones.

### 9. Docs
`README.md` — document the edit page, the `auth-tokens-edit-all` action, the
`log_token_usage` setting + retention rules, and the "lock down to
recently-used permissions" workflow.

## Files
- `datasette_auth_tokens/__init__.py` — new `auth-tokens-edit-all` action, new
  edit route, `asgi_wrapper` usage-capture hook.
- `datasette_auth_tokens/migrations.py` — `m004_create_usage_table`.
- `datasette_auth_tokens/views.py` — `token_edit`, `actor_can_edit`,
  `parse_restrictions_from_post`, wire `can_edit` + recent usage into
  `token_details`, `Config.log_token_usage`.
- `datasette_auth_tokens/utils.py` — `abbr_to_name`, reverse-checkbox helper,
  `recent_token_usage`, `recent_token_checks`.
- `datasette_auth_tokens/templates/edit_api_token.html` — new.
- `datasette_auth_tokens/templates/token_details.html` — edit link + recent usage.
- `tests/test_managed_tokens.py` — new tests.
- `README.md` — docs.

## Limitations / notes
- The capture *source* is still the private, in-memory `_permission_checks` deque
  (capped at 200 total across all actors), drained per request. Persisting to the
  table removes the volatility for the read side, but if a **single request**
  generates >200 checks for one token, the earliest could be evicted before the
  request ends (rare). Checks are only captured for requests handled by a process
  running this plugin (i.e. requests authenticated by the token).
- Writing on each request that produced new token checks adds one small write;
  fully skipped when `log_token_usage` is off or there are no new token checks.

## Verification
- **Tests** (`tests/test_managed_tokens.py`, existing `Datasette`/
  `pytest-asyncio` fixtures e.g. `ds_managed`):
  - GET `/-/api/tokens/{id}/edit` pre-checks current restrictions.
  - POST updates the `permissions` column **and** changes the token's effective
    `_r` (verify via an authenticated API call before/after).
  - `auth-tokens-edit-all` gating: owner edits own; non-owner needs the action;
    editing a revoked/expired token is rejected.
  - Capture: make authenticated requests (e.g. `GET` a table with `Authorization:
    Bearer dsatok_…`), then assert rows land in `auth_tokens_usage`
    with the right `token_id`/`action`/`parent`/`child`/`result`; assert dedup
    (repeat requests don't double-insert) and pruning (synthesize >1000 rows /
    old rows → capped/trimmed per the rules).
  - Suggestion + apply: after exercising a wide token, assert
    `recent_token_usage()` / the rendered panel reflects exactly the exercised
    actions, and "Apply" → save writes the matching abbreviated `permissions`.
  - `log_token_usage: false` disables capture (no rows written).
  - Run: `python -m pytest tests/test_managed_tokens.py -x` (datasette is not yet
    installed in this env — `pip install -e '.[dev]'` first, or rely on CI).
- **Manual**: run with `manage_tokens: true` and `auth-tokens-create` granted;
  create a wide token, `curl -H "Authorization: Bearer dsatok_…"` a table, open
  `/-/api/tokens/{id}/edit`, confirm the "Used in the last 5 minutes" panel lists
  `view-table` on that db/table, click Apply + Save, then confirm the token is
  now restricted (a `curl` to a different table returns 403).
