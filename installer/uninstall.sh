#!/bin/bash
# Удаляет comfy-image-gen и comfy-image-edit из ~/.lmstudio/mcp.json
# (venv и файлы проекта не трогает). Бэкап конфига: .bak
set -euo pipefail

MCP_JSON="$HOME/.lmstudio/mcp.json"
[ -f "$MCP_JSON" ] || { echo "$MCP_JSON не найден — удалять нечего"; exit 0; }

python3 - "$MCP_JSON" <<'PYEOF'
import json, shutil, sys

mcp_path = sys.argv[1]
shutil.copy2(mcp_path, mcp_path + ".bak")
with open(mcp_path) as f:
    cfg = json.load(f)
removed = [k for k in cfg.get("mcpServers", {}) if k.startswith("comfy")]
for k in removed:
    del cfg["mcpServers"][k]
with open(mcp_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print(f"Удалено из {mcp_path}: {', '.join(removed) if removed else '(ничего)'}")
PYEOF
echo "Перезапусти LM Studio, чтобы изменения вступили в силу."
