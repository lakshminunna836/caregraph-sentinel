"""FastAPI app: JSON API + the single-page UI.  Run: uvicorn app.api:api --reload"""
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import agent, db, seed

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app):
    if not db.tenants():  # first boot on a fresh FalkorDB (e.g. a new Cloud instance)
        seed.seed()
    yield


api = FastAPI(title="CareGraph Sentinel", lifespan=lifespan)


class Ask(BaseModel):
    tenant: str = "stmary"
    question: str = Field(min_length=2, max_length=400)
    session: str | None = None


@api.get("/api/health")
def health():
    return {"ok": True, "tenants": db.tenants()}


@api.get("/api/tenants")
def tenants():
    return {"tenants": db.tenants()}


@api.get("/api/graph")
def graph(tenant: str = "stmary"):
    if tenant not in db.tenants():
        raise HTTPException(404, "unknown tenant")
    g = db.graph(tenant)
    systems = g.ro_query("MATCH (s:System) RETURN s.name, s.kind, s.criticality ORDER BY s.name").result_set
    feeds = g.ro_query(
        "MATCH (a:System)-[f:FEEDS]->(b:System) RETURN a.name, b.name, f.interface, f.msg").result_set
    return {
        "systems": [{"name": n, "kind": k, "criticality": c} for n, k, c in systems],
        "feeds": [{"source": a, "target": b, "interface": i, "msg": m} for a, b, i, m in feeds],
    }


@api.post("/api/ask")
def ask(body: Ask):
    try:
        return agent.ask(body.tenant, body.question, body.session or uuid.uuid4().hex[:8])
    except ValueError as e:
        raise HTTPException(404, str(e))


@api.get("/")
def index():
    return FileResponse(STATIC / "index.html")
