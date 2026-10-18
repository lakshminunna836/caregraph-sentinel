"""CareGraph Sentinel agent.

Loop: question -> pick tools (LLM tool-use if ANTHROPIC_API_KEY is set, otherwise a
deterministic rule router) -> run validated tools against the tenant's graph ->
compose an answer FROM THE ROWS -> guard the wording -> write findings back to memory.

The agent returns one of five outcomes so it never bluffs:
  answered_with_path | answered_from_memory | not_in_graph | unknown_system | need_system
"""
import json
import os
import re

from . import db, tools

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

ALIASES = {
    "lis": "lab-lis", "lab": "lab-lis", "laboratory": "lab-lis",
    "ehr": "ehr-core", "emr": "ehr-core",
    "ris": "radiology-ris", "radiology": "radiology-ris",
    "pharmacy": "pharmacy-rx", "rx": "pharmacy-rx",
    "billing": "billing-rcm", "rcm": "billing-rcm",
    "hie": "hie-gateway", "portal": "patient-portal",
    "adt": "registration", "registration": "registration",
    "bed": "bed-mgmt", "er": "er-tracking",
    "warehouse": "data-warehouse",
}
NAME = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b")
ALLOWED_HYPHENATED = {"on-call", "multi-hop", "follow-up", "re-run"}
ALIAS_RE = re.compile(r"\b(" + "|".join(sorted(ALIASES, key=len, reverse=True)) + r")\b")


# --------------------------------------------------------------- understanding
def find_systems(question: str) -> list[str]:
    """Candidate system names in order of appearance (aliases + hyphenated tokens)."""
    ql = question.lower()
    hits = []
    for m in NAME.finditer(ql):
        if m.group(0) not in ALLOWED_HYPHENATED:
            hits.append((m.start(), m.group(0)))
    for m in ALIAS_RE.finditer(ql):
        hits.append((m.start(), ALIASES[m.group(1)]))
    seen, ordered = set(), []
    for _, name in sorted(hits):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def rule_plan(question: str, systems: list[str]) -> list[tuple[str, dict]]:
    q = question.lower()
    depth = 1 if re.search(r"\b(direct|immediate|one hop|1 hop|first hop)\b", q) else None
    m = re.search(r"depth\s*(\d)", q)
    if m:
        depth = int(m.group(1))
    hours = 48
    m = re.search(r"(\d+)\s*(hour|hr|h|day)", q)
    if m:
        hours = int(m.group(1)) * (24 if m.group(2) == "day" else 1)
    s = systems[0] if systems else None
    dargs = {"system": s, **({"depth": depth} if depth else {})}

    if re.match(r"\s*(note|remember|save|log)\b", q) and ":" in question and s:
        return [("remember", {"system": s, "text": question.split(":", 1)[1].strip()})]
    if re.search(r"\b(what do we know|history|previous|past|earlier|before|learned)\b", q) and s:
        return [("recall", {"system": s})]
    if re.search(r"\b(why|path|connected|between|link|route)\b", q) and len(systems) >= 2:
        return [("path_between", {"source": systems[0], "target": systems[1]})]
    if re.search(r"\bwho\b", q) and re.search(r"(page|wake|call|contact|on.?call)", q) and s:
        return [("who_to_page", dargs)]
    if re.search(r"\b(change[sd]?|recent|deploy\w*|modified|updated)\b", q) and s:
        return [("recent_changes", {"system": s, "hours": hours})]
    if re.search(r"\b(critical|hub|hubs|load.bearing|biggest|most important|central)\b", q):
        return [("critical_systems", {})]
    if s:
        return [
            ("downstream_impact", dargs),
            ("affected_workflows", dargs),
            ("who_to_page", dargs),
            ("recent_changes", {"system": s, "hours": hours}),
            ("recall", {"system": s}),
        ]
    return []


