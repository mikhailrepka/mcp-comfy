"""MCP-сервер: апскейл изображений через SeedVR2 на удалённом PC (ComfyUI).

Запуск: /Users/mikhailrepka/comfy-mcp/.venv/bin/python upscale_mcp_server.py
Зависимости: pip install "mcp<2" websocket-client

Двухфазный протокол (как у редактирования):
- upscale_image: загружает картинку на PC, ставит задачу в очередь и СРАЗУ
  возвращает job_id ("задача взята в работу").
- check_image: периодический опрос готовности; когда готово — возвращает картинку
  инлайн. По пути показывает прогресс и сразу сообщает об ошибке выполнения.
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

PC = "http://localhost:8188"
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "utility_seedvr2_7b_int8_upscale_image.json")
OUTPUT_DIR = os.path.expanduser("~/comfy_images")
INBOX_DIR = os.path.expanduser("~/comfy_edit_inbox")  # общая папка с edit-сервером
POLL_INTERVAL = 1
CHECK_WAIT_DEFAULT = 20   # сколько check_image ждёт за один вызов, секунд
CHECK_WAIT_MAX = 120      # верхний предел wait для check_image

# id узлов в workflow
NODE_IMAGE = "1"      # LoadImage: исходное изображение
NODE_SAVE = "9"       # SaveImage
NODE_RESIZE = "66:57"  # ResizeImageMaskNode (multiplier / longer_size / scale_method)

app = Server("comfy-image-upscale")
jobs = {}

CLIENT_ID = f"mcp_upscale_{random.randint(0, 10**10)}"
ws_progress = {}  # prompt_id -> {"value": int, "max": int} из websocket-событий ComfyUI


def _upload_image(path):
    """Загружает локальный файл на PC (в input ComfyUI), возвращает имя файла."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"Файл не найден: {path}")
    fname = f"mcp_{random.randint(0, 10**10)}_{os.path.basename(path)}"
    boundary = f"----ComfyMCPBoundary{random.randint(0, 10**10)}"
    with open(path, "rb") as fh:
        data = fh.read()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{fname}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        PC + "/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=120).read())
    return resp.get("name", fname)


