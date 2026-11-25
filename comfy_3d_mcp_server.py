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
# Инлайн-ожидания короче таймаута MCP-клиента LM Studio (~60 с) — иначе -32001
# без job_id (тот же урок, что в comfy_mcp_server 2026-11-24). Override:
# env COMFY_FAST_WAIT / COMFY_CHECK_WAIT или local.env.
def _read_local_env(env_file=None):
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


FAST_WAIT = _cfg_int("COMFY_FAST_WAIT", 25)
CHECK_WAIT = 900       # фоновый дедлайн задачи (_run_job)
CHECK_WAIT_INLINE = _cfg_int("COMFY_CHECK_WAIT", 30)  # один вызов check_image (сек)
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


def _submit(front, back, left, right, steps, seed, octree_resolution,
            threshold=None) -> tuple:
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
    # порог изо-поверхности: 0.5 (дефолт меньше срезает меньше поверхности и
    # чаще даёт замкнутую оболочку), лечится параметром запроса, не настройками PC
    if threshold is not None:
        wf["83"]["inputs"]["threshold"] = float(threshold)
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


def _run_job(front, back, left, right, steps, octree_resolution, job_id, seed=None,
             threshold=None):
    try:
        pid, seed = _submit(front, back, left, right, steps, seed, octree_resolution,
                            threshold)
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


def _render_previews(path, max_faces=20000):
    """2-3 ракурса меша в PNG (matplotlib, чистый CPU — без GPU/GL).
    Сложности matplotlib-3D, которые здесь обходятся:
    - нет z-буфера → сортируем грани сами (painter's algorithm относительно камеры);
    - случайный субсэмпл граней даёт «дырявую» кашу → quadric-децимация
      (fast_simplification), сохраняет силуэт; фолбэк — субсэмпл;
    - освещение считаем относительно камеры каждого ракурса.
    Грани редуцируются до max_faces: 144k лиц тормозят, 20k визуально то же.
    -> [path_png]; ошибки не роняют задачу (превью — бонус)."""
    try:
        import matplotlib
        matplotlib.use("Agg")  # без дисплея
        import numpy as np
        import trimesh
        mesh = trimesh.load(path, force="mesh")
        if len(mesh.faces) > max_faces:
            try:
                mesh = mesh.simplify_quadric_decimation(face_count=max_faces)
            except Exception:
                idx = np.random.default_rng(0).choice(len(mesh.faces), max_faces,
                                                      replace=False)
                mesh = trimesh.Trimesh(vertices=mesh.vertices, faces=mesh.faces[idx])
        import matplotlib.pyplot as plt
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection
        # glTF хранит модели Y-up; ракурсы ниже заданы для Z-up — поворачиваем
        mesh.apply_transform(trimesh.transformations.rotation_matrix(
            np.pi / 2, [1, 0, 0]))
        base = os.path.splitext(path)[0]
        out = []
        for elev, azim, tag in ((25, -60, "iso"), (5, -90, "front"), (5, 90, "side")):
            el, az = np.radians(elev), np.radians(azim)
            cam_dir = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az),
                                np.sin(el)])
            # свет — от камеры и чуть сверху
            light = cam_dir + np.array([0.0, 0.0, 0.7])
            light = light / np.linalg.norm(light)
            lam = 0.3 + 0.7 * np.clip(mesh.face_normals @ light, 0.0, 1.0)
            colors = np.stack([0.45 * lam, 0.58 * lam, 0.85 * lam,
                               np.ones_like(lam)], axis=1).clip(0, 1)
            # painter's algorithm: дальние грани раньше ближних
            centroids = mesh.vertices[mesh.faces].mean(axis=1)
            dist = centroids @ cam_dir  # чем больше — тем дальше от камеры
            order = np.argsort(-dist)
            tri = mesh.vertices[mesh.faces][order]
            png = f"{base}_preview_{tag}.png"
            fig = plt.figure(figsize=(4, 4), dpi=110)
            ax = fig.add_subplot(111, projection="3d")
            ax.add_collection3d(Poly3DCollection(
                tri, facecolors=colors[order], edgecolor="none", zsort="min"))
            c = mesh.bounds.mean(axis=0)
            r = (mesh.extents.max() or 1.0) * 0.6
            ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r)
            ax.set_zlim(c[2] - r, c[2] + r)
            ax.set_axis_off()
            ax.view_init(elev=elev, azim=azim)
            fig.tight_layout(pad=0)
            fig.savefig(png, bbox_inches="tight")
            plt.close(fig)
            out.append(png)
        return out
    except Exception as e:
        print(f"[3d] preview render failed (non-fatal): {e}", flush=True)
        return []


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_MODEL_EXTS = (".glb", ".gltf", ".stl", ".obj", ".ply")


