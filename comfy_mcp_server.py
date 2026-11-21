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

PC = "http://192.168.1.218:8188"
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nvidia-image-gen.json")
OUTPUT_DIR = os.path.expanduser("~/comfy_images")
FAST_WAIT = 90        # сколько generate_image ждёт результат сама, секунд
CHECK_WAIT = 600      # сколько check_image ждёт максимум, секунд
POLL_INTERVAL = 1     # период опроса /history

app = Server("comfy-image-gen")

# job_id -> {"status": "queued"|"done", "path": ..., "error": ...}
jobs = {}

CLIENT_ID = f"mcp_gen_{random.randint(0, 10**10)}"
ws_progress = {}  # prompt_id -> {"value": int, "max": int} из websocket-событий ComfyUI


def _submit(prompt: str, width: int, height: int, steps: int) -> str:
    with open(WORKFLOW) as f:
        wf = json.load(f)
    wf["57:27"]["inputs"]["text"] = prompt
    wf["57:13"]["inputs"]["width"] = width
    wf["57:13"]["inputs"]["height"] = height
    wf["57:3"]["inputs"]["seed"] = random.randint(0, 2**32)
    wf["57:3"]["inputs"]["steps"] = steps
    req = urllib.request.Request(
        f"{PC}/prompt",
        data=json.dumps({"prompt": wf, "client_id": CLIENT_ID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]


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
    """Возвращает (path, bytes) если картинка готова, иначе None."""
    hist = _history(prompt_id)
    if prompt_id not in hist:
        return None
    imgs = hist[prompt_id]["outputs"].get("9", {}).get("images", [])
    if not imgs:
        return None
    img = imgs[0]
    params = urllib.parse.urlencode({
        "filename": img["filename"],
        "subfolder": img["subfolder"],
        "type": img["type"],
    })
    data = urllib.request.urlopen(f"{PC}/view?{params}", timeout=60).read()
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out = os.path.join(OUTPUT_DIR, img["filename"])
    with open(out, "wb") as fh:
        fh.write(data)
    return out, data


def _run_job(prompt: str, width: int, height: int, steps: int, job_id: str):
    try:
        pid = _submit(prompt, width, height, steps)
        jobs[job_id]["prompt_id"] = pid
        jobstore.put(job_id, {"status": "queued", "prompt_id": pid})
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
                jobs[job_id] = {"status": "done", "path": res[0], "data": res[1]}
                jobstore.put(job_id, {"status": "done", "path": res[0]})
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
        content.append(ImageContent(
            type="image",
            data=base64.b64encode(job["data"]).decode(),
            mimeType="image/png",
        ))
        content.append(TextContent(type="text", text=f"Image generated: {job['path']}"))
    else:
        content.append(TextContent(type="text", text=f"Generation failed: {job['error']}"))
    return content


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_image",
            description="Generate an image from a text prompt using ComfyUI on the remote PC. "
                        "Usually returns the finished image inline within ~90 seconds; "
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
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="check_image",
            description="Check a pending image generation job and return the image when ready. "
                        "Call with the job_id returned by generate_image.",
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
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    if name == "generate_image":
        prompt = arguments["prompt"]
        width = int(arguments.get("width", 1024))
        height = int(arguments.get("height", 1024))
        steps = int(arguments.get("steps", 8))
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        loop = asyncio.get_event_loop()
        threading.Thread(target=_run_job, args=(prompt, width, height, steps, job_id), daemon=True).start()

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
        deadline = time.time() + CHECK_WAIT
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            if jobs[job_id]["status"] != "queued":
                return _content_for_result(jobs[job_id])
        return [TextContent(type="text", text=f"Still rendering (job_id={job_id}).{_progress_note(jobs[job_id])} Try again.")]

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
        return _image_file_content(rec["path"])
    if status == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    pr = rec.get("progress")
    prog = ""
    if pr and pr.get("max"):
        prog = f" Progress: {pr['value']}/{pr['max']} ({int(pr['value'] * 100 / pr['max'])}%)."
    return [TextContent(type="text",
            text=f"Still rendering (job_id={job_id}, started by another comfy-mcp "
                 f"server).{prog} Call check_image again.")]


def _image_file_content(path: str) -> list:
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        path.rsplit(".", 1)[-1].lower(), "image/png")
    with open(path, "rb") as fh:
        data = fh.read()
    return [
        ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime),
        TextContent(type="text", text=f"Image ready: {path}"),
    ]


def _progress_note(job: dict) -> str:
    pid = job.get("prompt_id")
    pr = job.get("progress") or (ws_progress.get(pid) if pid else None)
    if pr and pr.get("max"):
        return f" Progress: {pr['value']}/{pr['max']} steps ({int(pr['value'] * 100 / pr['max'])}%)."
    return ""


async def main():
    threading.Thread(target=_ws_listen_loop, daemon=True).start()
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