def llm_plan(question: str, known: list[str]):
    import anthropic

    props = {"system": {"type": "string"}, "source": {"type": "string"}, "target": {"type": "string"},
             "depth": {"type": "integer"}, "hours": {"type": "integer"}, "limit": {"type": "integer"},
             "text": {"type": "string"}}
    defs = [{
        "name": n,
        "description": spec["desc"],
        "input_schema": {"type": "object", "properties": {k: props[k] for k in spec["args"]},
                         "required": [k for k in spec["args"] if k not in tools.DEFAULTS]},
    } for n, spec in tools.CATALOGUE.items()]
    sys_prompt = (
        "You route questions from hospital integration engineers to tools over an HL7 system graph. "
        f"Known systems in this hospital: {', '.join(known)}. Map the user's words to these names, "
        "but if the user names a system that is not in the list, pass their name through verbatim "
        "(never substitute a similar one). For 'X is down / failing / what breaks' call "
        "downstream_impact, affected_workflows, who_to_page, recent_changes and recall together."
    )
    client = anthropic.Anthropic()
    resp = client.messages.create(model=MODEL, max_tokens=600, system=sys_prompt, tools=defs,
                                  messages=[{"role": "user", "content": question}])
    return [(b.name, dict(b.input)) for b in resp.content if b.type == "tool_use"]


# ------------------------------------------------------------------ answering
def _hops(n: int) -> str:
    return f"{n} hop" + ("" if n == 1 else "s")


def _ago(days: float) -> str:
    return "earlier today" if days < 1 else f"{round(days)} day{'s' if round(days) != 1 else ''} ago"


def compose(results: dict) -> str:
    """Build the answer straight from graph rows."""
    parts = []
    d = results.get("downstream_impact")
    if d is not None:
        names = [r["system"] for r in d["systems"]]
        if names:
            near = [r["system"] for r in d["systems"] if r["hops"] == 1]
            far = max(r["hops"] for r in d["systems"])
            parts.append(
                f"If {d['source']} goes down, {len(names)} downstream systems are affected within {_hops(d['depth'])} "
                f"(directly: {', '.join(near)}; furthest is {_hops(far)} away)."
            )
        else:
            parts.append(f"Nothing downstream of {d['source']} is modelled in the graph. That does not mean it is "
                         "safe; the feed may simply not be recorded.")
    w = results.get("affected_workflows")
    if w and w["workflows"]:
        top = w["workflows"][0]
        parts.append(f"Priority {w['priority']}: {len(w['workflows'])} clinical workflows at risk, most severe is "
                     f"{top['workflow']} (severity {top['severity']}).")
    p = results.get("who_to_page")
    if p and p["teams"]:
        people = [f"{t['oncall']} ({t['team']})" for t in p["teams"] if t["oncall"]]
        if people:
            parts.append("Page: " + ", ".join(people) + ".")
    c = results.get("recent_changes")
    if c is not None:
        if c["changes"]:
            first = c["changes"][0]
            parts.append(f"Most recent change on {first['system']}: {first['id']}, {first['hours_ago']}h ago - {first['summary']}.")
        else:
            parts.append(f"No changes recorded in the last {c['hours']} hours.")
    pb = results.get("path_between")
    if pb:
        if pb["found"]:
            parts.append(f"{pb['from']} reaches {pb['to']} through {' -> '.join(pb['hops'])} "
                         f"({len(pb['hops']) - 1} hops via {', '.join(pb['interfaces'])}).")
        else:
            parts.append(f"No feed path connects {pb['from']} and {pb['to']} in the graph.")
    cs = results.get("critical_systems")
    if cs:
        parts.append("Hub systems that the most feeds converge on (PageRank): " + ", ".join(
            f"{r['system']} ({r['score']})" for r in cs["systems"]) + ".")
    r = results.get("recall")
    if r is not None:
        if r["findings"]:
            human = [x for x in r["findings"] if not x["text"].startswith("Impact analysis")]
            f = (human or r["findings"])[0]
            parts.append(f"From memory ({_ago(f['days_ago'])}): {f['text']}")
        elif len(results) == 1:
            parts.append("Nothing has been recorded about this system in earlier sessions.")
    s = results.get("remember")
    if s:
        parts.append(f"Saved to memory for {s['system']}.")
    return " ".join(parts)


def guard(sentence: str, known_systems: set[str], results: dict) -> tuple[bool, str]:
    """Reject wording that names a system the graph did not return."""
    # anything hyphenated must be a system name or appear somewhere in the rows the graph returned
    # (team names, change ids ...). Everything else is treated as invented.
    returned = set(NAME.findall(json.dumps(results).lower()))
    invented = set(NAME.findall(sentence.lower())) - known_systems - returned - ALLOWED_HYPHENATED
    if invented:
        return False, f"named systems not in the graph: {sorted(invented)}"
    d = results.get("downstream_impact")
    if d is not None and not d["systems"] and not re.search(r"\b(no|not|nothing)\b", sentence.lower()):
        return False, "claimed impact when the traversal was empty"
    return True, "ok"


