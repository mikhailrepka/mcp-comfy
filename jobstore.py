"""Общий файловый реестр задач между comfy-mcp серверами.

Проблема: `jobs` живёт в памяти конкретного процесса MCP-сервера. У клиентов
(например, LM Studio) несколько серверов с одинаковым инструментом check_image,
и клиент может вызвать check_image не тем сервером, который поставил задачу, —
тогда он получит "Unknown job_id". Тот же эффект при перезапуске сервера.

Решение: каждый сервер дублирует публичные поля задач в общий JSON-файл
(~/.comfy_mcp_jobs.json). check_image любого сервера при неизвестном локально
job_id смотрит в реестр и отвечает по нему: для завершённых задач картинка
дочитывается с диска (results всегда сохраняются в OUTPUT_DIR).
"""
import json
import os
import time

JOBS_FILE = os.path.join(os.path.expanduser("~"), ".comfy_mcp_jobs.json")
MAX_AGE = 24 * 3600  # записи старше суток вычищаются при каждой записи


def _load():
    try:
        with open(JOBS_FILE) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save(reg):
    tmp = JOBS_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(reg, fh)
        os.replace(tmp, JOBS_FILE)  # атомарная замена
    except Exception as e:
        print(f"[jobstore] save error: {e}", flush=True)


def put(job_id, record):
    """Сохраняет/обновляет публичную запись задачи (status, path, error, prompt_id, progress)."""
    reg = _load()
    now = time.time()
    reg[job_id] = dict(record, updated=now)
    reg = {k: v for k, v in reg.items() if now - v.get("updated", 0) < MAX_AGE}
    _save(reg)


def get(job_id):
    """Возвращает запись задачи (или None), независимо от того, какой сервер её создал."""
    rec = _load().get(job_id)
    return dict(rec) if rec else None


def poll(job_id, wait=20, interval=1.0):
    """Честное ожидание чужой задачи: цикл опроса до `wait` секунд (не разовый sleep).
    Любой сервер, получив check_image чужого job_id, ведёт себя как родной —
    иначе клиент, маршрутизирующий check_image на «последний» сервер (LM Studio
    так и делает), получает деградированную проверку. Возвращает свежую запись
    (или последнюю известную/None)."""
    deadline = time.time() + max(int(wait), 1)
    rec = get(job_id)
    while time.time() < deadline:
        if rec and rec.get("status") in ("done", "error"):
            return rec
        time.sleep(interval)
        rec = get(job_id)
    return rec
