#!/usr/bin/env python3
"""Offline test suite for wfgen — no live n8n, no network required.

Run: python wfgen/test_wfgen.py   (exit 0 = all pass)
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WFDATA = HERE / "templates"
sys.path.insert(0, str(HERE))
import wfgen  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'} — {name}" + (f" · {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# T1: catalog has the 5 MVP templates, each with required params declared
names = [n for n, _, _ in wfgen.list_templates()]
expected = {"discord-digest", "rss-to-channel", "keyword-miner-schedule",
            "lead-enrich-drop", "backup-verify-report"}
check("T1 five MVP templates present", expected <= set(names), f"got {names}")

# T2: compile with full params → no unresolved placeholders
wf = wfgen.compile_workflow("discord-digest", {
    "schedule_cron": "0 9 * * 1",
    "discord_webhook_url": "https://discord.com/api/webhooks/x/y",
    "source_path": "/tmp/leads.jsonl",
})
check("T2 compile substitutes params", "$param:" not in json.dumps(wf))

# T3: compile missing required param → SystemExit
try:
    wfgen.compile_workflow("discord-digest", {"schedule_cron": "0 9 * * 1"})
    check("T3 missing-param rejection", False)
except SystemExit:
    check("T3 missing-param rejection", True)

# T4: validator catches unresolved $param in a corrupted file
bad = json.loads((WFDATA / "discord-digest.json").read_text())
check("T4 validate rejects unresolved params",
      any(f["class"] == "unresolved-param" for f in wfgen.validate_workflow(
          {k: v for k, v in bad.items() if k != "x-wfgen"})))

# T5: validator flags L2 (active + no trigger) on a crafted workflow
no_trig = {"name": "x", "active": True,
           "nodes": [{"name": "A", "type": "n8n-nodes-base.code", "parameters": {}}],
           "connections": {}}
check("T5 L2 active-no-trigger caught",
      any(f["class"] == "L2-active-no-trigger" for f in wfgen.validate_workflow(no_trig)))

# T6: validator flags L3 duplicate node names
dup = {"name": "y", "active": False,
       "nodes": [{"name": "N", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}},
                 {"name": "N", "type": "n8n-nodes-base.code", "parameters": {}}],
       "connections": {"N": {"main": [[{"node": "N", "type": "main", "index": 0}]]}}}
check("T6 L3 duplicate-names caught",
      any(f["class"] == "L3-duplicate-node-names" for f in wfgen.validate_workflow(dup)))

# T7: every template compiles clean when its declared required params are filled
fills = {
    "discord-digest": {"schedule_cron": "0 9 * * 1",
                       "discord_webhook_url": "https://discord/hooks/a",
                       "source_path": "C:/data/leads.jsonl"},
    "rss-to-channel": {"schedule_cron": "*/30 * * * *",
                       "rss_url": "https://example.com/feed.xml",
                       "discord_webhook_url": "https://discord/hooks/b"},
    "keyword-miner-schedule": {"schedule_cron": "0 8 * * *",
                               "corpus_path": "C:/data/corpus.txt",
                               "keywords": "lead,digest",
                               "discord_webhook_url": "https://discord/hooks/c"},
    "lead-enrich-drop": {"ledger_path": "C:/data/ledger.csv"},
    "backup-verify-report": {"schedule_cron": "0 6 * * *",
                             "watch_path": "C:/backups",
                             "max_age_hours": "26",
                             "discord_webhook_url": "https://discord/hooks/d"},
}
for tpl, fill in fills.items():
    w = wfgen.compile_workflow(tpl, fill)
    fs = [f for f in wfgen.validate_workflow(w) if f["level"] == "ERROR"]
    check(f"T7 {tpl} compiles+validates clean", not fs, json.dumps(fs))

# T8: install dry-run refuses a corrupted file (exit non-zero, no n8n call)
corrupt = dict(no_trig)
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump(corrupt, fh); cpath = fh.name
r = subprocess.run([sys.executable, str(HERE / "wfgen.py"), "install", cpath],
                   capture_output=True, text=True)
check("T8 install refuses invalid workflow", r.returncode != 0 and "dry-run" not in r.stdout)

# T9: install dry-run accepts a valid compiled file without touching n8n
with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
    json.dump(wf, fh); vpath = fh.name
r = subprocess.run([sys.executable, str(HERE / "wfgen.py"), "install", vpath],
                   capture_output=True, text=True)
check("T9 install dry-run passes valid workflow",
      r.returncode == 0 and ("dry-run" in r.stdout.lower() or "--go" in r.stdout))

print()
if FAILS:
    print(f"FAILED ({len(FAILS)}): {', '.join(FAILS)}")
    sys.exit(1)
print("ALL TESTS PASS")
