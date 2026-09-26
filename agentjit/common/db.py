from __future__ import annotations

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from .config import DB_NAME, DB_PREFIX, MONGODB_URI

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        _client = MongoClient(MONGODB_URI)
    return _client


def get_db() -> Database:
    return get_client()[DB_NAME]


def col(name: str) -> Collection:
    """Return the collection for `name` under this user's prefix."""
    return get_db()[f"{DB_PREFIX}_{name}"]
