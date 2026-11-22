"""End-to-end Agent Cloud demo: three agents, one shared pipeline, versioning, approval and rollback.

Run a server first:   agentcloud --data ./data          (or: python -m agentcloud_server)
Then:                 python examples/research_pipeline.py [--base-url http://127.0.0.1:8787]

Everything goes through the public HTTP API with the Python SDK, exactly as a developer's
agents would use it. No storage accounts are configured anywhere.
"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
from agentcloud import AgentCloud, Pending  # noqa: E402


def step(n: int, text: str) -> None:
    print(f"\n{n}. {text}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8787")
    ap.add_argument("--email", default=f"demo-{uuid.uuid4().hex[:6]}@example.com")
    ap.add_argument("--password", default="demo-password-123")
    a = ap.parse_args()

    step(1, f"A developer signs up ({a.email})")
    dev = AgentCloud.signup(a.email, a.password, base_url=a.base_url)

    step(2, "Provision three agents, each with its own workspace, storage and credentials")
    research = dev.provision_agent("research-agent", storage_mb=512, access="full",
                                   description="Collects market information")
    analysis = dev.provision_agent("analysis-agent", storage_mb=512, access="read_write",
                                   description="Turns raw data into findings")
    reporting = dev.provision_agent("reporting-agent", storage_mb=256, access="read_write", approval="overwrite",
                                    description="Builds the presentation (changes need approval)")
    for r in (research, analysis, reporting):
        print(f"   {r['agent']['name']:<16} agent={r['agent']['id']}  workspace={r['workspace']['id']}  token={r['token']['prefix']}…")

    # Each agent connects with nothing but its own token.
    ra = AgentCloud(research["token"]["token"], base_url=a.base_url)
    na = AgentCloud(analysis["token"]["token"], base_url=a.base_url)
    pa = AgentCloud(reporting["token"]["token"], base_url=a.base_url)
    r_ws = ra.workspace(research["workspace"]["id"])
    n_ws = na.workspace(analysis["workspace"]["id"])
    p_ws = pa.workspace(reporting["workspace"]["id"])

    step(3, "The research agent writes a spreadsheet into its own workspace")
    v = r_ws.write("market/competitors.csv",
                   "company,segment,arr_musd,growth\nAcme,smb,12,0.41\nGlobex,mid,48,0.22\nInitech,ent,130,0.09\n",
                   message="first pull")
    print(f"   market/competitors.csv v{v.version}")

    step(4, "It shares that one file with the analysis agent (read-only)")
    r_ws.grant(analysis["agent"]["id"], "market/competitors.csv", read=True)
    rows = na.workspace(research["workspace"]["id"]).read_text("market/competitors.csv").strip().splitlines()
    print(f"   analysis-agent reads {len(rows) - 1} rows from research-agent's workspace")
    try:
        na.workspace(research["workspace"]["id"]).write("market/competitors.csv", "oops")
    except Exception as e:  # read-only share
        print(f"   …and can't change it: {type(e).__name__}")

    step(5, "The analysis agent writes a report in its own workspace and shares it on")
    n_ws.write("reports/q3-findings.md", "# Q3 findings\n\n- Acme grows fastest (41%)\n- Initech is largest but slowing\n")
    dev.workspace(analysis["workspace"]["id"]).grant(reporting["agent"]["id"], "reports/", read=True)
    findings = pa.workspace(analysis["workspace"]["id"]).read_text("reports/q3-findings.md")
    print(f"   reporting-agent can read the report ({len(findings)} chars)")

    step(6, "The reporting agent drafts slides; editing them later needs human approval")
    p_ws.write("deck/q3.md", "# Q3 market\n\n" + findings)
    change = p_ws.write("deck/q3.md", "# Q3 market (final)\n\n" + findings + "\nRecommendation: double down on SMB.\n",
                        message="add recommendation")
    assert isinstance(change, Pending)
    print(f"   proposed change is pending: {change.proposal_id}")
    detail = dev.proposal(change.proposal_id)
    print(f"   human reviews the diff: +{detail['diff']['added']} / -{detail['diff']['removed']} lines")
    dev.approve(change.proposal_id, note="looks good")
    print(f"   approved → deck/q3.md is now v{p_ws.stat('deck/q3.md')['version']}")

    step(7, "A mistake: the research agent overwrites its data and drops two columns")
    r_ws.write("market/competitors.csv", "company,segment\nAcme,smb\n", message="cleanup")
    d = r_ws.diff("market/competitors.csv", 1)
    print(f"   diff v1→v2: columns removed {d['csv']['columns_removed']}, rows {d['csv']['rows_before']}→{d['csv']['rows_after']}")
    r_ws.restore("market/competitors.csv", 1, message="undo bad cleanup")
    print(f"   restored v1 as v{r_ws.stat('market/competitors.csv')['version']}; history: "
          + ", ".join(f"v{x['n']} {x['op']}" for x in reversed(r_ws.versions('market/competitors.csv'))))

    step(8, "The agent's environment shuts down; a fresh process reconnects with the same token")
    del ra, r_ws
    again = AgentCloud(research["token"]["token"], base_url=a.base_url).workspace(research["workspace"]["id"])
    print("   files still there:", [e["path"] for e in again.list(recursive=True) if e["kind"] == "file"])

    step(9, "The developer revokes the analysis agent's token")
    dev.revoke_agent_token(analysis["agent"]["id"], analysis["token"]["id"])
    try:
        na.workspace(analysis["workspace"]["id"]).list()
        print("   ✗ token still works")
    except Exception as e:
        print(f"   token rejected: {type(e).__name__}")

    print(f"\nDone. Open {a.base_url} and sign in as {a.email} / {a.password} to see it all in the dashboard.")


if __name__ == "__main__":
    main()
