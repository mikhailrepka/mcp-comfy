# comfy-mcp — MCP tools for image generation and editing via ComfyUI

Three MCP servers for LM Studio that work with a (local or remote) ComfyUI
instance. The ComfyUI address is an **installation variable**:
`./installer/install.sh http://<ComfyUI-host>:8188` (macOS/Linux) or
`.\installer\install.ps1 http://<ComfyUI-host>:8188` (Windows).

| Server | Tools | Purpose |
|---|---|---|
| `comfy-image-gen` | `generate_image`, `check_image` | text-to-image generation (nvidia workflow) |
| `comfy-image-edit` | `edit_image`, `check_image`, `list_images`, `convert_image` (+ `save_images` only with env `COMFY_ENABLE_SAVE_IMAGES=1`) | image editing by text instruction (qwen-image-edit) + format/quality conversion |
| `comfy-image-upscale` | `upscale_image`, `check_image`, `list_images` | SeedVR2 neural upscaling |

Key protocol feature: `edit_image` and `generate_image` **never wait for the
result** — they queue the job in ComfyUI and immediately return a `job_id`
("task accepted"). Readiness is checked by repeated `check_image` calls, which
return the finished image inline. This keeps the tool from hitting the MCP
client's tool-call timeout (rendering takes 1–5 minutes).

## Monitoring: errors and progress

- **Shared job registry.** All three servers mirror job statuses into
  `~/.comfy_mcp_jobs.json`. `check_image` on any server accepts any `job_id` —
  even if another server created the job or a server was restarted (finished
  jobs are re-read from disk). Entries older than 24 h are pruned automatically.
- **Error/cancel detection.** The poller checks `status.status_str` in
  `/history/<prompt_id>` on every poll: if a job failed (e.g. a corrupt image in
  `LoadImage`) or was cancelled, `check_image` immediately returns the error
  description — node, its type and the exception text (e.g.
  `LoadImage (node 470): cannot identify image file ...`) — instead of waiting
  out the timeout.
- **Progress percentage.** The servers listen to the ComfyUI WebSocket (`/ws`)
  and receive sampler `progress` events (requires the `websocket-client`
  package, installed by the installer). Progress shows up in `check_image`
  replies while rendering: `Progress: 10/25 steps (40%)`. Important: ComfyUI
  sends progress events **only to the submitting client** (by `client_id`), so
  progress is visible only for jobs submitted by the MCP servers themselves.

## How chat attachments reach the tools