def _submit(source_name, multiplier, longer_size, scale_method):
    with open(WORKFLOW) as f:
        wf = json.load(f)
    wf[NODE_IMAGE]["inputs"]["image"] = source_name
    rt = wf[NODE_RESIZE]["inputs"]
    if longer_size:
        rt["resize_type"] = "scale longer dimension"
        rt["resize_type.longer_size"] = int(longer_size)
        rt.pop("resize_type.multiplier", None)
    else:
        rt["resize_type"] = "scale by multiplier"
        rt["resize_type.multiplier"] = float(multiplier)
        rt.pop("resize_type.longer_size", None)
    rt["scale_method"] = scale_method
    req = urllib.request.Request(
        PC + "/prompt",
        data=json.dumps({"prompt": wf, "client_id": CLIENT_ID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]


def _history(prompt_id):
    return json.loads(urllib.request.urlopen(PC + "/history/" + prompt_id, timeout=30).read())


def _error_from_history(hist, prompt_id):
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


def _fetch_image_from_hist(hist, prompt_id):
    if prompt_id not in hist:
        return None
    imgs = hist[prompt_id]["outputs"].get(NODE_SAVE, {}).get("images", [])
    if not imgs:
        return None
    img = imgs[0]
    params = urllib.parse.urlencode({
        "filename": img["filename"],
        "subfolder": img["subfolder"],
        "type": img["type"],
    })
    data = urllib.request.urlopen(PC + "/view?" + params, timeout=120).read()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, img["filename"])
    with open(out, "wb") as fh:
        fh.write(data)
    return out, data


def _fetch_image(prompt_id):
    return _fetch_image_from_hist(_history(prompt_id), prompt_id)


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


def _start_job(source_path, multiplier, longer_size, scale_method, job_id):
    """Загружает картинку на PC и ставит задачу в очередь. Возвращает prompt_id."""
    src = _upload_image(source_path)
    pid = _submit(src, multiplier, longer_size, scale_method)
    jobs[job_id]["prompt_id"] = pid
    return pid


def _poll_job(job_id):
    """Фоновый опрос: ждёт результат, ловит ошибки и пишет прогресс в jobs[job_id]."""
    job = jobs[job_id]
    pid = job["prompt_id"]
    deadline = time.time() + 3600  # верхний страхующий предел
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            hist = _history(pid)
            err = _error_from_history(hist, pid)
            if err:
                jobs[job_id] = {"status": "error", "error": f"ComfyUI execution failed — {err}"}
                jobstore.put(job_id, jobs[job_id])
                return
            res = _fetch_image_from_hist(hist, pid)
        except Exception as e:
            res = None
            job["note"] = f"poll error: {e}"
        if res:
            jobs[job_id] = {"status": "done", "path": res[0], "data": res[1]}
            jobstore.put(job_id, {"status": "done", "path": res[0]})
            return
        pr = ws_progress.get(pid)
        if pr and pr != job.get("progress"):
            job["progress"] = pr
            jobstore.put(job_id, {"status": "queued", "prompt_id": pid, "progress": pr})
    jobs[job_id] = {"status": "error", "error": "timeout waiting for ComfyUI"}
    jobstore.put(job_id, jobs[job_id])


def _list_inbox():
    """Последние картинки в INBOX_DIR: [(path, mtime_iso)], новые первыми."""
    if not os.path.isdir(INBOX_DIR):
        os.makedirs(INBOX_DIR, exist_ok=True)
        return []
    exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    files = [
        os.path.join(INBOX_DIR, f)
        for f in os.listdir(INBOX_DIR)
        if f.lower().endswith(exts)
    ]
    files.sort(key=os.path.getmtime, reverse=True)
    out = []
    for p in files[:20]:
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
        out.append(f"{ts}  {p}")
    return out


def _content_for_result(job):
    content = []
    if job["status"] == "done":
        content.append(ImageContent(
            type="image",
            data=base64.b64encode(job["data"]).decode(),
            mimeType="image/png",
        ))
        content.append(TextContent(type="text", text=f"Upscaled image: {job['path']}"))
    else:
        content.append(TextContent(type="text", text=f"Upscale failed: {job['error']}"))
    return content


def _foreign_check(job_id):
    """check_image для задачи, созданной другим comfy-mcp сервером (через jobstore)."""
    rec = jobstore.get(job_id)
    if rec is None:
        return [TextContent(type="text",
                text=f"Unknown job_id: {job_id}. It may belong to a restarted session — "
                     f"start a new task.")]
    status = rec.get("status")
    if status == "done":
        return _image_file_content(rec["path"])
    if status == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    pr = rec.get("progress")
    prog = ""
    if pr and pr.get("max"):
        prog = f" Progress: {pr['value']}/{pr['max']} ({int(pr['value'] * 100 / pr['max'])}%)."
    rec = jobstore.get(job_id) or rec
    if rec.get("status") == "done":
        return _image_file_content(rec["path"])
    if rec.get("status") == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    return [TextContent(type="text",
            text=f"Still rendering (job_id={job_id}, started by another comfy-mcp "
                 f"server).{prog} Call check_image again.")]


def _image_file_content(path):
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        path.rsplit(".", 1)[-1].lower(), "image/png")
    with open(path, "rb") as fh:
        data = fh.read()
    return [
        ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime),
        TextContent(type="text", text=f"Image ready: {path}"),
    ]


def _progress_note(job):
    pid = job.get("prompt_id")
    pr = job.get("progress") or (ws_progress.get(pid) if pid else None)
    if pr and pr.get("max"):
        return f" Progress: {pr['value']}/{pr['max']} ({int(pr['value'] * 100 / pr['max'])}%)."
    return ""


