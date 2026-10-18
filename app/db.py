"""FalkorDB connection. One graph per hospital (tenant): `caregraph:<tenant>`.

Connection modes (env vars):
  FALKORDB_HOST / FALKORDB_PORT / FALKORDB_USERNAME / FALKORDB_PASSWORD / FALKORDB_SSL=1
      -> Docker, self-hosted, or FalkorDB Cloud (default: localhost:6379)
  FALKORDB_EMBEDDED=1
      -> embedded FalkorDB (falkordblite), used only for tests / no-Docker dev
"""
import os

from falkordb import FalkorDB

PREFIX = "caregraph:"
_db = None


def get_db():
    global _db
    if _db is None:
        if os.getenv("FALKORDB_EMBEDDED"):
            from redislite.falkordb_client import FalkorDB as Embedded

            _db = Embedded(os.getenv("FALKORDB_EMBEDDED_PATH", "caregraph.db"))
        else:
            _db = FalkorDB(
                host=os.getenv("FALKORDB_HOST", "localhost"),
                port=int(os.getenv("FALKORDB_PORT", "6379")),
                username=os.getenv("FALKORDB_USERNAME") or None,
                password=os.getenv("FALKORDB_PASSWORD") or None,
                ssl=os.getenv("FALKORDB_SSL") == "1",
            )
    return _db


def graph(tenant: str):
    """A tenant is a separate graph, not a WHERE clause anyone can forget."""
    return get_db().select_graph(PREFIX + tenant)


def tenants() -> list[str]:
    return sorted(n[len(PREFIX):] for n in get_db().list_graphs() if n.startswith(PREFIX))