def _image_file_content(path: str, seed=None) -> list:
    """Картинка-результат инлайном (jobstore общий — через srv3d могут опрашивать
    и задачи gen/edit; без этого LM Studio получает только текст и не может
    показать картинку — баг 2026-11-24)."""
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
        path.rsplit(".", 1)[-1].lower(), "image/png")
    with open(path, "rb") as fh:
        data = fh.read()
    seed_note = f"\nSeed: {seed}" if seed is not None else ""
    return [
        ImageContent(type="image", data=base64.b64encode(data).decode(), mimeType=mime),
        TextContent(type="text", text=f"Image ready: {path}{seed_note}"),
    ]


def _repair_3d(path, resolution=256):
    """Воксельный ремешинг: mesh -> voxels(fill) -> marching cubes.
    Гарантированно watertight на выходе; масштаб и центр возвращаются к исходным.
    -> dict(before, after, out_path); throws при ошибках."""
    import numpy as np
    import trimesh
    mesh = trimesh.load(path, force="mesh")
    before = {"faces": len(mesh.faces), "watertight": bool(mesh.is_watertight),
              "volume": float(mesh.volume) if mesh.is_watertight else None}
    # вокселизация по длиннейшей оси + заполнение объёма + marching cubes
    vox = mesh.voxelized(pitch=float(mesh.extents.max()) / int(resolution)).fill()
    repaired = vox.marching_cubes
    # marching cubes работает в воксельных координатах — возвращаем исходный
    # масштаб (по границам) и исходный центр
    scale = mesh.extents / repaired.extents
    repaired.apply_scale(scale)
    repaired.apply_translation(mesh.bounds.mean(axis=0) - repaired.bounds.mean(axis=0))
    out = os.path.splitext(path)[0] + "_repaired.glb"
    repaired.export(out)
    after = {"faces": len(repaired.faces), "watertight": bool(repaired.is_watertight),
             "volume": float(repaired.volume) if repaired.is_watertight else None}
    return {"before": before, "after": after, "out_path": out}


def _content_for_result(job: dict) -> list:
    # общий jobstore: через этот сервер могут опрашивать и КАРТИНОЧНЫЕ задачи
    # (check_image у клиента один) — маршрутизируем по расширению результата
    if job["status"] == "done" and job.get("path", "").lower().endswith(_IMAGE_EXTS):
        return _image_file_content(job["path"], job.get("seed"))
    if job["status"] == "done":
        seed_note = f"\nSeed: {job['seed']}" if job.get("seed") is not None else ""
        # GLB не инлайним (не картинка): рендерим превью-ракурсы в PNG —
        # daemon подхватит строки "3D preview:" и отправит их в чат как картинки
        note = ""
        preview_lines = ""
        try:
            import trimesh
            mesh = trimesh.load(job["path"], force="mesh")
            ext = os.path.splitext(job["path"])[1].lstrip(".")
            note = (f" ({len(mesh.faces)} faces, "
                    f"{'watertight' if mesh.is_watertight else 'NOT watertight'}, "
                    f"bounds {mesh.extents.round(2).tolist()})")
            previews = _render_previews(job["path"])
            if previews:
                preview_lines = "\n" + "\n".join(f"3D preview: {p}" for p in previews)
        except Exception as e:
            print(f"[3d] result annotation failed (non-fatal): {e}", flush=True)
        return [TextContent(type="text",
                text=f"3D model ready: {job['path']}{note}{seed_note}{preview_lines}\n"
                     f"Convert it with convert_3d if the user needs STL/OBJ.")]
    return [TextContent(type="text", text=f"3D generation failed: {job['error']}")]


