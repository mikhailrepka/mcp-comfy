"""MCP-сервер: image-to-3D (Hunyuan3D v2 multiview turbo) через ComfyUI на удалённом PC.

Запуск: python3 comfy_3d_mcp_server.py
Зависимости: pip install "mcp<2" trimesh

Вход — 1-4 вида объекта (front обязателен; back/left/right опциональны):
один объект на однородном светлом фоне. Текстового промта у модели нет —
«описанием» служит front-вид. Результат — GLB в ~/comfy_models; локально
конвертируется в STL/OBJ (trimesh, без сети).

Протокол как у генерации: generate_3d → job_id → check_image.
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
WORKFLOW = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "3d_hunyuan3d_multiview_to_model_turbo.json")
MODEL_DIR = os.path.expanduser("~/comfy_models")
FAST_WAIT = 120        # сколько generate_3d ждёт результат сама, секунд
CHECK_WAIT = 900       # сколько check_image ждёт максимум, секунд
POLL_INTERVAL = 2

# id узлов workflow (см. 3d_hunyuan3d_multiview_to_model_turbo.json)
N_FRONT = "56"         # LoadImage: front (обязателен)
N_BACK = "82"          # LoadImage: back (опционален)
N_ENC_BACK = "81"      # CLIPVisionEncode для back
N_COND = "65"          # Hunyuan3Dv2ConditioningMultiView (front/back/left/right)
N_SAMPLER = "3"        # KSampler: seed/steps

app = Server("comfy-image-3d")
jobs = {}
CLIENT_ID = f"mcp_3d_{random.randint(0, 10**10)}"


def _upload_image(path):
    """Загружает локальный файл на PC (в input ComfyUI), возвращает имя файла.
    LoadImage принимает только имена из input-папки PC, не локальные пути."""
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


def _submit(front, back, left, right, steps, seed, octree_resolution) -> tuple:
    """Грузит виды на PC, собирает граф под переданные виды (2-4), ставит задачу.
    -> (prompt_id, seed)."""
    with open(WORKFLOW) as f:
        wf = json.load(f)
    wf[N_FRONT]["inputs"]["image"] = _upload_image(front)
    if back:
        wf[N_BACK]["inputs"]["image"] = _upload_image(back)
    else:
        # back не передан — вырезаем его узел и энкодер, чтобы не валидировались
        wf[N_COND]["inputs"].pop("back", None)
        wf.pop(N_BACK, None)
        wf.pop(N_ENC_BACK, None)
    for side, name in (("left", left), ("right", right)):
        if name:
            # опциональные виды: добавляем копию CLIPVisionEncode-узла
            enc_id = f"enc_{side}"
            wf[enc_id] = {
                "inputs": {"crop": "none", "clip_vision": ["54", 1],
                           "image": _upload_image(name)},
                "class_type": "CLIPVisionEncode",
                "_meta": {"title": f"CLIP Vision Encode ({side})"},
            }
            wf[N_COND]["inputs"][side] = [enc_id, 0]
    if seed is None:
        seed = random.randint(0, 2 ** 32)
    wf[N_SAMPLER]["inputs"]["seed"] = int(seed)
    wf[N_SAMPLER]["inputs"]["steps"] = int(steps)
    if octree_resolution:
        wf["61"]["inputs"]["octree_resolution"] = int(octree_resolution)
    req = urllib.request.Request(
        f"{PC}/prompt",
        data=json.dumps({"prompt": wf, "client_id": CLIENT_ID}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        pid = json.loads(urllib.request.urlopen(req, timeout=30).read())["prompt_id"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(_prompt_error_text(e)) from None
    return pid, seed


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


def _history(prompt_id: str) -> dict:
    return json.loads(urllib.request.urlopen(f"{PC}/history/{prompt_id}", timeout=30).read())


def _error_from_history(hist: dict, prompt_id: str):
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


def _fetch_model(prompt_id: str):
    """Возвращает (path, bytes) готового GLB или None. SaveGLB пишет в output/mesh/,
    ключ outputs у узла может отличаться между сборками — ищем первый файловый
    выход в любом outputs-узле."""
    hist = _history(prompt_id)
    if prompt_id not in hist:
        return None
    for node_out in hist[prompt_id]["outputs"].values():
        items = []
        if isinstance(node_out, dict):
            for val in node_out.values():
                if isinstance(val, list):
                    items.extend(val)
        elif isinstance(node_out, list):
            items.extend(node_out)
        for item in items:
            if isinstance(item, dict) and "filename" in item:
                    params = urllib.parse.urlencode({
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    })
                    data = urllib.request.urlopen(f"{PC}/view?{params}", timeout=120).read()
                    os.makedirs(MODEL_DIR, exist_ok=True)
                    out = os.path.join(MODEL_DIR, os.path.basename(item["filename"]))
                    with open(out, "wb") as fh:
                        fh.write(data)
                    return out, data
    return None


def _run_job(front, back, left, right, steps, octree_resolution, job_id, seed=None):
    try:
        pid, seed = _submit(front, back, left, right, steps, seed, octree_resolution)
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
            res = _fetch_model(pid)
            if res:
                jobs[job_id] = {"status": "done", "path": res[0], "data": res[1], "seed": seed}
                jobstore.put(job_id, {"status": "done", "path": res[0], "seed": seed})
                return
        jobs[job_id] = {"status": "error", "error": "timeout waiting for ComfyUI"}
        jobstore.put(job_id, jobs[job_id])
    except Exception as e:
        jobs[job_id] = {"status": "error", "error": str(e)}
        jobstore.put(job_id, jobs[job_id])


def _convert_3d(path, formats):
    """GLB -> форматы trimesh (stl/obj/ply...) рядом с исходником. -> [(fmt, path, note)]."""
    import trimesh
    mesh = trimesh.load(path, force="mesh")
    out = []
    for fmt in formats:
        fmt = fmt.lower().lstrip(".")
        dst = os.path.splitext(path)[0] + "." + fmt
        mesh.export(dst)
        kb = os.path.getsize(dst) // 1024
        note = (f"({len(mesh.faces)} faces, "
                f"{'watertight' if mesh.is_watertight else 'NOT watertight'}, {kb}KB)")
        out.append((fmt, dst, note))
    return out


def _content_for_result(job: dict) -> list:
    if job["status"] == "done":
        seed_note = f"\nSeed: {job['seed']}" if job.get("seed") is not None else ""
        # GLB не инлайним (не картинка) — отдаём путь и факты о меше
        note = ""
        try:
            import trimesh
            mesh = trimesh.load(job["path"], force="mesh")
            ext = os.path.splitext(job["path"])[1].lstrip(".")
            note = (f" ({len(mesh.faces)} faces, "
                    f"{'watertight' if mesh.is_watertight else 'NOT watertight'}, "
                    f"bounds {mesh.extents.round(2).tolist()})")
        except Exception:
            pass
        return [TextContent(type="text",
                text=f"3D model ready: {job['path']}{note}{seed_note}\n"
                     f"Convert it with convert_3d if the user needs STL/OBJ.")]
    return [TextContent(type="text", text=f"3D generation failed: {job['error']}")]


def _foreign_check(job_id: str, wait=1) -> list:
    rec = jobstore.get(job_id)
    if rec is None:
        return [TextContent(type="text",
                text=f"Unknown job_id: {job_id}. It may belong to a restarted session — "
                     f"start a new task.")]
    status = rec.get("status")
    if status == "done":
        return _content_for_result({"status": "done", "path": rec["path"],
                                    "seed": rec.get("seed")})
    if status == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    time.sleep(min(wait, 5))
    rec = jobstore.get(job_id) or rec
    if rec.get("status") == "done":
        return _content_for_result({"status": "done", "path": rec["path"],
                                    "seed": rec.get("seed")})
    if rec.get("status") == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    return [TextContent(type="text",
            text=f"Still rendering (job_id={job_id}, started by another comfy-mcp "
                 f"server). Call check_image again.")]


async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="generate_3d",
            description="Generate a 3D model (GLB mesh) from images of an object using "
                        "Hunyuan3D multiview on the remote PC. IMAGE-TO-3D ONLY: there is "
                        "no text prompt — the front view image IS the description. Input "
                        "images should show ONE object on a plain light/uniform background "
                        "(use remove_background first if the background is busy). Views: "
                        "front required; back/left/right optional and improve quality when "
                        "the user has them. Returns job_id immediately; rendering takes a "
                        "few minutes — poll check_image until the GLB path arrives, then "
                        "offer convert_3d for STL/OBJ.",
            inputSchema={
                "type": "object",
                "properties": {
                    "front_image": {
                        "type": "string",
                        "description": "Absolute path: front view of the object.",
                    },
                    "back_image": {
                        "type": "string",
                        "description": "Optional: back view path.",
                    },
                    "left_image": {
                        "type": "string",
                        "description": "Optional: left view path.",
                    },
                    "right_image": {
                        "type": "string",
                        "description": "Optional: right view path.",
                    },
                    "steps": {
                        "type": "integer",
                        "description": "Sampling steps. Default 20.",
                        "default": 20,
                    },
                    "octree_resolution": {
                        "type": "integer",
                        "description": "Mesh voxel resolution (default 256; higher = more "
                                       "detail and slower).",
                        "default": 256,
                    },
                    "seed": {
                        "type": "integer",
                        "description": "Optional: fix the seed to reproduce a mesh "
                                       "(returned in the result). Default: random.",
                    },
                },
                "required": ["front_image"],
            },
        ),
        Tool(
            name="check_image",
            description="Check a pending 3D job and return the GLB path when ready. "
                        "Accepts job_id from generate_3d (shared registry across servers).",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string"},
                    "wait": {"type": "integer", "description": "Seconds to wait. Default 20.",
                             "default": 20},
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="convert_3d",
            description="Convert a 3D model file (GLB) to other formats locally (STL for "
                        "3D printing, OBJ, PLY) using trimesh — no network. Reports face "
                        "count, watertightness (important for printing) and size. Returns "
                        "the converted paths.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the GLB (from generate_3d).",
                    },
                    "formats": {
                        "type": "array",
                        "items": {"type": "string",
                                  "enum": ["stl", "obj", "ply", "glb"]},
                        "description": "Target formats. Default [\"stl\"].",
                        "default": ["stl"],
                    },
                },
                "required": ["path"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    if name == "generate_3d":
        front = arguments["front_image"]
        for role, p in (("front_image", front),):
            if not os.path.isfile(os.path.expanduser(p)):
                return [TextContent(type="text", text=f"{role} not found: {p}")]
        views = {}
        for arg, role in (("back_image", "back"), ("left_image", "left"),
                          ("right_image", "right")):
            p = arguments.get(arg)
            if p:
                p = os.path.expanduser(p)
                if not os.path.isfile(p):
                    return [TextContent(type="text", text=f"{arg} not found: {p}")]
                views[role] = p
        steps = int(arguments.get("steps", 20))
        octree = int(arguments.get("octree_resolution", 256))
        seed = arguments.get("seed")
        seed = int(seed) if seed is not None else None
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        loop = asyncio.get_event_loop()
        threading.Thread(target=_run_job,
                         args=(os.path.expanduser(front), views.get("back"),
                               views.get("left"), views.get("right"),
                               steps, octree, job_id, seed),
                         daemon=True).start()
        deadline = time.time() + FAST_WAIT
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            if jobs[job_id]["status"] != "queued":
                return _content_for_result(jobs[job_id])
        return [TextContent(type="text",
                text=f"3D model is still rendering on the remote PC (job_id={job_id}). "
                     f"Call check_image with this job_id to get the result.")]

    if name == "check_image":
        job_id = arguments["job_id"]
        if job_id not in jobs:
            return _foreign_check(job_id, wait=max(int(arguments.get("wait", 0)), 1))
        wait = min(int(arguments.get("wait", 20)), 300)
        deadline = time.time() + wait
        while time.time() < deadline:
            if jobs[job_id]["status"] != "queued":
                return _content_for_result(jobs[job_id])
            await asyncio.sleep(POLL_INTERVAL)
        return [TextContent(type="text",
                text=f"Still rendering (job_id={job_id}). Call check_image again.")]

    if name == "convert_3d":
        path = os.path.expanduser(arguments["path"])
        formats = arguments.get("formats") or ["stl"]
        if not os.path.isfile(path):
            return [TextContent(type="text", text=f"File not found: {path}")]
        try:
            results = await asyncio.to_thread(_convert_3d, path, formats)
        except Exception as e:
            return [TextContent(type="text", text=f"Convert failed: {e}")]
        lines = [f"Converted {path}:"]
        for fmt, dst, note in results:
            lines.append(f"  {dst} {note}")
        lines.append("STL has no color data — it is geometry only, as expected for printing.")
        return [TextContent(type="text", text="\n".join(lines))]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
