# datasette-auth-tokens

[![PyPI](https://img.shields.io/pypi/v/datasette-auth-tokens.svg)](https://pypi.org/project/datasette-auth-tokens/)
[![CircleCI](https://circleci.com/gh/simonw/datasette-auth-tokens.svg?style=svg)](https://circleci.com/gh/simonw/datasette-auth-tokens)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/simonw/datasette-auth-tokens/blob/master/LICENSE)

Datasette plugin for authenticating access using API tokens

## Installation

Install this plugin in the same environment as Datasette.

    $ pip install datasette-auth-tokens

## Simple usage: a hard-coded list of tokens

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
        "datasette-auth-tokens": [
            {
                "token": {
                    "$env": "BOT_TOKEN"
                },
                "actor": {
                    "bot_id": "my-bot"
                }
            }
        ]
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
