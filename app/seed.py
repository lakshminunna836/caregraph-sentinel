"""Seed SYNTHETIC hospital integration graphs (no real patient or customer data).

Two tenants, two isolated graphs, deliberately different shapes:
  stmary    - full network (11 systems, HIE + portal + bed management)
  riverside - small community hospital (no HIE gateway / portal / bed mgmt)

Run:  python -m app.seed            (creates missing tenants)
      python -m app.seed --reset    (drops and rebuilds, wiping agent memory)
"""
import sys
import time

from . import db

HOUR, DAY = 3600, 86400

# (name, kind, criticality 1-5)
SYSTEMS = [
    ("registration", "ADT / patient access", 5),
    ("ehr-core", "Electronic health record", 5),
    ("lab-lis", "Laboratory information system", 4),
    ("radiology-ris", "Radiology information system", 3),
    ("pharmacy-rx", "Pharmacy system", 4),
    ("billing-rcm", "Billing / revenue cycle", 2),
    ("hie-gateway", "Health information exchange gateway", 3),
    ("patient-portal", "Patient portal", 2),
    ("er-tracking", "Emergency dept tracking board", 4),
    ("bed-mgmt", "Bed management", 3),
    ("data-warehouse", "Analytics warehouse", 1),
]

# (source, target, interface channel, HL7 v2 message type)
FEEDS = [
    ("registration", "ehr-core", "ADT_REG_TO_EHR", "ADT^A01"),
    ("registration", "lab-lis", "ADT_REG_TO_LIS", "ADT^A01"),
    ("registration", "radiology-ris", "ADT_REG_TO_RIS", "ADT^A04"),
    ("registration", "billing-rcm", "ADT_REG_TO_BILL", "ADT^A08"),
    ("registration", "bed-mgmt", "ADT_REG_TO_BED", "ADT^A03"),
    ("bed-mgmt", "er-tracking", "ADT_BED_TO_ER", "ADT^A02"),
    ("ehr-core", "lab-lis", "ORM_EHR_TO_LIS", "ORM^O01"),
    ("ehr-core", "radiology-ris", "ORM_EHR_TO_RIS", "ORM^O01"),
    ("ehr-core", "pharmacy-rx", "RDE_EHR_TO_RX", "RDE^O11"),
    ("ehr-core", "billing-rcm", "DFT_EHR_TO_BILL", "DFT^P03"),
    ("ehr-core", "hie-gateway", "CCD_EHR_TO_HIE", "MDM^T02"),
    ("ehr-core", "patient-portal", "ORU_EHR_TO_PORTAL", "ORU^R01"),
    ("ehr-core", "data-warehouse", "ADT_EHR_TO_DW", "ADT^A08"),
    ("lab-lis", "ehr-core", "ORU_LIS_TO_EHR", "ORU^R01"),
    ("lab-lis", "hie-gateway", "ORU_LIS_TO_HIE", "ORU^R01"),
    ("radiology-ris", "ehr-core", "ORU_RIS_TO_EHR", "ORU^R01"),
    ("pharmacy-rx", "ehr-core", "RDS_RX_TO_EHR", "RDS^O13"),
]

# (clinical workflow, severity 1-5, systems it relies on)
WORKFLOWS = [
    ("Patient registration & identification", 5, ["registration"]),
    ("Medication ordering & dispensing", 5, ["ehr-core", "pharmacy-rx"]),
    ("Lab result delivery to clinicians", 5, ["lab-lis", "ehr-core"]),
    ("Radiology report delivery", 4, ["radiology-ris", "ehr-core"]),
    ("ED patient tracking", 4, ["er-tracking", "bed-mgmt"]),
    ("Referral & HIE exchange", 3, ["hie-gateway", "ehr-core"]),
    ("Claims & billing", 2, ["billing-rcm"]),
    ("Patient portal result release", 2, ["patient-portal"]),
]

TEAMS = {
    "lab-integration": ["lab-lis", "radiology-ris"],
    "clinical-apps": ["ehr-core", "pharmacy-rx", "er-tracking", "bed-mgmt"],
    "hie-team": ["hie-gateway", "patient-portal", "data-warehouse"],
    "revenue-cycle": ["billing-rcm", "registration"],
}

# synthetic people, per tenant: team -> (name, role)
PEOPLE = {
    "stmary": {
        "lab-integration": ("Asha Rao", "primary on-call"),
        "clinical-apps": ("Vikram Nair", "primary on-call"),
        "hie-team": ("Meera Iyer", "primary on-call"),
        "revenue-cycle": ("Rohan Das", "primary on-call"),
    },
    "riverside": {
        "lab-integration": ("Dana Whitfield", "primary on-call"),
        "clinical-apps": ("Tom Okafor", "primary on-call"),
        "revenue-cycle": ("Priya Menon", "primary on-call"),
    },
}

