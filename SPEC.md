# Model Manager — Behavioral Specification

> This is the **target** specification: the behavior the system should have once the
> confirmed decisions (§8) are implemented. §7 lists pure bugs in the current `main` code,
> and §8 is the change-log (current → intended) that produced this target.
> **Security/authentication is intentionally out of scope** — this is a local-network service.
> Where a detail is not yet decided it is marked **[open]**; there are currently none outstanding.

## 1. Overview

A single-container web service to download and manage Hugging Face models (GGUF and
SafeTensors) onto shared/network storage. Flask backend + single-page vanilla JS UI. One
process, in-process threading for downloads, JSON file for crash persistence.

- **Backend:** `app.py` (all routes, workers, SSE, persistence)
- **Scanner:** `models.py` (`ModelScanner`)
- **Frontend:** `static/index.html` (HTML+CSS+JS, no build step)
- **Runtime:** Docker (`python:3.12-slim`), served by **gunicorn** (1 worker, threaded — see §8)
- **Downloads:** `huggingface-hub` 1.24.0 (`hf_hub_download`, `snapshot_download`)

## 2. Configuration (env vars)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODELS_PATH` | `/models` | Root of model storage (NFS mount) |
| `DATA_PATH` | `/app/data` | Directory holding `download_queue.json` |
| `MAX_DOWNLOAD_WORKERS` | `3` | Max **concurrent** download workers. Queue is unlimited — extra submissions wait, none are rejected. |
| `QUEUE_SAVE_INTERVAL` | `30` | Seconds between periodic queue saves |
| `HF_TOKEN` | (none) | HF token; used by `/scan` and downloads (gated repos) |
| `COMPLETED_AUTO_CLEAR_SECONDS` | `10` | Seconds before a completed download row auto-clears |

## 3. Storage Layout

```
{MODELS_PATH}/
  gguf/
    <org__name>/                # namespaced by org: org/name -> org__name
      *.gguf
    <org__name>/<file>.gguf     # single-file download (hf_hub_download)
    <name>.gguf                 # (also recognized by scanner if present at root)
  safetensors/
    <org__name>/                # snapshot or single-file; dir per model
      *.safetensors + config/tokenizer/...
  <org__name>/.cache/huggingface/   # HF transfer cache, created inside each model dir
{DATA_PATH}/
  download_queue.json           # persisted queue state
```

**Path derivation:** HF `model_id` `org/name` → on-disk folder `org__name` (org and repo
joined with `__`), model id `gguf_org__name` / `safetensors_org__name`. Namespacing by org
prevents two repos that share a name from colliding. (Current code uses the last segment
only — see §8.)

## 4. Data Model

### 4.1 Download record (in-memory `downloads` dict, keyed by `download_id`)
| Field | Notes |
|-------|-------|
| `id` | UUID |
| `model_id` | HF repo id (`org/name`) |
| `model_type` | `gguf` \| `safetensors` |
| `status` | state machine value (§4.2) |
| `created_at`, `end_time` | ISO timestamps |
| `filename` | optional; present for single-file downloads |
| `expected_size` | bytes; the real file size from `/scan` (denominator for progress %) |
| `error` | set on failure |
| `target_dir` | on-disk destination dir (persisted; used by cancel/remove cleanup) |
| `proc`, `future`, `stop_progress` | in-memory only; stripped before serialization |

### 4.2 Download state machine
```
queued ──(worker picks up)──▶ downloading ──▶ completed ──(auto, shortly after)──▶ [removed]
   │                              │
   │                              ├──(exception)──▶ failed  (stays, for retry)
   │                              │
   └──(restart: was queued)──────▶ failed  (marked "interrupted by restart")
                                  │
                          (POST .../cancel)──▶ cancelled ──▶ [partial files deleted]
```
Notes:
- **Completed entries auto-clear** shortly after they finish (once the model is visible under
  Installed Models), so the list shows only queued/downloading/failed(+cancelled) rows.
