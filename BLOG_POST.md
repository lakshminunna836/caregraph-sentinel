# When the Lab Feed Dies: Building an HL7 Incident Agent on a Graph

*Draft for the Graph Hacks best-blog-post prize. Edit it into your own voice, add screenshots where marked.*

[Optional: one line about your own background in healthcare interoperability.]

## The question nobody can answer from one record

When a lab interface stops in a hospital, the first questions are always the same. What else breaks? Which clinical workflows are affected? Who is on call? What changed recently?

None of those answers sit in a single record. They sit in how systems connect: the lab sends results to the EHR, the EHR feeds pharmacy, billing, the portal and the health information exchange. Retrieval can hand an agent the records. It can't hand it the relationships, so the agent guesses at them.

So I gave the agent a graph.

## The graph

CareGraph Sentinel models a hospital's HL7 network in FalkorDB:

```
(:System)-[:FEEDS {interface, msg}]->(:System)
(:Workflow {severity})-[:RELIES_ON]->(:System)
(:Team)-[:OWNS]->(:System)      (:Person)-[:ONCALL_FOR]->(:Team)
(:Change)-[:TOUCHED]->(:System) (:Finding)-[:ABOUT]->(:System)
```

Every edge carries a real HL7 channel name and message type (for example `ORU_LIS_TO_EHR`, `ORU^R01`). All data is synthetic.

[Screenshot: FalkorDB Browser showing the graph]

## One pattern replaces a recursive query

The blast radius of a failing system is everything downstream, to whatever depth the architecture reaches:

```cypher
MATCH path = (s:System {name: $name})-[:FEEDS*1..4]->(d:System)
RETURN d.name, length(path) AS hops, [n IN nodes(path) | n.name] AS via
```

Ask for depth 1 and a lab outage returns 2 systems, the obvious ones. Ask for depth 4 and it returns 7, including systems three feeds away that nobody listed by hand. And `nodes(path)` is the explanation, so every answer shows the route it took.

[Screenshot: impact path table]

One gotcha: hospital feeds are cyclic (orders go out, results come back), so a naive traversal reports the failing system as its own victim. The tool drops revisits and keeps the shortest route per system.

## The agent never writes Cypher

The obvious design lets the model generate queries. It demos well and fails three ways: queries that are subtly wrong, queries that are expensive, and no way to explain a query the model has forgotten.

Instead, the agent chooses from eight fixed tools, each wrapping one human-written, parameterised query. Arguments are typed and clamped, and system names are resolved against the graph before anything runs. If you ask about `payments-api` and the graph says `lab-lis`, no query runs at all and the agent says so. The same eight tools are exposed over MCP.

## It says "not in the graph", never "safe"

An empty traversal means the graph doesn't know about a dependency. During an incident that is a different and more worrying statement than "nothing breaks". Every answer therefore lands in one of five outcomes: answered with path, answered from memory, not in graph, unknown system, or need a system name.

A wording guard backs this up: if the language model's summary names a system that isn't in the returned rows, the sentence is thrown away and a template built straight from the rows is used instead.

## Algorithms as tools

FalkorDB ships graph algorithms as procedures, so the agent gets them with three lines each:

```cypher
CALL algo.SPpaths({sourceNode: a, targetNode: b, relTypes: ['FEEDS'], relDirection: 'outgoing', weightProp: 'weight', pathCount: 1})
CALL algo.pageRank('System', 'FEEDS') YIELD node, score
```

Shortest path answers "why is registration connected to the patient portal?" with the actual channels. PageRank finds the hub systems that many feeds converge on.

## Memory is just more nodes

When the agent concludes something, it writes a `Finding` node linked to the system. The next session reads those findings before it reasons, and returns when they were learned. Each hospital is its own graph (`caregraph:<name>`), so isolation is structural rather than a WHERE clause somebody can forget.

## What I'd do next

Ingest real HL7 channel metadata from an integration engine's API (with proper de-identification), model message-level dependencies such as patient matching, and let the agent act, for example opening an incident ticket with the path attached.

## Try it

Repo: [link] · Demo: [link]

*Built with Claude as a coding assistant; I reviewed the design and can explain every query. Pattern credit: the organisers' tutorial on agents that reason over a graph.*