# (id, hours ago, author, summary, system)
CHANGES = [
    ("CHG-1042", 3, "a.rao", "Updated OBX-5 mapping for new LOINC codes on ORU channel", "lab-lis"),
    ("CHG-1039", 2, "m.iyer", "Rotated TLS certificate on HIE endpoint", "hie-gateway"),
    ("CHG-1031", 30, "v.nair", "Upgraded ORM channel from HL7 v2.3 to v2.5.1", "ehr-core"),
    ("CHG-1027", 52, "r.das", "Changed PID-3 identifier assigning authority", "registration"),
    ("CHG-1019", 120, "v.nair", "Added dose-unit validation on RDE channel", "pharmacy-rx"),
]

# (system, days ago, text) - memory the agent already holds from earlier sessions
FINDINGS = [
    ("lab-lis", 12, "Past outage: expired certificate on the MLLP listener. A listener restart fixed it; renewal added to monthly checklist."),
    ("ehr-core", 20, "ADT^A08 floods after registration merges can back up the queue; throttle the DW feed first."),
]

TENANTS = {
    "stmary": {"drop": set()},
    "riverside": {"drop": {"hie-gateway", "patient-portal", "bed-mgmt", "data-warehouse"}},
}


def build(tenant: str, now: int | None = None):
    now = now or int(time.time())
    drop = TENANTS[tenant]["drop"]
    systems = [s for s in SYSTEMS if s[0] not in drop]
    names = {s[0] for s in systems}
    g = db.graph(tenant)
    try:
        g.delete()
    except Exception:
        pass
    g = db.graph(tenant)
    g.query("CREATE INDEX FOR (s:System) ON (s.name)")

    g.query(
        "UNWIND $rows AS r CREATE (:System {name: r[0], kind: r[1], criticality: r[2]})",
        {"rows": [list(s) for s in systems]},
    )
    g.query(
        """UNWIND $rows AS r
           MATCH (a:System {name: r[0]}), (b:System {name: r[1]})
           CREATE (a)-[:FEEDS {interface: r[2], msg: r[3], weight: 1}]->(b)""",
        {"rows": [list(f) for f in FEEDS if f[0] in names and f[1] in names]},
    )
    for wf, sev, reliant in WORKFLOWS:
        present = [s for s in reliant if s in names]
        if not present:
            continue
        g.query(
            """CREATE (w:Workflow {name: $wf, severity: $sev})
               WITH w UNWIND $systems AS sname
               MATCH (s:System {name: sname}) CREATE (w)-[:RELIES_ON]->(s)""",
        {"wf": wf, "sev": sev, "systems": present},
        )
    for team, owned in TEAMS.items():
        owned = [s for s in owned if s in names]
        if not owned:
            continue
        g.query(
            """CREATE (t:Team {name: $team})
               WITH t UNWIND $systems AS sname
               MATCH (s:System {name: sname}) CREATE (t)-[:OWNS]->(s)""",
            {"team": team, "systems": owned},
        )
        if team in PEOPLE[tenant]:
            person, role = PEOPLE[tenant][team]
            g.query(
                """MATCH (t:Team {name: $team})
                   CREATE (:Person {name: $person, role: $role})-[:ONCALL_FOR {from: $f, to: $t}]->(t)""",
                {"team": team, "person": person, "role": role, "f": now - 3 * DAY, "t": now + 4 * DAY},
            )
    for cid, hrs, author, summary, system in CHANGES:
        if system in names:
            g.query(
                """MATCH (s:System {name: $sys})
                   CREATE (:Change {id: $id, at: $at, author: $author, summary: $summary})-[:TOUCHED]->(s)""",
                {"sys": system, "id": cid, "at": now - hrs * HOUR, "author": author, "summary": summary},
            )
    for system, days, text in FINDINGS:
        if system in names:
            g.query(
                """MATCH (s:System {name: $sys})
                   CREATE (:Finding {text: $text, at: $at, session: 'seed', author: 'sentinel'})-[:ABOUT]->(s)""",
                {"sys": system, "text": text, "at": now - days * DAY},
            )
    return g


def seed(reset: bool = False):
    existing = set(db.tenants())
    for tenant in TENANTS:
        if reset or tenant not in existing:
            build(tenant)
            print(f"seeded tenant '{tenant}'")
        else:
            print(f"tenant '{tenant}' already exists (use --reset to rebuild)")


if __name__ == "__main__":
    seed(reset="--reset" in sys.argv)
