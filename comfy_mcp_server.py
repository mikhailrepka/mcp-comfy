"""MCP-сервер: генерация изображений через ComfyUI на удалённом PC.

Запуск: python3 comfy_mcp_server.py
Зависимости: pip install "mcp<2"

Двухфазный протокол, чтобы не упираться в таймаут tool-вызова LM Studio:
- generate_image: ставит задачу в очередь ComfyUI и ждёт до FAST_WAIT секунд;
  если картинка готова — возвращает её инлайн, иначе возвращает job_id.
- check_image: дожидается (или проверяет) результат по job_id и возвращает картинку.
"""
import asyncio
import base64
import json
import os
import random
import threading
import time
import urllib.parse
import urllib.request

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import ImageContent, TextContent, Tool

import jobstore

# ComfyUI PC: env COMFY_PC_URL -> local.env рядом с сервером -> дефолт ниже.
# local.env (строка COMFY_PC_URL=..., untracked) пишут installer/update.sh — сами
# файлы серверов остаются чистыми, поэтому git pull проходит без конфликтов.
# Дефолт сохранён с первых версий: старые установки продолжают работать без
# local.env (все машины этой сети смотрят на один ComfyUI PC).
def _resolve_pc(env_value=None, env_file=None):
    if env_value is None:
        env_value = os.environ.get("COMFY_PC_URL")
    if env_value and env_value.strip():
        return env_value.strip().rstrip("/")
    env_file = env_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.env")
    try:
        with open(env_file) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("COMFY_PC_URL="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v.rstrip("/")
    except OSError:
        pass
    return "http://192.168.1.218:8188"


PC = _resolve_pc()
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nvidia-image-gen.json")
def _read_local_env(env_file=None):
    """KEY=VALUE из local.env рядом с сервером (без env-переопределений)."""
    env_file = env_file or os.path.join(os.path.dirname(os.path.abspath(__file__)), "local.env")
    out = {}
    try:
        with open(env_file) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _cfg_int(name, default):
    v = os.environ.get(name)
    if not v or not v.strip():
        v = _read_local_env().get(name)
    try:
        return int(v) if v else default
    except ValueError:
        return default


OUTPUT_DIR = os.path.expanduser("~/comfy_images")
# Сколько generate_image держит tool-вызов открытым, ждёт результат инлайн.
# КРИТИЧНО: LM Studio-клиент убивает MCP-вызов по своему таймауту (~60 с) —
# если инлайн-ожидание длиннее, модель получает -32001 БЕЗ job_id и плодит
# дубликаты. Дефолт безопасен для прямых вызовов из LM Studio; чат-боту хватает,
# его check_image-цикл добирает результат куска́ми. Переопределяется
# env COMFY_FAST_WAIT или local.env.
FAST_WAIT = _cfg_int("COMFY_FAST_WAIT", 25)
CHECK_WAIT = 600      # фоновый дедлайн задачи (_run_job), не связан с таймаутом клиента
CHECK_WAIT_INLINE = _cfg_int("COMFY_CHECK_WAIT", 30)  # один вызов check_image (сек)
POLL_INTERVAL = 1     # период опроса /history

app = Server("comfy-image-gen")

# job_id -> {"status": "queued"|"done", "path": ..., "error": ...}
jobs = {}

CLIENT_ID = f"mcp_gen_{random.randint(0, 10**10)}"
ws_progress = {}  # prompt_id -> {"value": int, "max": int} из websocket-событий ComfyUI


def _submit(prompt: str, width: int, height: int, steps: int, seed=None, n: int = 1) -> tuple:
    """Ставит задачу в очередь. Возвращает (prompt_id, seed)."""
    with open(WORKFLOW) as f:
        wf = json.load(f)
    wf["57:27"]["inputs"]["text"] = prompt
    wf["57:13"]["inputs"]["width"] = width
    wf["57:13"]["inputs"]["height"] = height
    # варианты за один рендер: один prompt_id, SaveImage отдаёт N файлов
    wf["57:13"]["inputs"]["batch_size"] = max(1, min(int(n), 4))
    if seed is None:
        seed = random.randint(0, 2 ** 32)
    wf["57:3"]["inputs"]["seed"] = int(seed)
    wf["57:3"]["inputs"]["steps"] = steps
    req = urllib.request.Request(
        f"{PC}/prompt",
        data=json.dumps({"prompt": wf, "client_id": CLIENT_ID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"], seed


def _history(prompt_id: str) -> dict:
    return json.loads(urllib.request.urlopen(f"{PC}/history/{prompt_id}", timeout=30).read())


def _error_from_history(hist: dict, prompt_id: str):
    """Текст ошибки из /history, если задача упала/отменена, иначе None."""
    entry = hist.get(prompt_id, {})
    st = entry.get("status", {})
    if st.get("status_str") != "error":
        return None
    for m in st.get("messages", []):
        if m[0] == "execution_error":
            d = m[1]
            return (f"{d.get('node_type', '?')} (node {d.get('node_id', '?')}): "
                    f"{d.get('exception_message', 'unknown error')}")
    return "execution error (no details)"


def _fetch_image(prompt_id: str):
    """Возвращает [(path, bytes), ...] если картинки готовы (батч отдаёт N файлов), иначе None."""
    hist = _history(prompt_id)
    if prompt_id not in hist:
        return None
    imgs = hist[prompt_id]["outputs"].get("9", {}).get("images", [])
    if not imgs:
        return None
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    results = []
    for img in imgs:
        params = urllib.parse.urlencode({
            "filename": img["filename"],
            "subfolder": img["subfolder"],
            "type": img["type"],
        })
        data = urllib.request.urlopen(f"{PC}/view?{params}", timeout=60).read()
        out = os.path.join(OUTPUT_DIR, img["filename"])
        with open(out, "wb") as fh:
            fh.write(data)
        results.append((out, data))
    return results


def _run_job(prompt: str, width: int, height: int, steps: int, job_id: str,
             seed=None, n: int = 1):
    try:
        pid, seed = _submit(prompt, width, height, steps, seed, n)
        jobs[job_id]["prompt_id"] = pid
        jobs[job_id]["seed"] = seed
        jobstore.put(job_id, {"status": "queued", "prompt_id": pid, "seed": seed})
        deadline = time.time() + CHECK_WAIT
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            hist = _history(pid)
            err = _error_from_history(hist, pid)
            if err:
                jobs[job_id] = {"status": "error", "error": f"ComfyUI execution failed — {err}"}
                jobstore.put(job_id, jobs[job_id])
                return
            res = _fetch_image(pid)
            if res:
                paths = [p for p, _ in res]
                jobs[job_id] = {"status": "done", "path": paths[0], "paths": paths,
                                "data": [d for _, d in res], "seed": seed}
                jobstore.put(job_id, {"status": "done", "path": paths[0], "paths": paths,
                                      "seed": seed})
                return
            pr = ws_progress.get(pid)
            if pr and pr != jobs[job_id].get("progress"):
                jobs[job_id]["progress"] = pr
                jobstore.put(job_id, {"status": "queued", "prompt_id": pid, "progress": pr})
        jobs[job_id] = {"status": "error", "error": "timeout waiting for ComfyUI"}
        jobstore.put(job_id, jobs[job_id])
    except Exception as e:
        jobs[job_id] = {"status": "error", "error": str(e)}
        jobstore.put(job_id, jobs[job_id])


def _ws_listen_loop():
    """Фон: слушает websocket ComfyUI и пишет прогресс сэмплера в ws_progress."""
    try:
        import websocket
    except ImportError:
        print("[ws] websocket-client не установлен — прогресс недоступен "
              "(.venv/bin/python -m pip install websocket-client)", flush=True)
        return
    url = PC.replace("http://", "ws://") + f"/ws?clientId={CLIENT_ID}"
    while True:
        try:
            conn = websocket.create_connection(url, timeout=10)
            while True:
                try:
                    msg = conn.recv()
                except Exception:
                    break  # таймаут простоя -> переподключение
                if isinstance(msg, bytes):
                    continue  # бинарные превью-кадры
                try:
                    ev = json.loads(msg)
                    d = ev.get("data", {})
                except Exception:
                    continue
                if ev.get("type") == "progress" and "value" in d and "max" in d:
                    pid = d.get("prompt_id")
                    if pid:
                        ws_progress[pid] = {"value": d["value"], "max": d["max"]}
        except Exception:
            time.sleep(3)  # PC недоступен — ретрай


def _content_for_result(job: dict) -> list:
    content = []
    if job["status"] == "done":
        paths = job.get("paths") or [job["path"]]
        raw = job.get("data")
        datas = raw if isinstance(raw, list) else [raw]
        for p, d in zip(paths, datas):
            content.append(ImageContent(
                type="image",
                data=base64.b64encode(d).decode(),
                mimeType="image/png",
            ))
        seed_note = f"\nSeed: {job['seed']}" if job.get("seed") is not None else ""
        text = "\n".join(f"Image generated: {p}" for p in paths)
        content.append(TextContent(type="text", text=f"{text}{seed_note}"))
    else:
        content.append(TextContent(type="text", text=f"Generation failed: {job['error']}"))
    return content


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image from a text prompt using ComfyUI on the remote PC. "
                        "Usually returns the finished image inline within ~25 seconds; "
                        "if it is still rendering, returns a job_id — then call check_image "
                        "with that job_id to get the image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Image description, in English, detailed.",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Image width in pixels (multiple of 8). Default 1024.",
                        "default": 1024,
                    },
                    "height": {
                        "type": "integer",
                        "description": "Image height in pixels (multiple of 8). Default 1024.",
                        "default": 1024,
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Number of sampling steps; more = more detail, slower. Default 8.",
                        "default": 8,
                    },
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 4,
                        "description": "Number of variants in ONE render (same prompt, different "
                                       "noise) — one job_id, check_image returns all images. "
                                       "Use when the user asks for options/variants to choose "
                                       "from. Default 1. Note: 4 variants take ~4x VRAM.",
                        "default": 1,
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Optional: fix the seed to reproduce or iterate on a "
                                       "previous result (the used seed is returned in the "
                                       "result text). Default: random.",
                    },
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="check_image",
            description="Check a pending image generation job and return the image when ready. "
                        "Call with the job_id returned by generate_image. Waits up to ~30 s "
                        "per call; if still rendering, call again until the image arrives.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id returned by generate_image.",
                    },
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="comfy_status",
            description="Check the ComfyUI PC (remote render server): reachable, GPU/VRAM, RAM "
                        "and the render queue. Use it before submitting work if a previous job "
                        "failed with a network/HTTP error, when the user asks whether the PC is "
                        "busy or asleep, or to diagnose 'unreachable' failures.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="cancel_job",
            description="Cancel a queued or running render on the ComfyUI PC by job_id (from "
                        "generate_image, edit_image or upscale_image). Finished jobs cannot be "
                        "cancelled. Use when the user asks to stop a task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id returned by generate_image/edit_image/upscale_image.",
                    },
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="list_results",
            description="List recent result images in ~/comfy_images (generated/edited/upscaled "
                        "outputs, newest first). Use when the user refers to an earlier result "
                        "without giving a path; then pass the listed path to edit_image, "
                        "upscale_image or convert_image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "How many entries to show (1-50). Default 10.",
                        "default": 10,
                    },
                },
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    if name == "generate_image":
        prompt = arguments["prompt"]
        width = int(arguments.get("width", 1024))
        height = int(arguments.get("height", 1024))
        steps = int(arguments.get("steps", 8))
        seed = arguments.get("seed")
        seed = int(seed) if seed is not None else None
        n = int(arguments.get("n", 1))
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        loop = asyncio.get_event_loop()
        threading.Thread(target=_run_job,
                         args=(prompt, width, height, steps, job_id, seed, n),
                         daemon=True).start()

        # короткое быстрое ожидание: если turbo успел — вернём инлайн
        deadline = time.time() + FAST_WAIT
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            if jobs[job_id]["status"] != "queued":
                return _content_for_result(jobs[job_id])
        return [TextContent(
            type="text",
            text=f"Image is still rendering on the remote PC (job_id={job_id}). "
                 f"Call check_image with this job_id to get the result.",
        )]

    if name == "check_image":
        job_id = arguments["job_id"]
        if job_id not in jobs:
            # задача могла быть создана другим comfy-mcp сервером — смотрим общий реестр
            return _foreign_check(job_id)
        deadline = time.time() + min(int(arguments.get("wait", CHECK_WAIT_INLINE)), CHECK_WAIT_INLINE)
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            if jobs[job_id]["status"] != "queued":
                return _content_for_result(jobs[job_id])
        return [TextContent(type="text", text=f"Still rendering (job_id={job_id}).{_progress_note(jobs[job_id])} Try again.")]

    if name == "comfy_status":
        return [TextContent(type="text", text=await asyncio.to_thread(_comfy_status_text))]

    if name == "cancel_job":
        return [TextContent(type="text", text=await asyncio.to_thread(_cancel_job, arguments["job_id"]))]

    if name == "list_results":
        limit = int(arguments.get("limit", 10))
        return [TextContent(type="text", text=_list_results_text(limit))]

    raise ValueError(f"Unknown tool: {name}")


