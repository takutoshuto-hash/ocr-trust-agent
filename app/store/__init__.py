from .base import Store  # noqa: F401
from .memory import MemoryStore  # noqa: F401


def get_store() -> "Store":
    from app.config import settings
    if settings.store_backend == "firestore":
        from .firestore import FirestoreStore
        return FirestoreStore(project=settings.gcp_project or None, prefix=settings.firestore_prefix)
    return MemoryStore()
