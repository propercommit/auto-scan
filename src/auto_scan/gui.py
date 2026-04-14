"""Web-based GUI for auto-scan using Flask."""

from __future__ import annotations

import base64
import io
import json
import os
import platform
import subprocess
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, jsonify, render_template_string, request
from PIL import Image

from auto_scan import AutoScanError
from auto_scan.analyzer import ALL_CATEGORIES, DocumentInfo, analyze_batch, analyze_document
from auto_scan.config import Config, load_config
from auto_scan.dedup import image_hash
from auto_scan.history import find_by_hash, record_scan, search_history
from auto_scan.organizer import sanitize_name, save_document, save_unclassified
from auto_scan.usage import check_budget, get_usage
from auto_scan.scanner.discovery import ScannerInfo, discover_all_scanners, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings

app = Flask(__name__)

# Thread lock for shared mutable state
_state_lock = threading.Lock()

# ── Persistent settings ─────────────────────────────────────────────

SETTINGS_DEFAULTS = {
    "output_dir": str(Path("~/Documents/Scans").expanduser()),
    "scanner_ip": "",
    "resolution": "300",
    "color_mode": "RGB24",
    "scan_source": "Feeder",
    "mode": "auto",
    "max_tokens": "0",
}


def _settings_path() -> Path:
    """Return the path to the persistent settings JSON file."""
    return Path.home() / ".auto_scan" / "settings.json"


def _load_settings() -> dict:
    """Load persistent settings from disk, falling back to defaults."""
    path = _settings_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return {**SETTINGS_DEFAULTS, **data}
        except Exception:
            pass
    return dict(SETTINGS_DEFAULTS)


def _save_settings(settings: dict) -> None:
    """Persist settings to disk."""
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")


# ── App state ────────────────────────────────────────────────────────

state = {
    "scanner_info": None,
    "logs": [],
    "pending_images": None,
    "pending_doc_info": None,
    "job": None,  # {"status": "scanning"|"analyzing"|"saving"|"done"|"error", "result": ...}
    "scanner_caps": None,  # Cached capabilities from last connect
}


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _state_lock:
        state["logs"].append(f"[{ts}] {msg}")
        if len(state["logs"]) > 200:
            state["logs"] = state["logs"][-200:]


def _get_config(**overrides) -> Config:
    return load_config(**overrides)


def _make_thumbnail(image_data: bytes, max_dim: int = 800) -> bytes:
    """Resize an image for preview, capping at max_dim pixels."""
    img = Image.open(io.BytesIO(image_data))
    w, h = img.size
    if w > max_dim or h > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


# ── Routes ───────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/config")
def api_config():
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    return jsonify({"has_api_key": has_key})


@app.route("/api/settings")
def api_get_settings():
    return jsonify(_load_settings())


@app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.json or {}
    current = _load_settings()
    for key in SETTINGS_DEFAULTS:
        if key in data:
            current[key] = data[key]
    _save_settings(current)
    return jsonify({"ok": True})


@app.route("/api/save-key", methods=["POST"])
def api_save_key():
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "API key cannot be empty"}), 400

    env_path = None
    for candidate in [Path(".env"), Path(__file__).resolve().parents[2] / ".env"]:
        if candidate.exists():
            env_path = candidate
            break
    if env_path is None:
        env_path = Path(".env")

    if env_path.exists():
        content = env_path.read_text()
        lines = content.splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith("ANTHROPIC_API_KEY="):
                lines[i] = f"ANTHROPIC_API_KEY={key}"
                found = True
                break
        if not found:
            lines.append(f"ANTHROPIC_API_KEY={key}")
        env_path.write_text("\n".join(lines) + "\n")
    else:
        env_path.write_text(f"ANTHROPIC_API_KEY={key}\n")

    os.environ["ANTHROPIC_API_KEY"] = key
    _log("API key saved and loaded")
    return jsonify({"ok": True})


@app.route("/api/browse-folder", methods=["POST"])
def api_browse_folder():
    """Open a native OS folder picker and return the selected path."""
    data = request.json or {}
    start_dir = data.get("current", "")
    if start_dir:
        start_dir = str(Path(start_dir).expanduser())
    try:
        selected = _open_folder_dialog(start_dir)
        if selected:
            return jsonify({"ok": True, "path": selected})
        return jsonify({"ok": False, "error": "No folder selected"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _open_folder_dialog(start_dir: str = "") -> str | None:
    """Open a native folder picker dialog. Returns path or None if cancelled."""
    system = platform.system()

    if system == "Darwin":
        script = 'set p to POSIX path of (choose folder'
        if start_dir and Path(start_dir).is_dir():
            script += f' default location POSIX file "{start_dir}"'
        script += ')\nreturn p'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return result.stdout.strip().rstrip("/\\")
        return None

    if system == "Linux":
        cmd = ["zenity", "--file-selection", "--directory", "--title=Select Output Folder"]
        if start_dir and Path(start_dir).is_dir():
            cmd.append(f"--filename={start_dir}/")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            return result.stdout.strip()
        return None

    if system == "Windows":
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d = New-Object System.Windows.Forms.FolderBrowserDialog; "
        )
        if start_dir and Path(start_dir).is_dir():
            ps_script += f'$d.SelectedPath = "{start_dir}"; '
        ps_script += (
            "$d.Description = 'Select Output Folder'; "
            "if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath } else { '' }"
        )
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True, text=True, timeout=120,
        )
        path = result.stdout.strip()
        return path if path else None

    return None


