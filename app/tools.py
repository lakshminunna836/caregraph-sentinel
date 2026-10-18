"""The agent's tool catalogue.

Design rule: the model NEVER writes Cypher. Every tool wraps one human-written,
parameterised query. Arguments are validated, and system names are resolved
against the graph itself before anything runs.
"""
import time

MAX_DEPTH = 4


class ToolError(Exception):
    def __init__(self, outcome: str, message: str):
        super().__init__(message)
        self.outcome, self.message = outcome, message


# name -> description + typed signature (used for validation AND for the LLM tool schema)
CATALOGUE = {
    "downstream_impact": {
        "desc": "Which systems break, hop by hop, if an HL7 source system goes down.",
        "args": {"system": "system", "depth": "int"},
    },
    "affected_workflows": {
        "desc": "Which clinical workflows are hit if a system goes down, with severity.",
        "args": {"system": "system", "depth": "int"},
    },
    "who_to_page": {
        "desc": "Which teams own the failing and affected systems, and who is on call now.",
        "args": {"system": "system", "depth": "int"},
    },
    "recent_changes": {
        "desc": "Interface/config changes touching the failing or downstream systems recently.",
        "args": {"system": "system", "hours": "int"},
    },
    "path_between": {
        "desc": "Shortest feed path explaining why two systems are connected (graph algorithm).",
        "args": {"source": "system", "target": "system"},
    },
    "critical_systems": {
        "desc": "Hub systems that many feeds converge on (PageRank graph algorithm).",
        "args": {"limit": "int"},
    },
    "recall": {
        "desc": "What earlier sessions already learned about a system (agent memory).",
        "args": {"system": "system"},
    },
    "remember": {
        "desc": "Save a short finding about a system to agent memory.",
        "args": {"system": "system", "text": "str"},
    },
}

DEFAULTS = {"depth": MAX_DEPTH, "hours": 48, "limit": 5}


# ---------------------------------------------------------------- validation
def resolve_system(g, name) -> str | None:
    if not isinstance(name, str):
        return None
    rows = g.ro_query("MATCH (s:System {name: $name}) RETURN s.name", {"name": name}).result_set
    return rows[0][0] if rows else None


def validate(g, tool: str, args: dict) -> dict:
    if tool not in CATALOGUE:
        raise ToolError("unknown_tool", f"'{tool}' is not in the tool catalogue")
    spec = CATALOGUE[tool]["args"]
    extra = set(args) - set(spec)
    if extra:
        raise ToolError("bad_args", f"unexpected arguments: {sorted(extra)}")
    clean = {}
    for key, kind in spec.items():
        val = args.get(key, DEFAULTS.get(key))
        if val is None:
            raise ToolError("bad_args", f"missing argument '{key}'")
        if kind == "system":
            resolved = resolve_system(g, val)
            if not resolved:
                raise ToolError("unknown_system", f"'{val}' is not a system in this hospital's graph")
            clean[key] = resolved
        elif kind == "int":
            if isinstance(val, bool) or not isinstance(val, (int, float, str)):
                raise ToolError("bad_args", f"'{key}' must be an integer")
            try:
                val = int(val)
            except ValueError:
                raise ToolError("bad_args", f"'{key}' must be an integer")
            cap = {"depth": MAX_DEPTH, "hours": 168, "limit": 20}[key]
            clean[key] = max(1, min(val, cap))
        else:  # str
            if not isinstance(val, str) or not val.strip():
                raise ToolError("bad_args", f"'{key}' must be non-empty text")
            clean[key] = val.strip()[:300]
    return clean


# ------------------------------------------------------------------- queries
def _downstream(g, name: str, depth: int) -> list[dict]:
    """Variable-length traversal = the recursion. `depth` is a validated int, so the
    f-string is safe; every other value is a bound parameter."""
    rows = g.ro_query(
        f"""MATCH path = (s:System {{name: $name}})-[:FEEDS*1..{int(depth)}]->(d:System)
            RETURN d.name AS system, d.criticality AS crit, length(path) AS hops,
                   [n IN nodes(path) | n.name] AS via,
                   [r IN relationships(path) | r.interface] AS ifaces
            ORDER BY hops, system""",
        {"name": name},
    ).result_set
    best: dict[str, dict] = {}
    for system, crit, hops, via, ifaces in rows:
        # feeds are cyclic (orders out, results back): drop self-loops and revisits,
        # keep the shortest route per system
        if system == name or len(set(via)) != len(via):
            continue
        if system not in best or hops < best[system]["hops"]:
            best[system] = {"system": system, "criticality": crit, "hops": hops, "via": via, "interfaces": ifaces}
    return sorted(best.values(), key=lambda r: (r["hops"], r["system"]))


def _impacted_names(g, name: str, depth: int) -> list[str]:
    return [name] + [r["system"] for r in _downstream(g, name, depth)]


def downstream_impact(g, a, session):
    return {"source": a["system"], "depth": a["depth"], "systems": _downstream(g, a["system"], a["depth"])}


