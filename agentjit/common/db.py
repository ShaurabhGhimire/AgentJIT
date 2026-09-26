from __future__ import annotations

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from . import config

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(config.MONGODB_URI, tz_aware=True)
    return _client


def get_db() -> Database:
    return get_client()[config.DB_NAME]


def col(name: str) -> Collection:
    """Return the collection for `name` under the current prefix.

    Reads config.DB_PREFIX on every call so tests and eval arms can switch
    prefixes at runtime (use_prefix).
    """
    return get_db()[f"{config.DB_PREFIX}_{name}"]


def use_prefix(prefix: str) -> None:
    config.DB_PREFIX = prefix


def drop_prefix(prefix: str) -> None:
    db = get_db()
    for name in db.list_collection_names():
        if name.startswith(f"{prefix}_"):
            db.drop_collection(name)
