# datasette-auth-tokens

[![PyPI](https://img.shields.io/pypi/v/datasette-auth-tokens.svg)](https://pypi.org/project/datasette-auth-tokens/)
[![CircleCI](https://circleci.com/gh/simonw/datasette-auth-tokens.svg?style=svg)](https://circleci.com/gh/simonw/datasette-auth-tokens)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/datasette-auth-tokens/blob/master/LICENSE)

Datasette plugin for authenticating access using API tokens

## Installation

Install this plugin in the same environment as Datasette.

    $ pip install datasette-auth-tokens

## Hard-coded tokens

Read about Datasette's [authentication and permissions system](https://datasette.readthedocs.io/en/latest/authentication.html).

This plugin lets you configure secret API tokens which can be used to make authenticated requests to Datasette.

First, create a random API token. A useful recipe for doing that is the following:

    $ python -c 'import secrets; print(secrets.token_hex(32))'
    5f9a486dd807de632200b17508c75002bb66ca6fde1993db1de6cbd446362589

Decide on the actor that this token should represent, for example:

```json
{
    "bot_id": "my-bot"
}
```

You can then use `"allow"` blocks to provide that token with permission to access specific actions. To enable access to a configured writable SQL query you could use this in your `metadata.json`:

```json
{
    "plugins": {
        "datasette-auth-tokens": {
            "tokens": [
                {
                    "token": {
                        "$env": "BOT_TOKEN"
                    },
                    "actor": {
                        "bot_id": "my-bot"
                    }
                }
            ]
        }
    },
    "databases": {
        ":memory:": {
            "queries": {
                "show_version": {
                    "sql": "select sqlite_version()",
                    "allow": {
                        "bot_id": "my-bot"
                    }
                }
            }
        }
    }
}
```
This uses Datasette's [secret configuration values mechanism](https://datasette.readthedocs.io/en/stable/plugins.html#secret-configuration-values) to allow the secret token to be passed as an environment variable.

Run Datasette like this:

    BOT_TOKEN="this-is-the-secret-token" \
        datasette -m metadata.json

You can now run authenticated API queries like this:

    $ curl -H 'Authorization: Bearer this-is-the-secret-token' \
      'http://127.0.0.1:8001/:memory:/show_version.json?_shape=array'
    [{"sqlite_version()": "3.31.1"}]

## Tokens from your database

As an alternative (or in addition) to the hard-coded list of tokens you can store tokens in a database table and configure the plugin to access them using a SQL query.

Your query needs to take a `:token_id` parameter and return at least two columns: one called `token_secret` and one called `actor_*` - usually `actor_id`. Further `actor_` prefixed columns can be returned to provide more details for the authenticated actor.

Here's a simple example of a configuration query:

```sql
select actor_id, actor_name, token_secret from tokens where token_id = :token_id
```

This can run against a table like this one:

| token_id | token_secret | actor_id | actor_name |
| -------- | ------------ | -------- | ---------- |
| 1        | bd3c94f51fcd | 78       | Cleopaws   |
| 2        | 86681b4d6f66 | 32       | Pancakes   |

The tokens are formed as the token id, then a hyphen, then the token secret. For example:

- `1-bd3c94f51fcd`
- `2-86681b4d6f66`

The SQL query will be executed with the portion before the hyphen as the `:token_id` parameter.

The `token_secret` value returned by the query will be compared to the portion of the token after the hyphen to check if the token is valid.

Columns with a prefix of `actor_` will be used to populate the actor dictionary. In the above example, a token of `2-86681b4d6f66` will become an actor dictionary of `{"id": 32, "name": "Pancakes"}`.

To configure this, use a `"query"` block in your plugin configuration like this:

```json
{
    "plugins": {
        "datasette-auth-tokens": {
            "query": {
                "sql": "select actor_id, actor_name, token_secret from tokens where token_id = :token_id",
                "database": "tokens"
            }
        }
    },
    "databases": {
        "tokens": {
            "allow": {}
        }
    }
}
```
The `"sql"` key here contains the SQL query. The `"database"` key has the name of the attached database file that the query should be executed against - in this case it would execute against `tokens.db`.

### Securing your tokens

Anyone with access to your Datasette instance can use it to read the `token_secret` column in your tokens table. This probably isn't what you want!

To avoid this, you should lock down access to that table. The configuration example above shows how to do this using an `"allow": {}` block. Consult Datasette's [Permissions documentation](https://datasette.readthedocs.io/en/stable/authentication.html#permissions) for more information about how to lock down this kind of access.
