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

`convert_image(paths, format, quality, max_width, max_height)` in `comfy-image-edit` converts images
to JPG/PNG/WEBP (quality 1–100, default 90), optionally downscaling so width ≤ `max_width` and/or
height ≤ `max_height` (aspect preserved, never upscales — ideal for large phone photos), saving the result next to the
original (inbox files stay in the inbox). Typical use case: rendering is
noticeably faster with a JPG source, so shrink large PNGs before `edit_image`:

1. `list_images()` → paths;
2. `convert_image(paths=[...], format="jpg", quality=85, max_width=1920)` → converted paths;
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

### Updating

The ComfyUI address lives in `local.env` (untracked, written by the installer) —
tracked files always match origin, so updates are clean and conflict-free:

```bash
cd comfy-mcp/installer
./update.sh --check    # what's new (commits + changelog), changes nothing
./update.sh --yes      # update: fetch, reset --hard origin/main, venv sync,
                       # kill stale LM Studio MCP processes, health checks
```

- Windows: `.\update.ps1 -Check` / `.\update.ps1 -Yes`.
- Your settings survive: `local.env`, `VERSION`, `.lmstudio_seen.json` are
  untracked. Do NOT edit tracked files in an installation — keep it pristine.
- Releases are tagged (`v1.0`, …); to pin a machine to a specific version,
  `git checkout <tag>` instead of running the updater.
- `comfy_status` reports the installed version (`Install: <commit>`) — handy
  for checking which machine runs what.

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

## Using with other MCP clients

The servers are standard **stdio MCP servers** — nothing in them is specific to
LM Studio. Any MCP client can run them: point the client at
`<repo>/.venv/bin/python` (macOS/Linux) or `<repo>\.venv\Scripts\python.exe`
(Windows) with the server script as the argument. The installer only automates
venv creation and the registration below.

| Client | Registration | Notes |
|---|---|---|
| LM Studio | `~/.lmstudio/mcp.json` (done by the installer) | chat-attachment auto-copy watcher is enabled |
| ZCode | workspace/user MCP config, same server entries | chat attachments have real file paths → enable `COMFY_ENABLE_SAVE_IMAGES=1` in the server env to get `save_images` |
| OpenCode | `opencode.json` → `"mcp": {"comfy-image-edit": {"type": "local", "command": [...], "env": {...}}}` | same entries as below |
| Claude Desktop / Cursor | `claude_desktop_config.json` / `~/.cursor/mcp.json` → standard `mcpServers` block | attachments have no paths → use the inbox flow (`list_images`) |
| Ollama | not an MCP client by itself | needs a bridge (e.g. `mcpo`) that exposes MCP tools over the Ollama chat API |

The generic registration block (works for most clients):

```json
{
  "mcpServers": {
    "comfy-image-gen": {
      "command": "/path/to/comfy-mcp/.venv/bin/python",
      "args": ["/path/to/comfy-mcp/comfy_mcp_server.py"],
      "env": {}
    },
    "comfy-image-edit": {
      "command": "/path/to/comfy-mcp/.venv/bin/python",
      "args": ["/path/to/comfy-mcp/edit_mcp_server.py"],
      "env": { "COMFY_ENABLE_SAVE_IMAGES": "1" }
    },
    "comfy-image-upscale": {
      "command": "/path/to/comfy-mcp/.venv/bin/python",
      "args": ["/path/to/comfy-mcp/upscale_mcp_server.py"],
      "env": {}
    }
  }
}
```

Client-agnostic notes:
- the two-phase `job_id`/`check_image` protocol exists precisely because tool-call
  timeouts differ per client — it works everywhere;
- the system prompt (below) needs the `list_images`-based attachment flow for
  clients without file-path attachments; for clients like ZCode enable
  `save_images` and prefer it;
- `jobstore` and the inbox folder are shared across all clients on the machine.

## System prompt for LM Studio

Paste into the model settings (System Prompt) or Project Rules:

```
You have image tools from the comfy-image-gen, comfy-image-edit and comfy-image-upscale MCP servers:
- generate_image(prompt, width, height, steps, [seed], [n]) — a new image from scratch. Optional
  seed fixes randomness (iterate on a previous result); the used seed is returned on a
  separate "Seed: N" line. Optional n (1-4) renders that many variants in ONE job (same
  prompt, different noise) — check_image returns all of them; use when the user asks for
  options to choose from (4 variants take ~4x VRAM).
- edit_image(prompt, source_image, [reference_image], steps, [seed]) — edit by path.
  Returns a job_id immediately ("task accepted"). Optional seed reproduces an edit.
- comfy_status() — check the ComfyUI PC (remote render server): reachable, GPU/VRAM, RAM,
  render queue. Call it when a job fails with a network/HTTP error, when the user asks
  whether the PC is busy or asleep, or when diagnosing repeated failures.
- cancel_job(job_id) — stop a queued or running render (job_id from any of the three task
  tools) when the user asks to cancel/stop. Finished jobs cannot be cancelled.
- list_results(limit) — recent outputs in ~/comfy_images (generated/edited/upscaled),
  newest first. Use when the user refers to an earlier result without giving a path;
  then pass the listed path to edit_image / upscale_image / convert_image.
- list_images() — images in the inbox folder (~/comfy_edit_inbox), newest first.
- convert_image(paths, format, quality, max_width, max_height) — convert to jpg/png/webp (quality 1-100,
  default 90), optionally downscale (e.g. max_width=1920 for phone photos);
  the result is saved next to the original. Cheap and safe to ALWAYS run before
  edit_image: a file already in the target format and within the limits comes back
  "unchanged" without re-encoding (no generational quality loss), and every result
  line reports WxH/format/KB.
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
- The user asks for several variants/options ("give me a few to choose from") →
  generate_image with n=2..4 in ONE call instead of several separate jobs; all variants
  arrive after check_image.
- If comfy_status says the PC is unreachable → the PC is probably asleep: tell the user
  to wake it up and do not retry blindly. If the queue shows running/pending jobs, new
  jobs will wait — mention it instead of resubmitting duplicates.
- If the user asks to stop/cancel a task → cancel_job(job_id); report what it returned.
- ANY image the user mentions in the chat (attached, dropped, pasted, or generated/edited
  earlier) → IMMEDIATELY call list_images: chat attachments and job results automatically
  land in the inbox (the former a few seconds after sending). NEVER try to save an
  attachment yourself and never ask the user to save anything — just take the path from
  list_images. If the image is not in the list yet, wait a couple of seconds and call
  list_images again.
- The user gave a file path directly → use it as-is, without list_images.
- If the image still never appears in the inbox → ask the user to put it into
  ~/comfy_edit_inbox and call list_images again.
- Before edit_image, ALWAYS run the source (and reference) through
  convert_image(paths, "jpg", 90, 4000) and use the returned paths. It is local and
  instant, and there is nothing to decide: files that are already compliant come back
  "unchanged" without re-encoding, oversized ones (> ~4 MB or > 4000 px, i.e. anything
  worth shrinking) are converted. Do not reason about file sizes — just call it.
  Final images are usually requested as JPG too — convert the result with the same tool.
- "Enhance/upscale/make bigger" → upscale_image; use longer_size when the user names a
  target size (e.g. "to 2048px"), otherwise a multiplier of 2-4. The upscale result is a
  PNG in ~/comfy_images; compress it with convert_image if needed.
- "Same image but X" / reproducing a previous render → take the seed from the earlier
  result ("Seed: N" line) and change only what the user asked for.
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
├── changelog.md                     # notable changes, newest first
├── test_edit_submit.py              # unit tests: edit workflow shaping, 400 parsing, seed
├── test_tools.py                    # unit tests: comfy_status, cancel_job, list_results, seed
├── nvidia-image-gen.json            # generation workflow API
├── image_qwen_image_2_1_image_edit.json  # editing workflow API
├── utility_seedvr2_7b_int8_upscale_image.json  # upscale workflow API
└── installer/
    ├── install.sh                   # macOS/Linux installer (venv + LM Studio registration)
    ├── install.ps1                   # Windows installer
    ├── update.sh                    # macOS/Linux updater (--check / --yes)
    ├── update.ps1                   # Windows updater
    ├── uninstall.sh                 # removes entries from mcp.json (bash)
    └── uninstall.ps1                # removes entries from mcp.json (PowerShell)
```

## Fine-tuning (edit_mcp_server.py)

| Constant | Default | Meaning |
|---|---|---|
| `PC` | `http://192.168.1.218:8188` | ComfyUI address: env `COMFY_PC_URL` → `local.env` → this default (installer/update write `local.env`) |
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
