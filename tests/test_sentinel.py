"""Run with embedded FalkorDB (no Docker needed):  pytest -q
Set FALKORDB_HOST to run against a real FalkorDB container instead."""
import os
import tempfile

if not os.getenv("FALKORDB_HOST"):
    os.environ["FALKORDB_EMBEDDED"] = "1"
    os.environ["FALKORDB_EMBEDDED_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("ANTHROPIC_API_KEY", None)  # tests exercise the deterministic router

import pytest
from fastapi.testclient import TestClient

from app import agent, db, seed, tools
from app.api import api


@pytest.fixture(scope="module", autouse=True)
def graphs():
    seed.seed(reset=True)


def test_depth_one_vs_four_shows_multi_hop_value():
    g = db.graph("stmary")
    near, _ = tools.run_tool(g, "downstream_impact", {"system": "lab-lis", "depth": 1})
    far, _ = tools.run_tool(g, "downstream_impact", {"system": "lab-lis", "depth": 4})
    assert len(far["systems"]) > len(near["systems"])
    assert any(r["hops"] >= 2 for r in far["systems"])
    assert all(r["via"][0] == "lab-lis" for r in far["systems"])  # every row carries its path


def test_cycles_do_not_report_the_failing_system_as_its_own_victim():
    g = db.graph("stmary")
    res, _ = tools.run_tool(g, "downstream_impact", {"system": "ehr-core"})
    assert "ehr-core" not in [r["system"] for r in res["systems"]]


def test_unknown_system_runs_no_query():
    out = agent.ask("stmary", "payments-api is down")
    assert out["outcome"] == "unknown_system"
    assert out["sections"] == {}
    assert out["trace"][0]["error"]


def test_empty_traversal_is_not_the_same_as_safe():
    out = agent.ask("stmary", "data-warehouse is down")
    assert out["outcome"] == "not_in_graph"
    assert "does not mean it is safe" in out["answer"]


def test_arguments_are_validated_and_clamped():
    g = db.graph("stmary")
    with pytest.raises(tools.ToolError):
        tools.run_tool(g, "downstream_impact", {"system": "lab-lis", "cypher": "MATCH (n) DETACH DELETE n"})
    with pytest.raises(tools.ToolError):
        tools.run_tool(g, "downstream_impact", {"system": "lab-lis' OR 1=1 //"})
    _, trace = tools.run_tool(g, "downstream_impact", {"system": "lab-lis", "depth": 99})
    assert trace["args"]["depth"] == tools.MAX_DEPTH


def test_tenants_are_isolated_graphs():
    full = agent.ask("stmary", "hie-gateway is down")
    small = agent.ask("riverside", "hie-gateway is down")
    assert full["outcome"] != "unknown_system"
    assert small["outcome"] == "unknown_system"  # riverside has no HIE gateway
    a = tools.run_tool(db.graph("stmary"), "who_to_page", {"system": "lab-lis"})[0]
    b = tools.run_tool(db.graph("riverside"), "who_to_page", {"system": "lab-lis"})[0]
    people = lambda r: {t["oncall"] for t in r["teams"]}
    assert not people(a) & people(b)


def test_memory_persists_across_sessions():
    agent.ask("stmary", "note lab-lis: root cause was a stuck MLLP listener", session="s1")
    out = agent.ask("stmary", "What do we know about lab-lis?", session="s2")
    assert out["outcome"] == "answered_from_memory"
    texts = [f["text"] for f in out["sections"]["recall"]["findings"]]
    assert any("stuck MLLP listener" in t for t in texts)
    assert any("expired certificate" in t for t in texts)  # seeded earlier-session memory


def test_incident_report_writes_its_conclusion_back():
    out = agent.ask("stmary", "ehr-core is down, what breaks?", session="s3")
    assert out["memory_written"]
    later = agent.ask("stmary", "history of ehr-core")
    assert any("Impact analysis" in f["text"] for f in later["sections"]["recall"]["findings"])


def test_algorithms_run_as_tools():
    g = db.graph("stmary")
    path, _ = tools.run_tool(g, "path_between", {"source": "registration", "target": "patient-portal"})
    assert path["found"] and path["hops"][0] == "registration" and path["hops"][-1] == "patient-portal"
    hubs, _ = tools.run_tool(g, "critical_systems", {"limit": 3})
    assert hubs["systems"][0]["system"] == "ehr-core"


def test_guard_rejects_invented_systems_and_false_impact():
    results = {"downstream_impact": {"systems": []}}
    ok, _ = agent.guard("Impact reaches auth-gateway and lab-lis.", {"lab-lis"}, results)
    assert not ok
    ok, _ = agent.guard("Everything downstream is affected.", {"lab-lis"}, results)
    assert not ok
    ok, _ = agent.guard("Nothing downstream is modelled.", {"lab-lis"}, results)
    assert ok


def test_http_api():
    c = TestClient(api)
    assert c.get("/api/health").json()["ok"]
    r = c.post("/api/ask", json={"tenant": "stmary", "question": "The LIS interface is down, what breaks?"})
    body = r.json()
    assert r.status_code == 200 and body["outcome"] == "answered_with_path"
    assert body["highlight"]["source"] == "lab-lis" and body["highlight"]["edges"]
    assert c.post("/api/ask", json={"tenant": "nope", "question": "lab-lis down"}).status_code == 404
    assert c.get("/api/graph?tenant=stmary").json()["feeds"]
    assert c.get("/").status_code == 200


@pytest.mark.parametrize("tenant,question", [
    ("stmary", "The LIS interface is down, what breaks?"),
    ("stmary", "lab-lis is down, direct dependencies only"),
    ("stmary", "Why is registration connected to patient-portal?"),
    ("stmary", "Who should I page if ehr-core fails?"),
    ("stmary", "What changed recently around hie-gateway?"),
    ("stmary", "Which systems are the biggest hubs?"),
    ("riverside", "The LIS interface is down, what breaks?"),
])
def test_our_own_answers_pass_the_guard(tenant, question):
    out = agent.ask(tenant, question)
    assert out["guard"]["ok"], out["guard"]