@app.list_tools()
async def list_tools():
    return [
        Tool(
            name="list_images",
            description="List images available for upscaling/editing in the inbox folder "
                        "(newest first, shared with the edit server). Use this when the user "
                        "refers to an attached/dropped/pasted image but did not give a file "
                        "path: images attached to the chat appear here automatically within "
                        "a few seconds. Then pass a listed path to upscale_image.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="upscale_image",
            description="Upscale an existing image with SeedVR2 (neural upscaler, quality "
                        "restore) using ComfyUI on the remote PC. source_image is an absolute "
                        "file path — get it from the user or from list_images. Scale either by "
                        "multiplier (e.g. 2 or 4x) or to a target longer dimension. Returns "
                        "immediately with a job_id ('task accepted'); rendering takes several "
                        "minutes. Then poll check_image with that job_id until the image "
                        "arrives.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source_image": {
                        "type": "string",
                        "description": "Absolute path to the local image to upscale.",
                    },
                    "multiplier": {
                        "type": "number",
                        "description": "Scale factor (e.g. 2 = double size, 4 = quadruple). "
                                       "Ignored if longer_size is set. Default 2. Range 0.01-8.",
                        "default": 2,
                    },
                    "longer_size": {
                        "type": "integer",
                        "description": "Optional: target longer side in pixels (aspect ratio "
                                       "preserved). If set, overrides multiplier.",
                    },
                    "scale_method": {
                        "type": "string",
                        "enum": ["lanczos", "bicubic", "area", "bilinear", "nearest-exact"],
                        "description": "Pre-resize interpolation for the SeedVR2 pipeline. "
                                       "Default lanczos.",
                        "default": "lanczos",
                    },
                },
                "required": ["source_image"],
            },
        ),
        Tool(
            name="check_image",
            description="Check a pending upscale job. Waits up to `wait` seconds (default 20) "
                        "and returns the finished image inline when ready, a progress status "
                        "('still rendering', with % when available), or an error description — "
                        "call again until the image arrives. The job_id comes from "
                        "upscale_image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id returned by upscale_image.",
                    },
                    "wait": {
                        "type": "integer",
                        "description": "How long to wait for readiness in this call, seconds. Default 20.",
                        "default": 20,
                    },
                },
                "required": ["job_id"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name, arguments):
    if name == "list_images":
        files = await asyncio.to_thread(_list_inbox)
        if not files:
            return [TextContent(
                type="text",
                text=f"Inbox folder is empty: {INBOX_DIR}. "
                     f"Ask the user to drop the image(s) there and try again.",
            )]
        return [TextContent(type="text", text="\n".join(files))]

    if name == "upscale_image":
        source_path = arguments["source_image"]
        multiplier = float(arguments.get("multiplier", 2))
        longer_size = int(arguments["longer_size"]) if arguments.get("longer_size") else None
        scale_method = arguments.get("scale_method", "lanczos")
        if not os.path.isfile(source_path):
            return [TextContent(
                type="text",
                text=f"source_image not found: {source_path}. "
                     f"Use list_images first (chat attachments appear there automatically).",
            )]
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        try:
            await asyncio.to_thread(_start_job, source_path, multiplier, longer_size, scale_method, job_id)
        except Exception as e:
            jobs[job_id] = {"status": "error", "error": str(e)}
            return _content_for_result(jobs[job_id])
        # фоновый опрос результата, ответ отдаём сразу
        threading.Thread(target=_poll_job, args=(job_id,), daemon=True).start()
        jobstore.put(job_id, {"status": "queued", "prompt_id": jobs[job_id]["prompt_id"]})
        mode = f"longer_size={longer_size}" if longer_size else f"x{multiplier}"
        return [TextContent(
            type="text",
            text=f"Upscale task accepted. job_id={job_id} (source={source_path}, "
                 f"{mode}, method={scale_method}, prompt_id={jobs[job_id]['prompt_id']}). "
                 f"Rendering takes several minutes — call check_image with this job_id "
                 f"(call it repeatedly until the image arrives).",
        )]

    if name == "check_image":
        job_id = arguments["job_id"]
        if job_id not in jobs:
            # задача могла быть создана другим comfy-mcp сервером — смотрим общий реестр
            return _foreign_check(job_id)
        wait = min(int(arguments.get("wait", CHECK_WAIT_DEFAULT)), CHECK_WAIT_MAX)
        deadline = time.time() + wait
        while time.time() < deadline:
            if jobs[job_id]["status"] != "queued":
                return _content_for_result(jobs[job_id])
            await asyncio.sleep(POLL_INTERVAL)
        note = jobs[job_id].get("note")
        msg = f"Still rendering (job_id={job_id}).{_progress_note(jobs[job_id])} Call check_image again."
        if note:
            msg += f" Last note: {note}."
        return [TextContent(type="text", text=msg)]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    threading.Thread(target=_ws_listen_loop, daemon=True).start()
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