@app.route("/api/discover", methods=["POST"])
def api_discover():
    """Discover all eSCL scanners on the network."""
    try:
        _log("Scanning network for eSCL scanners...")
        scanners = discover_all_scanners(timeout=6.0)
        return jsonify({
            "ok": True,
            "scanners": [
                {"ip": s.ip, "port": s.port, "name": s.name}
                for s in scanners
            ],
        })
    except Exception as e:
        _log(f"Discovery error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    ip = data.get("ip", "").strip()
    try:
        if ip:
            _log(f"Connecting to {ip}...")
            info = scanner_info_from_ip(ip)
        else:
            _log("Searching for scanner...")
            info = discover_scanner(timeout=8.0)

        client = ESCLClient(info.base_url)
        status = client.get_status()
        caps = client.get_capabilities()
        client.close()

        with _state_lock:
            state["scanner_info"] = info
        _log(f"Connected: {info.name} at {info.ip}")
        _log(f"  State: {status.state}, ADF: {status.adf_state or 'N/A'}")

        return jsonify({
            "ok": True, "name": info.name, "ip": info.ip,
            "state": status.state, "adf": status.adf_state,
            "sources": caps.sources, "resolutions": caps.resolutions,
        })
    except Exception as e:
        _log(f"Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    """Disconnect from the current scanner."""
    with _state_lock:
        state["scanner_info"] = None
    _log("Scanner disconnected")
    return jsonify({"ok": True})


def _do_scan(data: dict) -> tuple[list[bytes], Config]:
    """Common scan logic: connect, check status, scan, return images + config."""
    source = data.get("source", "Feeder")
    resolution = int(data.get("resolution", 300))
    color = data.get("color", "RGB24")

    overrides = {"scan_source": source, "resolution": resolution, "color_mode": color}
    output_dir = data.get("output_dir", "")
    if output_dir:
        overrides["output_dir"] = output_dir
    scanner_ip = data.get("scanner_ip", "").strip()
    if scanner_ip:
        overrides["scanner_ip"] = scanner_ip

    config = _get_config(**overrides)

    with _state_lock:
        info = state.get("scanner_info")
    if not info:
        if config.scanner_ip:
            info = scanner_info_from_ip(config.scanner_ip)
        else:
            info = discover_scanner(timeout=8.0)
        with _state_lock:
            state["scanner_info"] = info

    with ESCLClient(info.base_url) as client:
        status = client.get_status()
        if status.state != "Idle":
            raise AutoScanError(f"Scanner is {status.state}. Wait and try again.")
        _log("Scanning...")

        def _on_page(count):
            with _state_lock:
                job = state.get("job")
                if job:
                    job["pages_scanned"] = count

        settings = ScanSettings(
            source=source, color_mode=color,
            resolution=resolution, document_format=config.scan_format,
        )
        images = client.scan(settings, on_page=_on_page)

    _log(f"Scanned {len(images)} page(s)")
    return images, config


def _check_duplicate(images: list[bytes], config: Config) -> dict | None:
    """Check if these images were scanned before. Returns previous record or None."""
    h = image_hash(images)
    with _state_lock:
        state["_last_hash"] = h
    prev = find_by_hash(config.output_dir, h)
    return prev


def _record(config: Config, doc_info: DocumentInfo | None, folder: str, tags: list[str], pages: int, output_path) -> None:
    """Record a completed scan in the history database."""
    record_scan(
        output_dir=config.output_dir,
        filename=doc_info.filename if doc_info else Path(output_path).name,
        folder=folder,
        tags=tags,
        category=doc_info.category if doc_info else "unsorted",
        summary=doc_info.summary if doc_info else "",
        doc_date=doc_info.date if doc_info else None,
        risk_level=doc_info.risk_level if doc_info else "none",
        risks=doc_info.risks if doc_info else [],
        pages=pages,
        output_path=str(output_path),
        image_hash=state.get("_last_hash"),
    )


def _run_scan_job(data: dict, mode: str):
    """Run a scan job in a background thread. Updates state['job']."""
    try:
        # Check token budget before starting
        max_tokens = int(_load_settings().get("max_tokens", 0))
        if max_tokens > 0:
            within_budget, usage = check_budget(max_tokens)
            if not within_budget:
                raise AutoScanError(
                    f"Daily token budget exceeded: {usage['total_tokens']:,} / {max_tokens:,} tokens used. "
                    f"Increase the limit in Settings or wait until tomorrow."
                )

        with _state_lock:
            state["job"] = {"status": "scanning"}
        images, config = _do_scan(data)

        # Duplicate check
        prev = _check_duplicate(images, config)
        if prev:
            _log(f"Duplicate detected: previously saved as {prev['filename']}")
            with _state_lock:
                state["job"] = {
                    "status": "duplicate",
                    "result": {"duplicate": True, "previous": prev},
                }
            return

        if mode == "auto":
            classify = data.get("classify", True)
            if classify:
                with _state_lock:
                    state["job"]["status"] = "analyzing"
                _log("Analyzing with Claude Vision...")
                doc_info = analyze_document(images, config)
                _log(f"Classified as: {doc_info.category}")

                with _state_lock:
                    state["job"]["status"] = "saving"
                output_path = save_document(images, doc_info, config, tags=doc_info.tags)
                _log(f"Saved: {output_path}")
                _record(config, doc_info, doc_info.category, doc_info.tags, len(images), output_path)

                with _state_lock:
                    state["job"] = {
                        "status": "done",
                        "result": {
                            "ok": True, "classified": True, "pages": len(images),
                            "category": doc_info.category, "filename": doc_info.filename,
                            "summary": doc_info.summary, "date": doc_info.date,
                            "output_path": str(output_path), "tags": doc_info.tags,
                            "risk_level": doc_info.risk_level, "risks": doc_info.risks,
                        },
                    }
            else:
                with _state_lock:
                    state["job"]["status"] = "saving"
                output_path = save_unclassified(images, config)
                _log(f"Saved: {output_path}")
                _record(config, None, "unsorted", [], len(images), output_path)

                with _state_lock:
                    state["job"] = {
                        "status": "done",
                        "result": {
                            "ok": True, "classified": False, "pages": len(images),
                            "output_path": str(output_path),
                        },
                    }

        elif mode == "assisted":
            with _state_lock:
                state["job"]["status"] = "analyzing"
            _log("Analyzing with Claude Vision...")
            doc_info = analyze_document(images, config)
            _log(f"AI suggests: {doc_info.category}")

            thumb = _make_thumbnail(images[0])
            preview_b64 = base64.b64encode(thumb).decode("ascii")

            with _state_lock:
                state["pending_images"] = images
                state["pending_doc_info"] = doc_info
                state["pending_config"] = config
                state["job"] = {
                    "status": "done",
                    "result": {
                        "ok": True, "pages": len(images), "preview": preview_b64,
                        "category": doc_info.category,
                        "suggested_categories": doc_info.suggested_categories,
                        "all_categories": ALL_CATEGORIES,
                        "filename": doc_info.filename, "summary": doc_info.summary,
                        "date": doc_info.date, "key_fields": doc_info.key_fields,
                        "tags": doc_info.tags,
                        "risk_level": doc_info.risk_level, "risks": doc_info.risks,
                    },
                }

        elif mode == "batch-auto":
            with _state_lock:
                state["job"]["status"] = "analyzing"
            _log(f"Batch analyzing {len(images)} pages...")
            batch_results = analyze_batch(images, config)
            _log(f"Detected {len(batch_results)} document(s)")

            with _state_lock:
                state["job"]["status"] = "saving"
            saved = []
            for pages, doc_info in batch_results:
                doc_images = [images[p] for p in pages if p < len(images)]
                h = image_hash(doc_images)
                with _state_lock:
                    state["_last_hash"] = h
                output_path = save_document(doc_images, doc_info, config, tags=doc_info.tags)
                _record(config, doc_info, doc_info.category, doc_info.tags, len(doc_images), output_path)
                saved.append({
                    "pages": [p + 1 for p in pages],
                    "category": doc_info.category,
                    "filename": doc_info.filename,
                    "summary": doc_info.summary,
                    "tags": doc_info.tags,
                    "output_path": str(output_path),
                    "risk_level": doc_info.risk_level,
                    "risks": doc_info.risks,
                })
                _log(f"  Saved: {output_path.name}")

            with _state_lock:
                state["job"] = {
                    "status": "done",
                    "result": {"ok": True, "batch": True, "documents": saved},
                }

        elif mode == "batch-assisted":
            with _state_lock:
                state["job"]["status"] = "analyzing"
            _log(f"Batch analyzing {len(images)} pages...")
            batch_results = analyze_batch(images, config)
            _log(f"Detected {len(batch_results)} document(s)")

            # Thumbnails for ALL pages so frontend can rearrange freely
            all_previews = []
            for img_data in images:
                thumb = _make_thumbnail(img_data, max_dim=300)
                all_previews.append(base64.b64encode(thumb).decode("ascii"))

            docs = []
            for pages, doc_info in batch_results:
                docs.append({
                    "pages": [p + 1 for p in pages],
                    "category": doc_info.category,
                    "suggested_categories": doc_info.suggested_categories,
                    "all_categories": ALL_CATEGORIES,
                    "filename": doc_info.filename, "summary": doc_info.summary,
                    "date": doc_info.date, "key_fields": doc_info.key_fields,
                    "tags": doc_info.tags,
                    "risk_level": doc_info.risk_level, "risks": doc_info.risks,
                })

            with _state_lock:
                state["pending_images"] = images
                state["pending_batch_docs"] = batch_results
                state["pending_config"] = config
                state["job"] = {
                    "status": "done",
                    "result": {
                        "ok": True, "batch": True,
                        "all_previews": all_previews,
                        "documents": docs,
                    },
                }

    except Exception as e:
        _log(f"Error: {e}")
        with _state_lock:
            state["job"] = {"status": "error", "result": {"ok": False, "error": str(e)}}


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Automatic mode: start scan job in background."""
    with _state_lock:
        if state.get("job") and state["job"]["status"] not in ("done", "error", "duplicate"):
            return jsonify({"ok": False, "error": "A scan is already in progress."}), 409
    data = request.json or {}
    threading.Thread(
        target=_run_scan_job, args=(data, "auto"), daemon=True,
    ).start()
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/scan-assisted", methods=["POST"])
def api_scan_assisted():
    """Assisted mode: start scan + analyze job in background."""
    with _state_lock:
        if state.get("job") and state["job"]["status"] not in ("done", "error", "duplicate"):
            return jsonify({"ok": False, "error": "A scan is already in progress."}), 409
    data = request.json or {}
    threading.Thread(
        target=_run_scan_job, args=(data, "assisted"), daemon=True,
    ).start()
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/scan-batch", methods=["POST"])
def api_scan_batch():
    """Batch mode: scan all pages, group by document, classify each."""
    with _state_lock:
        if state.get("job") and state["job"]["status"] not in ("done", "error", "duplicate"):
            return jsonify({"ok": False, "error": "A scan is already in progress."}), 409
    data = request.json or {}
    mode = "batch-auto" if data.get("auto", True) else "batch-assisted"
    threading.Thread(
        target=_run_scan_job, args=(data, mode), daemon=True,
    ).start()
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/job")
def api_job():
    """Poll the current scan job status."""
    with _state_lock:
        job = state.get("job")
        if not job:
            return jsonify({"status": "idle"})
        return jsonify(job)


@app.route("/api/page-image/<int:page_num>")
def api_page_image(page_num):
    """Serve a full-size page image for preview. page_num is 1-indexed."""
    images = state.get("pending_images")
    if not images or page_num < 1 or page_num > len(images):
        return "Not found", 404
    img_data = images[page_num - 1]
    thumb = _make_thumbnail(img_data, max_dim=1200)
    return Response(thumb, mimetype="image/jpeg")


@app.route("/api/save-classified", methods=["POST"])
def api_save_classified():
    """Save pending scanned images with folder + tags."""
    data = request.json or {}
    folder = sanitize_name(data.get("folder", "").strip() or "other")
    tags = data.get("tags", [])
    filename = data.get("filename", "").strip()

    with _state_lock:
        images = state.get("pending_images")
        doc_info = state.get("pending_doc_info")
        config = state.get("pending_config")

    if not images or not doc_info:
        return jsonify({"ok": False, "error": "No pending scan to save."}), 400

    try:
        if filename:
            doc_info.filename = sanitize_name(filename.removesuffix(".pdf")) + ".pdf"

        if not config:
            config = _get_config(
                **({"output_dir": data["output_dir"]} if data.get("output_dir") else {}),
            )

        output_path = save_document(
            images, doc_info, config, folder=folder, tags=tags,
        )
        _log(f"Saved to {folder}/: {output_path.name}")
        if tags:
            _log(f"  Tags: {', '.join(tags)}")

        _record(config, doc_info, folder, tags, len(images), output_path)

        with _state_lock:
            state["pending_images"] = None
            state["pending_doc_info"] = None
            state["pending_config"] = None

        return jsonify({
            "ok": True,
            "output_path": str(output_path),
            "folder": folder,
            "tags": tags,
        })
    except Exception as e:
        _log(f"Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/save-batch", methods=["POST"])
def api_save_batch():
    """Save all documents from a batch scan with user-rearranged pages."""
    data = request.json or {}
    documents = data.get("documents", [])

    with _state_lock:
        images = state.get("pending_images")
        config = state.get("pending_config")

    if not images:
        return jsonify({"ok": False, "error": "No pending batch to save."}), 400

    if not config:
        config = _get_config(
            **({"output_dir": data["output_dir"]} if data.get("output_dir") else {}),
        )

    try:
        results = []
        for edit in documents:
            pages = [p - 1 for p in edit.get("pages", [])]  # 1-indexed → 0-indexed
            if not pages:
                continue

            folder = sanitize_name(edit.get("folder", "").strip() or "other")
            tags = edit.get("tags", [])
            raw_filename = edit.get("filename", "").strip()
            if raw_filename:
                filename = sanitize_name(raw_filename.removesuffix(".pdf")) + ".pdf"
            else:
                filename = f"{datetime.now().strftime('%Y-%m-%d')}_document.pdf"

            doc_info = DocumentInfo(
                category=folder,
                filename=filename,
                summary=edit.get("summary", ""),
                date=edit.get("date"),
                tags=tags,
            )

            doc_images = [images[p] for p in pages if p < len(images)]
            output_path = save_document(doc_images, doc_info, config, folder=folder, tags=tags)
            _log(f"Batch saved: {output_path.name}")

            h = image_hash(doc_images)
            state["_last_hash"] = h
            _record(config, doc_info, folder, tags, len(doc_images), output_path)

            results.append({
                "ok": True, "output_path": str(output_path),
                "folder": folder, "tags": tags, "filename": doc_info.filename,
            })

        with _state_lock:
            state["pending_images"] = None
            state["pending_batch_docs"] = None
            state["pending_config"] = None

        return jsonify({"ok": True, "documents": results})
    except Exception as e:
        _log(f"Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/history")
def api_history():
    """Search scan history."""
    query = request.args.get("q", "")
    try:
        config = _get_config()
    except RuntimeError:
        config = type("C", (), {"output_dir": Path("~/Documents/Scans").expanduser()})()
    results = search_history(config.output_dir, query)
    return jsonify(results)


@app.route("/api/usage")
def api_usage():
    """Return today's token usage and cost."""
    return jsonify(get_usage())


@app.route("/api/logs")
def api_logs():
    with _state_lock:
        return jsonify(list(state["logs"]))


# ── HTML Template ────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auto-Scan</title>
<style>
  :root { --bg: #f8f9fa; --card: #fff; --border: #dee2e6; --primary: #0858cf; --primary-hover: #0647a8; --primary-light: #dbe8fc; --primary-text: #063b87; --gray: #495057; --gray-light: #5f6b75; --green: #146c43; --green-bg: #d1e7dd; --red: #b02a37; --purple: #6d28d9; --radius: 8px; --transition: .2s ease; --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; --mono: "SF Mono", Menlo, Monaco, monospace; --focus-ring: 0 0 0 3px rgba(8,88,207,.4); }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: var(--font); background: var(--bg); color: #212529; line-height: 1.5; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); border: 0; }
  .container { max-width: 740px; margin: 0 auto; padding: 24px 16px; }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: var(--radius); padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 13px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--gray); margin-bottom: 12px; }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 4px; color: var(--gray); }
  input[type="text"], select { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: var(--radius); font-size: 14px; font-family: var(--font); background-color: var(--card); color: #212529; transition: border-color var(--transition), box-shadow var(--transition); }
  input[type="text"]:hover, select:hover { border-color: #adb5bd; }
  select { appearance: none; -webkit-appearance: none; background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23495057' stroke-width='1.5' fill='none' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 12px center; padding-right: 32px; cursor: pointer; }
  input:focus, select:focus { outline: 2px solid var(--primary); outline-offset: 1px; border-color: var(--primary); box-shadow: var(--focus-ring); }
  .input-error { border-color: var(--red) !important; background: #fff5f5 !important; box-shadow: 0 0 0 3px rgba(176,42,55,.15) !important; }
  .row { display: flex; gap: 12px; margin-bottom: 12px; }
  .row > * { flex: 1; }
  .radio-group { display: flex; gap: 16px; padding: 6px 0; }
  .radio-group label { display: flex; align-items: center; gap: 6px; font-weight: 400; cursor: pointer; }
  .radio-group input[type="radio"]:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .btn-row { display: flex; gap: 10px; }
  .scan-progress { margin-top: 14px; padding: 14px 16px; background: #0F1117; border-radius: 10px; color: #F1F5F9; }
  .scan-progress-inner { display: flex; align-items: center; gap: 10px; }
  .scan-progress-spinner { width: 16px; height: 16px; border: 2px solid rgba(129,140,248,.3); border-top-color: #818CF8; border-radius: 50%; animation: spin .8s linear infinite; flex-shrink: 0; }
  .scan-progress-text { font-size: 14px; font-weight: 600; }
  .scan-progress-pages { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 6px; }
  .scan-progress-page { display: inline-flex; align-items: center; gap: 5px; padding: 5px 10px; background: #1A1D2B; border-radius: 6px; font-size: 12px; font-family: var(--mono); color: #94A3B8; animation: fadeSlideIn .3s ease; }
  .scan-progress-page svg { color: #34D399; }
  @keyframes fadeSlideIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 10px 20px; border: none; border-radius: var(--radius); font-size: 15px; font-weight: 600; font-family: var(--font); cursor: pointer; transition: background var(--transition), box-shadow var(--transition), transform .1s; width: 100%; }
  .btn:hover:not(:disabled) { box-shadow: 0 2px 8px rgba(0,0,0,.1); }
  .btn:active:not(:disabled) { transform: scale(.98); }
  .btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; box-shadow: var(--focus-ring); }
  .btn:disabled { opacity: .55; cursor: not-allowed; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  .btn-secondary { background: #e9ecef; color: var(--gray); }
  .btn-secondary:hover:not(:disabled) { background: #dee2e6; }
  .btn-batch { background: var(--purple); color: #fff; }
  .btn-batch:hover:not(:disabled) { background: #5b21b6; }
  .btn-connect { padding: 8px 16px; width: auto; font-size: 14px; }
  .connect-row { display: flex; gap: 8px; align-items: flex-end; }
  .connect-row > :first-child { flex: 1; }
  .status { font-size: 13px; padding: 6px 0; }
  .status.connected { color: var(--green); font-weight: 600; }
  .status.disconnected { color: var(--gray-light); }
  .scanner-info { display: none; margin-top: 12px; padding: 12px 16px; background: var(--green-bg); border-radius: var(--radius); align-items: center; gap: 12px; }
  .scanner-info.visible { display: flex; }
  .scanner-info .scanner-icon { flex-shrink: 0; color: var(--green); }
  .scanner-info-text { flex: 1; min-width: 0; }
  .scanner-info-name { font-size: 14px; font-weight: 700; color: #0a3622; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .scanner-info-detail { font-size: 12px; color: #1a6b43; margin-top: 1px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .btn-disconnect { flex-shrink: 0; width: 32px; height: 32px; border: none; border-radius: var(--radius); background: rgba(176,42,55,.08); color: var(--red); cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background var(--transition), transform .1s; }
  .btn-disconnect:hover { background: rgba(176,42,55,.18); transform: scale(1.1); }
  .btn-disconnect:active { transform: scale(.95); }
  .btn-disconnect:focus-visible { outline: 2px solid var(--red); outline-offset: 2px; }
  .status.error { color: var(--red); font-weight: 600; }
  .results-grid { display: grid; grid-template-columns: 100px 1fr; gap: 4px 12px; font-size: 14px; }
  .results-grid dt { font-weight: 600; color: var(--gray); }
  .results-grid dd { color: #212529; word-break: break-word; }
  .log { background: #1a1a2e; color: #e0e0e0; border-radius: var(--radius); padding: 12px; font-family: var(--mono); font-size: 13px; height: 160px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
  .spinner { display: none; width: 18px; height: 18px; border: 2px solid #fff4; border-top-color: #fff; border-radius: 50%; animation: spin .6s linear infinite; margin-right: 8px; }
  .spinner-inline { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(0,0,0,.15); border-top-color: var(--primary); border-radius: 50%; animation: spin .6s linear infinite; margin-right: 6px; vertical-align: middle; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .busy .spinner { display: inline-block; }
  .output-path { margin-top: 12px; padding: 8px 12px; background: var(--green-bg); color: #0a3622; border-radius: var(--radius); font-size: 13px; word-break: break-all; }
  .mode-toggle { display: flex; background: #e9ecef; border-radius: var(--radius); padding: 3px; margin-bottom: 12px; }
  .mode-toggle button { flex: 1; padding: 8px 16px; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; font-family: var(--font); cursor: pointer; background: transparent; color: var(--gray-light); transition: all var(--transition); }
  .mode-toggle button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
  .mode-toggle button.active { background: #fff; color: #212529; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.active { display: flex; }
  .modal { background: #fff; border-radius: calc(var(--radius) * 1.5); padding: 24px; max-width: 90vw; box-shadow: 0 12px 40px rgba(0,0,0,.25); }
  .modal-sm { width: 480px; }
  .modal h2 { font-size: 18px; font-weight: 700; margin-bottom: 8px; color: #212529; text-transform: none; letter-spacing: 0; }
  .modal p { font-size: 14px; color: var(--gray); margin-bottom: 16px; line-height: 1.6; }
  .modal a { color: var(--primary); text-decoration: underline; }
  .modal a:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .modal input[type="password"] { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: var(--radius); font-size: 14px; font-family: var(--mono); margin-bottom: 6px; transition: border-color var(--transition); }
  .modal input[type="password"]:focus { outline: 2px solid var(--primary); outline-offset: 1px; border-color: var(--primary); box-shadow: var(--focus-ring); }
  .modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; position: sticky; bottom: 0; background: #fff; padding: 12px 0 0; }
  .modal-btns .btn { width: auto; }
  .modal-error { color: var(--red); font-size: 13px; font-weight: 600; min-height: 20px; }
  .classify-modal { width: 860px; max-height: 90vh; overflow-y: auto; }
  .classify-layout { display: flex; gap: 20px; }
  .classify-preview { flex: 0 0 340px; }
  .classify-preview img { width: 100%; border-radius: var(--radius); border: 1px solid var(--border); box-shadow: 0 2px 8px rgba(0,0,0,.08); }
  .classify-details { flex: 1; min-width: 0; }
  .classify-summary { font-size: 14px; color: var(--gray); margin-bottom: 16px; padding: 12px; background: var(--bg); border-radius: var(--radius); }
  .classify-summary strong { color: #212529; }
  .tag-section { margin-bottom: 14px; }
  .tag-section h3 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--gray); margin-bottom: 8px; }
  .tag-grid { display: flex; flex-wrap: wrap; gap: 6px; }
  .tag-btn { padding: 8px 16px; border: 2px solid var(--border); border-radius: var(--radius); background: #fff; color: #212529; font-size: 14px; font-weight: 500; font-family: var(--font); cursor: pointer; transition: all var(--transition); text-transform: capitalize; }
  .tag-btn:hover { border-color: var(--primary); color: var(--primary-text); background: var(--primary-light); }
  .tag-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .tag-btn.selected { border-color: var(--primary); background: var(--primary-light); color: var(--primary-text); font-weight: 700; }
  .tag-btn.suggested { border-color: #9dc2f7; background: #edf3fc; color: var(--primary-text); }
  .add-tag-row { display: flex; gap: 6px; margin-top: 8px; }
  .add-tag-row input { flex: 1; padding: 8px 12px; font-size: 14px; border: 1px solid var(--border); border-radius: var(--radius); background: #fff; color: #212529; }
  .btn-add-tag { flex-shrink: 0; padding: 8px 14px; width: auto; font-size: 13px; }
  .classify-folder { margin-top: 14px; }
  .classify-folder input[type="text"] { font-family: var(--mono); font-size: 13px; }
  .field-hint { font-size: 12px; color: var(--gray-light); margin-top: 3px; }
  .classify-filename { margin-top: 12px; }
  .classify-filename input[type="text"] { font-family: var(--mono); font-size: 13px; }
  .browse-row { display: flex; gap: 6px; align-items: center; }
  .browse-row input { flex: 1; }
  .btn-browse { flex-shrink: 0; width: 40px; height: 38px; padding: 0; border: 1px solid var(--border); border-radius: var(--radius); background: #fff; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background var(--transition), border-color var(--transition); color: var(--gray); }
  .btn-browse:hover { background: #e9ecef; border-color: var(--primary); color: var(--primary); }
  .btn-browse:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .risk-alert { margin-top: 12px; padding: 12px 14px; border-radius: var(--radius); font-size: 13px; line-height: 1.6; }
  .risk-alert.risk-none { display: none; }
  .risk-alert.risk-low { background: #fff3cd; border: 1px solid #cc9a06; color: #664d03; }
  .risk-alert.risk-medium { background: #ffe0cc; border: 1px solid #c35a02; color: #471a00; }
  .risk-alert.risk-high { background: #f8d7da; border: 1px solid var(--red); color: #6a1a21; }
  .risk-alert h4 { font-size: 13px; font-weight: 700; margin-bottom: 4px; }
  .risk-alert ul { margin: 4px 0 0 16px; padding: 0; }
  .risk-alert li { margin-bottom: 2px; }
  .batch-modal { width: 900px; max-height: 90vh; overflow-y: auto; }
  .batch-docs { display: flex; flex-direction: column; gap: 14px; max-height: 55vh; overflow-y: auto; padding: 4px; }
  .batch-doc { border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; background: var(--bg); }
  .batch-doc-head { margin-bottom: 10px; }
  .batch-doc-title { font-size: 15px; font-weight: 700; color: #212529; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
  .batch-doc-title .batch-doc-label { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .batch-doc-summary { font-size: 14px; color: var(--gray); margin-bottom: 8px; }
  .batch-page-grid { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; min-height: 90px; padding: 10px; border: 2px dashed var(--border); border-radius: var(--radius); transition: border-color var(--transition), background var(--transition); }
  .batch-page-grid.drop-target { border-color: var(--primary); background: var(--primary-light); }
  .batch-page { width: 56px; text-align: center; position: relative; border-radius: 6px; transition: opacity var(--transition); cursor: grab; }
  .batch-page:active { cursor: grabbing; }
  .batch-page.dragging { opacity: .3; }
  .batch-page img { width: 80px; height: 104px; object-fit: cover; border-radius: 6px; border: 2px solid var(--border); transition: border-color var(--transition); }
  .batch-page:hover img { border-color: var(--primary); }
  .batch-page span { display: block; font-size: 12px; color: var(--gray); margin-top: 2px; }
  .batch-page select { width: 100%; font-size: 11px; padding: 2px; border: 1px solid var(--border); border-radius: 3px; margin-top: 2px; cursor: pointer; }
  .batch-page-grid-empty { color: var(--gray-light); font-size: 14px; font-style: italic; padding: 16px; text-align: center; width: 100%; }
  .btn-add-doc { background: none; border: 2px dashed var(--border); border-radius: var(--radius); padding: 10px; width: 100%; font-size: 13px; font-weight: 600; color: var(--gray); cursor: pointer; transition: border-color var(--transition), color var(--transition); font-family: var(--font); margin-bottom: 8px; }
  .btn-add-doc:hover { border-color: var(--primary); color: var(--primary); }
  .btn-add-doc:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .btn-remove-doc { background: none; border: none; color: var(--red); font-size: 13px; cursor: pointer; font-weight: 600; font-family: var(--font); padding: 4px 10px; border-radius: 4px; transition: background var(--transition); }
  .btn-remove-doc:hover { background: #f8d7da; }
  .btn-clear { margin-top: 12px; display: inline-block; font-size: 13px; font-weight: 600; color: var(--gray); background: none; border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 14px; cursor: pointer; font-family: var(--font); transition: background var(--transition); }
  .btn-clear:hover { background: #e9ecef; }
  .lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.85); z-index: 200; align-items: center; justify-content: center; flex-direction: column; cursor: pointer; }
  .lightbox.active { display: flex; }
  .lightbox img { max-width: min(92vw, 1200px); max-height: 85vh; border-radius: var(--radius); box-shadow: 0 8px 40px rgba(0,0,0,.5); object-fit: contain; }
  .lightbox-label { color: #fff; font-size: 15px; font-weight: 600; margin-top: 12px; }
  .lightbox-nav { position: absolute; top: 50%; transform: translateY(-50%); background: rgba(255,255,255,.15); border: none; color: #fff; font-size: 32px; width: 48px; height: 48px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background var(--transition); }
  .lightbox-nav:hover { background: rgba(255,255,255,.3); }
  .lightbox-nav:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .lightbox-prev { left: 16px; }
  .lightbox-next { right: 16px; }
  .lightbox-close { position: absolute; top: 16px; right: 16px; background: rgba(255,255,255,.15); border: none; color: #fff; font-size: 24px; width: 40px; height: 40px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background var(--transition); }
  .lightbox-close:hover { background: rgba(255,255,255,.3); }
  .batch-fields { display: grid; grid-template-columns: 80px 1fr; gap: 6px 12px; font-size: 14px; align-items: center; }
  .batch-fields label { font-weight: 600; color: var(--gray); font-size: 13px; }
  .batch-fields input { padding: 8px 12px; font-size: 14px; font-family: var(--mono); }
  .batch-tag-grid { display: flex; flex-wrap: wrap; gap: 6px; grid-column: 2; }
  .batch-add-tag-row { display: flex; gap: 6px; grid-column: 2; margin-top: 4px; }
  .batch-add-tag-row input { flex: 1; padding: 8px 12px; font-size: 14px; font-family: var(--font); border: 1px solid var(--border); border-radius: var(--radius); background: #fff; color: #212529; }
  .batch-add-tag-row button { flex-shrink: 0; padding: 8px 14px; font-size: 13px; }
  .batch-tag { padding: 6px 14px; border: 2px solid var(--border); border-radius: var(--radius); font-size: 14px; font-weight: 500; font-family: var(--font); cursor: pointer; background: #fff; color: #212529; transition: all var(--transition); }
  .batch-tag.selected { border-color: var(--primary); background: var(--primary-light); color: var(--primary-text); font-weight: 600; }
  .batch-tag:hover { border-color: var(--primary); }
  .batch-tag:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .batch-results { list-style: none; padding: 0; }
  .batch-results li { padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
  .batch-results li:last-child { border-bottom: none; }
  .batch-results .br-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .batch-results .br-detail { font-size: 12px; color: var(--gray); }
  /* ── Dark usage dashboard (Monetir-inspired) ─────────────── */
  .usage-dash { background: #0F1117; border-radius: 16px; margin-bottom: 16px; padding: 24px; box-shadow: 0 4px 24px rgba(0,0,0,.25); }
  .usage-dash.over-budget { box-shadow: 0 0 0 1px rgba(239,68,68,.5), 0 4px 24px rgba(239,68,68,.15); }
  .usage-dash-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
  .usage-dash-title { font-size: 15px; font-weight: 700; color: #F1F5F9; letter-spacing: -0.2px; margin: 0; text-transform: none; }
  .usage-dash-subtitle { font-size: 12px; color: #64748B; margin: 0; }
  .usage-dash-live { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: #34D399; text-transform: uppercase; letter-spacing: .5px; }
  .usage-dash-live::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #34D399; box-shadow: 0 0 8px #34D399; animation: pulse-dot 2s infinite; }
  @keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
  .usage-heroes { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }
  .usage-hero { background: #1A1D2B; border-radius: 12px; padding: 16px; position: relative; overflow: hidden; transition: background .2s; }
  .usage-hero:hover { background: #1E2235; }
  .usage-hero-accent { position: absolute; top: 0; left: 0; width: 100%; height: 2px; }
  .usage-hero-accent.tokens { background: linear-gradient(90deg, #6366F1, #818CF8); }
  .usage-hero-accent.cost { background: linear-gradient(90deg, #22C55E, #4ADE80); }
  .usage-hero-accent.calls { background: linear-gradient(90deg, #A855F7, #C084FC); }
  .usage-hero-accent.budget { background: linear-gradient(90deg, #F59E0B, #FBBF24); }
  .usage-hero-top { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .usage-hero-icon { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
  .usage-hero-icon.tokens { background: rgba(99,102,241,.15); color: #818CF8; }
  .usage-hero-icon.cost { background: rgba(34,197,94,.15); color: #4ADE80; }
  .usage-hero-icon.calls { background: rgba(168,85,247,.15); color: #C084FC; }
  .usage-hero-icon.budget { background: rgba(245,158,11,.15); color: #FBBF24; }
  .usage-hero-label { font-size: 12px; color: #64748B; font-weight: 500; }
  .usage-hero-value { font-size: 24px; font-weight: 800; color: #F1F5F9; font-family: var(--mono); line-height: 1; letter-spacing: -0.5px; }
  .usage-hero-sub { font-size: 11px; color: #475569; margin-top: 4px; font-family: var(--mono); }
  .usage-dash.over-budget .usage-hero-value { color: #EF4444; }
  .usage-budget-wrap { margin-bottom: 16px; }
  .usage-budget-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  .usage-budget-header span { font-size: 12px; color: #64748B; }
  .usage-budget-header .over { color: #EF4444; font-weight: 700; }
  .usage-budget-bar { height: 6px; background: #1A1D2B; border-radius: 3px; overflow: hidden; }
  .usage-budget-fill { height: 100%; border-radius: 3px; transition: width .4s ease; background: linear-gradient(90deg, #6366F1, #818CF8); }
  .usage-budget-fill.warn { background: linear-gradient(90deg, #F59E0B, #FBBF24); }
  .usage-budget-fill.over { background: linear-gradient(90deg, #EF4444, #F87171); }
  .usage-chart-section { background: #1A1D2B; border-radius: 12px; padding: 16px; }
  .usage-chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .usage-chart-label { font-size: 13px; font-weight: 600; color: #94A3B8; }
  .usage-chart-legend { display: flex; gap: 14px; }
  .usage-chart-legend span { display: flex; align-items: center; gap: 5px; font-size: 11px; color: #64748B; }
  .usage-chart-legend span::before { content: ''; width: 8px; height: 8px; border-radius: 2px; }
  .usage-chart-legend .leg-in::before { background: #818CF8; }
  .usage-chart-legend .leg-out::before { background: #34D399; }
  .usage-chart-wrap { position: relative; height: 120px; }
  .usage-chart { width: 100%; height: 100%; display: block; }
  .usage-chart-empty { text-align: center; color: #475569; font-size: 13px; padding-top: 44px; }
  @media (max-width: 768px) { .usage-heroes { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 480px) { .btn-row { flex-direction: column; } .connect-row { flex-wrap: wrap; } .usage-heroes { grid-template-columns: repeat(2, 1fr); } .usage-hero-value { font-size: 20px; } .usage-dash { padding: 16px; } }
  @media (prefers-reduced-motion: reduce) { .spinner, .spinner-inline { animation: none; } * { transition: none !important; } }
  @media (max-width: 640px) { .batch-modal { width: 95vw; } .classify-layout { flex-direction: column; gap: 16px; } .classify-preview { flex: none; } .classify-modal { width: 95vw; } }
</style>
</head>
<body>
<a href="#main-content" class="sr-only">Skip to main content</a>
<main id="main-content">
<div class="container">
  <h1>Auto-Scan</h1>

  <div class="usage-dash" id="usage-dash">
    <div class="usage-dash-header">
      <div>
        <div class="usage-dash-title">API Usage</div>
        <div class="usage-dash-subtitle">Today's consumption</div>
      </div>
      <div class="usage-dash-live">Live</div>
    </div>
    <div class="usage-heroes">
      <div class="usage-hero">
        <div class="usage-hero-accent tokens"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon tokens"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
          <div class="usage-hero-label">Tokens</div>
        </div>
        <div class="usage-hero-value" id="usage-tokens">0</div>
        <div class="usage-hero-sub" id="usage-tokens-detail">0 in / 0 out</div>
      </div>
      <div class="usage-hero">
        <div class="usage-hero-accent cost"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon cost"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg></div>
          <div class="usage-hero-label">Est. Cost</div>
        </div>
        <div class="usage-hero-value" id="usage-cost">$0.00</div>
        <div class="usage-hero-sub" id="usage-cost-detail">$3/1M in, $15/1M out</div>
      </div>
      <div class="usage-hero">
        <div class="usage-hero-accent calls"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon calls"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg></div>
          <div class="usage-hero-label">API Calls</div>
        </div>
        <div class="usage-hero-value" id="usage-calls">0</div>
        <div class="usage-hero-sub" id="usage-calls-detail">today</div>
      </div>
      <div class="usage-hero">
        <div class="usage-hero-accent budget"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon budget"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M12 6v6l4 2"/></svg></div>
          <div class="usage-hero-label">Budget</div>
        </div>
        <div class="usage-hero-value" id="usage-budget-val">Unlimited</div>
        <div class="usage-hero-sub" id="usage-budget-detail">no cap set</div>
      </div>
    </div>
    <div class="usage-budget-wrap" id="usage-budget-section" style="display:none">
      <div class="usage-budget-header"><span id="usage-budget-left"></span><span id="usage-budget-right"></span></div>
      <div class="usage-budget-bar"><div class="usage-budget-fill" id="usage-budget-fill"></div></div>
    </div>
    <div class="usage-chart-section">
      <div class="usage-chart-header">
        <div class="usage-chart-label">Token History</div>
        <div class="usage-chart-legend"><span class="leg-in">Input</span><span class="leg-out">Output</span></div>
      </div>
      <div class="usage-chart-wrap" id="usage-chart-wrap">
        <div class="usage-chart-empty" id="usage-chart-empty">No API calls yet today</div>
        <canvas class="usage-chart" id="usage-chart" style="display:none"></canvas>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>Scanner</h2>
    <div class="connect-row">
      <div>
        <label for="scanner-ip">Scanner IP (leave blank for auto-discover)</label>
        <input type="text" id="scanner-ip" placeholder="192.168.1.x" list="scanner-list">
        <datalist id="scanner-list"></datalist>
      </div>
      <button class="btn btn-secondary btn-connect" onclick="discoverScanners()">Find</button>
      <button class="btn btn-primary btn-connect" onclick="connect()">Connect</button>
    </div>
    <div class="status disconnected" id="scanner-status" role="status" aria-live="polite">Not connected</div>
    <div class="scanner-info" id="scanner-info">
      <svg class="scanner-icon" width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="8" rx="2"/><path d="M6 8V5a1 1 0 011-1h10a1 1 0 011 1v3"/><path d="M6 16v2a1 1 0 001 1h10a1 1 0 001-1v-2"/><circle cx="17" cy="12" r="1" fill="currentColor"/></svg>
      <div class="scanner-info-text">
        <div class="scanner-info-name" id="scanner-info-name"></div>
        <div class="scanner-info-detail" id="scanner-info-detail"></div>
      </div>
      <button class="btn-disconnect" onclick="disconnect()" title="Disconnect scanner" aria-label="Disconnect scanner">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
  </div>

  <div class="card">
    <h2>Settings</h2>
    <div class="row">
      <div>
        <label>Source</label>
        <div class="radio-group" role="group" aria-label="Scan source">
          <label><input type="radio" name="source" value="Feeder" checked> Document Feeder</label>
          <label><input type="radio" name="source" value="Platen"> Flatbed</label>
        </div>
      </div>
    </div>
    <div class="row">
      <div><label for="resolution">Resolution</label><select id="resolution"><option value="150">150 DPI</option><option value="200">200 DPI</option><option value="300" selected>300 DPI</option><option value="600">600 DPI</option></select></div>
      <div><label for="color">Color Mode</label><select id="color"><option value="RGB24">Color</option><option value="Grayscale8">Grayscale</option></select></div>
    </div>
    <div>
      <label for="output-dir">Output Directory</label>
      <div class="browse-row">
        <input type="text" id="output-dir" value="">
        <button class="btn-browse" onclick="browseFolder()" title="Browse folders" aria-label="Browse folders"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg></button>
      </div>
    </div>
    <div style="margin-top:10px">
      <label for="max-tokens">Daily Token Budget (0 = unlimited)</label>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="max-tokens" placeholder="0" onchange="saveSettings()" style="flex:1">
        <button class="btn btn-secondary" style="width:auto;padding:8px 14px;font-size:13px;white-space:nowrap" onclick="crazyMode()">Crazy Mode</button>
      </div>
      <div class="field-hint">Blocks API calls when the daily limit is reached. Resets at midnight.</div>
    </div>
  </div>

  <div class="card">
    <div class="mode-toggle" role="tablist" aria-label="Scan mode">
      <button class="active" id="mode-auto" role="tab" aria-selected="true" onclick="setMode('auto')">Automatic</button>
      <button id="mode-assisted" role="tab" aria-selected="false" onclick="setMode('assisted')">Assisted</button>
    </div>
    <p id="mode-desc" style="font-size:13px;color:var(--gray);margin-bottom:12px" aria-live="polite">AI automatically classifies and saves the document.</p>
    <div class="btn-row">
      <button class="btn btn-primary" id="btn-classify" onclick="doScan()" disabled><span class="spinner" aria-hidden="true"></span><span class="sr-only busy-text" hidden>Scanning...</span>Scan &amp; Classify</button>
      <button class="btn btn-batch" id="btn-batch" onclick="doBatchScan()" disabled><span class="spinner" aria-hidden="true"></span>Batch Scan</button>
      <button class="btn btn-secondary" id="btn-scan" onclick="scanOnly()" disabled><span class="spinner" aria-hidden="true"></span><span class="sr-only busy-text" hidden>Scanning...</span>Scan Only</button>
    </div>
    <div class="scan-progress" id="scan-progress" style="display:none" aria-live="polite">
      <div class="scan-progress-inner">
        <span class="scan-progress-spinner"></span>
        <span class="scan-progress-text" id="scan-progress-text">Scanning...</span>
      </div>
      <div class="scan-progress-pages" id="scan-progress-pages"></div>
    </div>
  </div>

  <div class="card" id="results-card" style="display:none" aria-live="polite">
    <h2>Classification Results</h2>
    <dl class="results-grid">
      <dt>Folder</dt><dd id="r-folder">--</dd>
      <dt>Tags</dt><dd id="r-tags">--</dd>
      <dt>Filename</dt><dd id="r-filename">--</dd>
      <dt>Summary</dt><dd id="r-summary">--</dd>
      <dt>Date</dt><dd id="r-date">--</dd>
    </dl>
    <div class="risk-alert" id="r-risk" style="display:none"></div>
    <div class="output-path" id="r-path" style="display:none"></div>
    <button class="btn-clear" onclick="clearResults()">Clear</button>
  </div>

  <div class="card" id="batch-results-card" style="display:none" aria-live="polite">
    <h2 id="batch-results-title">Batch Complete</h2>
    <ul class="batch-results" id="batch-results-list"></ul>
    <button class="btn-clear" onclick="clearResults()">Clear</button>
  </div>

  <div class="card">
    <h2>Activity Log</h2>
    <div class="log" id="log" role="log" aria-live="polite" aria-label="Activity log"></div>
  </div>
</div>

</main>

<!-- API Key Modal -->
<div class="modal-overlay" id="api-key-modal" role="dialog" aria-modal="true" aria-labelledby="api-key-title">
  <div class="modal modal-sm">
    <h2 id="api-key-title">Anthropic API Key Required</h2>
    <p>An API key is needed for AI document classification.<br>Get one at <a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a></p>
    <label for="api-key-input">API Key</label>
    <input type="password" id="api-key-input" placeholder="sk-ant-...">
    <div class="modal-error" id="api-key-error"></div>
    <div class="modal-btns">
      <button class="btn btn-secondary" onclick="closeApiModal()">Skip</button>
      <button class="btn btn-primary" onclick="saveApiKey()">Save</button>
    </div>
  </div>
</div>

<!-- Classification Modal -->
<div class="modal-overlay" id="classify-modal" role="dialog" aria-modal="true" aria-labelledby="classify-title">
  <div class="modal classify-modal">
    <h2 id="classify-title">Classify Document</h2>
    <div class="classify-layout">
      <div class="classify-preview">
        <img id="classify-img" src="" alt="Document preview">
      </div>
      <div class="classify-details">
        <div class="classify-summary" id="classify-summary"></div>
        <div class="tag-section">
          <h3 id="tags-label">Tags</h3>
          <div class="tag-grid" id="tag-buttons" role="group" aria-labelledby="tags-label"></div>
          <div class="add-tag-row">
            <input type="text" id="add-tag-input" placeholder="Add a tag..." aria-label="Add a custom tag">
            <button class="btn btn-secondary btn-add-tag" onclick="addCustomTag()">Add</button>
          </div>
        </div>
        <div class="risk-alert" id="classify-risk" style="display:none"></div>
        <div class="classify-folder">
          <label for="classify-folder">Save to folder</label>
          <input type="text" id="classify-folder" value="" list="folder-suggestions">
          <datalist id="folder-suggestions"></datalist>
          <p class="field-hint">Subfolder inside your output directory where this document will be saved.</p>
        </div>
        <div class="classify-filename">
          <label for="classify-fn">Filename</label>
          <input type="text" id="classify-fn" value="">
        </div>
        <div class="modal-btns">
          <button class="btn btn-secondary" onclick="cancelClassify()">Cancel</button>
          <button class="btn btn-primary" id="btn-save-classify" onclick="saveClassified()">Save</button>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- Batch Review Modal -->
<div class="modal-overlay" id="batch-modal" role="dialog" aria-modal="true" aria-labelledby="batch-title">
  <div class="modal batch-modal">
    <h2 id="batch-title">Batch Scan &mdash; <span id="batch-count">0</span> Documents Detected</h2>
    <div class="batch-docs" id="batch-docs"></div>
    <div class="modal-btns" style="margin-top:16px">
      <button class="btn btn-secondary" onclick="cancelBatch()">Cancel</button>
      <button class="btn btn-primary" id="btn-save-batch" onclick="saveBatch()">Save All</button>
    </div>
  </div>
</div>

<!-- Page Lightbox -->
<div class="lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="Page preview">
  <button class="lightbox-close" onclick="closeLightbox()" aria-label="Close preview">&times;</button>
  <button class="lightbox-nav lightbox-prev" onclick="lightboxNav(-1)" aria-label="Previous page">&#8249;</button>
  <button class="lightbox-nav lightbox-next" onclick="lightboxNav(1)" aria-label="Next page">&#8250;</button>
  <img id="lightbox-img" src="" alt="Full page preview">
  <div class="lightbox-label" id="lightbox-label"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const mainContent = document.getElementById('main-content');
function openModal(id) { $(id).classList.add('active'); mainContent.setAttribute('inert', ''); }
function closeModal(id) { $(id).classList.remove('active'); if (!document.querySelector('.modal-overlay.active')) mainContent.removeAttribute('inert'); }
let currentMode = 'auto';
let selectedTags = new Set();
let pendingRisk = {level: null, risks: []};

(async function init() {
  // Load saved settings
  try {
    const res = await fetch('/api/settings');
    const s = await res.json();
    if (s.output_dir) $('#output-dir').value = s.output_dir;
    if (s.scanner_ip) $('#scanner-ip').value = s.scanner_ip;
    if (s.resolution) $('#resolution').value = s.resolution;
    if (s.color_mode) $('#color').value = s.color_mode;
    if (s.scan_source) {
      const radio = document.querySelector('input[name="source"][value="' + s.scan_source + '"]');
      if (radio) radio.checked = true;
    }
    if (s.mode) setMode(s.mode);
    if (s.max_tokens && s.max_tokens !== '0') $('#max-tokens').value = s.max_tokens;
  } catch(e) {}
  // Fallback handled by /api/settings defaults
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (!data.has_api_key) { openModal('#api-key-modal'); $('#api-key-input').focus(); }
  } catch(e) {}
  refreshLog();
  refreshUsage();
})();

function saveSettings() {
  const settings = {
    output_dir: $('#output-dir').value,
    scanner_ip: $('#scanner-ip').value.trim(),
    resolution: $('#resolution').value,
    color_mode: $('#color').value,
    scan_source: document.querySelector('input[name="source"]:checked').value,
    mode: currentMode,
    max_tokens: $('#max-tokens').value.trim() || '0',
  };
  fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(settings) }).catch(() => {});
}

function crazyMode() {
  if (!confirm('Remove all token limits? API costs will be uncapped.')) return;
  $('#max-tokens').value = '0';
  saveSettings();
  refreshUsage();
}

function setMode(mode) {
  currentMode = mode;
  $('#mode-auto').classList.toggle('active', mode === 'auto');
  $('#mode-assisted').classList.toggle('active', mode === 'assisted');
  $('#mode-auto').setAttribute('aria-selected', mode === 'auto');
  $('#mode-assisted').setAttribute('aria-selected', mode === 'assisted');
  $('#mode-desc').textContent = mode === 'auto'
    ? 'AI automatically classifies and saves the document.'
    : 'Scan and review AI suggestions before saving.';
  saveSettings();
}

// Auto-save settings when inputs change
['#output-dir','#scanner-ip','#resolution','#color'].forEach(s => {
  const el = $(s); if (el) el.addEventListener('change', saveSettings);
});
document.querySelectorAll('input[name="source"]').forEach(r => r.addEventListener('change', saveSettings));
$('#classify-folder').addEventListener('input', function() { this.classList.remove('input-error'); });

function closeApiModal() { closeModal('#api-key-modal'); }
async function saveApiKey() {
  const key = $('#api-key-input').value.trim();
  const err = $('#api-key-error');
  if (!key) { err.textContent = 'Please enter an API key.'; return; }
  err.textContent = 'Saving...';
  try {
    const res = await fetch('/api/save-key', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key}) });
    const data = await res.json();
    if (data.ok) { closeApiModal(); refreshLog(); } else { err.textContent = data.error; }
  } catch(e) { err.textContent = 'Error: ' + e.message; }
}
$('#api-key-input').addEventListener('keydown', e => { if (e.key === 'Enter') saveApiKey(); });
$('#add-tag-input').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addCustomTag(); } });

async function browseFolder() {
  try {
    const res = await fetch('/api/browse-folder', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({current: $('#output-dir').value}) });
    const data = await res.json();
    if (data.ok) { $('#output-dir').value = data.path; saveSettings(); }
  } catch(e) {}
}

function getScanParams() {
  return { source: document.querySelector('input[name="source"]:checked').value, resolution: $('#resolution').value, color: $('#color').value, output_dir: $('#output-dir').value, scanner_ip: $('#scanner-ip').value.trim() };
}
function setBusy(busy, statusText) {
  ['#btn-classify','#btn-scan','#btn-batch'].forEach(s => { const el = $(s); if (el) { el.disabled = busy; el.setAttribute('aria-busy', busy); el.classList.toggle('busy', busy); }});
  document.querySelectorAll('.busy-text').forEach(el => el.hidden = !busy);
  const prog = $('#scan-progress');
  if (busy) {
    const labels = {scanning: 'Scanning pages...', analyzing: 'Analyzing with AI...', saving: 'Saving documents...'};
    const txt = labels[statusText] || statusText || 'Working...';
    document.querySelectorAll('.busy-text').forEach(el => el.textContent = txt);
    $('#scan-progress-text').textContent = txt;
    prog.style.display = '';
    if (statusText !== 'scanning') $('#scan-progress-pages').innerHTML = '';
  } else {
    prog.style.display = 'none';
    $('#scan-progress-pages').innerHTML = '';
  }
}
function updateScanProgress(job) {
  const textEl = $('#scan-progress-text');
  const pagesEl = $('#scan-progress-pages');
  const labels = {scanning: 'Scanning pages...', analyzing: 'Analyzing with AI...', saving: 'Saving documents...'};
  textEl.textContent = labels[job.status] || 'Working...';
  if (job.status === 'scanning' && job.pages_scanned > 0) {
    textEl.textContent = 'Scanning page ' + (job.pages_scanned + 1) + '...';
    const current = pagesEl.children.length;
    for (let i = current + 1; i <= job.pages_scanned; i++) {
      const tag = document.createElement('span');
      tag.className = 'scan-progress-page';
      tag.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>Page ' + i;
      pagesEl.appendChild(tag);
    }
  } else if (job.status === 'analyzing') {
    const n = job.pages_scanned || 0;
    if (n > 0) textEl.textContent = 'Analyzing ' + n + ' page' + (n > 1 ? 's' : '') + ' with AI...';
  }
}

function pollJob() {
  return new Promise((resolve, reject) => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch('/api/job');
        const job = await res.json();
        if (job.status === 'scanning' || job.status === 'analyzing' || job.status === 'saving') {
          updateScanProgress(job);
          refreshLog();
          return; // keep polling
        }
        clearInterval(interval);
        refreshLog();
        refreshUsage();
        resolve(job);
      } catch(e) {
        clearInterval(interval);
        reject(e);
      }
    }, 600);
  });
}

async function discoverScanners() {
  const st = $('#scanner-status');
  st.textContent = 'Searching network...'; st.className = 'status disconnected';
  try {
    const res = await fetch('/api/discover', { method: 'POST' });
    const data = await res.json();
    if (data.ok && data.scanners.length > 0) {
      const dl = $('#scanner-list');
      dl.innerHTML = '';
      data.scanners.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.ip;
        opt.label = s.name + ' (' + s.ip + ')';
        dl.appendChild(opt);
      });
      st.textContent = 'Found ' + data.scanners.length + ' scanner(s) \u2014 select one and click Connect';
      st.className = 'status connected';
      if (data.scanners.length === 1) {
        $('#scanner-ip').value = data.scanners[0].ip;
      }
    } else if (data.ok) {
      st.textContent = 'No scanners found on the network.'; st.className = 'status error';
    } else {
      st.textContent = 'Error: ' + data.error; st.className = 'status error';
    }
  } catch(e) { st.textContent = 'Discovery failed: ' + e.message; st.className = 'status error'; }
  refreshLog();
}

async function connect() {
  const ip = $('#scanner-ip').value.trim();
  const st = $('#scanner-status');
  const info = $('#scanner-info');
  st.textContent = 'Connecting...'; st.className = 'status disconnected';
  info.classList.remove('visible');
  try {
    const res = await fetch('/api/connect', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip}) });
    const data = await res.json();
    if (data.ok) {
      st.textContent = 'Connected'; st.className = 'status connected';
      $('#scanner-info-name').textContent = data.name;
      const details = [data.ip];
      if (data.state) details.push(data.state);
      if (data.adf) details.push('ADF: ' + data.adf.replace('ScannerAdf', ''));
      if (data.sources) details.push('Sources: ' + data.sources.join(', '));
      $('#scanner-info-detail').textContent = details.join(' \u00b7 ');
      info.classList.add('visible');
      $('#btn-classify').disabled = false; $('#btn-scan').disabled = false; $('#btn-batch').disabled = false;
      saveSettings();
    } else {
      st.textContent = 'Error: ' + data.error; st.className = 'status error';
    }
  } catch(e) { st.textContent = 'Failed: ' + e.message; st.className = 'status error'; }
  refreshLog();
}

function disconnect() {
  fetch('/api/disconnect', { method: 'POST' }).catch(() => {});
  $('#scanner-info').classList.remove('visible');
  const st = $('#scanner-status');
  st.textContent = 'Not connected'; st.className = 'status disconnected';
  $('#btn-classify').disabled = true; $('#btn-scan').disabled = true; $('#btn-batch').disabled = true;
  _log('Scanner disconnected');
}

async function scanOnly() {
  setBusy(true, 'scanning'); $('#results-card').style.display = 'none';
  try {
    const res = await fetch('/api/scan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), classify: false}) });
    const start = await res.json();
    if (!start.ok) { alert('Error: ' + start.error); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { alert('Error: ' + (job.result && job.result.error || 'Unknown error')); }
    else if (job.status === 'duplicate') { alert('Duplicate detected: this document was previously saved as ' + (job.result.previous && job.result.previous.filename || 'unknown')); }
    else if (job.status === 'done' && job.result) { showResult({folder: 'unsorted', tags: [], filename: (job.result.output_path || '').split(/[/\\]/).pop(), summary: 'Saved without classification', path: job.result.output_path}); }
  } catch(e) { alert('Failed: ' + e.message); }
  setBusy(false); refreshLog();
}

function doScan() { return currentMode === 'auto' ? doScanAuto() : doScanAssisted(); }

async function doScanAuto() {
  setBusy(true, 'scanning'); $('#results-card').style.display = 'none';
  try {
    const res = await fetch('/api/scan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), classify: true}) });
    const start = await res.json();
    if (!start.ok) { alert('Error: ' + start.error); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { alert('Error: ' + (job.result && job.result.error || 'Unknown error')); }
    else if (job.status === 'duplicate') { alert('Duplicate detected: this document was previously saved as ' + (job.result.previous && job.result.previous.filename || 'unknown')); }
    else if (job.status === 'done' && job.result && job.result.classified) {
      const d = job.result;
      showResult({folder: d.category, tags: d.tags || [d.category], filename: d.filename, summary: d.summary, date: d.date, path: d.output_path, riskLevel: d.risk_level, risks: d.risks});
    }
  } catch(e) { alert('Failed: ' + e.message); }
  setBusy(false); refreshLog();
}

async function doScanAssisted() {
  setBusy(true, 'scanning'); $('#results-card').style.display = 'none';
  try {
    const res = await fetch('/api/scan-assisted', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(getScanParams()) });
    const start = await res.json();
    if (!start.ok) { alert('Error: ' + start.error); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { alert('Error: ' + (job.result && job.result.error || 'Unknown error')); }
    else if (job.status === 'duplicate') { alert('Duplicate detected: this document was previously saved as ' + (job.result.previous && job.result.previous.filename || 'unknown')); }
    else if (job.status === 'done' && job.result && job.result.ok) { showClassifyModal(job.result); }
  } catch(e) { alert('Failed: ' + e.message); }
  setBusy(false); refreshLog();
}

function renderRisk(el, level, risks) {
  if (!level || level === 'none' || !risks || risks.length === 0) {
    el.style.display = 'none'; el.className = 'risk-alert'; return;
  }
  const icons = {low: '\u26a0\ufe0f', medium: '\u26a0\ufe0f', high: '\ud83d\udea8'};
  const labels = {low: 'Low Risk', medium: 'Medium Risk', high: 'High Risk'};
  el.className = 'risk-alert risk-' + level;
  el.style.display = '';
  const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  el.innerHTML = '<h4>' + (icons[level]||'') + ' ' + esc(labels[level]||level) + '</h4><ul>' + risks.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul>';
}

function showResult({folder, tags, filename, summary, date, path, riskLevel, risks}) {
  $('#results-card').style.display = '';
  $('#r-folder').textContent = folder || '--';
  $('#r-tags').textContent = (tags && tags.length) ? tags.join(', ') : '--';
  $('#r-filename').textContent = filename || '--';
  $('#r-summary').textContent = summary || '--';
  $('#r-date').textContent = date || '--';
  $('#r-path').textContent = 'Saved to: ' + path;
  $('#r-path').style.display = '';
  renderRisk($('#r-risk'), riskLevel, risks);
}

function showClassifyModal(data) {
  $('#classify-img').src = 'data:image/jpeg;base64,' + data.preview;
  $('#classify-summary').innerHTML = '<strong>' + (data.summary || '') + '</strong><br>Date: ' + (data.date || 'Unknown');
  $('#classify-fn').value = data.filename || '';

  // All AI-suggested tags start selected
  const aiTags = data.tags || [];
  selectedTags = new Set(aiTags);

  // Pre-fill folder with AI's primary category
  $('#classify-folder').value = data.category || 'other';

  // Populate folder suggestions from all categories
  const dl = $('#folder-suggestions');
  dl.innerHTML = '';
  (data.all_categories || []).forEach(cat => {
    const opt = document.createElement('option');
    opt.value = cat;
    dl.appendChild(opt);
  });

  // Render tag buttons
  renderTagButtons();

  pendingRisk = {level: data.risk_level, risks: data.risks};
  renderRisk($('#classify-risk'), data.risk_level, data.risks);
  updateTagCount();
  $('#add-tag-input').value = '';
  openModal('#classify-modal');
}

function renderTagButtons() {
  const el = $('#tag-buttons');
  el.innerHTML = '';
  // Show all tags (selected ones first, then deselected)
  const sorted = [...selectedTags];
  sorted.forEach(tag => {
    el.appendChild(makeTagBtn(tag, true));
  });
}

function makeTagBtn(tag, selected) {
  const btn = document.createElement('button');
  btn.className = 'tag-btn' + (selected ? ' selected' : '');
  btn.textContent = tag;
  btn.setAttribute('aria-pressed', selected);
  btn.onclick = () => toggleTag(tag);
  return btn;
}

function toggleTag(tag) {
  if (selectedTags.has(tag)) {
    selectedTags.delete(tag);
  } else {
    selectedTags.add(tag);
  }
  document.querySelectorAll('#tag-buttons .tag-btn').forEach(btn => {
    const on = selectedTags.has(btn.textContent);
    btn.classList.toggle('selected', on);
    btn.setAttribute('aria-pressed', on);
  });
  updateTagCount();
}

function addCustomTag() {
  const input = $('#add-tag-input');
  const tag = input.value.trim().toLowerCase().replace(/\s+/g, '-');
  if (!tag) return;
  if (selectedTags.has(tag)) { input.value = ''; return; }
  selectedTags.add(tag);
  $('#tag-buttons').appendChild(makeTagBtn(tag, true));
  input.value = '';
  input.focus();
  updateTagCount();
}

function updateTagCount() {
  const n = selectedTags.size;
  const label = n === 0 ? 'Save' : n === 1 ? 'Save (1 tag)' : 'Save (' + n + ' tags)';
  $('#btn-save-classify').textContent = label;
}

function cancelClassify() { closeModal('#classify-modal'); }

async function saveClassified() {
  const folderEl = $('#classify-folder');
  const folder = folderEl.value.trim();
  if (!folder) { folderEl.classList.add('input-error'); folderEl.focus(); return; }
  folderEl.classList.remove('input-error');
  const btn = $('#btn-save-classify');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-inline"></span>Saving...';
  try {
    const res = await fetch('/api/save-classified', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ folder, tags: [...selectedTags], filename: $('#classify-fn').value, output_dir: $('#output-dir').value }) });
    const data = await res.json();
    if (data.ok) {
      closeModal('#classify-modal');
      showResult({folder: data.folder, tags: data.tags, filename: $('#classify-fn').value, path: data.output_path, riskLevel: pendingRisk.level, risks: pendingRisk.risks});
    } else alert('Error: ' + data.error);
  } catch(e) { alert('Failed: ' + e.message); }
  btn.disabled = false;
  btn.textContent = 'Save';
  refreshLog();
}

async function refreshLog() {
  try { const res = await fetch('/api/logs'); const logs = await res.json(); const el = $('#log'); el.textContent = logs.join('\n'); el.scrollTop = el.scrollHeight; } catch(e) {}
}
setInterval(refreshLog, 2000);

function drawUsageChart(history) {
  const canvas = $('#usage-chart');
  const empty = $('#usage-chart-empty');
  if (!history || history.length === 0) {
    canvas.style.display = 'none'; empty.style.display = '';
    return;
  }
  canvas.style.display = ''; empty.style.display = 'none';
  const ctx = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  const pad = {t: 10, r: 12, b: 22, l: 44};
  const cw = W - pad.l - pad.r, ch = H - pad.t - pad.b;
  const mono = getComputedStyle(document.body).getPropertyValue('--mono');

  ctx.clearRect(0, 0, W, H);

  const maxVal = Math.max(...history.map(h => h.cumulative)) * 1.2 || 1;
  const pts = history.map((h, i) => ({
    x: pad.l + (history.length === 1 ? cw / 2 : (i / (history.length - 1)) * cw),
    y: pad.t + ch - (h.cumulative / maxVal) * ch,
    ...h
  }));

  // Horizontal grid lines
  ctx.strokeStyle = 'rgba(148,163,184,.1)';
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75].forEach(f => {
    const y = pad.t + ch - f * ch;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cw, y); ctx.stroke();
  });

  // Smooth curve helper (cardinal spline)
  function smoothLine(points) {
    if (points.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    if (points.length === 2) { ctx.lineTo(points[1].x, points[1].y); return; }
    for (let i = 0; i < points.length - 1; i++) {
      const cp1x = points[i].x + (points[Math.min(i+1, points.length-1)].x - points[Math.max(i-1,0)].x) / 6;
      const cp1y = points[i].y + (points[Math.min(i+1, points.length-1)].y - points[Math.max(i-1,0)].y) / 6;
      const cp2x = points[i+1].x - (points[Math.min(i+2, points.length-1)].x - points[i].x) / 6;
      const cp2y = points[i+1].y - (points[Math.min(i+2, points.length-1)].y - points[i].y) / 6;
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, points[i+1].x, points[i+1].y);
    }
  }

  // Area fill gradient (indigo)
  const grad = ctx.createLinearGradient(0, pad.t, 0, pad.t + ch);
  grad.addColorStop(0, 'rgba(99,102,241,.3)');
  grad.addColorStop(0.7, 'rgba(99,102,241,.05)');
  grad.addColorStop(1, 'rgba(99,102,241,0)');
  smoothLine(pts);
  ctx.lineTo(pts[pts.length-1].x, pad.t + ch);
  ctx.lineTo(pts[0].x, pad.t + ch);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();

  // Main line (indigo)
  smoothLine(pts);
  ctx.strokeStyle = '#818CF8';
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  // Dots with glow
  pts.forEach(p => {
    ctx.beginPath();
    ctx.arc(p.x, p.y, 5, 0, Math.PI * 2);
    ctx.fillStyle = 'rgba(129,140,248,.2)';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
    ctx.fillStyle = '#818CF8';
    ctx.fill();
    ctx.beginPath();
    ctx.arc(p.x, p.y, 1.5, 0, Math.PI * 2);
    ctx.fillStyle = '#C7D2FE';
    ctx.fill();
  });

  // Stacked bars (input = indigo, output = green)
  const barW = Math.max(3, Math.min(16, cw / history.length * 0.4));
  history.forEach((h, i) => {
    const x = pts[i].x - barW / 2;
    const total = h.input + h.output;
    if (total === 0) return;
    const inH = Math.max(2, (h.input / maxVal) * ch * 0.4);
    const outH = Math.max(2, (h.output / maxVal) * ch * 0.4);
    // Input bar
    ctx.fillStyle = 'rgba(129,140,248,.25)';
    ctx.beginPath();
    ctx.roundRect(x, pad.t + ch - inH - outH, barW, inH, 2);
    ctx.fill();
    // Output bar
    ctx.fillStyle = 'rgba(52,211,153,.25)';
    ctx.beginPath();
    ctx.roundRect(x, pad.t + ch - outH, barW, outH, 2);
    ctx.fill();
  });

  // Y-axis labels
  ctx.fillStyle = '#475569';
  ctx.font = '10px ' + mono;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  [0, 0.5, 1].forEach(f => {
    const y = pad.t + ch - f * ch;
    const val = Math.round(f * maxVal);
    ctx.fillText(val >= 1000 ? (val/1000).toFixed(val >= 10000 ? 0 : 1) + 'k' : val, pad.l - 8, y);
  });

  // X-axis time labels
  ctx.fillStyle = '#475569';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const labelStep = Math.max(1, Math.floor(pts.length / 6));
  pts.forEach((p, i) => {
    if (i % labelStep === 0 || i === pts.length - 1) {
      ctx.fillText(p.time, p.x, pad.t + ch + 6);
    }
  });
}

function fmtNum(n) { return n >= 1000000 ? (n/1000000).toFixed(2) + 'M' : n >= 1000 ? (n/1000).toFixed(1) + 'k' : n.toLocaleString(); }

async function refreshUsage() {
  try {
    const res = await fetch('/api/usage');
    const u = await res.json();
    const maxTok = parseInt($('#max-tokens').value) || 0;
    const dash = $('#usage-dash');

    // Hero values
    $('#usage-tokens').textContent = fmtNum(u.total_tokens);
    $('#usage-tokens-detail').textContent = fmtNum(u.input_tokens) + ' in / ' + fmtNum(u.output_tokens) + ' out';
    $('#usage-cost').textContent = '$' + u.estimated_cost.toFixed(4);
    $('#usage-calls').textContent = u.api_calls;
    $('#usage-calls-detail').textContent = u.api_calls === 1 ? '1 call today' : u.api_calls + ' calls today';

    // Budget hero + bar
    const budgetSection = $('#usage-budget-section');
    const budgetVal = $('#usage-budget-val');
    const budgetDetail = $('#usage-budget-detail');
    if (maxTok > 0) {
      const pct = Math.min(100, (u.total_tokens / maxTok) * 100);
      budgetVal.textContent = Math.round(pct) + '%';
      budgetDetail.textContent = fmtNum(maxTok - u.total_tokens) + ' remaining';
      budgetSection.style.display = '';
      const fill = $('#usage-budget-fill');
      fill.style.width = pct + '%';
      fill.className = 'usage-budget-fill' + (pct >= 100 ? ' over' : pct >= 75 ? ' warn' : '');
      dash.classList.toggle('over-budget', pct >= 100);
      $('#usage-budget-left').textContent = fmtNum(u.total_tokens) + ' of ' + fmtNum(maxTok) + ' tokens';
      const right = $('#usage-budget-right');
      if (pct >= 100) {
        right.innerHTML = '<span class="over">Budget exceeded</span>';
        budgetDetail.textContent = 'exceeded!';
      } else {
        right.textContent = Math.round(pct) + '% used';
      }
    } else {
      budgetVal.textContent = 'Unlimited';
      budgetDetail.textContent = 'no cap set';
      budgetSection.style.display = 'none';
      dash.classList.remove('over-budget');
    }

    // Chart
    drawUsageChart(u.history || []);
  } catch(e) {}
}
setInterval(refreshUsage, 5000);

// ── Batch scan ──────────────────────────────────────────────────────
let batchData = [];   // Array of doc objects from API
let batchTags = [];   // Array of Sets, per document
let batchPages = [];  // Array of arrays of 1-indexed page numbers
let allPreviews = []; // Base64 thumbs for every scanned page

async function doBatchScan() {
  setBusy(true, 'scanning');
  $('#results-card').style.display = 'none';
  $('#batch-results-card').style.display = 'none';
  try {
    // Batch always shows review modal for page rearrangement
    const res = await fetch('/api/scan-batch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), auto: false}) });
    const start = await res.json();
    if (!start.ok) { alert('Error: ' + start.error); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { alert('Error: ' + (job.result && job.result.error || 'Unknown error')); }
    else if (job.status === 'done' && job.result && job.result.batch) {
      showBatchModal(job.result);
    }
  } catch(e) { alert('Failed: ' + e.message); }
  setBusy(false); refreshLog();
}

function showBatchResults(docs) {
  const card = $('#batch-results-card');
  card.style.display = '';
  $('#batch-results-title').textContent = 'Batch Complete \u2014 ' + docs.length + ' Document' + (docs.length !== 1 ? 's' : '');
  const list = $('#batch-results-list');
  list.innerHTML = '';
  const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  docs.forEach(doc => {
    const li = document.createElement('li');
    const name = doc.filename || (doc.output_path || '').split(/[/\\]/).pop() || 'document';
    const detail = [doc.folder || doc.category, doc.summary].filter(Boolean).join(' \u2014 ');
    li.innerHTML = '<span class="br-name">' + esc(name) + '</span>' + (detail ? '<br><span class="br-detail">' + esc(detail) + '</span>' : '');
    list.appendChild(li);
  });
}

function showBatchModal(result) {
  const docs = result.documents;
  allPreviews = result.all_previews || [];
  batchData = docs;
  batchTags = docs.map(d => new Set(d.tags || []));
  batchPages = docs.map(d => [...(d.pages || [])]);
  renderBatchDocs();
  openModal('#batch-modal');
}

function renderBatchDocs() {
  // Preserve user-edited filenames and folders before re-rendering
  const savedFn = {}, savedFolder = {};
  batchData.forEach((_, i) => {
    const fnEl = $('#batch-fn-' + i), folderEl = $('#batch-folder-' + i);
    if (fnEl) savedFn[i] = fnEl.value;
    if (folderEl) savedFolder[i] = folderEl.value;
  });

  const docsWithPages = batchPages.filter(p => p.length > 0).length;
  $('#batch-count').textContent = docsWithPages;
  const container = $('#batch-docs');
  container.innerHTML = '';
  const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  const numDocs = batchData.length;

  batchData.forEach((doc, i) => {
    const card = document.createElement('div');
    card.className = 'batch-doc';
    card.dataset.docIdx = i;

    // Page thumbnails with drag-and-drop + move select
    let pagesHtml = '';
    const pages = batchPages[i] || [];
    if (pages.length === 0) {
      pagesHtml = '<div class="batch-page-grid-empty">No pages \u2014 drag pages here or remove this document</div>';
    }
    pages.forEach(pNum => {
      const preview = allPreviews[pNum - 1] || '';
      let moveOpts = '';
      for (let d = 0; d < numDocs; d++) {
        moveOpts += '<option value="' + d + '"' + (d === i ? ' selected' : '') + '>Doc ' + (d + 1) + '</option>';
      }
      moveOpts += '<option value="new">+ New doc</option>';
      pagesHtml += '<div class="batch-page" draggable="true" data-page="' + pNum + '" data-doc="' + i + '">' +
        '<img src="data:image/jpeg;base64,' + preview + '" alt="Page ' + pNum + '" onclick="openLightbox(' + pNum + ')" title="Click to preview" style="cursor:zoom-in" draggable="false">' +
        '<span>Page ' + pNum + '</span>' +
        '<select class="batch-page-move" data-page="' + pNum + '" data-doc="' + i + '" aria-label="Move page ' + pNum + '">' + moveOpts + '</select>' +
        '</div>';
    });

    let tagsHtml = [...(batchTags[i] || [])].map(t =>
      '<button class="batch-tag selected" data-doc="' + i + '" data-tag="' + esc(t) + '" aria-pressed="true">' + esc(t) + '</button>'
    ).join('');

    let riskHtml = '';
    if (doc.risk_level && doc.risk_level !== 'none' && doc.risks && doc.risks.length) {
      const icons = {low: '\u26a0\ufe0f', medium: '\u26a0\ufe0f', high: '\ud83d\udea8'};
      const labels = {low: 'Low Risk', medium: 'Medium Risk', high: 'High Risk'};
      riskHtml = '<div class="risk-alert risk-' + esc(doc.risk_level) + '" style="margin-top:8px"><h4>' + (icons[doc.risk_level]||'') + ' ' + esc(labels[doc.risk_level]||doc.risk_level) + '</h4><ul>' + doc.risks.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></div>';
    }

    const fn = i in savedFn ? savedFn[i] : (doc.filename || '');
    const folder = i in savedFolder ? savedFolder[i] : (doc.category || 'other');

    card.innerHTML =
      '<div class="batch-doc-head">' +
        '<div class="batch-doc-title">' +
          '<span class="batch-doc-label">Document ' + (i + 1) + (doc.summary ? ' \u2014 ' + esc(doc.summary) : '') + '</span>' +
          (pages.length === 0 ? '<button class="btn-remove-doc" onclick="removeBatchDoc(' + i + ')">Remove</button>' : '') +
        '</div>' +
      '</div>' +
      '<div class="batch-page-grid" data-doc="' + i + '">' + pagesHtml + '</div>' +
      '<div class="batch-fields">' +
        '<label>Filename</label><input type="text" id="batch-fn-' + i + '" value="' + esc(fn) + '">' +
        '<label>Folder</label><input type="text" id="batch-folder-' + i + '" value="' + esc(folder) + '" list="folder-suggestions">' +
        '<label>Tags</label><div class="batch-tag-grid" id="batch-tags-' + i + '">' + tagsHtml + '</div>' +
        '<label></label><div class="batch-add-tag-row"><input type="text" id="batch-add-tag-' + i + '" placeholder="Add a tag..." aria-label="Add tag to document ' + (i+1) + '"><button class="btn btn-secondary" onclick="addBatchTag(' + i + ')">Add</button></div>' +
      '</div>' +
      riskHtml;

    container.appendChild(card);
  });

  // "Add Document" button
  const addBtn = document.createElement('button');
  addBtn.className = 'btn-add-doc';
  addBtn.textContent = '+ Add Document Group';
  addBtn.onclick = addBatchDocument;
  container.appendChild(addBtn);

  updateBatchSaveBtn();
  initBatchDragDrop();
}

function initBatchDragDrop() {
  // Draggable pages
  document.querySelectorAll('.batch-page[draggable]').forEach(el => {
    el.addEventListener('dragstart', e => {
      e.dataTransfer.setData('text/plain', JSON.stringify({page: parseInt(el.dataset.page), fromDoc: parseInt(el.dataset.doc)}));
      el.classList.add('dragging');
    });
    el.addEventListener('dragend', () => el.classList.remove('dragging'));
  });
  // Drop zones
  document.querySelectorAll('.batch-page-grid').forEach(grid => {
    grid.addEventListener('dragover', e => { e.preventDefault(); grid.classList.add('drop-target'); });
    grid.addEventListener('dragleave', e => { if (!grid.contains(e.relatedTarget)) grid.classList.remove('drop-target'); });
    grid.addEventListener('drop', e => {
      e.preventDefault();
      grid.classList.remove('drop-target');
      try {
        const data = JSON.parse(e.dataTransfer.getData('text/plain'));
        const toDoc = parseInt(grid.dataset.doc);
        if (data.fromDoc !== toDoc) movePage(data.page, data.fromDoc, toDoc);
      } catch(err) {}
    });
  });
}

function movePage(pageNum, fromDoc, toDoc) {
  const idx = batchPages[fromDoc].indexOf(pageNum);
  if (idx === -1) return;
  batchPages[fromDoc].splice(idx, 1);
  batchPages[toDoc].push(pageNum);
  batchPages[toDoc].sort((a, b) => a - b);
  renderBatchDocs();
}

function addBatchDocument() {
  batchData.push({category: 'other', filename: '', summary: '', tags: [], risks: [], risk_level: 'none'});
  batchTags.push(new Set());
  batchPages.push([]);
  renderBatchDocs();
}

function removeBatchDoc(idx) {
  if (batchPages[idx] && batchPages[idx].length > 0) return; // Can't remove doc with pages
  batchData.splice(idx, 1);
  batchTags.splice(idx, 1);
  batchPages.splice(idx, 1);
  renderBatchDocs();
}

function addBatchTag(docIdx) {
  const input = $('#batch-add-tag-' + docIdx);
  if (!input) return;
  const tag = input.value.trim().toLowerCase().replace(/\s+/g, '-');
  if (!tag) return;
  if (batchTags[docIdx].has(tag)) { input.value = ''; return; }
  batchTags[docIdx].add(tag);
  // Append new tag button without full re-render
  const grid = $('#batch-tags-' + docIdx);
  if (grid) {
    const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
    const btn = document.createElement('button');
    btn.className = 'batch-tag selected';
    btn.dataset.doc = docIdx;
    btn.dataset.tag = tag;
    btn.setAttribute('aria-pressed', 'true');
    btn.textContent = tag;
    grid.appendChild(btn);
  }
  input.value = '';
  input.focus();
}

// Enter key on batch add-tag inputs
document.addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  const input = e.target.closest('[id^="batch-add-tag-"]');
  if (!input) return;
  e.preventDefault();
  const docIdx = parseInt(input.id.replace('batch-add-tag-', ''));
  addBatchTag(docIdx);
});

// Event delegation: batch tag toggling
document.addEventListener('click', e => {
  const btn = e.target.closest('.batch-tag');
  if (!btn) return;
  const docIdx = parseInt(btn.dataset.doc);
  const tag = btn.dataset.tag;
  if (batchTags[docIdx].has(tag)) {
    batchTags[docIdx].delete(tag);
    btn.classList.remove('selected');
    btn.setAttribute('aria-pressed', 'false');
  } else {
    batchTags[docIdx].add(tag);
    btn.classList.add('selected');
    btn.setAttribute('aria-pressed', 'true');
  }
});

// Event delegation: page move select
document.addEventListener('change', e => {
  if (!e.target.classList.contains('batch-page-move')) return;
  const pageNum = parseInt(e.target.dataset.page);
  const fromDoc = parseInt(e.target.dataset.doc);
  const toDoc = e.target.value;
  if (toDoc === 'new') {
    addBatchDocument();
    movePage(pageNum, fromDoc, batchData.length - 1);
  } else {
    const to = parseInt(toDoc);
    if (to !== fromDoc) movePage(pageNum, fromDoc, to);
  }
});

function updateBatchSaveBtn() {
  const docsWithPages = batchPages.filter(p => p.length > 0).length;
  $('#btn-save-batch').textContent = 'Save All (' + docsWithPages + ' document' + (docsWithPages !== 1 ? 's' : '') + ')';
}

function cancelBatch() { closeModal('#batch-modal'); }

async function saveBatch() {
  const btn = $('#btn-save-batch');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner-inline"></span>Saving...';
  const documents = [];
  batchData.forEach((doc, i) => {
    if (!batchPages[i] || batchPages[i].length === 0) return;
    documents.push({
      pages: batchPages[i],
      folder: ($('#batch-folder-' + i) || {}).value || doc.category || 'other',
      tags: [...(batchTags[i] || [])],
      filename: ($('#batch-fn-' + i) || {}).value || doc.filename,
      summary: doc.summary || '',
      date: doc.date || null,
    });
  });
  try {
    const res = await fetch('/api/save-batch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({documents, output_dir: $('#output-dir').value}) });
    const data = await res.json();
    if (data.ok) {
      closeModal('#batch-modal');
      showBatchResults(data.documents);
    } else alert('Error: ' + data.error);
  } catch(e) { alert('Failed: ' + e.message); }
  btn.disabled = false;
  btn.textContent = 'Save All';
  refreshLog();
}

function clearResults() {
  $('#results-card').style.display = 'none';
  $('#batch-results-card').style.display = 'none';
}

// ── Lightbox (fullscreen page preview) ──────────────────────────────
let lightboxPages = []; // Ordered list of page numbers available in lightbox
let lightboxIdx = 0;    // Current index within lightboxPages

function openLightbox(pageNum) {
  // Build ordered page list from all batch pages
  lightboxPages = [];
  batchPages.forEach(pages => pages.forEach(p => { if (!lightboxPages.includes(p)) lightboxPages.push(p); }));
  lightboxPages.sort((a, b) => a - b);
  lightboxIdx = lightboxPages.indexOf(pageNum);
  if (lightboxIdx === -1) lightboxIdx = 0;
  showLightboxPage();
  $('#lightbox').classList.add('active'); mainContent.setAttribute('inert', '');
}

function showLightboxPage() {
  const pNum = lightboxPages[lightboxIdx];
  $('#lightbox-img').src = '/api/page-image/' + pNum;
  $('#lightbox-img').alt = 'Page ' + pNum;
  $('#lightbox-label').textContent = 'Page ' + pNum + ' of ' + lightboxPages.length;
}

function closeLightbox() { $('#lightbox').classList.remove('active'); if (!document.querySelector('.modal-overlay.active')) mainContent.removeAttribute('inert'); }

function lightboxNav(dir) {
  lightboxIdx = (lightboxIdx + dir + lightboxPages.length) % lightboxPages.length;
  showLightboxPage();
}

// Click lightbox background to close (but not on image or buttons)
$('#lightbox').addEventListener('click', e => { if (e.target === $('#lightbox')) closeLightbox(); });

// Keyboard: Escape closes modals / lightbox, arrows navigate lightbox
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if ($('#lightbox').classList.contains('active')) { closeLightbox(); }
    else if ($('#batch-modal').classList.contains('active')) { cancelBatch(); }
    else if ($('#classify-modal').classList.contains('active')) { cancelClassify(); }
    else if ($('#api-key-modal').classList.contains('active')) { closeApiModal(); }
  }
  if ($('#lightbox').classList.contains('active')) {
    if (e.key === 'ArrowLeft') lightboxNav(-1);
    if (e.key === 'ArrowRight') lightboxNav(1);
  }
});

// Focus trapping inside active modals
document.addEventListener('keydown', e => {
  if (e.key !== 'Tab') return;
  const overlay = document.querySelector('.modal-overlay.active');
  if (!overlay) return;
  const focusable = overlay.querySelectorAll('button:not([disabled]), input:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])');
  if (focusable.length === 0) return;
  const first = focusable[0], last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});
</script>
</body>
</html>"""


def main() -> None:
    port = 8470

    try:
        load_config()
        _log("Config loaded")
    except RuntimeError:
        _log("Warning: No ANTHROPIC_API_KEY found. Set it in .env for AI classification.")

    def open_browser():
        import time
        time.sleep(1)
        webbrowser.open(f"http://localhost:{port}")

    threading.Thread(target=open_browser, daemon=True).start()

    print(f"Auto-Scan running at http://localhost:{port}")
    print("Press Ctrl+C to quit")

    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
