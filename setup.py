from setuptools import setup
import os

VERSION = "0.2.2"


def get_long_description():
    with open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md"),
        encoding="utf8",
    ) as fp:
        return fp.read()


setup(
    name="datasette-auth-tokens",
    description="Datasette plugin for authenticating access using API tokens",
    long_description=get_long_description(),
    long_description_content_type="text/markdown",
    author="Simon Willison",
    url="https://github.com/simonw/datasette-auth-tokens",
    license="Apache License, Version 2.0",
    version=VERSION,
    packages=["datasette_auth_tokens"],
    entry_points={"datasette": ["auth_tokens = datasette_auth_tokens"]},
    install_requires=["datasette>=0.44",],
    extras_require={"test": ["pytest", "pytest-asyncio", "httpx", "sqlite-utils"]},
    tests_require=["datasette-auth-tokens[test]"],
)
