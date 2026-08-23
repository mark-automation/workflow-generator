"""wfgen Wk1 offline tests — no live n8n required.

Run: python -m pytest den/tools/wfgen/tests -v
"""
import json
import subprocess
import sys
from pathlib import Path

WFGEN_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WFGEN_DIR))
import wfgen  # noqa: E402


# ------------------------------------------------------------ catalog ------

def test_five_templates_load():
    templates = wfgen.load_templates()
    assert len(templates) == 5, f"expected 5 templates, got {sorted(templates)}"


def test_every_template_has_contract():
    for slug, tpl in wfgen.load_templates().items():
        spec = wfgen.template_params(tpl)
        assert "description" in tpl.get("x-wfgen", {}), slug
        assert isinstance(spec, dict) and spec, f"{slug}: no params declared"
        for key, meta in spec.items():
            assert key.replace("_", "").isalnum() and key.isupper(), f"{slug}:{key}"
            if meta.get("required"):
                assert "default" not in meta, \
                    f"{slug}:{key} is required but carries a default"


def test_templates_have_trigger_and_connections():
    hints = ("trigger", "webhook")
    for slug, tpl in wfgen.load_templates().items():
        types = [n["type"].lower() for n in tpl["nodes"]]
        assert any(any(h in t for h in hints) for t in types), f"{slug}: no trigger"
        names = [n["name"] for n in tpl["nodes"]]
        conns = tpl.get("connections", {})
        targets = {e["node"] for outs in conns.values()
                   if isinstance(outs, dict)
                   for br in outs.values() for slot in br for e in slot or []}
        assert set(conns) <= set(names), f"{slug}: connection source not a node"
        # every node except the entry point must be reachable from another node
        assert targets | {names[0]} >= set(names), f"{slug}: disconnected node"


# ------------------------------------------------------------- compile -----

