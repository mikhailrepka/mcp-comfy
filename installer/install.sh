#!/bin/bash
# Инсталлер comfy-mcp: venv + регистрация обоих MCP-серверов в LM Studio.
#
# СКРИПТ ДОЛЖЕН ЛЕЖАТЬ ВНУТРИ ПАПКИ ПРОЕКТА comfy-mcp
# (рядом с comfy_mcp_server.py, edit_mcp_server.py и workflow-файлами .json),
# путь к проекту берётся как родитель папки installer.
#
# Использование:
#   ./install.sh [PC_URL]
# По умолчанию PC_URL = http://192.168.1.218:8188
#
# Что делает:
#   1. находит Python >= 3.10, создаёт .venv, ставит "mcp<2", websocket-client, Pillow;
#   2. прописывает PC_URL во все серверы;
#   3. создаёт ~/comfy_images и ~/comfy_edit_inbox;
#   4. регистрирует comfy-image-gen, comfy-image-edit и comfy-image-upscale
#      в ~/.lmstudio/mcp.json (merge с бэкапом .bak, старые имена записей убирает);
#   5. проверяет доступность ComfyUI.
set -euo pipefail

PC_URL="${1:-http://192.168.1.218:8188}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # корень проекта comfy-mcp
cd "$DIR"

for f in comfy_mcp_server.py edit_mcp_server.py upscale_mcp_server.py jobstore.py; do
  [ -f "$DIR/$f" ] || { echo "Не найден $DIR/$f — скрипт должен лежать в папке installer внутри репозитория comfy-mcp"; exit 1; }
done

# 1. python >= 3.10 (поддерживаются и будущие мажорные 3.x)
PY=""
for cand in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
    PY="$(command -v "$cand")"; break
  fi
done
if [ -z "$PY" ]; then
  cat <<EOF
Нужен Python >= 3.10, но подходящий интерпретатор не найден.
Установите поддерживаемую версию (пример для macOS/Homebrew):
  brew install python@3.14
  brew link --overwrite python@3.14   # если нужно, чтобы python3 указывал на неё
После установки повторите ./install.sh
EOF
  exit 1
fi
echo "Python: $PY ($($PY --version))"

# 2. venv + зависимости
if [ ! -x "$DIR/.venv/bin/python" ]; then
  "$PY" -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/python" -m pip install -q "mcp<2" websocket-client Pillow
echo "venv готов: $DIR/.venv"

# 3. адрес ComfyUI во всех серверах
"$DIR/.venv/bin/python" - "$PC_URL" <<'PYEOF'
import re, sys
url = sys.argv[1]
for fname in ("comfy_mcp_server.py", "edit_mcp_server.py", "upscale_mcp_server.py"):
    with open(fname) as f:
        src = f.read()
    if not re.search(r'PC\s*=\s*"http://[^"]+"', src):
        print(f"ВНИМАНИЕ: PC не найден в {fname}, проверь вручную")
        continue
    new = re.sub(r'PC\s*=\s*"http://[^"]+"', f'PC = "{url}"', src, count=1)
    with open(fname, "w") as f:
        f.write(new)
    print(f"{fname}: PC = {url}")
PYEOF

# 4. рабочие папки
mkdir -p ~/comfy_images ~/comfy_edit_inbox

# 5. регистрация в ~/.lmstudio/mcp.json (merge, с бэкапом)
MCP_JSON="$HOME/.lmstudio/mcp.json"
"$DIR/.venv/bin/python" - "$DIR" "$MCP_JSON" <<'PYEOF'
import json, os, shutil, sys

srv_dir, mcp_path = sys.argv[1], sys.argv[2]
servers = {
    "comfy-image-gen": {
        "command": f"{srv_dir}/.venv/bin/python",
        "args": [f"{srv_dir}/comfy_mcp_server.py"],
    },
    "comfy-image-edit": {
        "command": f"{srv_dir}/.venv/bin/python",
        "args": [f"{srv_dir}/edit_mcp_server.py"],
    },
    "comfy-image-upscale": {
        "command": f"{srv_dir}/.venv/bin/python",
        "args": [f"{srv_dir}/upscale_mcp_server.py"],
    },
}
# старые имена записей (до перехода на новые) — убираем, чтобы не дублировать
legacy = {"comfy_mcp_server", "edit_mcp_server"}
cfg = {"mcpServers": {}}
if os.path.exists(mcp_path):
    shutil.copy2(mcp_path, mcp_path + ".bak")
    with open(mcp_path) as f:
        cfg = json.load(f)
cfg.setdefault("mcpServers", {})
removed = [k for k in cfg["mcpServers"] if k in legacy]
for k in removed:
    del cfg["mcpServers"][k]
for name, s in servers.items():
    cfg["mcpServers"][name] = s
with open(mcp_path, "w") as f:
    json.dump(cfg, f, indent=2, ensure_ascii=False)
print(f"Зарегистрировано в {mcp_path}: {', '.join(servers)}")
if removed:
    print(f"Убраны дубликаты: {', '.join(removed)}")
if os.path.exists(mcp_path + ".bak"):
    print(f"Бэкап предыдущего конфига: {mcp_path}.bak")
PYEOF

# 6. проверка ComfyUI
curl -s --max-time 5 "$PC_URL/system_stats" >/dev/null \
  && echo "ComfyUI доступен: $PC_URL" \
  || echo "ВНИМАНИЕ: $PC_URL не отвечает (проверь, что PC включён и ComfyUI запущен)"

echo
echo "Готово. Перезапусти LM Studio (или Developer -> MCP -> Restart) —"
echo "появятся инструменты comfy-image-gen, comfy-image-edit и comfy-image-upscale."
echo "Системный промт для модели — см. README.md в этой папке."
