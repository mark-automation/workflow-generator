#!/usr/bin/env python3
"""wfgen — one-line brief -> validated n8n workflow JSON (workflow-generator v0.1).

Pipeline (spec v0.1, den/reports/workflow-generator-spec.md):
  compile  : brief + params -> template JSON with $wfgen.* placeholders resolved;
             REJECTS if any required param is still unresolved.
  validate : placeholder check + L2 no-trigger / L3 dup-names / L4 orphan checks
             on the compiled file (reuses workflow_lint.py logic in --file mode).
  install  : import into local n8n via CLI, lands INACTIVE. (Wk3 dogfood; stub.)
  activate : explicit second step after import. (Wk3 dogfood; stub.)

Wk1 scope: list | show | compile | validate + offline tests. Mapper (brief ->
template/params LLM-assisted) lands Wk2.

Usage:
  python wfgen.py list
  python wfgen.py show <template>
  python wfgen.py compile <template> [--param KEY=VALUE ...] [--name NAME] [-o OUT.json]
  python wfgen.py validate <file.json>
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

WFGEN_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = WFGEN_DIR / "templates"
PLACEHOLDER_RE = re.compile(r"\{\{\s*\$wfgen\.([A-Z][A-Z0-9_]*)\s*\}\}")
STICKY = "sticky"
TRIGGER_HINTS = ("trigger", "webhook", "schedule", "cron", "interval",
                 "chatTrigger", "n8n-nodes-base.formTrigger")


# ---------------------------------------------------------------- catalog ---

def load_templates():
    """Return {slug: template_dict} for every *.template.json in templates/."""
    out = {}
    for p in sorted(TEMPLATE_DIR.glob("*.template.json")):
        out[p.name.replace(".template.json", "")] = json.loads(p.read_text(encoding="utf-8"))
    return out


def template_params(tpl):
    return tpl.get("x-wfgen", {}).get("params", {})


# ---------------------------------------------------------------- compile ---

def resolve_placeholders(obj, params):
    """Recursively replace {{ $wfgen.KEY }} strings with params[KEY]."""
    if isinstance(obj, dict):
        return {k: resolve_placeholders(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_placeholders(v, params) for v in obj]
    if isinstance(obj, str):
        def sub(m):
            key = m.group(1)
            return str(params[key]) if key in params else m.group(0)
        return PLACEHOLDER_RE.sub(sub, obj)
    return obj


def compile_workflow(tpl, params=None, name=None):
    """Compile template+params -> workflow dict. Raises ValueError on problems."""
    params = dict(params or {})
    spec = template_params(tpl)

    unknown = set(params) - set(spec)
    if unknown:
        raise ValueError(f"unknown param(s): {', '.join(sorted(unknown))}; "
                         f"template accepts: {', '.join(sorted(spec)) or '(none)'}")

    merged = {}
    missing_required = []
    for key, meta in spec.items():
        if key in params and str(params[key]).strip():
            merged[key] = params[key]
        elif "default" in meta and not meta.get("required"):
            merged[key] = meta["default"]
        elif meta.get("required"):
            missing_required.append(key)

    # Fill optional keys that have no default so the placeholder scan stays quiet.
    for key, meta in spec.items():
        merged.setdefault(key, f"$UNSET:{key}")

    if missing_required:
        raise ValueError("missing required param(s): " + ", ".join(missing_required))

    wf = json.loads(json.dumps({k: v for k, v in tpl.items() if k != "x-wfgen"}))
    wf = resolve_placeholders(wf, merged)
    if name:
        wf["name"] = name
    wf["active"] = False  # installer law: always land inactive
    leftovers = find_unresolved(wf)
    if leftovers:
        raise ValueError(f"unresolved $wfgen placeholder(s) remain: {leftovers}")
    return wf


def find_unresolved(wf):
    """Return sorted unique unresolved placeholder names anywhere in the dict."""
    found = set()

    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, str):
            found.update(PLACEHOLDER_RE.findall(o))

    walk(wf)
    return sorted(found)


# --------------------------------------------------------------- validate ---

def lint_file(path):
    """--file mode: static checks on a workflow JSON file (L2/L3/L4 + P-rules).

    Returns (findings, parsed_ok). Findings are dicts {rule, severity, node,
    detail}. P-rules are wfgen-specific pre-import checks (placeholders, shape).
    """
    findings = []
    try:
        wf = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return [{"rule": "L0-unparseable", "severity": "error", "node": "-",
                 "detail": str(exc)}], False
    if not isinstance(wf, dict) or not isinstance(wf.get("nodes"), list):
        findings.append({"rule": "P-shape", "severity": "error", "node": "-",
                         "detail": "not a workflow object with a nodes[] array"})
        return findings, False

    real = [n for n in wf["nodes"] if isinstance(n, dict)
            and STICKY not in str(n.get("type", "")).lower()]

    # P-placeholders: any $wfgen.* left before import breaks at runtime.
    for ph in find_unresolved(wf):
        findings.append({"rule": "P-unresolved-placeholder", "severity": "error",
                         "node": "-", "detail": f"${ph} never resolved"})

    # L3 duplicate node names
    names = [str(n.get("name")) for n in real]
    for nm, cnt in Counter(names).items():
        if cnt > 1:
            findings.append({"rule": "L3-duplicate-node-names", "severity": "error",
                             "node": nm, "detail": f"{cnt} nodes share this name"})

    conns = wf.get("connections") or {}
    indeg = Counter()
    linked_src = set()
    if isinstance(conns, dict):
        for src, outs in conns.items():
            linked_src.add(src)
            branches = outs.values() if isinstance(outs, dict) else []
            for branch in branches:
                if not isinstance(branch, list):
                    continue
                for slot in branch:
                    for edge in slot or []:
                        if isinstance(edge, dict) and edge.get("node"):
                            indeg[edge["node"]] += 1

    has_trigger = False
    for n in real:
        low = str(n.get("type", "")).lower()
        nm = str(n.get("name", "?"))
        if any(h.lower() in low for h in TRIGGER_HINTS):
            has_trigger = True
        elif len(real) > 1 and indeg[nm] == 0 and nm not in linked_src:
            findings.append({"rule": "L4-orphan-node", "severity": "warn",
                             "node": nm,
                             "detail": "no incoming and no outgoing connections"})

    # L2: file mode treats a trigger-less workflow as an error unconditionally —
    # imports reset the active flag anyway, so a trigger-less compile is dead code.
    if not has_trigger:
        findings.append({"rule": "L2-no-trigger", "severity": "error", "node": "-",
                         "detail": "no trigger node found (webhook/schedule/etc.)"})
    return findings, True


def validate_file(path):
    findings, ok = lint_file(path)
    errors = [f for f in findings if f["severity"] == "error"]
    warns = [f for f in findings if f["severity"] == "warn"]
    for f in findings:
        icon = {"error": "ERROR", "warn": "WARN"}[f["severity"]]
        print(f"  [{icon}] {f['rule']} @ {f['node']}: {f['detail']}")
    verdict = "PASS" if ok and not errors else "FAIL"
    print(f"{verdict}: {Path(path).name} "
          f"(errors={len(errors)} warnings={len(warns)})")
    return 0 if verdict == "PASS" else 1


# ------------------------------------------------------------------- main ---

def cmd_list(_args):
    templates = load_templates()
    print(f"{len(templates)} template(s):\n")
    for slug, tpl in templates.items():
        meta = tpl.get("x-wfgen", {})
        req = [k for k, v in template_params(tpl).items() if v.get("required")]
        print(f"  {slug:<24} {meta.get('description', '')[:70]}")
        print(f"  {'':<24} required params: {', '.join(req) if req else '(none)'}")
    return 0


def cmd_show(args):
    templates = load_templates()
    if args.template not in templates:
        print(f"unknown template: {args.template}", file=sys.stderr)
        print(f"available: {', '.join(sorted(templates))}", file=sys.stderr)
        return 2
    tpl = templates[args.template]
    meta = tpl.get("x-wfgen", {})
    print(f"# {args.template}\n")
    print(meta.get("description", "(no description)") + "\n")
    print("params:")
    for key, p in template_params(tpl).items():
        tag = "REQUIRED" if p.get("required") else f"default={p.get('default', '<unset>')!r}"
        print(f"  {key:<22} {tag:<28} {p.get('description', '')}")
    print(f"\nnodes: {' -> '.join(n['name'] for n in tpl['nodes'])}")
    return 0


def cmd_compile(args):
    templates = load_templates()
    if args.template not in templates:
        print(f"unknown template: {args.template}", file=sys.stderr)
        return 2
    params = {}
    for kv in args.param or []:
        if "=" not in kv:
            print(f"bad --param (want KEY=VALUE): {kv}", file=sys.stderr)
            return 2
        k, _, v = kv.partition("=")
        params[k.strip()] = v
    try:
        wf = compile_workflow(templates[args.template], params, name=args.name)
    except ValueError as exc:
        print(f"compile REJECTED: {exc}", file=sys.stderr)
        return 1
    text = json.dumps(wf, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def cmd_validate(args):
    return validate_file(args.file)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="wfgen", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list available templates").set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="show template details + params")
    p_show.add_argument("template")
    p_show.set_defaults(func=cmd_show)

    p_c = sub.add_parser("compile", help="compile template+params to workflow JSON")
    p_c.add_argument("template")
    p_c.add_argument("--param", action="append", metavar="KEY=VALUE",
                     help="set a template param (repeatable)")
    p_c.add_argument("--name", help="override output workflow name")
    p_c.add_argument("-o", "--out", help="write JSON here instead of stdout")
    p_c.set_defaults(func=cmd_compile)

    p_v = sub.add_parser("validate", help="lint a workflow JSON file pre-import")
    p_v.add_argument("file")
    p_v.set_defaults(func=cmd_validate)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
