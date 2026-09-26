from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from . import config

_client: MongoClient | None = None
_local = threading.local()


def get_client() -> MongoClient:
    global _client
    if _client is None:
        kwargs = {}
        if config.MONGODB_URI.startswith("mongodb+srv://"):
            # Atlas needs TLS; some Python installs (e.g. python.org macOS builds) ship no CA roots
            import certifi

            kwargs["tlsCAFile"] = certifi.where()
        _client = MongoClient(config.MONGODB_URI, tz_aware=True, **kwargs)
    return _client


def get_db() -> Database:
    return get_client()[config.DB_NAME]


def col(name: str) -> Collection:
    """Return the collection for `name` under the current prefix.

    Reads config.DB_PREFIX on every call so tests and eval arms can switch
    prefixes at runtime (use_prefix).
    """
    return get_db()[f"{current_prefix()}_{name}"]


def current_prefix() -> str:
    return getattr(_local, "prefix", None) or config.DB_PREFIX


def use_prefix(prefix: str) -> None:
    config.DB_PREFIX = prefix


@contextmanager
def prefix_scope(prefix: str) -> Iterator[None]:
    """Override the prefix for the current thread only (side-by-side lanes in one process)."""
    old = getattr(_local, "prefix", None)
    _local.prefix = prefix
    try:
        yield
    finally:
        _local.prefix = old


def drop_prefix(prefix: str) -> None:
    db = get_db()
    for name in db.list_collection_names():
        if name.startswith(f"{prefix}_"):
            db.drop_collection(name)