def _foreign_check(job_id: str) -> list:
    """check_image для задачи, созданной другим comfy-mcp сервером (через jobstore)."""
    rec = jobstore.get(job_id)
    if rec is None:
        return [TextContent(type="text",
                text=f"Unknown job_id: {job_id}. It may belong to a restarted session — "
                     f"start a new task.")]
    status = rec.get("status")
    if status == "done":
        # батч-задачи несут список путей — отдаём все картинки
        content = []
        for p in (rec.get("paths") or [rec["path"]]):
            content.extend(_image_file_content(p, rec.get("seed")))
        return content
    if status == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    pr = rec.get("progress")
    prog = ""
    if pr and pr.get("max"):
        prog = f" Progress: {pr['value']}/{pr['max']} ({int(pr['value'] * 100 / pr['max'])}%)."
    return [TextContent(type="text",
            text=f"Still rendering (job_id={job_id}, started by another comfy-mcp "
                 f"server).{prog} Call check_image again.")]


def _image_file_content(path: str, seed=None) -> list:
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        path.rsplit(".", 1)[-1].lower(), "image/png")
    with open(path, "rb") as fh:
        data = fh.read()
    seed_note = f"\nSeed: {seed}" if seed is not None else ""
    return [
        ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime),
        TextContent(type="text", text=f"Image ready: {path}{seed_note}"),
    ]


