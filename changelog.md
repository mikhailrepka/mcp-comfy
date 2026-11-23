# Changelog — comfy-mcp

All notable changes to the comfy-mcp MCP servers (generation / edit / upscale)
are documented here. The file is written in reverse chronological order.

## 2026-11-24

### Added

- **`repair_3d`** tool (comfy-image-3d): fix NOT-watertight meshes for 3D printing via
  voxel remeshing (mesh -> filled voxel grid -> marching cubes, scikit-image) —
  GUARANTEED watertight output, original scale/position preserved; `resolution`
  (default 256 voxels on the longest axis) trades detail vs face count. Reports
  faces/watertight/volume before and after. `scikit-image` added to the venv deps.
- **`threshold`** parameter for `generate_3d` (iso-surface threshold, default 0.6):
  lower values (e.g. 0.5) often close the surface shell at the source, reducing the
  need for repair.

### Fixed

- **3D server: same LM-Studio-safe inline waits** (FAST_WAIT 25 s, check_image 30 s
  per call) — generate_3d previously held the call up to 120 s and timed out at the
  LM Studio client (-32001) without returning the job_id.
- **gen server: inline waits are now LM-Studio-safe** (`COMFY_FAST_WAIT` default 25 s,
  `COMFY_CHECK_WAIT` default 30 s per check_image call; env or local.env). Previously
  generate_image held the tool call up to 90 s and check_image up to 600 s — longer
  than the LM Studio MCP client timeout (~60 s), so the client killed the call with
  -32001 BEFORE the job_id reached the model; the model then saw an empty queue and
  resubmitted duplicates. Batch renders (n=3, 1024px) pushed past the window; single
  turbo renders used to fit. The background job deadline (600 s) is unchanged.

### Added

- **3D generation** (`comfy-image-3d` server, new): `generate_3d` — Hunyuan3D v2
  multiview turbo, image-to-3D (NO text prompt; front view = description, one object
  on plain light background). Views: front required, back/left/right optional (the
  graph is built dynamically per provided views, unused nodes are cut). GLB lands in
  `~/comfy_models`; `convert_3d` converts GLB→STL/OBJ/PLY locally via trimesh (no
  network), reporting face count and watertightness. Registered as `comfy-image-3d`
  in the installers; `trimesh` added to the venv dependency set; updater kill pattern
  covers the new process. Live-verified end-to-end (GLB 2.1 MB, 144k faces, STL+OBJ).
- **`remove_background`** tool (edit server): isolate the main object via the qwen
  edit pipeline with FIXED prompts (no LLM enhancer). Modes: `white` (object on pure
  white) and `transparent` (isolated object; NOTE: qwen VAE is RGB — no real alpha).
  Generative: the object may be re-rendered slightly; for pixel-exact preservation a
  dedicated alpha tool would be needed.

## 2026-11-23

### Added

- **`convert_image` becomes decision-free** (edit server): skip logic — a file already
  in the target format and within max_width/max_height limits is returned as
  `unchanged` WITHOUT re-encoding (repeat JPG→JPG re-encoding loses quality);
  oversized/same-format-too-big files are converted in place. Every result line
  reports `WxH format KB`. `list_images` and `list_results` lines now also carry
  `(WxH ext KB)`. The system-prompt rule is now UNCONDITIONAL ("always run
  convert_image before edit_image") — the model no longer has to reason about file
  sizes it cannot see (observed wasting a whole reasoning block on this).
- **Updater** (`installer/update.sh` / `update.ps1`): safe update of a git clone
  to `origin/main` — `--check` mode (commits + changelog, changes nothing),
  `--yes` for unattended runs, one-time migration of a baked PC_URL into
  `local.env`, venv dependency sync, killing stale LM Studio MCP processes
  (they respawn on the next MCP session), ComfyUI health check. Releases are
  tagged (`v1.0`, …).
- **`comfy_status` reports the install version** (`Install: <commit>`, read
  from the untracked `VERSION` file written by installer/updater) — useful to
  see which machine runs what.
- **`n` variants in one generation** for `generate_image` (batch_size on the latent
  node, 1–4): one `job_id`, `check_image` returns ALL images of the batch, and the
  result text lists every path (`Image generated: ...` per line). The chat-bot daemon
  now pairs image #i with path #i from the result text — the previous code took the
  FIRST path for every image, which would have sent identical copies for batches.
- **`comfy_status`** tool (comfy_mcp_server): check the ComfyUI PC before
  submitting work — reachability, GPU/VRAM, RAM and the render queue. Returns a
  human-readable diagnosis instead of a cryptic "No route to host" when the PC
  is asleep or busy.
- **`cancel_job`** tool (comfy_mcp_server): cancel a queued render
  (`DELETE /queue`) or the running one (`POST /interrupt`) by `job_id`, with
  queue-state lookup so a foreign running job is never interrupted by mistake.
  Finished jobs are reported, not cancelled.
- **`list_results`** tool (comfy_mcp_server): list recent images in
  `~/comfy_images` (newest first, `limit` 1–50) — results of generations,
  edits and upscales, so a model can find "yesterday's picture" without a path.
- **`seed`** parameter for `generate_image` and `edit_image`: fix the seed to
  reproduce or iterate on a previous result. The used seed is returned in the
  result text on a separate line (`Seed: N`) so downstream path-extraction
  regexes keep working.
- Persistent unit tests: `test_edit_submit.py` (edit workflow graph shaping,
  400-body parsing, seed), `test_tools.py` (comfy_status, cancel_job,
  list_results, seed). Run with `.venv/bin/python <test>.py`; network is mocked.

### Fixed

- **`edit_image` HTTP 400 without `reference_image`**: the workflow's reference
  `LoadImage` node kept its template default filename; ComfyUI validates ALL
  nodes including inactive branches, so a missing file in the PC input dir
  failed the whole prompt. The reference node is now cut from the graph
  together with every `images.*` reference when no reference is passed
  (single-image mode; also removes a hidden phantom second reference that
  silently conditioned previous edits).
- `/prompt` HTTP 400 body is parsed (`error` + `node_errors`) into a readable
  error message (`node 475: Custom validation failed ... — Invalid image file:
  ...`) instead of a bare "HTTP Error 400".

### Changed

- **ComfyUI address moved out of the server files**: `PC` is resolved at startup
  (env `COMFY_PC_URL` → `local.env` next to the server → built-in default
  `http://192.168.1.218:8188`). The installer no longer rewrites tracked files —
  it writes `local.env` and `VERSION`; a `git pull` in an installation is now
  always conflict-free. Existing installs keep working unchanged without
  `local.env` (they used the same default address).

## 2026-11-22

- `convert_image`: WEBP support and optional downscaling (`max_width` /
  `max_height`, aspect preserved, never upscales) — shrink big phone photos
  before `edit_image`.
- Inbox watcher: atomic seen-claim via file lock — multiple MCP instances no
  longer duplicate chat attachments from `~/.lmstudio/user-files`.

## 2026-11-21

- Initial release: two-phase `generate_image` / `edit_image` / `upscale_image`
  + `check_image` protocol (inline result or `job_id` to poll), shared jobstore
  (`~/.comfy_mcp_jobs.json`) so `check_image` works across server instances and
  restarts, `list_images` / `convert_image` / `save_images` helpers,
  `~/comfy_edit_inbox` watcher for chat attachments, installer
  (macOS/Linux/Windows, parameterized PC URL).