LM Studio stores chat attachments in `~/.lmstudio/user-files/` (on Windows:
`%USERPROFILE%\.lmstudio\user-files\`). A background watcher thread inside
`comfy-image-edit` copies new image attachments into the inbox folder
automatically (keeping the original name with a `chat_` prefix); they then show
up via `list_images`. On first run the historical library is only marked as
seen — only new attachments are copied.

Folders:
- `~/comfy_edit_inbox` (Windows: `%USERPROFILE%\comfy_edit_inbox`) — incoming
  images for editing/upscaling (automatic + manual), shared by the edit and
  upscale servers;
- `~/comfy_images` (Windows: `%USERPROFILE%\comfy_images`) — saved results of
  generation, editing and upscaling.

## Format/quality conversion

`convert_image(paths, format, quality)` in `comfy-image-edit` converts images
to JPG (quality 1–100, default 90) or PNG, saving the result next to the
original (inbox files stay in the inbox). Typical use case: rendering is
noticeably faster with a JPG source, so shrink large PNGs before `edit_image`:

1. `list_images()` → paths;
2. `convert_image(paths=[...], format="jpg", quality=90)` → converted paths;
3. `edit_image(..., source_image=<jpg path>)`.

JPEG does not support transparency — alpha is flattened onto a white background.

## Upscaling (comfy-image-upscale)

`upscale_image(source_image, multiplier | longer_size, scale_method)` — SeedVR2
7B neural upscaling via ComfyUI. Scale is set either by `multiplier` (0.01–8,
default 2) or by `longer_size` (target longer side in pixels, aspect ratio
preserved) — the second is more precise for "make it 2048px". `scale_method`
(default `lanczos`) is the interpolation used for the pre-resize inside the
pipeline. The result is a PNG in `~/comfy_images` (compressing the upscale
result with a lossy conversion inside the workflow is pointless; apply
`convert_image` afterwards if needed). Same two-phase protocol: `job_id` →
repeated `check_image`; upscaling is heavy — rendering can take noticeably
longer than editing.

## Workflow files

The three `*.json` files in the repo root are **required** — each server loads
its workflow at submit time and sends it to ComfyUI with every job. They are
plain ComfyUI workflow API exports and contain **no local data** (no paths,
addresses or machine info; only node ids, parameters and model filenames listed
in the table below). Nothing needs to be downloaded into ComfyUI to use them —
they are submitted over the API, not installed. If you use different models,
export your own workflow in API format and replace the corresponding file
(keep the node ids referenced in the server's header constants — see the
requirements table below).

## Requirements on the ComfyUI side

The `*.json` files in this repo are **ComfyUI workflow API exports** — they are
submitted to ComfyUI as-is. The target ComfyUI instance must therefore have the
same models and custom nodes installed, otherwise jobs fail with execution
errors (the error text is returned by `check_image`, so a missing model is easy
to identify).

| Workflow file | Models (in ComfyUI `models/` folders) | Custom nodes |
|---|---|---|
| `nvidia-image-gen.json` | `z_image_turbo_bf16.safetensors` (diffusion models), `qwen_3_4b.safetensors` (text encoders), `ae.safetensors` (vae) | core nodes only |
| `image_qwen_image_2_1_image_edit.json` | `qwen_image_2.1_int8_convrot.safetensors`, `qwen3vl_8b_int8_convrot.safetensors`, `qwen_image_2.1_vae_bf16.safetensors`, `qwen3.5_9b_qwen_image_2.1_pe_i2i.int8_convrot.safetensors` | `SaveImageAdvanced`, `TextEncodeQwenImage21`, `QwenImage21Cache`, `ComfySwitchNode`, `BatchImagesNode`, `PrimitiveStringMultiline`, `TextGenerate`, `PreviewAny` |
| `utility_seedvr2_7b_int8_upscale_image.json` | `seedvr2_7b_int8_convrot.safetensors`, `seedvr2_ema_vae_fp16.safetensors` | SeedVR2 custom nodes (`SeedVR2Preprocess`, `SeedVR2Conditioning`, `SeedVR2PostProcessing`), `ResizeImageMaskNode`, `JoinImageWithAlpha`, `VAEEncodeTiled`, `VAEDecodeTiled` |

If you use different models/workflows, export your own workflow in API format
(ComfyUI: "Workflow → Export (API)") and replace the corresponding `.json`
file — the servers reference nodes by id, so keep the node ids used in the
server's `*_SERVER`-style constants (save-node id and text/size inputs) or
update them at the top of the server file.

## Installation

### Choosing the ComfyUI address (important!)

- **LM Studio and ComfyUI on the same computer** → use
  `http://localhost:8188` (or `http://127.0.0.1:8188`). ComfyUI's default
  listen address works as-is.
- **Two computers on a LAN** → use `http://<ComfyUI-PC-IP>:8188` and:
  1. start ComfyUI with `--listen 0.0.0.0` (by default it only listens on
     `127.0.0.1` and is unreachable from other machines);
  2. **assign fixed IP addresses to both computers in the router** (DHCP
     reservation). If the ComfyUI PC's IP changes, the servers will silently
     lose the target until you re-run the installer with the new URL.

### macOS / Linux

Requirements: Python ≥ 3.10 (`brew install python`), LM Studio.

1. Clone/copy the whole repo (server `.py` files, workflow `.json` files,
   `installer/` folder).
2. Run:
   ```bash
   cd comfy-mcp/installer
   ./install.sh http://<ComfyUI-host>:8188
   ```
   (without arguments it falls back to the default URL; if no suitable Python
   is found the script prints the exact `brew install python@3.14` commands).
3. Restart LM Studio (Developer → MCP → Restart). The `comfy-image-gen`,
   `comfy-image-edit` and `comfy-image-upscale` tools appear.
4. Paste the system prompt (below) into the model's System Prompt / Project
   Rules in LM Studio.

The script does: venv + `pip install "mcp<2" websocket-client Pillow`,
substituting the ComfyUI URL into all servers, creating the working folders,
registering the servers in `~/.lmstudio/mcp.json` (merge with a `.bak` backup;
legacy `comfy_mcp_server`/`edit_mcp_server` entries are replaced with the new
names).

### Windows

Requirements: Python ≥ 3.10 (python.org installer with "Add python.exe to
PATH", or `winget install Python.Python.3.14`), LM Studio.

1. Clone/copy the repo.
2. In PowerShell:
   ```powershell
   cd comfy-mcp\installer
   .\install.ps1 http://<ComfyUI-host>:8188
   ```
3. Restart LM Studio (Developer → MCP → Restart).
4. Paste the system prompt (below).

The PowerShell installer performs the same steps as the bash one, with Windows
paths: venv in `.venv\Scripts\python.exe`, inbox/results under
`%USERPROFILE%`, config at `%USERPROFILE%\.lmstudio\mcp.json`.

### Uninstall

```bash
# macOS / Linux
cd comfy-mcp/installer && ./uninstall.sh
```
```powershell
# Windows
cd comfy-mcp\installer
.\uninstall.ps1
```
Removes the servers from `~/.lmstudio/mcp.json` (config backed up to `.bak`).
Repo files and the venv are untouched.

## System prompt for LM Studio

Paste into the model settings (System Prompt) or Project Rules:

```
You have image tools from the comfy-image-gen, comfy-image-edit and comfy-image-upscale MCP servers:
- generate_image(prompt, width, height, steps) — a new image from scratch.
- list_images() — images in the inbox folder (~/comfy_edit_inbox), newest first.
- convert_image(paths, format, quality) — convert to jpg (quality 1-100, default 90) or png;
  the result is saved next to the original. Use before edit_image when the source is
  a large PNG: a JPG source renders noticeably faster.
- edit_image(prompt, source_image, [reference_image], steps) — edit by path.
  Returns a job_id immediately ("task accepted").
- upscale_image(source_image, multiplier | longer_size, scale_method) — SeedVR2 neural
  upscale (a multiplier, e.g. 2 or 4, or a target longer side in pixels). Also returns
  a job_id immediately.
- check_image(job_id) — readiness poll. Present on all three servers and accepts
  ANY job_id (jobs live in a shared registry): poll via the same server you started
  the job with, but any server works if needed. It returns one of: the finished
  image (inline), "still rendering" with progress (Progress: 10/25 steps, 40%),
  or an error description (which node failed and why).

Rules:
- "Draw/generate" → generate_image.
- ANY image the user mentions in the chat (attached, dropped, pasted, or generated/edited
  earlier) → IMMEDIATELY call list_images: chat attachments and job results automatically
  land in the inbox (the former a few seconds after sending). NEVER try to save an
  attachment yourself and never ask the user to save anything — just take the path from
  list_images. If the image is not in the list yet, wait a couple of seconds and call
  list_images again.
- The user gave a file path directly → use it as-is, without list_images.
- If the image still never appears in the inbox → ask the user to put it into
  ~/comfy_edit_inbox and call list_images again.
- Before edit_image, shrink a large PNG source (> ~3 MB): convert_image(paths, "jpg", 90) →
  use the converted path. Final images are usually requested as JPG too — convert the
  result with the same tool.
- "Enhance/upscale/make bigger" → upscale_image; use longer_size when the user names a
  target size (e.g. "to 2048px"), otherwise a multiplier of 2-4. The upscale result is a
  PNG in ~/comfy_images; compress it with convert_image if needed.
- Two images for edit_image: source_image — what to edit (decide by content),
  reference_image — what to put on/add.
- If edit_image/upscale_image returned a job_id → keep calling check_image until the
  image arrives, NEVER restart the job. Rendering takes 1–5 minutes (upscaling longer);
  do not ask the user between polls — just poll check_image and report the progress
  included in the response.
- If check_image returned an error (e.g. "cannot identify image file" — a corrupt
  image) → tell the user the error text and suggest replacing the source. Do not restart
  the same job with the same files and do NOT switch to generate_image.
- If check_image replied "Unknown job_id" → the job was lost (the server restarted
  between calls). Report it and create the job again — exactly once.
```

## Project structure

```
comfy-mcp/
├── comfy_mcp_server.py              # generation MCP server (comfy-image-gen)
├── edit_mcp_server.py               # editing MCP server + convert_image (comfy-image-edit)
├── upscale_mcp_server.py            # SeedVR2 upscale MCP server (comfy-image-upscale)
├── jobstore.py                      # shared cross-server job registry
├── nvidia-image-gen.json            # generation workflow API
├── image_qwen_image_2_1_image_edit.json  # editing workflow API
├── utility_seedvr2_7b_int8_upscale_image.json  # upscale workflow API
└── installer/
    ├── install.sh                   # macOS/Linux installer (venv + LM Studio registration)
    ├── install.ps1                   # Windows installer
    ├── uninstall.sh                 # removes entries from mcp.json (bash)
    └── uninstall.ps1                # removes entries from mcp.json (PowerShell)
```

## Fine-tuning (edit_mcp_server.py)

| Constant | Default | Meaning |
|---|---|---|
| `PC` | `http://localhost:8188` | ComfyUI address (rewritten by the installer) |
| `INBOX_DIR` | `~/comfy_edit_inbox` | incoming images |
| `LM_FILES_DIR` | `~/.lmstudio/user-files` | where the watcher picks up chat attachments |
| `WATCH_INTERVAL` | `5` s | attachment scan period |
| `CHECK_WAIT_DEFAULT` | `20` s | how long one `check_image` call waits |
| `CHECK_WAIT_MAX` | `120` s | upper bound of `wait` for `check_image` |

(the same applies to `comfy_mcp_server.py`, except the inbox/watch constants)

The watcher thread lives inside the MCP server, i.e. attachment copying works
while LM Studio is connected to the server (always, in an active session). The
"already copied" state is stored in `.lmstudio_seen.json` next to the server.

Русская версия: [README_RU.md](README_RU.md)