def _progress_note(job: dict) -> str:
    pid = job.get("prompt_id")
    pr = job.get("progress") or (ws_progress.get(pid) if pid else None)
    if pr and pr.get("max"):
        return f" Progress: {pr['value']}/{pr['max']} steps ({int(pr['value'] * 100 / pr['max'])}%)."
    return ""


def _install_version(dir=None) -> str:
    """Версия установки (короткий коммит) — пишется в VERSION инсталлером/update.sh."""
    base = dir or os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(base, "VERSION")) as fh:
            return fh.read().strip() or "unknown"
    except OSError:
        return "unknown"


def _comfy_status_text() -> str:
    """Доступность PC, GPU/RAM и очередь рендера — для диагностики перед задачей."""
    try:
        st = json.loads(urllib.request.urlopen(PC + "/system_stats", timeout=8).read())
    except Exception as e:
        return (f"ComfyUI PC unreachable ({e}). The PC is probably asleep or ComfyUI is "
                f"not running — ask the user to wake it up; image jobs will fail until then.")
    sysinfo = st.get("system", {})
    lines = [f"ComfyUI OK: {PC} (version {sysinfo.get('comfyui_version', '?')})",
             f"Install: {_install_version()}"]
    for dev in st.get("devices", []):
        vt, vf = dev.get("vram_total", 0) or 0, dev.get("vram_free", 0) or 0
        lines.append(f"GPU: {dev.get('name', '?')} — VRAM free {vf / 2**30:.1f} / {vt / 2**30:.1f} GB")
    if sysinfo.get("ram_total"):
        lines.append(f"RAM free {sysinfo.get('ram_free', 0) / 2**30:.1f} / "
                     f"{sysinfo['ram_total'] / 2**30:.1f} GB")
    try:
        q = json.loads(urllib.request.urlopen(PC + "/queue", timeout=8).read())
        q_running = len(q.get("queue_running") or [])
        q_pending = len(q.get("queue_pending") or [])
        qline = f"Queue: {q_running} running, {q_pending} pending"
        if q_running or q_pending:
            qline += " — a render is in progress; new jobs will wait behind it"
        lines.append(qline)
    except Exception:
        pass  # /queue недоступен — не критично, основной статус уже есть
    return "\n".join(lines)


