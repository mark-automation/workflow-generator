#!/usr/bin/env python3
"""wfgen.py — workflow generator CLI (v0.1).

Pipeline: brief -> template+params (LLM via OpenRouter ox-alpha, or explicit)
        -> compile (substitute $param:* into template)
        -> validate (placeholders + L2/L3/L4 static checks on the FILE)
        -> install via n8n CLI (INACTIVE by default; activate is separate)

Usage:
  python wfgen.py list
  python wfgen.py show <template>
  python wfgen.py compile <template> --set key=value [--set k=v ...] -o out.json
  python wfgen.py map "weekly lead digest to Discord Mondays" [-o mapping.json]
  python wfgen.py validate <workflow.json>
  python wfgen.py install <workflow.json> [--go]     # dry-run default
  python wfgen.py activate <n8n-workflow-name>       # prints instructions/API path
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

WFDATA = Path(__file__).resolve().parent / "templates"
N8N = os.environ.get("N8N_BIN", str(Path.home() / "AppData/Roaming/npm/n8n"))
OPENROUTER_MODEL = os.environ.get("WFGEN_MODEL", "stealth/ox-alpha")

# ────────────────────────── templates ──────────────────────────

def load_template(name):
    p = WFDATA / f"{name}.json"
    if not p.exists():
        sys.exit(f"[wfgen] no such template: {name}")
    return json.loads(p.read_text(encoding="utf-8"))


def list_templates():
    out = []
    for p in sorted(WFDATA.glob("*.json")):
        t = json.loads(p.read_text(encoding="utf-8")).get("x-wfgen", {})
        req = [k for k, v in t.get("params", {}).items() if v.get("required")]
        out.append((t.get("name", p.stem), t.get("description", ""), req))
    return out

# ────────────────────────── compile ──────────────────────────

PARAM_RE = re.compile(r"\$param:([a-z0-9_]+)")


def compile_workflow(template_name, params):
    t = load_template(template_name)
    meta = t.get("x-wfgen", {})
    spec = meta.get("params", {})

    missing = [k for k, v in spec.items() if v.get("required") and k not in params]
    if missing:
        sys.exit(f"[wfgen] missing required params for {template_name}: {', '.join(missing)}\n"
                 f"        pass with --set key=value")

    unknown = set(params) - set(spec)
    if unknown:
        print(f"[wfgen] WARN ignoring unknown params: {', '.join(sorted(unknown))}")

    defaults = {k: v.get("default") for k, v in spec.items() if not v.get("required") and v.get("default")}

    def sub(obj):
        if isinstance(obj, dict):
            return {k: sub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sub(v) for v in obj]
        if isinstance(obj, str):
            # optional params left unset resolve to "" so no $param residue remains
            return PARAM_RE.sub(lambda m: str(params.get(m.group(1), defaults.get(m.group(1), ""))), obj)
        return obj

    wf = sub({k: v for k, v in t.items() if k not in ("x-wfgen",)})
    wf["name"] = params.get("workflow_name", f"wfgen: {template_name}")
    wf.pop("active", None)          # never import as active
    wf.setdefault("settings", {"executionOrder": "v1"})
    wf["meta"] = {"wfgen_template": template_name, "wfgen_version": "0.1"}
    return wf

# ────────────────────────── validate (file-based lint) ──────────────────────────

def validate_workflow(wf):
    """Static checks on a workflow dict. Returns list of findings."""
    findings = []
    nodes = wf.get("nodes", [])
    conns = wf.get("connections", {}) or {}
    names = [n.get("name", "") for n in nodes]

    # unresolved $param placeholders anywhere
    raw = json.dumps(wf)
    left = sorted(set(PARAM_RE.findall(raw)))
    if left:
        findings.append({"level": "ERROR", "class": "unresolved-param",
                         "detail": f"$param placeholders never substituted: {', '.join(left)}"})

    # L2: active without trigger
    trig = [nm for nm, n in zip(names, nodes)
            if any(h in str(n.get("type", "")).lower() for h in ("trigger", "webhook", "schedule"))]
    if wf.get("active") and not trig:
        findings.append({"level": "ERROR", "class": "L2-active-no-trigger",
                         "detail": "workflow active but no trigger node"})

    # L3: duplicate node names
    seen, dups = set(), set()
    for nm in names:
        (dups if nm in seen else seen).add(nm) if nm in seen else seen.add(nm)
    if dups:
        findings.append({"level": "ERROR", "class": "L3-duplicate-node-names",
                         "detail": f"duplicate node names: {', '.join(sorted(dups))}"})

    # L4: orphan non-trigger nodes
    connected = set(conns.keys())
    for outs in conns.values():
        for branch in outs.get("main", []):
            for edge in branch or []:
                connected.add(edge.get("node", ""))
    for n in nodes:
        nm = n.get("name", "")
        if n.get("type", "").endswith(("stickyNote",)) or nm in trig:
            continue
        if nm not in connected and nm not in conns:
            findings.append({"level": "WARN", "class": "L4-orphan-node",
                             "detail": f"node '{nm}' has no connections"})

    # L5-lite: HTTP nodes should have onError set
    for n in nodes:
        if "httpRequest" in str(n.get("type", "")) and not n.get("onError"):
            findings.append({"level": "WARN", "class": "L5-http-no-error-handling",
                             "detail": f"HTTP node '{n.get('name')}' has no onError"})
    return findings

# ────────────────────────── LLM mapper (OpenRouter ox-alpha) ──────────────────────────

def map_brief(brief, api_key=None, retries=4):
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        sys.exit("[wfgen] OPENROUTER_API_KEY not set; use compile --set explicitly instead")
    from urllib import request as rq

    catalog = "\n".join(
        f"- {name}: {desc} | required params: {', '.join(req)}"
        for name, desc, req in list_templates())
    prompt = (
        "Pick the best template for this automation brief and extract its parameters.\n"
        f"Templates:\n{catalog}\n\nRules:\n"
        "- Respond ONLY with JSON: {\"template\": name, \"params\": {...}, \"confidence\": \"high|medium|low\"}\n"
        "- Only include params you can infer from the brief; leave others as \"\".\n"
        "- schedule_cron must be a valid 5-field cron.\n\n"
        f"Brief: {brief}")

    body = json.dumps({
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 600,
    }).encode()
    last_err = None
    for attempt in range(retries):
        try:
            r = rq.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                           headers={"Authorization": f"Bearer {api_key}",
                                    "Content-Type": "application/json"})
            with rq.urlopen(r, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            txt = data["choices"][0]["message"].get("content") or ""
            m = re.search(r"\{.*\}", txt, re.S)
            if not m:
                raise ValueError(f"no JSON in model reply: {txt[:120]!r}")
            parsed = json.loads(m.group(0))
            if parsed.get("template") not in {t[0] for t in list_templates()}:
                raise ValueError(f"unknown template picked: {parsed.get('template')!r}")
            return parsed
        except Exception as e:
            last_err = e
            wait = 8 * (attempt + 1)
            print(f"[wfgen] mapper attempt {attempt+1} failed ({e}); retrying in {wait}s…", file=sys.stderr)
            time.sleep(wait)
    sys.exit(f"[wfgen] mapper failed after {retries} attempts: {last_err}")

# ────────────────────────── install / activate ──────────────────────────

def run_n8n(*args, timeout=90):
    cmd = [N8N, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"n8n binary not found at {N8N}"
    except subprocess.TimeoutExpired:
        return 124, "n8n command timed out"


def install_workflow(path, go=False):
    wf = json.loads(Path(path).read_text(encoding="utf-8"))
    findings = validate_workflow(wf)
    errs = [f for f in findings if f["level"] == "ERROR"]
    if errs:
        print(json.dumps(findings, indent=2))
        sys.exit("[wfgen] validation FAILED — refusing to import")

    name = wf.get("name", "(unnamed)")
    if not go:
        print(f"[dry-run] would validate+install: {name} ({len(wf.get('nodes', []))} nodes) → n8n INACTIVE")
        print(f"[dry-run] findings: {json.dumps(findings) if findings else 'clean'}")
        print("[dry-run] pass --go to execute")
        return

    rc, out = run_n8n("import:workflow", "--input", str(Path(path).resolve()))
    print(f"[n8n import exit={rc}] {out[-500:] if out else '(no output)'}")
    if rc != 0:
        sys.exit("[wfgen] import failed — nothing activated")
    print(f"[wfgen] imported '{name}' — verify it appears INACTIVE in n8n UI, then run:")
    print(f"         python wfgen.py activate \"{name}\"")


def activate(name):
    print(f"""[wfgen] activation is deliberately manual (known gotcha: n8n CLI import resets active flag).
