# Changelog — comfy-mcp

All notable changes to the comfy-mcp MCP servers (generation / edit / upscale)
are documented here. The file is written in reverse chronological order.

## 2026-11-23

### Added

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