def affected_workflows(g, a, session):
    names = _impacted_names(g, a["system"], a["depth"])
    rows = g.ro_query(
        """MATCH (w:Workflow)-[:RELIES_ON]->(s:System) WHERE s.name IN $names
           RETURN w.name AS workflow, w.severity AS severity, collect(s.name) AS via_systems
           ORDER BY severity DESC, workflow""",
        {"names": names},
    ).result_set
    wfs = [{"workflow": w, "severity": sev, "via_systems": vs} for w, sev, vs in rows]
    top = max((w["severity"] for w in wfs), default=0)
    priority = "P1" if top >= 5 else "P2" if top >= 4 else "P3" if top else "none"
    return {"workflows": wfs, "priority": priority}


def who_to_page(g, a, session):
    names = _impacted_names(g, a["system"], a["depth"])
    rows = g.ro_query(
        """MATCH (t:Team)-[:OWNS]->(s:System) WHERE s.name IN $names
           OPTIONAL MATCH (p:Person)-[o:ONCALL_FOR]->(t) WHERE o.from <= $now AND o.to >= $now
           RETURN t.name AS team, p.name AS person, p.role AS role, collect(DISTINCT s.name) AS systems
           ORDER BY team""",
        {"names": names, "now": int(time.time())},
    ).result_set
    return {"teams": [{"team": t, "oncall": p, "role": r, "systems": s} for t, p, r, s in rows]}


def recent_changes(g, a, session):
    names = _impacted_names(g, a["system"], MAX_DEPTH)
    now = int(time.time())
    rows = g.ro_query(
        """MATCH (c:Change)-[:TOUCHED]->(s:System) WHERE s.name IN $names AND c.at >= $since
           RETURN c.id AS id, c.summary AS summary, c.author AS author, c.at AS at, s.name AS system
           ORDER BY c.at DESC""",
        {"names": names, "since": now - a["hours"] * 3600},
    ).result_set
    return {
        "hours": a["hours"],
        "changes": [
            {"id": i, "summary": s, "author": au, "system": sy, "hours_ago": round((now - at) / 3600, 1),
             "on_failing_system": sy == a["system"]}
            for i, s, au, at, sy in rows
        ],
    }


def path_between(g, a, session):
    q = """MATCH (a:System {name: $a}), (b:System {name: $b})
           CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['FEEDS'],
                              relDirection: 'outgoing', weightProp: 'weight', pathCount: 1})
           YIELD path, pathWeight
           RETURN [n IN nodes(path) | n.name] AS hops, [r IN relationships(path) | r.interface] AS ifaces, pathWeight"""
    for src, dst in ((a["source"], a["target"]), (a["target"], a["source"])):
        rows = g.ro_query(q, {"a": src, "b": dst}).result_set
        if rows:
            hops, ifaces, w = rows[0]
            return {"found": True, "from": src, "to": dst, "hops": hops, "interfaces": ifaces, "weight": w}
    return {"found": False, "from": a["source"], "to": a["target"]}


def critical_systems(g, a, session):
    rows = g.ro_query(
        """CALL algo.pageRank('System', 'FEEDS') YIELD node, score
           RETURN node.name AS system, node.kind AS kind, score
           ORDER BY score DESC LIMIT $limit""",
        {"limit": a["limit"]},
    ).result_set
    return {"systems": [{"system": s, "kind": k, "score": round(sc, 3)} for s, k, sc in rows]}


def recall(g, a, session):
    now = int(time.time())
    rows = g.ro_query(
        """MATCH (f:Finding)-[:ABOUT]->(s:System {name: $name})
           RETURN f.text AS text, f.at AS at, f.session AS session
           ORDER BY f.at DESC LIMIT 5""",
        {"name": a["system"]},
    ).result_set
    return {"findings": [{"text": t, "days_ago": round((now - at) / 86400, 1), "session": se} for t, at, se in rows]}


def remember(g, a, session):
    g.query(
        """MATCH (s:System {name: $name})
           CREATE (f:Finding {text: $text, at: $at, session: $session, author: 'engineer'})-[:ABOUT]->(s)""",
        {"name": a["system"], "text": a["text"], "at": int(time.time()), "session": session},
    )
    return {"saved": True, "system": a["system"], "text": a["text"]}


def auto_remember(g, system: str, text: str, session: str):
    """Write the conclusion back into the graph (once per text per day)."""
    day = int(time.time()) // 86400
    g.query(
        """MATCH (s:System {name: $name})
           MERGE (f:Finding {text: $text, day: $day})
           ON CREATE SET f.at = $at, f.session = $session, f.author = 'sentinel'
           MERGE (f)-[:ABOUT]->(s)""",
        {"name": system, "text": text, "day": day, "at": int(time.time()), "session": session},
    )


IMPLS = {f.__name__: f for f in (
    downstream_impact, affected_workflows, who_to_page, recent_changes,
    path_between, critical_systems, recall, remember,
)}
READ_ONLY = {k for k in IMPLS if k != "remember"}


def run_tool(g, tool: str, args: dict, session: str = "adhoc"):
    """Validate -> execute -> return (result, trace entry)."""
    clean = validate(g, tool, args)
    t0 = time.perf_counter()
    result = IMPLS[tool](g, clean, session)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    rows = next((len(v) for v in result.values() if isinstance(v, list)), 1)
    return result, {"tool": tool, "args": clean, "ms": ms, "rows": rows}