def _cancel_job(job_id: str) -> str:
    """Отмена queued/running задачи по job_id (свой или из jobstore)."""
    rec = jobs.get(job_id) or jobstore.get(job_id)
    if rec is None:
        return (f"Unknown job_id: {job_id}. It may belong to a restarted session — "
                f"check comfy_status or just start a new task.")
    if rec.get("status") in ("done", "error"):
        what = "already done" if rec.get("status") == "done" else "already failed"
        return f"Job {job_id} is {what} — nothing to cancel."
    pid = rec.get("prompt_id")
    if not pid:
        return f"Job {job_id} has no ComfyUI prompt_id yet — nothing to cancel."
    try:
        q = json.loads(urllib.request.urlopen(PC + "/queue", timeout=8).read())
    except Exception as e:
        return f"Cannot reach ComfyUI to cancel: {e}"
    running_ids = {e[1] for e in (q.get("queue_running") or [])}
    pending_ids = {e[1] for e in (q.get("queue_pending") or [])}
    try:
        if pid in pending_ids:
            req = urllib.request.Request(
                PC + "/queue", method="POST",
                data=json.dumps({"delete": [pid]}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=8).read()
            what = "cancelled (was queued)"
        elif pid in running_ids:
            # /interrupt останавливает текущий рендер — это наш pid, см. проверку выше
            urllib.request.urlopen(
                urllib.request.Request(PC + "/interrupt", method="POST"), timeout=8).read()
            what = "cancelled (was running)"
        else:
            return (f"Job {job_id} ({pid}) is no longer in the ComfyUI queue — it has "
                    f"probably finished; call check_image to get the result.")
    except Exception as e:
        return f"Failed to cancel: {e}"
    jobs[job_id] = {"status": "error", "error": "cancelled by user (cancel_job)"}
    jobstore.put(job_id, jobs[job_id])
    return f"Job {job_id} {what}."


def _img_note(p):
    """"(1024x768 jpg 350KB)" для list_results; tolerant — без Pillow вернёт только байты."""
    try:
        from PIL import Image
        with Image.open(p) as im:
            w, h = im.size
        kb = os.path.getsize(p) // 1024
        ext = os.path.splitext(p)[1].lstrip(".").lower() or "?"
        return f"({w}x{h} {ext} {kb}KB)"
    except Exception:
        try:
            return f"({os.path.getsize(p) // 1024}KB)"
        except Exception:
            return ""


def _list_results_text(limit: int = 10) -> str:
    """Последние результаты в OUTPUT_DIR (генерации/правки/апскейлы), новые первыми."""
    if not os.path.isdir(OUTPUT_DIR):
        return f"No results folder yet: {OUTPUT_DIR}"
    exts = (".png", ".jpg", ".jpeg", ".webp")
    files = []
    for fname in os.listdir(OUTPUT_DIR):
        p = os.path.join(OUTPUT_DIR, fname)
        if os.path.isfile(p) and fname.lower().endswith(exts):
            files.append((os.path.getmtime(p), p))
    if not files:
        return f"No result images yet in {OUTPUT_DIR}."
    files.sort(reverse=True)
    limit = max(1, min(int(limit), 50))
    lines = [f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))}  {p} {_img_note(p)}"
             for mt, p in files[:limit]]
    if len(files) > limit:
        lines.append(f"(+{len(files) - limit} older; pass a higher `limit` to see more)")
    return "\n".join(lines)


async def main():
    threading.Thread(target=_ws_listen_loop, daemon=True).start()
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