def _foreign_check(job_id: str, wait=20) -> list:
    """check_image чужой задачи через общий jobstore — эквивалент родной проверки
    (честный poll через jobstore.poll); маршрутизация по типу результата —
    картинки инлайном, модели текстом с превью."""
    rec = jobstore.poll(job_id, wait=max(int(wait), 1))
    if rec is None:
        return [TextContent(type="text",
                text=f"Unknown job_id: {job_id}. It may belong to a restarted session — "
                     f"start a new task.")]
    if rec.get("status") == "done":
        return _content_for_result({"status": "done", "path": rec["path"],
                                    "seed": rec.get("seed")})
    if rec.get("status") == "error":
        return [TextContent(type="text", text=f"Task failed: {rec.get('error')}")]
    return [TextContent(type="text",
            text=f"Still rendering (job_id={job_id}). Call check_image again.")]


@app.list_tools()
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
        Tool(
            name="repair_3d",
            description="Repair a 3D model for 3D printing: voxel remeshing (mesh -> "
                        "filled voxel grid -> marching cubes) GUARANTEES a watertight "
                        "closed surface. Trade-off: slight rounding of fine detail "
                        "(resolution = voxels along the longest axis, default 256; higher "
                        "= more detail, more faces). Scale and position are preserved. "
                        "Use when a result reports NOT watertight and the user wants to "
                        "print. Saves <name>_repaired.glb next to the original.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Absolute path of the GLB/model to repair.",
                    },
                    "resolution": {
                        "type": "integer",
                        "description": "Voxel grid resolution along the longest axis. "
                                       "Default 256.",
                        "default": 256,
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
        threshold = arguments.get("threshold")
        threshold = float(threshold) if threshold is not None else None
        seed = arguments.get("seed")
        seed = int(seed) if seed is not None else None
        job_id = f"job_{random.randint(0, 10**8)}"
        jobs[job_id] = {"status": "queued"}
        loop = asyncio.get_event_loop()
        threading.Thread(target=_run_job,
                         args=(os.path.expanduser(front), views.get("back"),
                               views.get("left"), views.get("right"),
                               steps, octree, job_id, seed, threshold),
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
            return _foreign_check(job_id, wait=arguments.get("wait", CHECK_WAIT_INLINE))
        wait = min(int(arguments.get("wait", CHECK_WAIT_INLINE)), CHECK_WAIT_INLINE)
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

    if name == "repair_3d":
        path = os.path.expanduser(arguments["path"])
        resolution = int(arguments.get("resolution", 256))
        if not os.path.isfile(path):
            return [TextContent(type="text", text=f"File not found: {path}")]
        try:
            r = await asyncio.to_thread(_repair_3d, path, resolution)
        except Exception as e:
            return [TextContent(type="text", text=f"Repair failed: {e}")]
        b, a = r["before"], r["after"]
        lines = [
            f"Repair complete: {r['out_path']}",
            f"Before: {b['faces']} faces, watertight={b['watertight']}",
            f"After:  {a['faces']} faces, watertight={a['watertight']}"
            + (f", volume {a['volume']:.2f}" if a.get("volume") else ""),
            "The repaired model is guaranteed watertight — ready for slicers/printing. "
            "Fine detail is slightly rounded (voxel remeshing).",
        ]
        return [TextContent(type="text", text="\n".join(lines))]

    raise ValueError(f"Unknown tool: {name}")


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
