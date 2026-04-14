"""Web-based GUI for auto-scan using Flask."""

from __future__ import annotations

import base64
import io
import os
import platform
import subprocess
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request
from PIL import Image

from auto_scan import AutoScanError
from auto_scan.analyzer import ALL_CATEGORIES, DocumentInfo, analyze_batch, analyze_document
from auto_scan.config import Config, load_config
from auto_scan.dedup import image_hash
from auto_scan.history import find_by_hash, record_scan, search_history
from auto_scan.organizer import save_document, save_unclassified
from auto_scan.scanner.discovery import ScannerInfo, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings

app = Flask(__name__)

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
    state["logs"].append(f"[{ts}] {msg}")
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]


def _get_config(**overrides) -> Config:
    return load_config(**overrides)


def _make_thumbnail(image_data: bytes, max_dim: int = 800) -> bytes:
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
            return result.stdout.strip().rstrip("/")
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


@app.route("/api/connect", methods=["POST"])
def api_connect():
    data = request.json or {}
    ip = data.get("ip", "").strip()
    try:
        if ip:
            _log(f"Connecting to {ip}...")
            info = scanner_info_from_ip(ip)
        else:
            _log("Searching for Canon scanner...")
            info = discover_scanner(timeout=8.0)

        client = ESCLClient(info.base_url)
        status = client.get_status()
        caps = client.get_capabilities()
        client.close()

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

    info = state.get("scanner_info")
    if not info:
        if config.scanner_ip:
            info = scanner_info_from_ip(config.scanner_ip)
        else:
            info = discover_scanner(timeout=8.0)
        state["scanner_info"] = info

    with ESCLClient(info.base_url) as client:
        status = client.get_status()
        if status.state != "Idle":
            raise AutoScanError(f"Scanner is {status.state}. Wait and try again.")
        _log("Scanning...")
        settings = ScanSettings(
            source=source, color_mode=color,
            resolution=resolution, document_format=config.scan_format,
        )
        images = client.scan(settings)

    _log(f"Scanned {len(images)} page(s)")
    return images, config


def _check_duplicate(images: list[bytes], config: Config) -> dict | None:
    """Check if these images were scanned before. Returns previous record or None."""
    h = image_hash(images)
    state["_last_hash"] = h
    prev = find_by_hash(config.output_dir, h)
    return prev


