# wfgen — workflow generator v0.1 (Wk1)

One-line brief -> validated n8n workflow JSON. Spec: `den/reports/workflow-generator-spec.md`.

## Layout

```
den/tools/wfgen.py                      # CLI: list | show | compile | validate
den/tools/wfgen/templates/              # 5 parametrized templates (*.template.json)
den/tools/wfgen/templates/_schema.json  # param contract shape
den/tools/wfgen/tests/test_wfgen.py     # 21 offline tests (pytest, no live n8n)
```

## Usage

```bash
python den/tools/wfgen.py list
python den/tools/wfgen.py show discord-digest
python den/tools/wfgen.py compile discord-digest \
    --param DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/X/Y' \
    -o out/workflow.json
python den/tools/wfgen.py validate out/workflow.json   # exit 0 = safe to import
```

## Rules baked in

- `compile` REJECTS if any required param is missing or a `$wfgen.*` placeholder survives.
- Output workflows are always `"active": false` — activation is an explicit second step
  (import resets the active flag; known n8n CLI gotcha).
- `validate` checks pre-import bug classes: P-unresolved-placeholder, L2-no-trigger,
  L3-duplicate-node-names, L4-orphan-node (+ P-shape / L0-unparseable).
- `workflow_lint.py --file <json>` (sibling tool) lints exports with the same L1-L5
  classes it uses on the live DB.

## Wk2 (next)

LLM brief->params mapper (`mapper.py`): brief text -> {template, params}, deterministic
JSON output, routed through openrouter ox-alpha (Dan's directive). Offline fixtures first.

## Tests

```bash
python -m pytest den/tools/wfgen/tests -q   # 21 passed (2026-08-23)
```