def llm_summary(question: str, results: dict) -> str:
    import anthropic

    resp = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=350,
        system=("You are an on-call assistant for hospital HL7 integration teams. Write 3-4 plain sentences "
                "answering the question using ONLY the JSON results. Use system and person names exactly as "
                "given. Never add a system, person, or cause that is not in the results. Do not use hyphenated "
                "words except system names and 'on-call'."),
        messages=[{"role": "user", "content": f"Question: {question}\nResults: {json.dumps(results)[:6000]}"}],
    )
    return "".join(b.text for b in resp.content if b.type == "text").strip()


def _edges(vias):
    return [[v[i], v[i + 1]] for v in vias for i in range(len(v) - 1)]


# ----------------------------------------------------------------------- main
def ask(tenant: str, question: str, session: str = "web") -> dict:
    if tenant not in db.tenants():
        raise ValueError(f"unknown tenant '{tenant}'")
    g = db.graph(tenant)
    known = [r[0] for r in g.ro_query("MATCH (s:System) RETURN s.name ORDER BY s.name").result_set]
    systems = find_systems(question)

    mode, plan = "rules", []
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            plan, mode = llm_plan(question, known), "llm"
        except Exception as e:  # network / key / schema problems must never break the demo
            mode = f"rules (llm unavailable: {type(e).__name__})"
    if not plan:
        plan = rule_plan(question, systems)

    out = {"tenant": tenant, "question": question, "mode": mode, "trace": [], "sections": {},
           "highlight": {"source": None, "affected": [], "edges": []}, "memory_written": False}

    if not plan:
        out.update(outcome="need_system", answer="Which system? Name one of: " + ", ".join(known) + ".")
        return out

    results = {}
    for tool, args in plan:
        try:
            res, trace = tools.run_tool(g, tool, args, session)
        except tools.ToolError as e:
            out["trace"].append({"tool": tool, "args": args, "error": e.message, "ms": 0, "rows": 0})
            if e.outcome == "unknown_system":
                out.update(outcome="unknown_system", sections={}, answer=(
                    f"{e.message}. No query was run, so nothing is guessed. "
                    f"Systems in this graph: {', '.join(known)}."))
                return out
            continue
        results[tool] = res
        out["trace"].append(trace)

    out["sections"] = results
    if not results:
        out.update(outcome="need_system", answer="I could not run any tool for that question.")
        return out

    # highlight for the UI
    d = results.get("downstream_impact")
    if d:
        out["highlight"] = {"source": d["source"], "affected": [r["system"] for r in d["systems"]],
                            "edges": _edges([r["via"] for r in d["systems"]])}
    pb = results.get("path_between")
    if pb and pb["found"]:
        out["highlight"] = {"source": pb["from"], "affected": pb["hops"][1:], "edges": _edges([pb["hops"]])}

    # outcome
    if set(results) <= {"recall"}:
        out["outcome"] = "answered_from_memory" if results["recall"]["findings"] else "not_in_graph"
    elif (d is not None and not d["systems"]) or (pb and not pb["found"]):
        out["outcome"] = "not_in_graph"
    else:
        out["outcome"] = "answered_with_path"

    template = compose(results)
    answer, source = template, "template"
    if mode == "llm":
        try:
            candidate = llm_summary(question, results)
            ok, why = guard(candidate, set(known), results)
            if ok and candidate:
                answer, source = candidate, "llm"
            else:
                out["guard_rejected"] = why
        except Exception:
            pass
    ok, why = guard(answer, set(known), results)
    out["answer"] = answer
    out["guard"] = {"ok": ok, "reason": why, "source": source}

    # memory: write the conclusion back so the next session finds it
    if d and d["systems"]:
        wf = results.get("affected_workflows", {})
        try:
            tools.auto_remember(
                g, d["source"],
                f"Impact analysis ({d['depth']} hops): {len(d['systems'])} downstream systems, "
                f"{len(wf.get('workflows', []))} workflows, priority {wf.get('priority', 'n/a')}.",
                session,
            )
            out["memory_written"] = True
        except Exception:
            pass
    if "remember" in results:
        out["memory_written"] = True
    return out
