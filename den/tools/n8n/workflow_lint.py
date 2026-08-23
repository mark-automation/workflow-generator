#!/usr/bin/env python3
"""n8n workflow linter — static bug-class scanner (read-only, keyless).

Reads ~/.n8n/database.sqlite in mode=ro and flags common n8n bug classes
that only manifest at runtime (so exec-failure digests see them too late):

  L1 raw-html-email-extract : Code node extracts emails but never strips HTML
                              tags first -> fragmented addresses like
                              "user@ domain" split by markup. THE live B2B
                              AI Leads Automation bug (2026-08-22).
  L2 active-no-trigger      : workflow is ACTIVE but has no trigger node
                              (will never fire; usually a broken import).
  L3 duplicate-node-names   : two nodes share a name inside one workflow
                              (connection graph silently corrupts).
  L4 orphan-node            : non-trigger node with no incoming AND no
                              outgoing connections (dead code / lost node).
  L5 http-no-error-handling : HTTP Request node without onError set on an
                              ACTIVE workflow (one 5xx kills the whole run).

Output: den/reports/n8n-lint.json + .md. Exit 0 = scan ran (check report);
exit 2 = DB missing.

Usage: python den/tools/n8n/workflow_lint.py [--json]
"""
import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

DB = Path.home() / ".n8n" / "database.sqlite"
OUT_DIR = Path(__file__).resolve().parents[2] / "reports"

EMAIL_RE_ASSIGN = re.compile(
    r"""(match(?:es)?|\.match|exec|test)\s*\(\s*[/'"].{0,120}[@].{0,60}[/'"]""",
    re.I,
)
HTML_STRIP_HINTS = ("replace(/<[^>]*>", "replace(/\\<[^>]*>", "striptags",
                    "strip_tags", "cheerio", "text()", "innerText",
                    "textContent", "htmlToText", "html_to_text")
TRIGGER_HINTS = ("trigger", "webhook", "schedule", "cron", "interval",
                 "chatTrigger", "n8n-nodes-base.formTrigger")
STICKY = "sticky"


def load_workflows():
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, name, active, nodes, connections FROM workflow_entity"
    ).fetchall()
    con.close()
    return rows


def load_workflows_from_files(paths):
    """--file mode: wrap exported workflow JSON files as lint rows (pre-import).

    Files are treated as INACTIVE imports (import resets the active flag anyway),
    so L2 only fires via wfgen's own stricter no-trigger check.
    """
    rows = []
    for p in paths:
        path = Path(p)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARN: skipping {path}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"WARN: skipping {path}: not an object", file=sys.stderr)
            continue
        nodes = data.get("nodes")
        rows.append({
            "id": str(path),
            "name": data.get("name") or path.stem,
            "active": 0,
            "nodes": json.dumps(nodes) if nodes is not None else "[]",
            "connections": json.dumps(data.get("connections") or {}),
        })
    return rows


