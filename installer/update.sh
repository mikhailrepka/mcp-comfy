#!/bin/bash
# Апдейтер comfy-mcp: безопасное обновление git-клона до origin/main.
#
# Использование:
#   ./update.sh            # интерактивно: показать изменения, спросить подтверждение
#   ./update.sh --check    # только показать, есть ли обновления (ничего не меняет)
#   ./update.sh --yes      # обновить без вопросов (для cron/скриптов)
#   ./update.sh --no-kill  # не останавливать протухшие MCP-процессы LM Studio
#
# Почему это безопасно: PC_URL живёт в local.env (untracked), состояние вотчера
# (.lmstudio_seen.json) и VERSION тоже untracked — трекаемые файлы всегда
# совпадают с origin, поэтому reset --hard не теряет настроек и конфликтов не
# возникает. Ставить/менять что-либо в трекаемых файлах установки нельзя.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"
DEFAULT_PC="http://192.168.1.218:8188"

CHECK=0; ASSUME_YES=0; KILL=1
for arg in "$@"; do
  case "$arg" in
    --check) CHECK=1 ;;
    --yes) ASSUME_YES=1 ;;
    --no-kill) KILL=0 ;;
    *) echo "Неизвестный аргумент: $arg (см. шапку скрипта)"; exit 1 ;;
  esac
done

if [ ! -d .git ]; then
  echo "ERROR: $DIR не git-клон — update.sh здесь не работает."
  echo "Обновите вручную (синком файлов из свежего клона) или переклонируйте и запустите installer/install.sh."
  exit 1
fi

git fetch origin main --quiet
LOCAL=$(git rev-parse --short HEAD)
REMOTE=$(git rev-parse --short origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
  echo "Уже последняя версия: $LOCAL (install: $(cat VERSION 2>/dev/null || echo unknown))"
  exit 0
fi

echo "Обновление: $LOCAL -> $REMOTE. Новые коммиты:"
git log --oneline "HEAD..origin/main" | head -20
echo
echo "Изменения (последний блок changelog):"
awk 'BEGIN{c=0} /^## /{c++} c==1{print} c==2{exit}' changelog.md 2>/dev/null | head -30 || true
echo

if [ "$CHECK" = "1" ]; then
  echo "--check: ничего не изменено. Запусти ./update.sh --yes для обновления."
  exit 0
fi

# миграция старых установок: если PC_URL был перепечён инсталлером в файлы
# (и он не совпадает с дефолтом) — сохранить его в local.env до reset --hard
if [ ! -f local.env ]; then
  BAKED=$(grep -h -m1 -o 'PC = "http://[^"]*"' comfy_mcp_server.py edit_mcp_server.py upscale_mcp_server.py 2>/dev/null \
          | head -1 | sed 's/^PC = //; s/"//g' || true)
  if [ -n "$BAKED" ] && [ "$BAKED" != "$DEFAULT_PC" ]; then
    printf 'COMFY_PC_URL=%s\n' "$BAKED" > local.env
    echo "Миграция: прежний адрес $BAKED сохранён в local.env"
  fi
fi

if [ "$ASSUME_YES" != "1" ]; then
  read -r -p "Обновить до $REMOTE? [y/N] " ans
  case "$ans" in
    y|Y|yes|Yes|да|Да) ;;
    *) echo "Отменено."; exit 0 ;;
  esac
fi

git reset --hard "origin/main" >/dev/null
git rev-parse --short HEAD > VERSION
echo "Код обновлён: $(cat VERSION)"

# синк зависимостей venv (no-op, если не менялись)
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "ERROR: .venv не найден — запусти installer/install.sh (создаст venv и пропишет mcp.json)."
  exit 1
fi
"$PY" -m pip install -q "mcp<2" websocket-client Pillow \
  || echo "ВНИМАНИЕ: pip sync завершился с ошибкой — проверь вручную"

# протухшие MCP-процессы LM Studio: останавливаем, свежие поднимутся на
# следующей MCP-сессии (daemon chat-bot импортирует серверы в себе и под
# этот паттерн не попадает)
if [ "$KILL" = "1" ]; then
  PIDS=$(pgrep -f '(comfy_mcp_server|edit_mcp_server|upscale_mcp_server)\.py' || true)
  if [ -n "${PIDS:-}" ]; then
    for pid in $PIDS; do
      kill "$pid" 2>/dev/null && echo "остановлен протухший MCP-процесс $pid" || true
    done
    sleep 1
  else
    echo "Протухших MCP-процессов нет."
  fi
fi

PC_URL_CHECK=$(sed -n 's/^COMFY_PC_URL=//p' local.env 2>/dev/null || true)
PC_URL_CHECK=${PC_URL_CHECK:-$DEFAULT_PC}
if curl -s --max-time 5 "$PC_URL_CHECK/system_stats" >/dev/null; then
  echo "ComfyUI доступен: $PC_URL_CHECK"
else
  echo "ВНИМАНИЕ: ComfyUI ($PC_URL_CHECK) не отвечает — код обновлён, но рендер не проверен."
fi

echo
echo "Готово: $DIR @ $(cat VERSION)"
echo "Перезапусти MCP-сессию LM Studio (Developer -> MCP -> Restart), чтобы серверы поднялись на новом коде."