- **Cancel** aborts the running download and deletes its partial files (see §6.1 caveat).
- `deleted` is referenced by legacy cleanup logic but is not a state any handler sets.
- On restart, any `queued`/`downloading` entry is forced to `failed` ("Download interrupted
  by restart") — intentional; the user retries manually.

### 4.3 Model record (from `GET /models` / scanner)
`id` (`gguf_org__name` | `safetensors_org__name`), `name`, `type`, `size_bytes`, `size_gb`,
`file_count`, `path` (relative to `MODELS_PATH`). `size` is the **full folder footprint** —
all bytes in the folder including `config.json`, tokenizer, index, and `.cache`. A folder
counts as an installed model only if it holds **≥1** `.gguf`/`.safetensors` file.

## 5. API Reference

All routes accept/return JSON unless noted. **No authentication. No rate limiting.** Up to
`MAX_DOWNLOAD_WORKERS` downloads run concurrently; the queue is unlimited.

| Method | Path | Behavior |
|--------|------|----------|
| `GET`  | `/health` | `{status:"healthy", timestamp}` |
| `GET`  | `/` | Serves `static/index.html` |
| `GET`  | `/models` | Scans `MODELS_PATH`; returns `{models:[...]}` (§4.3). Full-folder size; `org__name` ids. |
| `GET`  | `/downloads` | Returns download records (non-serializable fields stripped). Typically active/failed rows only — completed auto-clear. |
| `POST` | `/download` | Start a download. Body: `model_id` (req), `model_type` (def `gguf`), `filename`?, `expected_size`? (real size when known). Queues it (no rejection). → `202` + `download_id`; `400` on bad input. |
| `POST` | `/scan` | Lists **all** files in a repo via HF API (uses `HF_TOKEN`), **with real sizes**. → `{files:[...]}`. |
| `GET`  | `/events?download_id=` | SSE stream: `status` → `started` → `progress`* → (`completed` \| `error` \| `cancelled`). 30s keepalive comments; client uses polling fallback if SSE can't connect. |
| `POST` | `/downloads/<id>/cancel` | **Aborts** the running download and **deletes its partial files**; sets `cancelled`. (See §6.1 caveat.) |
| `POST` | `/downloads/<id>/retry` | Re-queues a `failed`/`cancelled` download, **replacing the old entry in place** (old row removed). |
| `DELETE` | `/downloads/<id>` | Removes the record. **If incomplete, deletes its partial files**; completed files are kept (still a real model). |
| `POST` | `/downloads/clear` | **Stops all in-flight** downloads, clears the list, and **deletes partial files** (completed kept); removes `download_queue.json`. |
| `POST` | `/cache/clear` | Removes **all** `.cache/huggingface` transfer-cache dirs (no per-model option). |
| `DELETE` | `/models/<id>` | Deletes the installed model dir **(incl. its `.cache`)** **and removes any download rows referencing that model**. |

## 6. Core Flows

### 6.1 Download lifecycle
1. `POST /download` validates input, creates a `queued` record + an SSE event queue, persists,
   and submits a worker to the `ThreadPoolExecutor`. There is **no active-count cap** — the
   executor runs `MAX_DOWNLOAD_WORKERS` at a time and queues the rest.
2. Worker resolves target dir `MODELS_PATH/<type>/<org__name>`, emits `started`, sets
   `downloading`, starts a **progress thread**, then calls `hf_hub_download` (single file) or
   `snapshot_download` (whole repo).
3. On success → `completed` (then auto-clears, §4.2); on exception → `failed` (with `error`).
   Queue is saved in the worker's `finally`.

**Selection model.** GGUF repos hold multiple quantization variants; the intended flow is
**SCAN → pick the file(s) for one quant → download them** into one model folder. Multi-file
selection is N single-file downloads into the same `<org__name>` folder, which the scanner
presents as one model with `file_count = N`. SafeTensors repos are normally pulled as a full
snapshot (weights + `config.json` + tokenizer + index). Whole-repo "download all" is therefore
appropriate for SafeTensors but **not** for GGUF.

> **Caveat — cancel/abort feasibility.** `huggingface_hub`'s `hf_hub_download` /
> `snapshot_download` have no native cancellation hook, and Python threads cannot be force
> stopped cleanly. "Abort the running download" (cancel / clear-all) therefore needs a real
> strategy — e.g. run each download in a **subprocess** that can be terminated, or check a
> cancel flag around per-file/per-chunk work and delete partials afterward. This is the one
> decision that is **not a trivial code change**; the exact mechanism is left to
> implementation. (Tracked as the main implementation risk.)

### 6.2 Progress reporting
A background thread per download wakes every **3 s**, sums file sizes under the target dir,
and computes `percent = min(round(current/expected_size*100), 99)` (capped at 99 until the
`completed` event). `expected_size` is the **real** size returned by `/scan` (or the summed
total for a multi-file selection), so the percentage is meaningful. Progress is emitted over
SSE; the UI falls back to polling (`/downloads` + `/models`) when an SSE connection cannot be
established or drops. **The polling-fallback wiring must be fixed** so it activates on page
load and re-opens/recovers after SSE errors.

### 6.3 Persistence & restart
- `save_queue()`: snapshots `downloads` under `download_lock`, writes to a **unique** temp
  file in `DATA_PATH`, then atomically `replace()`s `download_queue.json`. The write is
  serialized under a dedicated save lock (the current fixed-temp-file/no-lock race is a bug,
  §7). Runs on the 30s timer, on every worker exit, and on mutating requests.
- `load_queue()` (startup): restores `downloads`; completed entries are not retained (they
  auto-clear during the session); any `queued`/`downloading` is forced to `failed`.
- Failed entries persist across restart so the user can retry them.

### 6.4 Frontend (`static/index.html`)
- **Download form:** `MODEL ID` + `MODEL TYPE`. `SCAN FILES` → `POST /scan` (all files, real
  sizes) → checkbox list → `DOWNLOAD SELECTED` (per file, with `expected_size`). `DOWNLOAD
  ALL` = whole-repo snapshot (appropriate for SafeTensors; for GGUF prefer SCAN + select).
- **Active downloads:** one SSE `EventSource` per item (polling fallback when SSE fails);
  progress bar + status badge; per-row `REMOVE` / `CANCEL`; failed/cancelled rows get `RETRY`;
  **completed rows auto-clear** shortly after finish.
- **Installed models:** list with `DELETE` (full-folder size shown).
- `CLEAR LIST` → `POST /downloads/clear`; `CLEAR CACHE` → `POST /cache/clear` (all caches).

## 7. Resolved defects (fixed in the spec-conformance work)

These were bugs in the original `main`; all are fixed now:
1. **Completed downloads used to appear to hang ~1 hour** — the worker joined the progress
   thread *before* signaling it to stop. Fixed: signal stop before join (`app.py` worker).
2. **`save_queue` could corrupt the queue JSON** — unlocked write to a fixed temp path. Fixed:
   serialized under `queue_save_lock` with a unique temp file.
3. **Deleting/clearing a download mid-flight** could raise `KeyError` and orphan files. Fixed:
   the worker guards every `downloads[id]` access; cancel/clear/delete terminate the subprocess
   first.
4. **No real test suite** — the old `test_periodic_save.py` re-implemented `save_queue` (it
   tested a copy). Fixed: removed it; `test_app.py` is a real pytest suite over the actual module.

## 8. Feature Decisions (confirmed in walkthrough)

> Format: **Feature** — *current* → **intended**. Locations reference `main`.

### Downloads
- **GGUF download** — whole-repo snapshot → **select one quant's file(s) via SCAN, into one
  folder**. SafeTensors stays a full snapshot. (§6.1; `app.py:282`, frontend form submit.)
- **Scan** — only `.gguf`/`.safetensors` → **list ALL files** in the repo. Type still comes
  from the form dropdown (routes to folder). (`app.py:545`.)
- **Multi-file selection** — N separate single-file downloads → **keep as N separate
  downloads** (scanner groups them into one model by folder).
- **Progress values** — `size_bytes:0` / no `expected_size` (so % is 0) → **fetch real file
  sizes from the HF API**; downloads compute a total. (§6.2; `app.py:545`, `app.py:282`.)
- **Cancel** — status flag only, download continues, overwritten to `completed` → **abort the
  running download AND delete its partial files**. (`app.py:391`, `app.py:225`.) *See §6.1
  caveat — abort mechanism is the main implementation risk.*
- **Retry** — new id, old failed row left behind → **replace in place** (remove old entry;
  one active row). (`app.py:409`.)
- **Remove entry** — record only, all files kept → **row + delete partial files only**
  (completed files stay as a real installed model). (`app.py:461`.)
- **Clear all** — wipe list + queue file, workers/files untouched → **stop in-flight +
  clear list + delete partials** (completed kept). (`app.py:521`.)

### Cleanup & models
- **Clear cache** — per-model + all → **all only** (drop per-model). (`app.py:488`.)
- **Model size** — weights-only (`.gguf`/`.safetensors` bytes) → **full folder footprint**
  (all bytes incl. `config.json`, tokenizer, `.cache`). (§4.3; `models.py` per-model sum.)
- **Delete model** — dir only (`.cache` already inside dir) → **dir + `.cache` + remove
  related download rows**. (`models.py:138`, `app.py`.)

### System
- **Restart** — in-flight → `failed` → **keep as-is** (manual retry). (§4.2.)
- **History** — completed accumulate until manual clear, vanish on restart → **auto-clear a
  completed entry shortly after it finishes**; keep queued/downloading/failed.
- **Concurrency** — cap == workers, 4th rejected (429) → **3 concurrent, unlimited queue**
  (extra submissions wait their turn, no rejection). (`app.py:309`.)
- **Rate limit** — 10/min on `POST /download` → **remove entirely**. (`app.py:259`.)
- **Progress transport** — SSE + 30 s polling fallback (fallback wired incorrectly) →
  **keep SSE + polling fallback, fix the wiring** so reloads/SSE errors recover.
- **Runtime** — Flask dev server (`app.run`) → **gunicorn** (1 worker + threads; exactly 1
  worker is required because download state is in-process). Python base 3.10 → 3.12
  (3.10 reaches EOL Oct 2026).

### Storage layout
- **On-disk path** — last repo segment only (`org/foo` → `foo`, collisions) → **namespace by
  org**: `org/foo` → `MODELS_PATH/gguf/org__foo/`, model id `gguf_org__foo`. (§3, §4.3.)

### Assumed defaults (say so to override)
- `/health` stays as-is. · A folder is an installed model only if it holds ≥1
  `.gguf`/`.safetensors` file. · The `gguf`/`safetensors` type dropdown stays (routes to folder).