def lint_workflow(row):
    """Return list of finding dicts for one workflow row."""
    findings = []
    try:
        nodes = json.loads(row["nodes"] or "[]")
        conns = json.loads(row["connections"] or "{}")
    except json.JSONDecodeError:
        findings.append({
            "rule": "L0-unparseable", "severity": "error", "node": "-",
            "detail": "nodes/connections JSON failed to parse; export/import may be corrupt",
        })
        nodes, conns = [], {}
    if not isinstance(nodes, list):
        nodes = []

    real = [n for n in nodes if isinstance(n, dict)
            and STICKY not in str(n.get("type", "")).lower()]

    names = [str(n.get("name")) for n in real]
    for name, cnt in Counter(names).items():
        if cnt > 1:
            findings.append({
                "rule": "L3-duplicate-node-names", "severity": "error",
                "node": name, "detail": f"{cnt} nodes share this name",
            })

    # connection degree map (source side + target side)
    linked = set()
    if isinstance(conns, dict):
        for src, outs in conns.items():
            linked.add(src)
            for branch in (outs or {}).values() if isinstance(outs, dict) else []:
                if isinstance(branch, list):
                    for slot in branch:
                        for edge in slot or []:
                            if isinstance(edge, dict) and edge.get("node"):
                                linked.add(edge["node"])

    has_trigger = False
    for n in real:
        low = str(n.get("type", "")).lower()
        nm = str(n.get("name", "?"))
        if any(h.lower() in low for h in TRIGGER_HINTS):
            has_trigger = True

        # L1: code node pulling emails out of what may be raw HTML
        if "code" in low:
            js = ""
            params = n.get("parameters") or {}
            for v in (params.get("jsCode"), params.get("code"),
                      params.get("pythonCode")):
                if isinstance(v, str) and len(v) > len(js):
                    js = v
            if js:
                looks_email = (
                    EMAIL_RE_ASSIGN.search(js)
                    or "@" in js and ("mail" in js.lower())
                )
                strips_html = any(h in js for h in HTML_STRIP_HINTS)
                if looks_email and not strips_html:
                    findings.append({
                        "rule": "L1-raw-html-email-extract", "severity": "error",
                        "node": nm,
                        "detail": "email extraction with no HTML strip "
                                  "(regex over markup fragments addresses)",
                    })

        # L4: orphan node
        if not any(h.lower() in low for h in TRIGGER_HINTS):
            indeg = sum(
                1 for src, outs in conns.items() if isinstance(outs, dict)
                for branch in outs.values()
                if isinstance(branch, list)
                for slot in branch
                for edge in slot or []
                if isinstance(edge, dict) and edge.get("node") == nm
            ) if isinstance(conns, dict) else 0
            outdeg = 1 if nm in linked and indeg == 0 else (1 if nm in linked else 0)
            if indeg == 0 and outdeg == 0 and len(real) > 1:
                findings.append({
                    "rule": "L4-orphan-node", "severity": "warn",
                    "node": nm,
                    "detail": "no incoming and no outgoing connections",
                })

        # L5: HTTP request nodes on active workflows without error handling
        if "httprequest" in low.replace(" ", ""):
            if n.get("onError") in (None, "", "stopWorkflow"):
                findings.append({
                    "rule": "L5-http-no-error-handling", "severity": "info",
                    "node": nm,
                    "detail": "onError unset/stopWorkflow: one upstream 5xx aborts the run",
                })

    active = bool(row["active"])
    if active and not has_trigger:
        findings.append({
            "rule": "L2-active-no-trigger", "severity": "error", "node": "-",
            "detail": "workflow marked active but contains no trigger node",
        })
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="print full JSON")
    ap.add_argument("--file", nargs="*", metavar="PATH",
                    help="lint exported workflow JSON file(s) pre-import "
                         "instead of the live n8n DB")
    args = ap.parse_args()

    if args.file is not None:
        wf_rows = load_workflows_from_files(args.file)
        if not wf_rows:
            print("FATAL: no parseable workflow files given", file=sys.stderr)
            return 2
        source = "files (--file mode)"
    else:
        if not DB.exists():
            print(f"FATAL: {DB} not found", file=sys.stderr)
            return 2
        wf_rows = load_workflows()
        source = str(DB)
    results = []
    sev_count = Counter()
    for row in wf_rows:
        fs = lint_workflow(row)
        for f in fs:
            sev_count[f["severity"]] += 1
        if fs:
            results.append({
                "id": row["id"], "name": row["name"],
                "active": bool(row["active"]), "findings": fs,
            })

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db": source,
        "totals": {},
        "summary": {"errors": sev_count["error"], "warnings": sev_count["warn"],
                    "infos": sev_count["info"],
                    "workflows_with_findings": len(results)},
        "workflows": sorted(results, key=lambda w: (not w["active"], w["name"].lower())),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    jp = OUT_DIR / "n8n-lint.json"
    mp = OUT_DIR / "n8n-lint.md"

    lines = [
        "# n8n Workflow Lint",
        f"_Generated {result['generated_at']} — static scan, read-only sqlite_",
        "",
        f"Errors **{sev_count['error']}** · Warnings **{sev_count['warn']}** · "
        f"Info **{sev_count['info']}** across {len(results)} workflow(s) with findings",
        "",
    ]
    for w in result["workflows"]:
        flag = "🔴" if w["active"] else "⚪"
        lines.append(f"- {flag} **{w['name']}** `{w['id']}`")
        for f in w["findings"]:
            icon = {"error": "❌", "warn": "⚠️", "info": "ℹ️"}[f["severity"]]
            lines.append(f"  - {icon} {f['rule']} @ {f['node']}: {f['detail']}")
    mp.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # rewrite JSON cleanly now that totals are known
    result["totals"] = {
        "workflows_scanned": len(wf_rows),
        "rules": ["L0-unparseable", "L1-raw-html-email-extract",
                  "L2-active-no-trigger", "L3-duplicate-node-names",
                  "L4-orphan-node", "L5-http-no-error-handling"],
    }
    jp.write_text(json.dumps(result, indent=2), encoding="utf-8")

    print(f"scanned={len(wf_rows)} errors={sev_count['error']} "
          f"warnings={sev_count['warn']} infos={sev_count['info']} "
          f"workflows_with_findings={len(results)}")
    print(f"wrote {jp}\nwrote {mp}")
    if args.json:
        print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
