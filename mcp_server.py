"""MCP server exposing the Sentinel tool catalogue (Track 1: tool selection through MCP).

Any MCP client (Claude Desktop, Cursor, ...) can call these. The model still never
writes Cypher: each tool runs a fixed, parameterised query against one hospital's graph.

Run:  python mcp_server.py          (stdio)
"""
import json

try:  # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from app import db, tools

mcp = _Server("caregraph-sentinel")


def _run(tenant: str, tool: str, **args):
    if tenant not in db.tenants():
        return json.dumps({"outcome": "unknown_tenant", "tenants": db.tenants()})
    try:
        result, trace = tools.run_tool(db.graph(tenant), tool, args, session="mcp")
        return json.dumps({"outcome": "ok", "result": result, "trace": trace}, default=str)
    except tools.ToolError as e:
        return json.dumps({"outcome": e.outcome, "message": e.message})


@mcp.tool()
def downstream_impact(tenant: str, system: str, depth: int = 4) -> str:
    """Which systems break, hop by hop, if an HL7 source system goes down."""
    return _run(tenant, "downstream_impact", system=system, depth=depth)


@mcp.tool()
def affected_workflows(tenant: str, system: str, depth: int = 4) -> str:
    """Which clinical workflows are hit if a system goes down, with severity and priority."""
    return _run(tenant, "affected_workflows", system=system, depth=depth)


@mcp.tool()
def who_to_page(tenant: str, system: str, depth: int = 4) -> str:
    """Teams owning the failing and affected systems, and who is on call right now."""
    return _run(tenant, "who_to_page", system=system, depth=depth)


@mcp.tool()
def recent_changes(tenant: str, system: str, hours: int = 48) -> str:
    """Recent interface/config changes touching the failing or downstream systems."""
    return _run(tenant, "recent_changes", system=system, hours=hours)


@mcp.tool()
def path_between(tenant: str, source: str, target: str) -> str:
    """Shortest feed path between two systems (FalkorDB algo.SPpaths)."""
    return _run(tenant, "path_between", source=source, target=target)


@mcp.tool()
def critical_systems(tenant: str, limit: int = 5) -> str:
    """Hub systems many feeds converge on (FalkorDB algo.pageRank)."""
    return _run(tenant, "critical_systems", limit=limit)


@mcp.tool()
def recall(tenant: str, system: str) -> str:
    """What earlier sessions learned about a system (agent memory)."""
    return _run(tenant, "recall", system=system)


@mcp.tool()
def remember(tenant: str, system: str, text: str) -> str:
    """Save a short finding about a system to this hospital's memory."""
    return _run(tenant, "remember", system=system, text=text)


if __name__ == "__main__":
    mcp.run()