Options for '{name}':
  1. n8n UI: open the workflow → toggle Active ON.
  2. REST (needs N8N_API_KEY): PATCH /api/v1/workflows — set "active": true.
Never DB-write database.sqlite directly.
After activating, run the DB linter to confirm L2 passes:
  python ~/.hermes/den/tools/n8n/workflow_lint.py""")

# ────────────────────────── main ──────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="wfgen")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("list"); sp.set_defaults(fn=lambda a: [
        print(f"{n:28s} req: {', '.join(r) or '—'}\n{'':28s}{d}") for n, d, r in list_templates()])

    sp = sub.add_parser("show"); sp.add_argument("template"); sp.set_defaults(
        fn=lambda a: print(json.dumps(load_template(a.template).get("x-wfgen", {}), indent=2)))

    sp = sub.add_parser("compile"); sp.add_argument("template")
    sp.add_argument("--set", action="append", default=[], dest="sets")
    sp.add_argument("-o", "--out", default="compiled.json")
    def do_compile(a):
        params = {}
        for s in a.sets:
            k, _, v = s.partition("=")
            if not _: sys.exit(f"[wfgen] --set expects key=value, got: {s}")
            params[k.strip()] = v.strip()
        wf = compile_workflow(a.template, params)
        Path(a.out).write_text(json.dumps(wf, indent=2), encoding="utf-8")
        findings = validate_workflow(wf)
        print(f"[wfgen] compiled {a.template} → {a.out} ({len(wf['nodes'])} nodes)")
        print(f"[wfgen] validation: {'FAIL' if any(f['level']=='ERROR' for f in findings) else 'PASS'}")
        for f in findings: print(f"  [{f['level']}] {f['class']}: {f['detail']}")
    sp.set_defaults(fn=do_compile)

    sp = sub.add_parser("map"); sp.add_argument("brief"); sp.add_argument("-o", "--out")
    def do_map(a):
        res = map_brief(a.brief)
        print(json.dumps(res, indent=2))
        if a.out: Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
    sp.set_defaults(fn=do_map)

    sp = sub.add_parser("validate"); sp.add_argument("file")
    def do_validate(a):
        findings = validate_workflow(json.loads(Path(a.file).read_text(encoding="utf-8")))
        if not findings: print("PASS — clean")
        else:
            for f in findings: print(f"[{f['level']}] {f['class']}: {f['detail']}")
        sys.exit(1 if any(f["level"] == "ERROR" for f in findings) else 0)
    sp.set_defaults(fn=do_validate)

    sp = sub.add_parser("install"); sp.add_argument("file"); sp.add_argument("--go", action="store_true")
    sp.set_defaults(fn=lambda a: install_workflow(a.file, a.go))
    sp = sub.add_parser("activate"); sp.add_argument("name"); sp.set_defaults(fn=lambda a: activate(a.name))

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
