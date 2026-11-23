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

# ComfyUI PC: env COMFY_PC_URL -> local.env рядом с сервером -> дефолт ниже.
# local.env (строка COMFY_PC_URL=..., untracked) пишут installer/update.sh — сами
# файлы серверов остаются чистыми, поэтому git pull проходит без конфликтов.
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


def _submit(prompt, source_name, ref_name, steps, seed=None):
    """Ставит задачу в очередь. Возвращает (prompt_id, seed)."""
    with open(WORKFLOW) as f:
        wf = json.load(f)
    wf[NODE_SOURCE]["inputs"]["image"] = source_name
    if NODE_REF and ref_name:
        wf[NODE_REF]["inputs"]["image"] = ref_name
    elif NODE_REF:
        # Референс не передан — вырезаем его узел из графа ВМЕСТЕ со всеми
        # входами-ссылками на него. ComfyUI валидирует ВСЕ узлы графа, включая
        # неактивные ветки: оставшийся LoadImage с дефолтным именем файла из
        # шаблона даёт 400 'Invalid image file', если этого файла нет во
        # входной папке PC (наблюдалось live 2026-11-23,
        # clothing_light_blue_denim_shirt.png).
        wf.pop(NODE_REF, None)
        for node in wf.values():
            ins = node.get("inputs")
            if not isinstance(ins, dict):
                continue
            node["inputs"] = {k: v for k, v in ins.items()
                              if not (isinstance(v, list) and v[0] == NODE_REF)}
    wf[NODE_PROMPT]["inputs"]["prompt"] = prompt
    if seed is None:
        seed = random.randint(0, 2 ** 32)
    wf[NODE_SAMPLER]["inputs"]["seed"] = int(seed)
    if "steps" in wf[NODE_SAMPLER]["inputs"]:
        wf[NODE_SAMPLER]["inputs"]["steps"] = steps
    req = urllib.request.Request(
        PC + "/prompt",
        data=json.dumps({"prompt": wf, "client_id": CLIENT_ID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"], seed
    except urllib.error.HTTPError as e:
        # причина отказа валидации лежит в теле ответа — без этого клиент видит
        # только "HTTP Error 400" и не может понять, что не так
        raise RuntimeError(_prompt_error_text(e)) from None


def _prompt_error_text(e):
    """Читаемое описание отказа /prompt из тела 400 (error + node_errors)."""
    try:
        body = json.loads(e.read().decode())
    except Exception:
        return f"ComfyUI rejected the prompt: HTTP {e.code}"
    parts = []
    err = body.get("error") or {}
    if err.get("message"):
        parts.append(str(err["message"]))
    for nid, ne in (body.get("node_errors") or {}).items():
        if not isinstance(ne, dict):
            continue
        for d in ne.get("errors", []):
            msg = d.get("message", "?")
            det = d.get("details", "")
            parts.append(f"node {nid}: {msg}" + (f" — {det}" if det else ""))
    if not parts:
        return f"ComfyUI rejected the prompt: HTTP {e.code}"
    return "ComfyUI rejected the prompt: " + "; ".join(parts)


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


def _start_job(prompt, source_path, ref_path, steps, job_id, seed=None):
    """Загружает картинки на PC и ставит задачу в очередь. Возвращает prompt_id."""
    src = _upload_image(source_path)
    ref = _upload_image(ref_path) if (NODE_REF and ref_path) else None
    pid, seed = _submit(prompt, src, ref, steps, seed)
    jobs[job_id]["prompt_id"] = pid
    jobs[job_id]["seed"] = seed
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
            jobs[job_id] = {"status": "done", "path": res[0], "data": res[1],
                            "seed": job.get("seed")}
            jobstore.put(job_id, {"status": "done", "path": res[0], "seed": job.get("seed")})
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


def _convert_images(paths, fmt="jpg", quality=90, max_width=None, max_height=None):
    """Конвертирует картинки в fmt (jpg/png) с качеством quality, сохраняя рядом
    с исходником (для файлов в INBOX_DIR) — возвращает список новых путей."""
    from PIL import Image

    fmt = fmt.lower().lstrip(".")
    if fmt in ("jpeg", "jpg"):
        fmt, pil_fmt, ext = "jpg", "JPEG", ".jpg"
    elif fmt == "png":
        fmt, pil_fmt, ext = "png", "PNG", ".png"
    elif fmt == "webp":
        fmt, pil_fmt, ext = "webp", "WEBP", ".webp"
    else:
        raise ValueError(f"Unsupported format: {fmt} (use jpg, png or webp)")
    quality = max(1, min(int(quality), 100))
    max_width = int(max_width) if max_width else None
    max_height = int(max_height) if max_height else None

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
            if max_width or max_height:
                w, h = im.size
                scale = min((max_width / w) if max_width else 1.0,
                            (max_height / h) if max_height else 1.0, 1.0)
                if scale < 1.0:
                    im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                   Image.LANCZOS)
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
            elif pil_fmt == "WEBP":
                im.save(dst, pil_fmt, quality=quality)
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

    import fcntl

    while True:
        try:
            if os.path.isdir(LM_FILES_DIR):
                # атомарный claim: под файловой блокировкой перечитываем seen
                # (могут работать несколько инстансов сервера) и добавляем новые
                with open(SEEN_STATE, "a+") as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX)
                    fh.seek(0)
                    try:
                        seen = set(json.load(fh))
                    except Exception:
                        seen = set()
                    copied = []
                    for f in sorted(os.listdir(LM_FILES_DIR)):
                        if f in seen or f.lower().endswith((".metadata.json", ".json")):
                            continue
                        if not f.lower().endswith(IMAGE_EXTS):
                            continue
                        src_path = os.path.join(LM_FILES_DIR, f)
                        if not os.path.isfile(src_path):
                            continue
                        # не копируем файл, который ещё пишется
                        try:
                            if time.time() - os.path.getmtime(src_path) < 2:
                                continue
                        except OSError:
                            continue
                        seen.add(f)
                        copied.append((f, src_path))
                    fh.seek(0)
                    fh.truncate()
                    json.dump(sorted(seen), fh)
                    fcntl.flock(fh, fcntl.LOCK_UN)
                for f, src_path in copied:
                    orig = _original_name(f)
                    base = _sanitize_name(orig) if orig else _sanitize_name(f)
                    dst = os.path.join(INBOX_DIR, f"chat_{base}")
                    n = 1
                    while os.path.exists(dst):
                        root_, ext_ = os.path.splitext(f"chat_{base}")
                        dst = os.path.join(INBOX_DIR, f"{root_}_{n}{ext_}")
                        n += 1
                    import shutil
                    shutil.copy2(src_path, dst)
                    print(f"[watch] copied {f} -> {dst}", flush=True)
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
        seed_note = f"\nSeed: {job['seed']}" if job.get("seed") is not None else ""
        content.append(TextContent(type="text", text=f"Edited image: {job['path']}{seed_note}"))
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
        return _image_file_content(rec["path"], rec.get("seed"))
    if status == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    pr = rec.get("progress")
    prog = ""
    if pr and pr.get("max"):
        prog = f" Progress: {pr['value']}/{pr['max']} ({int(pr['value'] * 100 / pr['max'])}%)."
    time.sleep(min(wait, 5))
    rec = jobstore.get(job_id) or rec
    if rec.get("status") == "done":
        return _image_file_content(rec["path"], rec.get("seed"))
    if rec.get("status") == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    return [TextContent(type="text",
            text=f"Still rendering (job_id={job_id}, started by another comfy-mcp "
                 f"server).{prog} Call check_image again.")]


def _image_file_content(path, seed=None):
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        path.rsplit(".", 1)[-1].lower(), "image/png")
    with open(path, "rb") as fh:
        data = fh.read()
    seed_note = f"\nSeed: {seed}" if seed is not None else ""
    return [
        ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime),
        TextContent(type="text", text=f"Image ready: {path}{seed_note}"),
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
        "seed": {
            "type": "integer",
            "description": "Optional: fix the seed to reproduce or iterate on a previous "
                           "edit (the used seed is returned in the result text). "
                           "Default: random.",
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
                        "enum": ["jpg", "png", "webp"],
                        "description": "Target format. Default jpg.",
                        "default": "jpg",
                    },
                    "quality": {
                        "type": "integer",
                        "description": "JPG/WEBP quality 1-100. Default 90.",
                        "default": 90,
                    },
                    "max_width": {
                        "type": "integer",
                        "description": "Optional: downscale so width <= this (aspect preserved, never upscales). E.g. 1920 for phone photos.",
                    },
                    "max_height": {
                        "type": "integer",
                        "description": "Optional: downscale so height <= this (aspect preserved, never upscales).",
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
        seed = arguments.get("seed")
        seed = int(seed) if seed is not None else None
        if not os.path.isfile(source_path):
            return [TextContent(
                type="text",
                text=f"source_image not found: {source_path}. "
                     f"Use list_images or save_images first.",
            )]
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        try:
            await asyncio.to_thread(_start_job, prompt, source_path, ref_path, steps,
                                    job_id, seed)
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
            converted = await asyncio.to_thread(
                _convert_images, paths, fmt, quality,
                arguments.get("max_width"), arguments.get("max_height"))
        except Exception as e:
            return [TextContent(type="text", text=f"Convert failed: {e}")]
        ok = [p for p in converted if not p.startswith("NOT FOUND")]
        bad = [p for p in converted if p.startswith("NOT FOUND")]
        size_note = ""
        if arguments.get("max_width") or arguments.get("max_height"):
            size_note = f", max {arguments.get('max_width') or '∞'}x{arguments.get('max_height') or '∞'}"
        lines = [f"Converted to {fmt} (quality={quality}{size_note}):"] + [f"  {p}" for p in ok] + bad
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