def _record(config, doc_info, folder, tags, pages, output_path):
    """Record a scan in the history database."""
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
        state["job"] = {"status": "scanning"}
        images, config = _do_scan(data)
        state["job"]["status"] = "scanning"

        # Duplicate check
        prev = _check_duplicate(images, config)
        if prev:
            _log(f"Duplicate detected: previously saved as {prev['filename']}")
            state["job"] = {
                "status": "duplicate",
                "result": {"duplicate": True, "previous": prev},
            }
            return

        if mode == "auto":
            classify = data.get("classify", True)
            if classify:
                state["job"]["status"] = "analyzing"
                _log("Analyzing with Claude Vision...")
                doc_info = analyze_document(images, config)
                _log(f"Classified as: {doc_info.category}")

                state["job"]["status"] = "saving"
                output_path = save_document(images, doc_info, config, tags=doc_info.tags)
                _log(f"Saved: {output_path}")
                _record(config, doc_info, doc_info.category, doc_info.tags, len(images), output_path)

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
                state["job"]["status"] = "saving"
                output_path = save_unclassified(images, config)
                _log(f"Saved: {output_path}")
                _record(config, None, "unsorted", [], len(images), output_path)

                state["job"] = {
                    "status": "done",
                    "result": {
                        "ok": True, "classified": False, "pages": len(images),
                        "output_path": str(output_path),
                    },
                }

        elif mode == "assisted":
            state["job"]["status"] = "analyzing"
            _log("Analyzing with Claude Vision...")
            doc_info = analyze_document(images, config)
            _log(f"AI suggests: {doc_info.category}")

            state["pending_images"] = images
            state["pending_doc_info"] = doc_info
            state["pending_config"] = config

            thumb = _make_thumbnail(images[0])
            preview_b64 = base64.b64encode(thumb).decode("ascii")

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
            state["job"]["status"] = "analyzing"
            _log(f"Batch analyzing {len(images)} pages...")
            batch_results = analyze_batch(images, config)
            _log(f"Detected {len(batch_results)} document(s)")

            state["job"]["status"] = "saving"
            saved = []
            for pages, doc_info in batch_results:
                doc_images = [images[p] for p in pages if p < len(images)]
                h = image_hash(doc_images)
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

            state["job"] = {
                "status": "done",
                "result": {"ok": True, "batch": True, "documents": saved},
            }

        elif mode == "batch-assisted":
            state["job"]["status"] = "analyzing"
            _log(f"Batch analyzing {len(images)} pages...")
            batch_results = analyze_batch(images, config)
            _log(f"Detected {len(batch_results)} document(s)")

            state["pending_images"] = images
            state["pending_batch_docs"] = batch_results
            state["pending_config"] = config

            docs = []
            for pages, doc_info in batch_results:
                first_page = pages[0] if pages else 0
                preview_b64 = ""
                if first_page < len(images):
                    thumb = _make_thumbnail(images[first_page])
                    preview_b64 = base64.b64encode(thumb).decode("ascii")
                docs.append({
                    "pages": [p + 1 for p in pages],
                    "preview": preview_b64,
                    "category": doc_info.category,
                    "suggested_categories": doc_info.suggested_categories,
                    "all_categories": ALL_CATEGORIES,
                    "filename": doc_info.filename, "summary": doc_info.summary,
                    "date": doc_info.date, "key_fields": doc_info.key_fields,
                    "tags": doc_info.tags,
                    "risk_level": doc_info.risk_level, "risks": doc_info.risks,
                })

            state["job"] = {
                "status": "done",
                "result": {"ok": True, "batch": True, "documents": docs},
            }

    except Exception as e:
        _log(f"Error: {e}")
        state["job"] = {"status": "error", "result": {"ok": False, "error": str(e)}}


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Automatic mode: start scan job in background."""
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
    job = state.get("job")
    if not job:
        return jsonify({"status": "idle"})
    return jsonify(job)


@app.route("/api/save-classified", methods=["POST"])
def api_save_classified():
    """Save pending scanned images with folder + tags."""
    data = request.json or {}
    folder = data.get("folder", "").strip() or "other"
    tags = data.get("tags", [])
    filename = data.get("filename", "")

    images = state.get("pending_images")
    doc_info = state.get("pending_doc_info")
    config = state.get("pending_config")

    if not images or not doc_info:
        return jsonify({"ok": False, "error": "No pending scan to save."}), 400

    try:
        if filename:
            doc_info.filename = filename

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
    """Save all documents from a batch scan with user edits."""
    data = request.json or {}
    documents = data.get("documents", [])

    images = state.get("pending_images")
    batch_docs = state.get("pending_batch_docs")
    config = state.get("pending_config")

    if not images or not batch_docs:
        return jsonify({"ok": False, "error": "No pending batch to save."}), 400

    try:
        results = []
        for i, edit in enumerate(documents):
            if i >= len(batch_docs):
                break
            pages, doc_info = batch_docs[i]

            if edit.get("filename"):
                doc_info.filename = edit["filename"]
            folder = edit.get("folder", "").strip() or doc_info.category
            tags = edit.get("tags", doc_info.tags)

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


@app.route("/api/logs")
def api_logs():
    return jsonify(state["logs"])


# ── HTML Template ────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Auto-Scan</title>
<style>
  :root { --bg: #f8f9fa; --card: #fff; --border: #dee2e6; --primary: #0858cf; --primary-hover: #064bb3; --primary-light: #dbe8fc; --primary-text: #063b87; --gray: #495057; --gray-light: #5f6b75; --green: #146c43; --green-bg: #d1e7dd; --red: #b02a37; --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; --mono: "SF Mono", Menlo, Monaco, monospace; --focus-ring: 0 0 0 3px rgba(8,88,207,.4); }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: var(--font); background: var(--bg); color: #212529; line-height: 1.5; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0,0,0,0); border: 0; }
  .container { max-width: 740px; margin: 0 auto; padding: 24px 16px; }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 15px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--gray); margin-bottom: 12px; }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 4px; color: var(--gray); }
  input[type="text"], select { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; font-family: var(--font); }
  input:focus, select:focus { outline: 2px solid var(--primary); outline-offset: 1px; border-color: var(--primary); box-shadow: var(--focus-ring); }
  .row { display: flex; gap: 12px; margin-bottom: 10px; }
  .row > * { flex: 1; }
  .radio-group { display: flex; gap: 16px; padding: 6px 0; }
  .radio-group label { display: flex; align-items: center; gap: 6px; font-weight: 400; cursor: pointer; }
  .radio-group input[type="radio"]:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .btn-row { display: flex; gap: 10px; }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 10px 20px; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; font-family: var(--font); cursor: pointer; transition: background .15s; width: 100%; }
  .btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; box-shadow: var(--focus-ring); }
  .btn:disabled { opacity: .55; cursor: not-allowed; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  .btn-secondary { background: #e9ecef; color: var(--gray); }
  .btn-secondary:hover:not(:disabled) { background: #dee2e6; }
  .btn-connect { padding: 8px 16px; width: auto; font-size: 14px; }
  .connect-row { display: flex; gap: 8px; align-items: flex-end; }
  .connect-row > :first-child { flex: 1; }
  .status { font-size: 13px; padding: 6px 0; }
  .status.connected { color: var(--green); font-weight: 600; }
  .status.disconnected { color: var(--gray-light); }
  .status.error { color: var(--red); font-weight: 600; }
  .results-grid { display: grid; grid-template-columns: 100px 1fr; gap: 4px 12px; font-size: 14px; }
  .results-grid dt { font-weight: 600; color: var(--gray); }
  .results-grid dd { color: #212529; word-break: break-word; }
  .log { background: #1a1a2e; color: #e0e0e0; border-radius: 8px; padding: 12px; font-family: var(--mono); font-size: 12px; height: 160px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
  .spinner { display: none; width: 18px; height: 18px; border: 2px solid #fff4; border-top-color: #fff; border-radius: 50%; animation: spin .6s linear infinite; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .busy .spinner { display: inline-block; }
  .output-path { margin-top: 10px; padding: 8px 12px; background: var(--green-bg); color: #0a3622; border-radius: 6px; font-size: 13px; word-break: break-all; }
  .mode-toggle { display: flex; background: #e9ecef; border-radius: 8px; padding: 3px; margin-bottom: 12px; }
  .mode-toggle button { flex: 1; padding: 8px 16px; border: none; border-radius: 6px; font-size: 14px; font-weight: 600; font-family: var(--font); cursor: pointer; background: transparent; color: var(--gray-light); transition: all .15s; }
  .mode-toggle button:focus-visible { outline: 2px solid var(--primary); outline-offset: -2px; }
  .mode-toggle button.active { background: #fff; color: #212529; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.active { display: flex; }
  .modal { background: #fff; border-radius: 14px; padding: 28px; max-width: 90vw; box-shadow: 0 12px 40px rgba(0,0,0,.25); }
  .modal-sm { width: 480px; }
  .modal h2 { font-size: 18px; font-weight: 700; margin-bottom: 8px; color: #212529; text-transform: none; letter-spacing: 0; }
  .modal p { font-size: 14px; color: var(--gray); margin-bottom: 16px; line-height: 1.6; }
  .modal a { color: var(--primary); text-decoration: underline; }
  .modal a:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .modal input[type="password"] { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; font-family: var(--mono); margin-bottom: 6px; }
  .modal input[type="password"]:focus { outline: 2px solid var(--primary); outline-offset: 1px; border-color: var(--primary); box-shadow: var(--focus-ring); }
  .modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
  .modal-btns .btn { width: auto; }
  .modal-error { color: var(--red); font-size: 13px; font-weight: 600; min-height: 20px; }
  .classify-modal { width: 860px; max-height: 90vh; overflow-y: auto; }
  .classify-layout { display: flex; gap: 24px; }
  .classify-preview { flex: 0 0 340px; }
  .classify-preview img { width: 100%; border-radius: 8px; border: 1px solid var(--border); box-shadow: 0 2px 8px rgba(0,0,0,.08); }
  .classify-details { flex: 1; min-width: 0; }
  .classify-summary { font-size: 14px; color: var(--gray); margin-bottom: 16px; padding: 12px; background: var(--bg); border-radius: 8px; }
  .classify-summary strong { color: #212529; }
  .tag-section { margin-bottom: 14px; }
  .tag-section h3 { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--gray); margin-bottom: 8px; }
  .tag-grid { display: flex; flex-wrap: wrap; gap: 6px; }
  .tag-btn { padding: 8px 16px; border: 2px solid var(--border); border-radius: 8px; background: #fff; color: #212529; font-size: 14px; font-weight: 500; font-family: var(--font); cursor: pointer; transition: all .15s; text-transform: capitalize; }
  .tag-btn:hover { border-color: var(--primary); color: var(--primary-text); background: var(--primary-light); }
  .tag-btn:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .tag-btn.selected { border-color: var(--primary); background: var(--primary-light); color: var(--primary-text); font-weight: 700; }
  .tag-btn.suggested { border-color: #9dc2f7; background: #edf3fc; color: var(--primary-text); }
  .add-tag-row { display: flex; gap: 6px; margin-top: 8px; }
  .add-tag-row input { flex: 1; padding: 6px 10px; font-size: 13px; }
  .btn-add-tag { flex-shrink: 0; padding: 6px 14px; width: auto; font-size: 13px; }
  .classify-folder { margin-top: 14px; }
  .classify-folder input[type="text"] { font-family: var(--mono); font-size: 13px; }
  .field-hint { font-size: 12px; color: var(--gray-light); margin-top: 3px; }
  .classify-filename { margin-top: 10px; }
  .classify-filename input[type="text"] { font-family: var(--mono); font-size: 13px; }
  .browse-row { display: flex; gap: 6px; align-items: center; }
  .browse-row input { flex: 1; }
  .btn-browse { flex-shrink: 0; width: 40px; height: 38px; padding: 0; border: 1px solid var(--border); border-radius: 6px; background: #fff; font-size: 18px; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background .15s, border-color .15s; }
  .btn-browse:hover { background: #e9ecef; border-color: var(--primary); }
  .btn-browse:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .risk-alert { margin-top: 12px; padding: 12px 14px; border-radius: 8px; font-size: 13px; line-height: 1.6; }
  .risk-alert.risk-none { display: none; }
  .risk-alert.risk-low { background: #fff3cd; border: 1px solid #cc9a06; color: #664d03; }
  .risk-alert.risk-medium { background: #ffe0cc; border: 1px solid #c35a02; color: #653000; }
  .risk-alert.risk-high { background: #f8d7da; border: 1px solid var(--red); color: #6a1a21; }
  .risk-alert h4 { font-size: 13px; font-weight: 700; margin-bottom: 4px; }
  .risk-alert ul { margin: 4px 0 0 16px; padding: 0; }
  .risk-alert li { margin-bottom: 2px; }
  .batch-modal { width: 900px; max-height: 90vh; overflow-y: auto; }
  .batch-docs { display: flex; flex-direction: column; gap: 14px; max-height: 55vh; overflow-y: auto; padding: 4px; }
  .batch-doc { border: 1px solid var(--border); border-radius: 10px; padding: 14px; background: var(--bg); }
  .batch-doc-head { display: flex; gap: 12px; }
  .batch-doc-thumb { width: 64px; height: 88px; object-fit: cover; border-radius: 6px; border: 1px solid var(--border); flex-shrink: 0; }
  .batch-doc-body { flex: 1; min-width: 0; }
  .batch-doc-pages { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; color: var(--primary); margin-bottom: 2px; }
  .batch-doc-summary { font-size: 13px; color: var(--gray); margin-bottom: 8px; }
  .batch-fields { display: grid; grid-template-columns: 64px 1fr; gap: 5px 10px; font-size: 13px; align-items: center; }
  .batch-fields label { font-weight: 600; color: var(--gray); }
  .batch-fields input { padding: 5px 8px; font-size: 13px; font-family: var(--mono); }
  .batch-tag-grid { display: flex; flex-wrap: wrap; gap: 4px; grid-column: 2; }
  .batch-tag { padding: 3px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 12px; font-family: var(--font); cursor: pointer; background: #fff; color: #212529; transition: all .15s; }
  .batch-tag.selected { border-color: var(--primary); background: var(--primary-light); color: var(--primary-text); font-weight: 600; }
  .batch-tag:hover { border-color: var(--primary); }
  .batch-tag:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .batch-results { list-style: none; padding: 0; }
  .batch-results li { padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
  .batch-results li:last-child { border-bottom: none; }
  .batch-results .br-name { font-weight: 600; }
  .batch-results .br-detail { font-size: 12px; color: var(--gray); }
  @media (prefers-reduced-motion: reduce) { .spinner { animation: none; } * { transition: none !important; } }
  @media (max-width: 640px) { .batch-modal { width: 95vw; } }
  @media (max-width: 640px) { .classify-layout { flex-direction: column; } .classify-preview { flex: none; } .classify-modal { width: 95vw; } }
</style>
</head>
<body>
<a href="#main-content" class="sr-only">Skip to main content</a>
<main id="main-content">
<div class="container">
  <h1>Auto-Scan</h1>

  <div class="card">
    <h2>Scanner</h2>
    <div class="connect-row">
      <div>
        <label for="scanner-ip">Scanner IP (leave blank for auto-discover)</label>
        <input type="text" id="scanner-ip" placeholder="192.168.1.x">
      </div>
      <button class="btn btn-primary btn-connect" onclick="connect()">Connect</button>
    </div>
    <div class="status disconnected" id="scanner-status" role="status" aria-live="polite">Not connected</div>
  </div>

  <div class="card">
    <h2>Settings</h2>
    <div class="row">
      <div>
        <label>Source</label>
        <div class="radio-group">
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
        <button class="btn-browse" onclick="browseFolder()" title="Browse folders" aria-label="Browse folders">&#128193;</button>
      </div>
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
      <button class="btn btn-primary" id="btn-batch" onclick="doBatchScan()" disabled style="background:#6d28d9"><span class="spinner" aria-hidden="true"></span>Batch Scan</button>
      <button class="btn btn-secondary" id="btn-scan" onclick="scanOnly()" disabled><span class="spinner" aria-hidden="true"></span><span class="sr-only busy-text" hidden>Scanning...</span>Scan Only</button>
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
  </div>

  <div class="card" id="batch-results-card" style="display:none" aria-live="polite">
    <h2 id="batch-results-title">Batch Complete</h2>
    <ul class="batch-results" id="batch-results-list"></ul>
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

<script>
const $ = s => document.querySelector(s);
let currentMode = 'auto';
let selectedTags = new Set();
let pendingRisk = {level: null, risks: []};

(async function init() {
  if (!$('#output-dir').value) $('#output-dir').value = '~/Documents/Scans';
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (!data.has_api_key) { $('#api-key-modal').classList.add('active'); $('#api-key-input').focus(); }
  } catch(e) {}
  refreshLog();
})();

function setMode(mode) {
  currentMode = mode;
  $('#mode-auto').classList.toggle('active', mode === 'auto');
  $('#mode-assisted').classList.toggle('active', mode === 'assisted');
  $('#mode-auto').setAttribute('aria-selected', mode === 'auto');
  $('#mode-assisted').setAttribute('aria-selected', mode === 'assisted');
  $('#mode-desc').textContent = mode === 'auto'
    ? 'AI automatically classifies and saves the document.'
    : 'Scan and review AI suggestions before saving.';
}

function closeApiModal() { $('#api-key-modal').classList.remove('active'); }
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
    if (data.ok) { $('#output-dir').value = data.path; }
  } catch(e) {}
}

function getScanParams() {
  return { source: document.querySelector('input[name="source"]:checked').value, resolution: $('#resolution').value, color: $('#color').value, output_dir: $('#output-dir').value, scanner_ip: $('#scanner-ip').value.trim() };
}
function setBusy(busy, statusText) {
  ['#btn-classify','#btn-scan','#btn-batch'].forEach(s => { const el = $(s); if (el) { el.disabled = busy; el.setAttribute('aria-busy', busy); el.classList.toggle('busy', busy); }});
  document.querySelectorAll('.busy-text').forEach(el => el.hidden = !busy);
  if (statusText) {
    const labels = {scanning: 'Scanning...', analyzing: 'Analyzing...', saving: 'Saving...'};
    const txt = labels[statusText] || statusText;
    document.querySelectorAll('.busy-text').forEach(el => el.textContent = txt);
  }
}

function pollJob() {
  return new Promise((resolve, reject) => {
    const interval = setInterval(async () => {
      try {
        const res = await fetch('/api/job');
        const job = await res.json();
        if (job.status === 'scanning' || job.status === 'analyzing' || job.status === 'saving') {
          setBusy(true, job.status);
          refreshLog();
          return; // keep polling
        }
        clearInterval(interval);
        refreshLog();
        resolve(job);
      } catch(e) {
        clearInterval(interval);
        reject(e);
      }
    }, 600);
  });
}

async function connect() {
  const ip = $('#scanner-ip').value.trim();
  const st = $('#scanner-status');
  st.textContent = 'Connecting...'; st.className = 'status disconnected';
  try {
    const res = await fetch('/api/connect', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ip}) });
    const data = await res.json();
    if (data.ok) { st.textContent = data.name + ' \u2014 ' + data.state; st.className = 'status connected'; $('#btn-classify').disabled = false; $('#btn-scan').disabled = false; $('#btn-batch').disabled = false; }
    else { st.textContent = 'Error: ' + data.error; st.className = 'status error'; }
  } catch(e) { st.textContent = 'Failed: ' + e.message; st.className = 'status error'; }
  refreshLog();
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
    else if (job.status === 'done' && job.result) { showResult({folder: 'unsorted', tags: [], filename: (job.result.output_path || '').split('/').pop(), summary: 'Saved without classification', path: job.result.output_path}); }
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
  $('#classify-modal').classList.add('active');
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

function cancelClassify() { $('#classify-modal').classList.remove('active'); }

async function saveClassified() {
  const folder = $('#classify-folder').value.trim();
  if (!folder) { alert('Please enter a folder name.'); $('#classify-folder').focus(); return; }
  $('#btn-save-classify').disabled = true;
  try {
    const res = await fetch('/api/save-classified', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ folder, tags: [...selectedTags], filename: $('#classify-fn').value, output_dir: $('#output-dir').value }) });
    const data = await res.json();
    if (data.ok) {
      $('#classify-modal').classList.remove('active');
      showResult({folder: data.folder, tags: data.tags, filename: $('#classify-fn').value, path: data.output_path, riskLevel: pendingRisk.level, risks: pendingRisk.risks});
    } else alert('Error: ' + data.error);
  } catch(e) { alert('Failed: ' + e.message); }
  $('#btn-save-classify').disabled = false;
  refreshLog();
}

async function refreshLog() {
  try { const res = await fetch('/api/logs'); const logs = await res.json(); const el = $('#log'); el.textContent = logs.join('\n'); el.scrollTop = el.scrollHeight; } catch(e) {}
}
setInterval(refreshLog, 2000);

// ── Batch scan ──────────────────────────────────────────────────────
let batchData = [];
let batchTags = [];

async function doBatchScan() {
  setBusy(true, 'scanning');
  $('#results-card').style.display = 'none';
  $('#batch-results-card').style.display = 'none';
  try {
    const auto = currentMode === 'auto';
    const res = await fetch('/api/scan-batch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), auto}) });
    const start = await res.json();
    if (!start.ok) { alert('Error: ' + start.error); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { alert('Error: ' + (job.result && job.result.error || 'Unknown error')); }
    else if (job.status === 'done' && job.result && job.result.batch) {
      if (auto) { showBatchResults(job.result.documents); }
      else { showBatchModal(job.result.documents); }
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
  docs.forEach((doc, i) => {
    const li = document.createElement('li');
    const name = doc.filename || (doc.output_path || '').split('/').pop() || 'document';
    const detail = [doc.folder || doc.category, doc.summary].filter(Boolean).join(' \u2014 ');
    const pages = doc.pages ? 'Pages ' + doc.pages.join(', ') : '';
    li.innerHTML = '<span class="br-name">' + esc(name) + '</span>' + (pages ? ' <span class="br-detail">(' + esc(pages) + ')</span>' : '') + (detail ? '<br><span class="br-detail">' + esc(detail) + '</span>' : '');
    list.appendChild(li);
  });
}

function showBatchModal(docs) {
  batchData = docs;
  batchTags = docs.map(d => new Set(d.tags || []));
  $('#batch-count').textContent = docs.length;
  const container = $('#batch-docs');
  container.innerHTML = '';
  const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };

  docs.forEach((doc, i) => {
    const card = document.createElement('div');
    card.className = 'batch-doc';

    let tagsHtml = (doc.tags || []).map(t =>
      '<button class="batch-tag selected" data-doc="' + i + '" data-tag="' + esc(t) + '" aria-pressed="true">' + esc(t) + '</button>'
    ).join('');

    let riskHtml = '';
    if (doc.risk_level && doc.risk_level !== 'none' && doc.risks && doc.risks.length) {
      const icons = {low: '\u26a0\ufe0f', medium: '\u26a0\ufe0f', high: '\ud83d\udea8'};
      const labels = {low: 'Low Risk', medium: 'Medium Risk', high: 'High Risk'};
      riskHtml = '<div class="risk-alert risk-' + esc(doc.risk_level) + '" style="margin-top:8px"><h4>' + (icons[doc.risk_level]||'') + ' ' + esc(labels[doc.risk_level]||doc.risk_level) + '</h4><ul>' + doc.risks.map(r => '<li>' + esc(r) + '</li>').join('') + '</ul></div>';
    }

    card.innerHTML = '<div class="batch-doc-head">' +
      '<img class="batch-doc-thumb" src="data:image/jpeg;base64,' + (doc.preview || '') + '" alt="Page preview">' +
      '<div class="batch-doc-body">' +
        '<div class="batch-doc-pages">Pages ' + esc((doc.pages || []).join(', ')) + '</div>' +
        '<div class="batch-doc-summary">' + esc(doc.summary || '') + '</div>' +
        '<div class="batch-fields">' +
          '<label>Filename</label><input type="text" id="batch-fn-' + i + '" value="' + esc(doc.filename || '') + '">' +
          '<label>Folder</label><input type="text" id="batch-folder-' + i + '" value="' + esc(doc.category || 'other') + '" list="folder-suggestions">' +
          '<label>Tags</label><div class="batch-tag-grid" id="batch-tags-' + i + '">' + tagsHtml + '</div>' +
        '</div>' +
        riskHtml +
      '</div></div>';
    container.appendChild(card);
  });

  updateBatchSaveBtn();
  $('#batch-modal').classList.add('active');
}

// Event delegation for batch tag toggling
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
  updateBatchSaveBtn();
});

function updateBatchSaveBtn() {
  $('#btn-save-batch').textContent = 'Save All (' + batchData.length + ' document' + (batchData.length !== 1 ? 's' : '') + ')';
}

function cancelBatch() { $('#batch-modal').classList.remove('active'); }

async function saveBatch() {
  $('#btn-save-batch').disabled = true;
  const documents = batchData.map((doc, i) => ({
    folder: ($('#batch-folder-' + i) || {}).value || doc.category,
    tags: [...batchTags[i]],
    filename: ($('#batch-fn-' + i) || {}).value || doc.filename,
  }));
  try {
    const res = await fetch('/api/save-batch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({documents, output_dir: $('#output-dir').value}) });
    const data = await res.json();
    if (data.ok) {
      $('#batch-modal').classList.remove('active');
      showBatchResults(data.documents);
    } else alert('Error: ' + data.error);
  } catch(e) { alert('Failed: ' + e.message); }
  $('#btn-save-batch').disabled = false;
  refreshLog();
}

// Keyboard: Escape closes modals
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if ($('#batch-modal').classList.contains('active')) { cancelBatch(); }
    else if ($('#classify-modal').classList.contains('active')) { cancelClassify(); }
    else if ($('#api-key-modal').classList.contains('active')) { closeApiModal(); }
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
