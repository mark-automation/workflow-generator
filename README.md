# workflow-generator (wfgen)

One-line brief → validated n8n workflow JSON → one-command install.
Templates wrap proven local patterns; the brief→params mapper runs on the same
model this Hermes agent uses (`stealth/ox-alpha` via OpenRouter).

## Quick start
```bash
python wfgen/wfgen.py list                 # see templates + required params
python wfgen/wfgen.py map "brief here"     # LLM picks template + params
python wfgen/wfgen.py compile <tpl> --set k=v --set k=v -o out.json
python wfgen/wfgen.py validate out.json    # placeholder + L2/L3/L4/L5 checks
python wfgen/wfgen.py install out.json     # dry-run; add --go to import INACTIVE
```

## Templates (v0.1)
discord-digest · rss-to-channel · keyword-miner-schedule · lead-enrich-drop · backup-verify-report

## Tests
```bash
python wfgen/test_wfgen.py   # 13 offline checks, no network needed
```

## Safety model
- compile rejects missing required params; optional unset params resolve to "" (no $param residue)
- install refuses any file failing validation — never imports broken JSON
- imports land INACTIVE; activation is an explicit manual step (known n8n CLI gotcha)