def test_compile_resolves_all_placeholders():
    tpl = wfgen.load_templates()["discord-digest"]
    params = {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/X/Y"}
    wf = wfgen.compile_workflow(tpl, params)
    assert wfgen.find_unresolved(wf) == []
    assert "$wfgen." not in json.dumps(wf)


def test_compile_rejects_missing_required_param():
    tpl = wfgen.load_templates()["discord-digest"]
    try:
        wfgen.compile_workflow(tpl, {})
    except ValueError as exc:
        assert "DISCORD_WEBHOOK_URL" in str(exc)
    else:
        raise AssertionError("compile accepted a missing required param")


def test_compile_rejects_unknown_param():
    tpl = wfgen.load_templates()["rss-to-channel"]
    try:
        wfgen.compile_workflow(tpl, {"NOT_A_PARAM": "x",
                                     "RSS_URL": "https://r", "DISCORD_WEBHOOK_URL": "w"})
    except ValueError as exc:
        assert "NOT_A_PARAM" in str(exc)
    else:
        raise AssertionError("compile accepted an unknown param")


def test_compile_defaults_applied():
    tpl = wfgen.load_templates()["keyword-miner-schedule"]
    params = {"SOURCE_URL": "https://src.example/json",
              "DISCORD_WEBHOOK_URL": "https://d"}
    wf = wfgen.compile_workflow(tpl, params)
    sched = wf["nodes"][0]["parameters"]["rule"]["interval"][0]
    assert sched["field"] == "days"          # from default
    assert wf["active"] is False             # installer law: always inactive


def test_compile_name_override():
    tpl = wfgen.load_templates()["backup-verify-report"]
    wf = wfgen.compile_workflow(
        tpl, {"TARGET_URL": "https://t", "DISCORD_WEBHOOK_URL": "https://d"},
        name="Nightly DR Check")
    assert wf["name"] == "Nightly DR Check"


# ------------------------------------------------------------ validate -----

def _write(tmp_path, payload):
    p = tmp_path / "wf.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


GOOD_WF = {
    "name": "ok",
    "nodes": [
        {"name": "Webhook", "type": "n8n-nodes-base.webhook", "parameters": {}},
        {"name": "Do Thing", "type": "n8n-nodes-base.code", "parameters": {}},
    ],
    "connections": {"Webhook": {"main": [[{"node": "Do Thing", "type": "main",
                                           "index": 0}]]}},
}


def test_validate_passes_clean_file(tmp_path):
    findings, ok = wfgen.lint_file(_write(tmp_path, GOOD_WF))
    errors = [f for f in findings if f["severity"] == "error"]
    assert ok and not errors, findings


def test_validate_fails_unparseable_json(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    findings, ok = wfgen.lint_file(p)
    assert not ok and findings[0]["rule"] == "L0-unparseable"


def test_validate_fails_no_trigger(tmp_path):
    bad = {"nodes": [{"name": "A", "type": "n8n-nodes-base.code"},
                     {"name": "B", "type": "n8n-nodes-base.set"}],
           "connections": {"A": {"main": [[{"node": "B", "type": "main",
                                            "index": 0}]]}}}
    findings, _ = wfgen.lint_file(_write(tmp_path, bad))
    assert any(f["rule"] == "L2-no-trigger" and f["severity"] == "error"
               for f in findings)


def test_validate_fails_duplicate_names(tmp_path):
    bad = {"nodes": [{"name": "Dup", "type": "n8n-nodes-base.webhook"},
                     {"name": "Dup", "type": "n8n-nodes-base.code"}],
           "connections": {}}
    findings, _ = wfgen.lint_file(_write(tmp_path, bad))
    assert any(f["rule"] == "L3-duplicate-node-names" and f["severity"] == "error"
               for f in findings)


def test_validate_warns_orphan_node(tmp_path):
    bad = {"nodes": [{"name": "Webhook", "type": "n8n-nodes-base.webhook"},
                     {"name": "Lost Node", "type": "n8n-nodes-base.code"}],
           "connections": {}}
    findings, _ = wfgen.lint_file(_write(tmp_path, bad))
    assert any(f["rule"] == "L4-orphan-node" for f in findings)


def test_validate_flags_unresolved_placeholder(tmp_path):
    bad = dict(GOOD_WF)
    bad = json.loads(json.dumps(bad))
    bad["nodes"][1]["parameters"] = {"url": "{{ $wfgen.DISCORD_WEBHOOK_URL }}"}
    findings, _ = wfgen.lint_file(_write(tmp_path, bad))
    assert any(f["rule"] == "P-unresolved-placeholder" and f["severity"] == "error"
               for f in findings)


def test_validate_not_a_workflow_shape(tmp_path):
    findings, ok = wfgen.lint_file(_write(tmp_path, {"foo": 1}))
    assert not ok and any(f["rule"] == "P-shape" for f in findings)


# -------------------------------------------------- compiled artifacts pass -

def test_every_compiled_template_validates_clean(tmp_path, ):
    for slug, tpl in wfgen.load_templates().items():
        req = {k: f"https://placeholder/{k.lower()}"
               for k, v in wfgen.template_params(tpl).items() if v.get("required")}
        wf = wfgen.compile_workflow(tpl, req)
        p = _write(tmp_path, wf)
        findings, ok = wfgen.lint_file(p)
        errors = [f for f in findings if f["severity"] == "error"]
        assert ok and not errors, f"{slug}: {findings}"


# ----------------------------------------------------------------- CLI ------

def run_cli(*argv):
    return subprocess.run(
        [sys.executable, str(WFGEN_DIR / "wfgen.py"), *argv],
        capture_output=True, text=True)


def test_cli_list_and_show():
    r = run_cli("list")
    assert r.returncode == 0 and "discord-digest" in r.stdout
    r = run_cli("show", "lead-enrich-drop")
    assert r.returncode == 0 and "CRM_ENDPOINT_URL" in r.stdout


def test_cli_compile_stdout():
    r = run_cli("compile", "rss-to-channel",
                "--param", "RSS_URL=https://f",
                "--param", "DISCORD_WEBHOOK_URL=https://d")
    assert r.returncode == 0
    wf = json.loads(r.stdout)
    assert wfgen.find_unresolved(wf) == []


def test_cli_compile_out_flag(tmp_path):
    out = tmp_path / "out.json"
    r = run_cli("compile", "discord-digest",
                "--param", "DISCORD_WEBHOOK_URL=https://d",
                "-o", str(out))
    assert r.returncode == 0 and out.exists()
    assert wfgen.lint_file(out)[1]


def test_cli_compile_rejects_missing_required():
    r = run_cli("compile", "lead-enrich-drop")
    assert r.returncode == 1 and "missing required param" in r.stderr


def test_cli_validate_exit_codes(tmp_path):
    good = _write(tmp_path, GOOD_WF)
    assert run_cli("validate", str(good)).returncode == 0
    bad = tmp_path / "b.json"
    bad.write_text("{oops", encoding="utf-8")
    r = run_cli("validate", str(bad))
    assert r.returncode == 1 and "FAIL" in r.stdout
