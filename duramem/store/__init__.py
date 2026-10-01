"""存储层：多库注册表、连接管理、数据访问。"""

from duramem.store.database import Database, Library
from duramem.store.registry import DatabaseRegistry, RegistryEntry, get_registry
from duramem.store.repository import Repository, content_hash, new_uid
from duramem.store.schema import EmbeddingMismatchError, assert_embedding_compatible

__all__ = [
    "Database",
    "DatabaseRegistry",
    "EmbeddingMismatchError",
    "Library",
    "RegistryEntry",
    "Repository",
    "assert_embedding_compatible",
    "content_hash",
    "get_registry",
    "new_uid",
]
