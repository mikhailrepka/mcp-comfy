"""MCP-сервер: редактирование изображений через ComfyUI на удалённом PC.

Сгенерирован make_comfy_edit_mcp.py из image_qwen_image_2_1_image_edit.json.
Запуск: /Users/mikhailrepka/comfy-mcp/.venv/bin/python edit_mcp_server.py
Зависимости: pip install "mcp<2"

Протокол как у генерации:
- edit_image: загружает картинки на PC, ставит задачу в очередь и СРАЗУ возвращает
  job_id ("задача взята в работу") — никакого долгого ожидания, чтобы не упираться
  в таймаут tool-вызова клиента.
- check_image: периодический опрос готовности; когда готово — возвращает картинку инлайн.
- save_images: копирует картинки в INBOX_DIR по путям или base64 (для клиентов,
  где вложения доступны как файлы).
- Фоновый watch-поток следит за ~/.lmstudio/user-files: новые картинки-вложения
  чата LM Studio автоматически копируются в INBOX_DIR и видны через list_images.
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
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "image_qwen_image_2_1_image_edit.json")
OUTPUT_DIR = os.path.expanduser("~/comfy_images")
INBOX_DIR = os.path.expanduser("~/comfy_edit_inbox")  # сюда пользователь кладёт картинки для редактирования
LM_FILES_DIR = os.path.expanduser("~/.lmstudio/user-files")  # вложения чата LM Studio
WATCH_INTERVAL = 5          # период сканирования user-files, секунд
SEEN_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".lmstudio_seen.json")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")
POLL_INTERVAL = 1
CHECK_WAIT_DEFAULT = 20   # сколько check_image ждёт за один вызов, секунд
CHECK_WAIT_MAX = 120      # верхний предел wait для check_image

# id узлов в workflow
NODE_SOURCE = "470"   # LoadImage: исходное изображение
NODE_REF = "475"         # LoadImage: референс (или None)
NODE_PROMPT = "459:474"
NODE_SAMPLER = "459:458"
NODE_SAVE = "461"

app = Server("comfy-image-edit")
jobs = {}

CLIENT_ID = f"mcp_edit_{random.randint(0, 10**10)}"
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


def _submit(prompt, source_name, ref_name, steps):
    with open(WORKFLOW) as f:
        wf = json.load(f)
    wf[NODE_SOURCE]["inputs"]["image"] = source_name
    if NODE_REF and ref_name:
        wf[NODE_REF]["inputs"]["image"] = ref_name
    wf[NODE_PROMPT]["inputs"]["prompt"] = prompt
    wf[NODE_SAMPLER]["inputs"]["seed"] = random.randint(0, 2 ** 32)
    if "steps" in wf[NODE_SAMPLER]["inputs"]:
        wf[NODE_SAMPLER]["inputs"]["steps"] = steps
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
    data = urllib.request.urlopen(PC + "/view?" + params, timeout=60).read()
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


def _start_job(prompt, source_path, ref_path, steps, job_id):
    """Загружает картинки на PC и ставит задачу в очередь. Возвращает prompt_id."""
    src = _upload_image(source_path)
    ref = _upload_image(ref_path) if (NODE_REF and ref_path) else None
    pid = _submit(prompt, src, ref, steps)
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


def _convert_images(paths, fmt="jpg", quality=90):
    """Конвертирует картинки в fmt (jpg/png) с качеством quality, сохраняя рядом
    с исходником (для файлов в INBOX_DIR) — возвращает список новых путей."""
    from PIL import Image

    fmt = fmt.lower().lstrip(".")
    if fmt in ("jpeg", "jpg"):
        fmt, pil_fmt, ext = "jpg", "JPEG", ".jpg"
    elif fmt == "png":
        fmt, pil_fmt, ext = "png", "PNG", ".png"
    else:
        raise ValueError(f"Unsupported format: {fmt} (use jpg or png)")
    quality = max(1, min(int(quality), 100))

    converted = []
    for p in paths:
        p = os.path.expanduser(p)
        if not os.path.isfile(p):
            converted.append(f"NOT FOUND: {p}")
            continue
        root, _ = os.path.splitext(p)
        dst = root + ext
        n = 1
        while os.path.exists(dst) and os.path.abspath(dst) != os.path.abspath(p):
            dst = f"{root}_{n}{ext}"
            n += 1
        with Image.open(p) as im:
            if pil_fmt == "JPEG":
                # JPEG не умеет альфу — кладём на белый фон
                if im.mode in ("RGBA", "LA", "P"):
                    im = im.convert("RGBA")
                    bg = Image.new("RGB", im.size, (255, 255, 255))
                    bg.paste(im, mask=im.split()[-1])
                    im = bg
                else:
                    im = im.convert("RGB")
                im.save(dst, pil_fmt, quality=quality, optimize=True)
            else:
                im.save(dst, pil_fmt)
        converted.append(dst)
    return converted


def _save_images(paths=None, images=None):
    """Копирует картинки (из чата или откуда угодно) в INBOX_DIR. Возвращает пути."""
    import shutil
    import uuid

    os.makedirs(INBOX_DIR, exist_ok=True)
    saved = []
    if paths:
        for p in paths:
            p = os.path.expanduser(p)
            if not os.path.isfile(p):
                saved.append(f"NOT FOUND: {p}")
                continue
            dst = os.path.join(INBOX_DIR, f"chat_{uuid.uuid4().hex[:8]}_{os.path.basename(p)}")
            shutil.copy2(p, dst)
            saved.append(dst)
    if images:
        for i, item in enumerate(images):
            try:
                data = base64.b64decode(item["data"])
            except Exception:
                saved.append(f"BAD BASE64: item {i}")
                continue
            ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                   "image/bmp": ".bmp", "image/gif": ".gif"}.get(item.get("mimeType", ""), ".png")
            dst = os.path.join(INBOX_DIR, f"chat_{uuid.uuid4().hex[:8]}{ext}")
            with open(dst, "wb") as fh:
                fh.write(data)
            saved.append(dst)
    return saved


def _sanitize_name(name):
    """Безопасное имя файла: оставляем буквы/цифры/._- , остальное — на подчёркивание."""
    name = os.path.basename(name)
    out = "".join(c if (c.isalnum() or c in "._- ") else "_" for c in name)
    return out.strip() or "image"


def _original_name(lm_fname):
    """originalName из .metadata.json рядом с файлом вложения, если есть."""
    meta_path = os.path.join(LM_FILES_DIR, lm_fname + ".metadata.json")
    try:
        with open(meta_path, encoding="utf-8") as fh:
            meta = json.load(fh)
        orig = meta.get("originalName")
        if orig and os.path.splitext(orig)[1].lower() in IMAGE_EXTS:
            return orig
    except Exception:
        pass
    return None


def _watch_user_files_loop():
    """Копирует новые картинки-вложения чата LM Studio в INBOX_DIR.

    user-files — постоянная библиотека LM Studio, поэтому уже скопированные
    файлы запоминаем в SEEN_STATE и при следующем запуске не копируем заново.
    """
    os.makedirs(INBOX_DIR, exist_ok=True)
    seen = set()
    if os.path.isfile(SEEN_STATE):
        try:
            with open(SEEN_STATE) as fh:
                seen = set(json.load(fh))
        except Exception:
            seen = set()
    else:
        # первый запуск: исторические вложения библиотеки помечаем как увиденные,
        # чтобы не засыпать inbox старыми картинками; копируем только новые
        try:
            if os.path.isdir(LM_FILES_DIR):
                seen = {
                    f for f in os.listdir(LM_FILES_DIR)
                    if f.lower().endswith(IMAGE_EXTS)
                }
                with open(SEEN_STATE, "w") as fh:
                    json.dump(sorted(seen), fh)
                print(f"[watch] initialized with {len(seen)} existing user-files, "
                      f"only new attachments will be copied", flush=True)
        except Exception as e:
            print(f"[watch] init error: {e}", flush=True)

    while True:
        try:
            if os.path.isdir(LM_FILES_DIR):
                new_seen = []
                for f in sorted(os.listdir(LM_FILES_DIR)):
                    if f in seen or f.lower().endswith((".metadata.json", ".json")):
                        continue
                    if not f.lower().endswith(IMAGE_EXTS):
                        continue
                    src = os.path.join(LM_FILES_DIR, f)
                    if not os.path.isfile(src):
                        continue
                    # не копируем файл, который ещё пишется
                    try:
                        if time.time() - os.path.getmtime(src) < 2:
                            continue
                    except OSError:
                        continue
                    orig = _original_name(f)
                    base = _sanitize_name(orig) if orig else _sanitize_name(f)
                    dst = os.path.join(INBOX_DIR, f"chat_{base}")
                    n = 1
                    while os.path.exists(dst):
                        root, ext = os.path.splitext(f"chat_{base}")
                        dst = os.path.join(INBOX_DIR, f"{root}_{n}{ext}")
                        n += 1
                    import shutil
                    shutil.copy2(src, dst)
                    seen.add(f)
                    new_seen.append(f)
                if new_seen:
                    with open(SEEN_STATE, "w") as fh:
                        json.dump(sorted(seen), fh)
        except Exception as e:
            print(f"[watch] user-files scan error: {e}", flush=True)
        time.sleep(WATCH_INTERVAL)


def _content_for_result(job):
    content = []
    if job["status"] == "done":
        content.append(ImageContent(
            type="image",
            data=base64.b64encode(job["data"]).decode(),
            mimeType="image/png",
        ))
        content.append(TextContent(type="text", text=f"Edited image: {job['path']}"))
    else:
        content.append(TextContent(type="text", text=f"Edit failed: {job['error']}"))
    return content


def _foreign_check(job_id, wait=1):
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
    time.sleep(min(wait, 5))
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
        return f" Progress: {pr['value']}/{pr['max']} steps ({int(pr['value'] * 100 / pr['max'])}%)."
    return ""


@app.list_tools()
async def list_tools():
    tools = await _list_tools_impl()
    # save_images осмыслен только для клиентов, которые дают пути к вложениям
    if not os.environ.get("COMFY_ENABLE_SAVE_IMAGES"):
        tools = [t for t in tools if t.name != "save_images"]
    return tools


async def _list_tools_impl():
    props = {
        "prompt": {
            "type": "string",
            "description": "Edit instruction describing what to change, in English, detailed. "
                           "The source image stays the canvas: everything not mentioned is preserved.",
        },
        "source_image": {
            "type": "string",
            "description": "Absolute path to the local image to edit.",
        },
        "steps": {
            "type": "integer",
            "description": "Number of sampling steps. Default 25.",
            "default": 25,
        },
    }
    if NODE_REF:
        props["reference_image"] = {
            "type": "string",
            "description": "Absolute path to an optional second reference image "
                           "(e.g. style or object to apply).",
        }
    return [
        Tool(
            name="list_images",
            description="List images available for editing in the inbox folder (newest first). "
                        "Images the user attaches/pastes/drops into the chat appear here "
                        "automatically within a few seconds (copied from LM Studio), as well "
                        "as images the user manually puts into the inbox folder. Then pass a "
                        "listed path to edit_image as source_image or reference_image.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="edit_image",
            description="Edit an existing image according to a text instruction using ComfyUI "
                        "(image-to-image) on the remote PC. source_image is an absolute file "
                        "path — get it from the user, from list_images, or from save_images. "
                        "Returns immediately with a job_id ('task accepted'); rendering takes "
                        "1-5 minutes. Then poll check_image with that job_id until it returns "
                        "the finished image.",
            inputSchema={
                "type": "object",
                "properties": props,
                "required": ["prompt", "source_image"],
            },
        ),
        Tool(
            name="check_image",
            description="Check a pending image edit job. Waits up to `wait` seconds (default 20) "
                        "and returns the finished image inline when ready, or a 'still rendering' "
                        "status — call again until the image arrives. The job_id comes from "
                        "edit_image.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "Job id returned by edit_image.",
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
        Tool(
            name="save_images",
            description="Save images into the inbox folder used by edit_image: pass local "
                        "file `paths` (e.g. chat attachments on disk) or base64 `images` "
                        "items. Returns the saved file paths to use as source_image / "
                        "reference_image in edit_image. Only available when the MCP client "
                        "exposes attachment file paths (set env COMFY_ENABLE_SAVE_IMAGES=1 "
                        "in the server registration); otherwise use list_images — chat "
                        "attachments are copied to the inbox automatically.",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Absolute local file paths of the chat image attachments.",
                    },
                    "images": {
                        "type": "array",
                        "description": "Alternative: raw image payloads.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "data": {"type": "string", "description": "base64-encoded image"},
                                "mimeType": {"type": "string"},
                            },
                            "required": ["data"],
                        },
                    },
                },
            },
        ),
        Tool(
            name="convert_image",
            description="Convert image(s) to JPG or PNG with a given quality, saving the "
                        "result next to the original (for inbox files — directly in the "
                        "inbox). Use it to shrink large PNGs to JPG before edit_image — "
                        "smaller uploads render faster. Returns the converted file paths.",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Absolute paths of images to convert.",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["jpg", "png"],
                        "description": "Target format. Default jpg.",
                        "default": "jpg",
                    },
                    "quality": {
                        "type": "integer",
                        "description": "JPG quality 1-100. Default 90.",
                        "default": 90,
                    },
                },
                "required": ["paths"],
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

    if name == "edit_image":
        prompt = arguments["prompt"]
        source_path = arguments["source_image"]
        ref_path = arguments.get("reference_image")
        steps = int(arguments.get("steps", 25))
        if not os.path.isfile(source_path):
            return [TextContent(
                type="text",
                text=f"source_image not found: {source_path}. "
                     f"Use list_images or save_images first.",
            )]
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        try:
            await asyncio.to_thread(_start_job, prompt, source_path, ref_path, steps, job_id)
        except Exception as e:
            jobs[job_id] = {"status": "error", "error": str(e)}
            return _content_for_result(jobs[job_id])
        # фоновый опрос результата, ответ отдаём сразу
        threading.Thread(target=_poll_job, args=(job_id,), daemon=True).start()
        jobstore.put(job_id, {"status": "queued", "prompt_id": jobs[job_id]["prompt_id"]})
        ref_note = f", reference={ref_path}" if ref_path else ""
        return [TextContent(
            type="text",
            text=f"Task accepted for editing. job_id={job_id} "
                 f"(source={source_path}{ref_note}, prompt_id={jobs[job_id]['prompt_id']}). "
                 f"Rendering takes a few minutes — call check_image with this job_id "
                 f"(call it repeatedly until the image arrives).",
        )]

    if name == "check_image":
        job_id = arguments["job_id"]
        if job_id not in jobs:
            # задача могла быть создана другим comfy-mcp сервером — смотрим общий реестр
            return _foreign_check(job_id, wait=max(int(arguments.get("wait", 0)), 1))
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

    if name == "save_images":
        saved = await asyncio.to_thread(
            _save_images, arguments.get("paths"), arguments.get("images"))
        if not saved:
            return [TextContent(
                type="text",
                text="Nothing to save: pass file `paths` (chat attachments) or base64 `images`.",
            )]
        ok = [p for p in saved if not p.startswith(("NOT FOUND", "BAD BASE64"))]
        bad = [p for p in saved if p.startswith(("NOT FOUND", "BAD BASE64"))]
        lines = [f"Saved to inbox ({INBOX_DIR}):"] + [f"  {p}" for p in ok]
        lines += bad
        lines.append("Pass one of these paths to edit_image as source_image "
                     "(and optionally another as reference_image).")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "convert_image":
        paths = arguments.get("paths") or []
        fmt = arguments.get("format", "jpg")
        quality = int(arguments.get("quality", 90))
        if not paths:
            return [TextContent(type="text", text="Nothing to convert: pass `paths`.")]
        try:
            converted = await asyncio.to_thread(_convert_images, paths, fmt, quality)
        except Exception as e:
            return [TextContent(type="text", text=f"Convert failed: {e}")]
        ok = [p for p in converted if not p.startswith("NOT FOUND")]
        bad = [p for p in converted if p.startswith("NOT FOUND")]
        lines = [f"Converted to {fmt} (quality={quality}):"] + [f"  {p}" for p in ok] + bad
        lines.append("Pass these paths to edit_image / upscale_image.")
        return [TextContent(type="text", text="\n".join(lines))]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    threading.Thread(target=_watch_user_files_loop, daemon=True).start()
    threading.Thread(target=_ws_listen_loop, daemon=True).start()
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
