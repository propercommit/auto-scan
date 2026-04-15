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

from auto_scan import AutoScanError, ScanError, ScannerBusyError
from auto_scan.analyzer import ALL_CATEGORIES, DocumentInfo, analyze_batch, analyze_document
from auto_scan.config import Config, load_config
from auto_scan.dedup import image_hash
from auto_scan.history import find_by_hash, record_scan, search_history
from auto_scan.organizer import sanitize_name, save_document, save_unclassified
from auto_scan.usage import get_usage
from auto_scan.scanner.discovery import ScannerInfo, discover_all_scanners, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings

app = Flask(__name__)

# Thread lock for shared mutable state
_state_lock = threading.Lock()


# ── CSRF protection: reject cross-origin POST requests ─────────────
@app.before_request
def _csrf_check():
    """Block POST requests that originate from a different site (CSRF)."""
    if request.method != "POST":
        return None
    origin = request.headers.get("Origin") or ""
    referer = request.headers.get("Referer") or ""
    # Allow requests from our own server (localhost / 127.0.0.1)
    allowed = {f"http://127.0.0.1:{_server_port}", f"http://localhost:{_server_port}"}
    if origin and origin.rstrip("/") not in allowed:
        return jsonify({"ok": False, "error": "Cross-origin request blocked"}), 403
    if not origin and referer:
        # Check referer as fallback
        if not any(referer.startswith(a) for a in allowed):
            return jsonify({"ok": False, "error": "Cross-origin request blocked"}), 403
    return None


_server_port = 8470  # updated by main() before app.run()

# ── Persistent settings ─────────────────────────────────────────────

SETTINGS_DEFAULTS = {
    "output_dir": str(Path("~/Documents/Scans").expanduser()),
    "scanner_ip": "",
    "resolution": "300",
    "color_mode": "RGB24",
    "scan_source": "Feeder",
    "mode": "auto",
    "daily_budget": "0",
    "redact_enabled": True,
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
    os.chmod(path, 0o600)


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


def _open_image(image_data: bytes) -> Image.Image:
    """Open image bytes, handling formats PIL can't directly open (e.g. PDF from scanner)."""
    try:
        img = Image.open(io.BytesIO(image_data))
        img.load()
        return img
    except Exception:
        pass
    if image_data[:5] == b"%PDF-":
        try:
            import pikepdf
            pdf = pikepdf.open(io.BytesIO(image_data))
            page = pdf.pages[0]
            for image_key in page.images:
                pil_img = page.images[image_key].as_pil_image()
                pdf.close()
                return pil_img
            pdf.close()
        except Exception:
            pass
    raise ValueError("Cannot read scanned image — unsupported format from scanner.")


def _make_thumbnail(image_data: bytes, max_dim: int = 800) -> bytes:
    """Resize an image for preview, capping at max_dim pixels."""
    img = _open_image(image_data)
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


@app.route("/api/test-ocr", methods=["POST"])
def api_test_ocr():
    """Pick 3 random realistic test documents and run OCR + redaction on each."""
    import shutil
    import time

    result = {
        "tesseract": False, "pytesseract": False,
        "ocr_works": False, "redaction_works": False,
        "details": "", "files": [],
    }

    # Check tesseract binary
    if not shutil.which("tesseract"):
        result["details"] = "tesseract is not installed. Run: brew install tesseract"
        return jsonify(result)
    result["tesseract"] = True

    # Check pytesseract module
    try:
        import pytesseract
    except ImportError:
        result["details"] = "pytesseract Python package not installed. Run: pip install pytesseract"
        return jsonify(result)
    result["pytesseract"] = True

    # Pick 3 random test documents from the 14 available
    try:
        from auto_scan.test_documents import pick_random
        test_docs = pick_random(3)
    except Exception as e:
        result["details"] = f"Failed to generate test documents: {e}"
        return jsonify(result)

    # Verify OCR can read text from the first document
    try:
        first_img = Image.open(io.BytesIO(test_docs[0][0]))
        ocr_text = pytesseract.image_to_string(first_img)
        if len(ocr_text.strip()) < 5:
            result["details"] = "OCR returned no text. Tesseract may not be configured correctly."
            return jsonify(result)
        result["ocr_works"] = True
    except Exception as e:
        result["details"] = f"OCR failed: {e}"
        return jsonify(result)

    # Run the full redaction pipeline on each test document
    from auto_scan.redactor import redact_image

    total_found = 0
    total_expected = 0
    file_results = []
    t0 = time.monotonic()

    for img_bytes, doc_name, expected_types in test_docs:
        try:
            r = redact_image(img_bytes)
            if r.skipped:
                file_results.append({
                    "name": doc_name, "status": "skipped",
                    "detail": r.skip_reason,
                })
                continue

            found_types = set(r.redacted_types)
            has_sensitive = len(expected_types) > 0
            detected = r.redaction_count > 0

            if has_sensitive and detected:
                status = "pass"
                total_found += r.redaction_count
            elif not has_sensitive and not detected:
                status = "pass"
            elif has_sensitive and not detected:
                status = "miss"
            else:
                status = "extra"  # found something in clean doc (false positive)

            total_expected += len(expected_types)
            file_results.append({
                "name": doc_name, "status": status,
                "count": r.redaction_count,
                "found": r.redacted_types,
                "expected": expected_types,
            })
        except Exception as e:
            file_results.append({
                "name": doc_name, "status": "error",
                "detail": str(e),
            })

    elapsed = time.monotonic() - t0
    result["files"] = file_results
    passes = sum(1 for f in file_results if f["status"] == "pass")
    result["redaction_works"] = passes >= 2  # at least 2/3 pass
    result["details"] = (
        f"Tested 3 documents in {elapsed:.1f}s: "
        f"{passes}/3 passed, {total_found} region(s) redacted"
    )
    return jsonify(result)


@app.route("/api/save-key", methods=["POST"])
def api_save_key():
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "API key cannot be empty"}), 400
    # Prevent .env injection via newlines or control characters
    if any(c in key for c in "\n\r\0"):
        return jsonify({"ok": False, "error": "Invalid API key (contains control characters)"}), 400
    if not key.startswith("sk-"):
        return jsonify({"ok": False, "error": "Invalid API key format (should start with sk-)"}), 400

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
        os.chmod(env_path, 0o600)
    else:
        env_path.write_text(f"ANTHROPIC_API_KEY={key}\n")
        os.chmod(env_path, 0o600)

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

    # Sanitize start_dir to prevent injection into shell commands
    if start_dir and ('"' in start_dir or "'" in start_dir or "\\" in start_dir
                       or "\n" in start_dir or "\r" in start_dir or "\0" in start_dir):
        start_dir = ""  # reject paths with shell metacharacters

    if system == "Darwin":
        script = 'set p to POSIX path of (choose folder'
        if start_dir and Path(start_dir).is_dir():
            # AppleScript string: escape backslashes and quotes
            safe_dir = start_dir.replace("\\", "\\\\").replace('"', '\\"')
            script += f' default location POSIX file "{safe_dir}"'
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
            # PowerShell: escape quotes, backticks, and dollar signs
            safe_dir = start_dir.replace('`', '``').replace('"', '`"').replace("$", "`$")
            ps_script += f'$d.SelectedPath = "{safe_dir}"; '
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


@app.route("/api/reveal", methods=["POST"])
def api_reveal():
    """Reveal a file in the OS file manager (Finder on macOS, Explorer on Windows)."""
    import platform
    import subprocess

    data = request.get_json(silent=True) or {}
    file_path = data.get("path", "")
    if not file_path:
        return jsonify({"ok": False, "error": "No path provided"}), 400

    path = Path(file_path).resolve()
    if not path.exists():
        return jsonify({"ok": False, "error": "File not found"}), 404

    # Only allow revealing files within the configured output directory
    settings = _load_settings()
    output_dir = Path(settings.get("output_dir", "~/Documents/Scans")).expanduser().resolve()
    if not str(path).startswith(str(output_dir)):
        return jsonify({"ok": False, "error": "Path is outside the output directory"}), 403

    try:
        system = platform.system()
        if system == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        elif system == "Windows":
            subprocess.Popen(["explorer", "/select,", str(path)])
        else:
            # Linux: open the containing folder
            subprocess.Popen(["xdg-open", str(path.parent)])
        return jsonify({"ok": True})
    except Exception as e:
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

        # Use validated config values (not raw user input) to prevent XML injection
        settings = ScanSettings(
            source=config.scan_source, color_mode=config.color_mode,
            resolution=config.resolution, document_format=config.scan_format,
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
    with _state_lock:
        last_hash = state.get("_last_hash")
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
        image_hash=last_hash,
    )


def _run_scan_job(data: dict, mode: str):
    """Run a scan job in a background thread. Updates state['job']."""
    # Clear stale state from any previous scan
    with _state_lock:
        state["pending_images"] = None
        state["pending_doc_info"] = None
        state["pending_batch_docs"] = None
        state["pending_config"] = None
        state["_redacted_previews"] = {}
    try:
        # Load settings
        settings = _load_settings()
        daily_budget = float(settings.get("daily_budget", 0))
        reckless = bool(settings.get("reckless_mode"))
        redact = bool(settings.get("redact_enabled")) and not reckless
        redact_pats = set(settings.get("redact_patterns", "").split(",")) - {""} if redact else None
        if daily_budget > 0:
            usage = get_usage()
            if usage["estimated_cost"] >= daily_budget:
                raise AutoScanError(
                    f"Daily budget exceeded: ${usage['estimated_cost']:.2f} / ${daily_budget:.2f} spent. "
                    f"Increase the limit in Settings or wait until tomorrow."
                )

        with _state_lock:
            state["job"] = {"status": "scanning"}
        images, config = _do_scan(data)

        # Store images early so they're available for preview endpoints
        with _state_lock:
            state["pending_images"] = images

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

        # ── Privacy gate: ALWAYS require explicit user confirmation ──────
        # unless reckless mode is on. This is the ONLY code path to AI.
        import time as _time
        page_redactions = {}  # tracks which pages had redactions (1-indexed)

        if reckless:
            _log("OCR privacy check: skipped (reckless mode)")
        else:
            # Run OCR redaction if enabled
            redaction_info = {"status": "disabled"}
            if redact:
                from auto_scan.redactor import redact_image

                with _state_lock:
                    state["job"]["status"] = "checking_privacy"
                _log(f"OCR privacy check: scanning {len(images)} page(s)...")

                t0 = _time.monotonic()
                total_redactions = 0
                all_redacted_types = set()
                redaction_skipped = False
                skip_reason = ""
                redacted_previews = {}  # page (1-indexed) -> redacted JPEG bytes
                for idx, img_data in enumerate(images):
                    r = redact_image(img_data, enabled_patterns=redact_pats)
                    total_redactions += r.redaction_count
                    all_redacted_types.update(r.redacted_types)
                    if r.redaction_count > 0:
                        page_redactions[idx + 1] = {"count": r.redaction_count, "types": r.redacted_types}
                        redacted_previews[idx + 1] = r.redacted_image
                    if r.skipped:
                        redaction_skipped = True
                        skip_reason = r.skip_reason
                elapsed = _time.monotonic() - t0

                if redaction_skipped:
                    _log(f"OCR privacy check: SKIPPED — {skip_reason}")
                    redaction_info = {"status": "skipped", "reason": skip_reason}
                elif total_redactions > 0:
                    types_str = ", ".join(sorted(all_redacted_types))
                    _log(f"OCR privacy check: {total_redactions} region(s) redacted [{types_str}] in {elapsed:.1f}s")
                    for pg, info in sorted(page_redactions.items()):
                        _log(f"  Page {pg}: {info['count']} region(s) [{', '.join(info['types'])}]")
                    redaction_info = {
                        "status": "redacted",
                        "count": total_redactions,
                        "types": sorted(all_redacted_types),
                    }
                else:
                    _log(f"OCR privacy check: clean — no sensitive data found ({elapsed:.1f}s)")
                    redaction_info = {"status": "clean"}

                with _state_lock:
                    state["_redacted_previews"] = redacted_previews
            else:
                _log("OCR privacy check: redaction disabled in settings")
                redaction_info = {"status": "skipped", "reason": "Redaction is disabled in settings. Enable it to scan for sensitive data."}

            # ── MANDATORY confirmation gate — blocks until user clicks confirm ──
            with _state_lock:
                state["job"]["status"] = "confirm_send"
                state["job"]["redaction"] = redaction_info
                state["job"]["page_redactions"] = page_redactions

            _log("Waiting for explicit user confirmation before sending to AI...")
            confirmed = False
            for _ in range(600):  # 10 min max wait
                _time.sleep(1)
                with _state_lock:
                    if state["job"].get("user_confirmed"):
                        confirmed = True
                        break
                    if state["job"].get("user_cancelled"):
                        _log("Cancelled by user — documents NOT sent to AI")
                        state["job"] = {
                            "status": "error",
                            "result": {"ok": False, "error": "Cancelled: documents not sent to AI."},
                        }
                        return

            if not confirmed:
                _log("Timed out waiting for confirmation — documents NOT sent to AI")
                with _state_lock:
                    state["job"] = {
                        "status": "error",
                        "result": {"ok": False, "error": "Timed out: no confirmation received. Documents were not sent to AI."},
                    }
                return

            _log("User confirmed — proceeding to AI analysis")

        if mode == "auto":
            classify = data.get("classify", True)
            if classify:
                with _state_lock:
                    state["job"]["status"] = "analyzing"
                _log("Analyzing with Claude Vision...")
                doc_info = analyze_document(images, config, redact=redact, redact_patterns=redact_pats)
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
            doc_info = analyze_document(images, config, redact=redact, redact_patterns=redact_pats)
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
            batch_results = analyze_batch(images, config, redact=redact, redact_patterns=redact_pats)
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
                    "result": {"ok": True, "batch": True, "documents": saved, "page_redactions": page_redactions},
                }

        elif mode == "batch-assisted":
            with _state_lock:
                state["job"]["status"] = "analyzing"
            _log(f"Batch analyzing {len(images)} pages...")
            batch_results = analyze_batch(images, config, redact=redact, redact_patterns=redact_pats)
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
                    "confidence": doc_info.confidence,
                    "page_confidence": doc_info.page_confidence,
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
                        "page_redactions": page_redactions,
                    },
                }

    except Exception as e:
        _log(f"Error: {e}")
        error_info: dict = {"ok": False, "error": str(e)}

        # Classify the error type for the frontend
        if isinstance(e, ScannerBusyError):
            error_info["error_type"] = "busy"
            error_info["hint"] = "The scanner is busy with another job. Wait for it to finish and try again."
        elif isinstance(e, ScanError):
            error_info["error_type"] = "scanner"
            error_info["hint"] = "A scanner error occurred. Check the scanner for paper jams or other issues, then try again."
        elif isinstance(e, AutoScanError):
            error_info["error_type"] = "app"
            error_info["hint"] = str(e)
        else:
            error_info["error_type"] = "unknown"
            error_info["hint"] = "An unexpected error occurred. Check the log for details."

        # Try to re-check scanner state for more specific info
        try:
            with _state_lock:
                info = state.get("scanner_info")
            if info:
                with ESCLClient(info.base_url) as client:
                    status = client.get_status()
                    error_info["scanner_state"] = status.state
                    error_info["adf_state"] = status.adf_state
                    # Provide specific guidance based on scanner state
                    if status.adf_state and "Jam" in status.adf_state:
                        error_info["hint"] = "Paper jam detected! Open the scanner, clear the jammed paper, and try again."
                        error_info["error_type"] = "jam"
                    elif status.adf_state and "Empty" in status.adf_state:
                        error_info["hint"] = "The document feeder is empty. Load your documents and try again."
                        error_info["error_type"] = "empty"
                    elif status.adf_state and "Mispick" in status.adf_state:
                        error_info["hint"] = "The scanner failed to pick up the paper. Re-align your documents and try again."
                        error_info["error_type"] = "mispick"
                    elif status.state == "Stopped":
                        error_info["hint"] = "The scanner has stopped. Check for errors on the scanner display, resolve them, and try again."
                        error_info["error_type"] = "stopped"
                    elif status.state == "Processing":
                        error_info["hint"] = "The scanner is still processing. Wait a moment and try again."
                        error_info["error_type"] = "busy"
                    _log(f"Scanner state after error: {status.state}, ADF: {status.adf_state}")
        except Exception:
            pass  # Scanner unreachable — use the original error info

        with _state_lock:
            state["job"] = {"status": "error", "result": error_info}


def _start_scan_job(data: dict, mode: str):
    """Reset job state and launch scan in a background thread.

    Setting state["job"] to scanning HERE (in the request thread) prevents
    a race where the poll loop sees the OLD "done" result before the
    background thread has started.
    """
    with _state_lock:
        state["job"] = {"status": "scanning"}
    threading.Thread(
        target=_run_scan_job, args=(data, mode), daemon=True,
    ).start()


@app.route("/api/scan", methods=["POST"])
def api_scan():
    """Automatic mode: start scan job in background."""
    with _state_lock:
        if state.get("job") and state["job"]["status"] not in ("done", "error", "duplicate"):
            return jsonify({"ok": False, "error": "A scan is already in progress."}), 409
    data = request.json or {}
    _start_scan_job(data, "auto")
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/scan-assisted", methods=["POST"])
def api_scan_assisted():
    """Assisted mode: start scan + analyze job in background."""
    with _state_lock:
        if state.get("job") and state["job"]["status"] not in ("done", "error", "duplicate"):
            return jsonify({"ok": False, "error": "A scan is already in progress."}), 409
    data = request.json or {}
    _start_scan_job(data, "assisted")
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/scan-batch", methods=["POST"])
def api_scan_batch():
    """Batch mode: scan all pages, group by document, classify each."""
    with _state_lock:
        if state.get("job") and state["job"]["status"] not in ("done", "error", "duplicate"):
            return jsonify({"ok": False, "error": "A scan is already in progress."}), 409
    data = request.json or {}
    mode = "batch-auto" if data.get("auto", True) else "batch-assisted"
    _start_scan_job(data, mode)
    return jsonify({"ok": True, "status": "started"})


@app.route("/api/job")
def api_job():
    """Poll the current scan job status."""
    with _state_lock:
        job = state.get("job")
        if not job:
            return jsonify({"status": "idle"})
        return jsonify(job)


@app.route("/api/job/confirm", methods=["POST"])
def api_job_confirm():
    """User confirms sending unredacted data to AI."""
    with _state_lock:
        job = state.get("job")
        if job and job.get("status") == "confirm_send":
            job["user_confirmed"] = True
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/api/job/cancel", methods=["POST"])
def api_job_cancel():
    """User cancels sending data to AI."""
    with _state_lock:
        job = state.get("job")
        if job and job.get("status") == "confirm_send":
            job["user_cancelled"] = True
            return jsonify({"ok": True})
    return jsonify({"ok": False}), 400


@app.route("/api/page-image/<int:page_num>")
def api_page_image(page_num):
    """Serve a full-size page image for preview. page_num is 1-indexed."""
    with _state_lock:
        images = state.get("pending_images")
    if not images or page_num < 1 or page_num > len(images):
        return "Not found", 404
    img_data = images[page_num - 1]
    thumb = _make_thumbnail(img_data, max_dim=1200)
    return Response(thumb, mimetype="image/jpeg")


@app.route("/api/redacted-image/<int:page_num>")
def api_redacted_image(page_num):
    """Serve redacted version of a page during privacy check. page_num is 1-indexed."""
    with _state_lock:
        previews = state.get("_redacted_previews", {})
    img_data = previews.get(page_num)
    if not img_data:
        return "Not found", 404
    thumb = _make_thumbnail(img_data, max_dim=1200)
    return Response(thumb, mimetype="image/jpeg")


@app.route("/api/original-image/<int:page_num>")
def api_original_image(page_num):
    """Serve original (unredacted) page image during privacy check. page_num is 1-indexed."""
    with _state_lock:
        images = state.get("pending_images")
    if not images or page_num < 1 or page_num > len(images):
        return "Not found", 404
    thumb = _make_thumbnail(images[page_num - 1], max_dim=1200)
    return Response(thumb, mimetype="image/jpeg")


@app.route("/api/rotate-page", methods=["POST"])
def api_rotate_page():
    """Rotate a pending page image. Accepts page_num (1-indexed) and degrees (90, 180, 270)."""
    data = request.json or {}
    page_num = data.get("page_num", 0)
    degrees = data.get("degrees", 90)
    if degrees not in (90, 180, 270):
        return jsonify({"ok": False, "error": "Degrees must be 90, 180, or 270"}), 400

    with _state_lock:
        images = state.get("pending_images")
    if not images or page_num < 1 or page_num > len(images):
        return jsonify({"ok": False, "error": "Page not found"}), 404

    idx = page_num - 1
    img = _open_image(images[idx])
    # PIL rotate is counter-clockwise, we want clockwise
    rotated = img.rotate(-degrees, expand=True)
    buf = io.BytesIO()
    rotated.save(buf, format="JPEG", quality=95)
    new_bytes = buf.getvalue()

    with _state_lock:
        state["pending_images"][idx] = new_bytes

    # Regenerate thumbnail for preview
    thumb = _make_thumbnail(new_bytes, max_dim=300)
    preview_b64 = base64.b64encode(thumb).decode("ascii")
    _log(f"Rotated page {page_num} by {degrees}\u00b0")
    return jsonify({"ok": True, "preview": preview_b64})


@app.route("/api/crop-page", methods=["POST"])
def api_crop_page():
    """Crop a pending page image. Accepts page_num and crop box as fractions (0-1)."""
    data = request.json or {}
    page_num = data.get("page_num", 0)
    # Crop box as fractions of image dimensions
    left = float(data.get("left", 0))
    top = float(data.get("top", 0))
    right = float(data.get("right", 1))
    bottom = float(data.get("bottom", 1))

    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        return jsonify({"ok": False, "error": "Invalid crop box"}), 400

    with _state_lock:
        images = state.get("pending_images")
    if not images or page_num < 1 or page_num > len(images):
        return jsonify({"ok": False, "error": "Page not found"}), 404

    idx = page_num - 1
    img = _open_image(images[idx])
    w, h = img.size
    box = (int(left * w), int(top * h), int(right * w), int(bottom * h))
    cropped = img.crop(box)
    buf = io.BytesIO()
    cropped.save(buf, format="JPEG", quality=95)
    new_bytes = buf.getvalue()

    with _state_lock:
        state["pending_images"][idx] = new_bytes

    thumb = _make_thumbnail(new_bytes, max_dim=300)
    preview_b64 = base64.b64encode(thumb).decode("ascii")
    _log(f"Cropped page {page_num}")
    return jsonify({"ok": True, "preview": preview_b64})


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
                "summary": doc_info.summary,
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


@app.route("/api/usage/reset", methods=["POST"])
def api_usage_reset():
    """Reset today's usage counters."""
    from auto_scan.usage import reset_daily_usage
    reset_daily_usage()
    _log("Usage counters reset")
    return jsonify({"ok": True})


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
  .pipeline-wrap { margin-top: 14px; padding: 16px; background: #0F1117; border-radius: 10px; color: #F1F5F9; }
  .pipeline { display: flex; flex-direction: column; gap: 0; position: relative; }
  .pipeline-step { display: flex; align-items: flex-start; gap: 12px; padding: 8px 0; position: relative; }
  .pipeline-step:not(:last-child)::after { content: ''; position: absolute; left: 15px; top: 36px; bottom: -8px; width: 2px; background: #2D3348; transition: background .3s; }
  .pipeline-step[data-status="done"]:not(:last-child)::after { background: #34D399; }
  .pipe-dot { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0; background: #2D3348; transition: background .3s, box-shadow .3s; position: relative; z-index: 1; }
  .pipe-num { font-size: 13px; font-weight: 700; color: #64748B; transition: color .3s; }
  .pipeline-step[data-status="active"] .pipe-dot { background: #3B82F6; box-shadow: 0 0 0 4px rgba(59,130,246,.25); }
  .pipeline-step[data-status="active"] .pipe-num { color: #fff; }
  .pipeline-step[data-status="active"] .pipe-dot::before { content: ''; position: absolute; inset: -4px; border: 2px solid transparent; border-top-color: rgba(59,130,246,.6); border-radius: 50%; animation: spin .8s linear infinite; }
  .pipeline-step[data-status="done"] .pipe-dot { background: #22C55E; }
  .pipeline-step[data-status="done"] .pipe-num { color: #fff; font-size: 0; }
  .pipeline-step[data-status="done"] .pipe-num::after { content: '\2713'; font-size: 15px; }
  .pipeline-step[data-status="warning"] .pipe-dot { background: #F59E0B; }
  .pipeline-step[data-status="warning"] .pipe-num { color: #fff; font-size: 0; }
  .pipeline-step[data-status="warning"] .pipe-num::after { content: '!'; font-size: 15px; font-weight: 800; }
  .pipeline-step[data-status="warning"]:not(:last-child)::after { background: #F59E0B; }
  .pipeline-step[data-status="error"] .pipe-dot { background: #EF4444; }
  .pipeline-step[data-status="error"] .pipe-num { color: #fff; font-size: 0; }
  .pipeline-step[data-status="error"] .pipe-num::after { content: '\2717'; font-size: 15px; }
  .pipeline-step[data-status="skipped"] .pipe-dot { background: #475569; }
  .pipeline-step[data-status="skipped"] .pipe-num { color: #94A3B8; font-size: 0; }
  .pipeline-step[data-status="skipped"] .pipe-num::after { content: '\2014'; font-size: 14px; }
  .pipeline-step[data-status="skipped"]:not(:last-child)::after { background: #475569; }
  .pipe-body { flex: 1; min-width: 0; padding-top: 4px; }
  .pipe-label { font-size: 14px; font-weight: 700; color: #94A3B8; transition: color .3s; }
  .pipeline-step[data-status="active"] .pipe-label { color: #F1F5F9; }
  .pipeline-step[data-status="done"] .pipe-label { color: #86EFAC; }
  .pipeline-step[data-status="warning"] .pipe-label { color: #FCD34D; }
  .pipeline-step[data-status="error"] .pipe-label { color: #FCA5A5; }
  .pipe-detail { font-size: 12px; color: #64748B; margin-top: 2px; line-height: 1.4; }
  .pipeline-step[data-status="active"] .pipe-detail { color: #94A3B8; }
  .pipeline-step[data-status="done"] .pipe-detail { color: #6EE7B7; }
  .pipeline-step[data-status="warning"] .pipe-detail { color: #FDE68A; }
  .pipeline-step[data-status="error"] .pipe-detail { color: #FCA5A5; }
  .redact-alert { margin-top: 12px; padding: 12px 14px; border-radius: 8px; font-size: 13px; }
  .redact-alert svg { flex-shrink: 0; }
  .redact-alert.redact-clean { background: rgba(34,197,94,.1); border: 1px solid rgba(34,197,94,.3); color: #86EFAC; }
  .redact-alert.redact-clean svg { color: #22C55E; }
  .redact-alert.redact-redacted { background: rgba(245,158,11,.1); border: 1px solid rgba(245,158,11,.3); color: #FDE68A; }
  .redact-alert.redact-redacted svg { color: #F59E0B; }
  .redact-alert.redact-warning { background: rgba(239,68,68,.1); border: 1px solid rgba(239,68,68,.3); color: #FCA5A5; }
  .redact-alert.redact-warning svg { color: #EF4444; }
  .ocr-badge { display: inline-flex; align-items: center; gap: 3px; font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 6px; background: #dbeafe; color: #1e40af; position: absolute; top: 2px; left: 2px; z-index: 1; white-space: nowrap; }
  .ocr-badge svg { width: 10px; height: 10px; }
  .ocr-doc-badge { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 10px; background: #dbeafe; color: #1e40af; white-space: nowrap; flex-shrink: 0; }
  .ocr-doc-badge svg { width: 12px; height: 12px; }
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
  .log-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
  .log-header h2 { margin-bottom: 0; }
  .log-copy-btn { display: inline-flex; align-items: center; gap: 5px; background: none; border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; font-size: 12px; font-weight: 600; font-family: var(--font); color: var(--gray-light); cursor: pointer; transition: color var(--transition), border-color var(--transition), background var(--transition); }
  .log-copy-btn:hover { color: var(--gray); border-color: var(--gray); background: var(--bg); }
  .log-copy-btn.copied { color: var(--green); border-color: var(--green); }
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
  .classify-preview img { width: 100%; border-radius: var(--radius); border: 1px solid var(--border); box-shadow: 0 2px 8px rgba(0,0,0,.08); cursor: zoom-in; transition: box-shadow var(--transition), transform var(--transition); }
  .classify-preview img:hover { box-shadow: 0 4px 16px rgba(0,0,0,.18); transform: scale(1.01); }
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
  .classify-folder input[type="text"], .classify-folder select { font-family: var(--mono); font-size: 13px; }
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
  .error-card { border: 1px solid var(--red); background: #fef2f2; text-align: center; }
  .error-card h2 { color: #991b1b; margin-bottom: 8px; }
  .error-card-icon { font-size: 48px; margin-bottom: 8px; line-height: 1; }
  .error-card-message { color: #7f1d1d; font-size: 14px; margin-bottom: 6px; font-family: var(--mono); background: #fff5f5; border-radius: var(--radius); padding: 10px 14px; word-break: break-word; }
  .error-card-hint { color: #991b1b; font-size: 15px; font-weight: 600; margin-bottom: 14px; }
  .error-card-state { font-size: 13px; color: var(--gray); margin-bottom: 14px; font-family: var(--mono); }
  .error-card-actions { display: flex; gap: 10px; justify-content: center; }
  .risk-alert h4 { font-size: 13px; font-weight: 700; margin-bottom: 4px; }
  .risk-alert ul { margin: 4px 0 0 16px; padding: 0; }
  .risk-alert li { margin-bottom: 2px; }
  .batch-modal { width: 900px; max-height: 90vh; overflow-y: auto; }
  .batch-docs { display: flex; flex-direction: column; gap: 14px; max-height: 55vh; overflow-y: auto; padding: 4px; }
  .batch-doc { border: 1px solid var(--border); border-radius: var(--radius); padding: 14px; background: var(--bg); }
  .batch-doc-head { margin-bottom: 10px; }
  .batch-doc-title { font-size: 15px; font-weight: 700; color: #212529; margin-bottom: 4px; display: flex; align-items: center; gap: 8px; }
  .batch-doc-title .batch-doc-label { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .confidence-badge { display: inline-block; font-size: 12px; font-weight: 700; padding: 2px 8px; border-radius: 10px; white-space: nowrap; flex-shrink: 0; }
  .confidence-high { background: #d1e7dd; color: #146c43; }
  .confidence-med { background: #fff3cd; color: #664d03; }
  .confidence-low { background: #f8d7da; color: #b02a37; }
  .page-confidence { font-size: 10px; font-weight: 700; position: absolute; top: 2px; right: 2px; padding: 1px 5px; border-radius: 6px; z-index: 1; }
  .page-confidence.high { background: #d1e7dd; color: #146c43; }
  .page-confidence.med { background: #fff3cd; color: #664d03; }
  .page-confidence.low { background: #f8d7da; color: #b02a37; }
  .batch-doc-summary { font-size: 14px; color: var(--gray); margin-bottom: 8px; }
  .batch-page-grid { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; min-height: 90px; padding: 10px; border: 2px dashed var(--border); border-radius: var(--radius); transition: border-color var(--transition), background var(--transition); }
  .batch-page-grid.drop-target { border-color: var(--primary); background: var(--primary-light); }
  .batch-page { width: 96px; text-align: center; position: relative; border-radius: 6px; transition: opacity var(--transition); cursor: grab; }
  .batch-page:active { cursor: grabbing; }
  .batch-page.dragging { opacity: .3; }
  .batch-page img { width: 96px; height: 124px; object-fit: cover; border-radius: 6px; border: 2px solid var(--border); transition: border-color var(--transition); }
  .batch-page:hover img { border-color: var(--primary); }
  .batch-page span { display: block; font-size: 13px; font-weight: 600; color: var(--gray); margin-top: 4px; }
  .batch-page select { width: 100%; font-size: 13px; padding: 4px 6px; border: 1px solid var(--border); border-radius: var(--radius); margin-top: 4px; cursor: pointer; background: #fff; color: #212529; }
  .page-actions { position: absolute; top: 2px; right: 2px; display: flex; gap: 2px; opacity: 0; transition: opacity var(--transition); z-index: 2; }
  .batch-page:hover .page-actions { opacity: 1; }
  .page-action-btn { width: 22px; height: 22px; border: none; border-radius: 4px; background: rgba(0,0,0,.6); color: #fff; font-size: 13px; cursor: pointer; display: flex; align-items: center; justify-content: center; padding: 0; line-height: 1; transition: background var(--transition); }
  .page-action-btn:hover { background: rgba(0,0,0,.85); }
  .batch-page-grid-empty { color: var(--gray-light); font-size: 14px; font-style: italic; padding: 16px; text-align: center; width: 100%; }
  .btn-add-doc { background: none; border: 2px dashed var(--border); border-radius: var(--radius); padding: 10px; width: 100%; font-size: 13px; font-weight: 600; color: var(--gray); cursor: pointer; transition: border-color var(--transition), color var(--transition); font-family: var(--font); margin-bottom: 8px; }
  .btn-add-doc:hover { border-color: var(--primary); color: var(--primary); }
  .btn-add-doc:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .btn-remove-doc { background: none; border: none; color: var(--red); font-size: 13px; cursor: pointer; font-weight: 600; font-family: var(--font); padding: 4px 10px; border-radius: 4px; transition: background var(--transition); }
  .btn-remove-doc:hover { background: #f8d7da; }
  .btn-scan-next { margin-top: 16px; width: 100%; padding: 12px 20px; font-size: 15px; font-weight: 700; }
  .lightbox { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.85); z-index: 200; align-items: center; justify-content: center; flex-direction: column; cursor: pointer; }
  .lightbox.active { display: flex; }
  .lightbox-img-wrap { position: relative; display: inline-block; }
  .lightbox-img-wrap img { max-width: min(92vw, 1200px); max-height: 75vh; border-radius: var(--radius); box-shadow: 0 8px 40px rgba(0,0,0,.5); object-fit: contain; display: block; }
  .lightbox-toolbar { display: flex; gap: 8px; margin-bottom: 12px; z-index: 2; }
  .lightbox-tool { background: rgba(255,255,255,.15); border: 1px solid rgba(255,255,255,.25); color: #fff; font-size: 14px; font-weight: 600; padding: 6px 14px; border-radius: 6px; cursor: pointer; transition: background var(--transition); font-family: var(--font); }
  .lightbox-tool:hover { background: rgba(255,255,255,.3); }
  .lightbox-tool-danger:hover { background: rgba(220,38,38,.7); }
  .lightbox-crop-bar { display: flex; gap: 8px; margin-top: 10px; z-index: 2; }
  .crop-overlay { position: absolute; inset: 0; z-index: 3; cursor: crosshair; }
  .crop-box { position: absolute; border: 2px dashed #fff; background: rgba(59,130,246,.15); box-shadow: 0 0 0 9999px rgba(0,0,0,.5); pointer-events: none; }
  .lightbox-label { color: #fff; font-size: 15px; font-weight: 600; margin-top: 12px; }
  .redact-preview-split { display: flex; gap: 20px; align-items: flex-start; max-width: 95vw; width: 95vw; }
  .redact-preview-pane { flex: 1; text-align: center; min-width: 0; }
  .redact-preview-pane img { max-width: 100%; max-height: 80vh; border-radius: 4px; border: 2px solid rgba(255,255,255,.15); cursor: pointer; transition: opacity .2s; }
  .redact-preview-pane img:hover { opacity: .85; }
  .redact-preview-label { color: rgba(255,255,255,.7); font-size: 13px; font-weight: 700; margin-bottom: 8px; text-transform: uppercase; letter-spacing: .5px; }
  .redact-preview-pane img.zoomed { position: fixed; inset: 0; max-width: 100vw; max-height: 100vh; width: auto; height: auto; margin: auto; z-index: 10001; border: none; border-radius: 0; object-fit: contain; background: rgba(0,0,0,.95); padding: 10px; }
  .lightbox-nav { position: absolute; top: 50%; transform: translateY(-50%); background: rgba(255,255,255,.15); border: none; color: #fff; font-size: 32px; width: 48px; height: 48px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background var(--transition); }
  .lightbox-nav:hover { background: rgba(255,255,255,.3); }
  .lightbox-nav:focus-visible { outline: 2px solid #fff; outline-offset: 2px; }
  .lightbox-prev { left: 16px; }
  .lightbox-next { right: 16px; }
  .lightbox-close { position: absolute; top: 16px; right: 16px; background: rgba(255,255,255,.15); border: none; color: #fff; font-size: 24px; width: 40px; height: 40px; border-radius: 50%; cursor: pointer; display: flex; align-items: center; justify-content: center; transition: background var(--transition); }
  .lightbox-close:hover { background: rgba(255,255,255,.3); }
  .batch-fields { display: grid; grid-template-columns: 80px 1fr; gap: 6px 12px; font-size: 14px; align-items: center; }
  .batch-fields label { font-weight: 600; color: var(--gray); font-size: 13px; }
  .batch-fields input[type="text"] { padding: 8px 12px; font-size: 14px; font-family: var(--mono); border: 1px solid var(--border); border-radius: var(--radius); background: #fff; color: #212529; width: 100%; box-sizing: border-box; }
  .batch-fields input[type="text"]:focus { outline: 2px solid var(--primary); outline-offset: 1px; border-color: var(--primary); box-shadow: var(--focus-ring); }
  .batch-tag-grid { display: flex; flex-wrap: wrap; gap: 6px; grid-column: 2; }
  .batch-add-tag-row { display: flex; flex-direction: column; gap: 6px; grid-column: 2; margin-top: 6px; }
  .batch-add-tag-row input[type="text"] { width: 100%; box-sizing: border-box; padding: 10px 14px; font-size: 14px; font-family: var(--font); border: 1px solid var(--border); border-radius: var(--radius); background: #fff; color: #212529; }
  .batch-add-tag-row button { align-self: flex-start; padding: 8px 18px; font-size: 13px; }
  .batch-tag { padding: 6px 14px; border: 2px solid var(--border); border-radius: var(--radius); font-size: 14px; font-weight: 500; font-family: var(--font); cursor: pointer; background: #fff; color: #212529; transition: all var(--transition); }
  .batch-tag.selected { border-color: var(--primary); background: var(--primary-light); color: var(--primary-text); font-weight: 600; }
  .batch-tag:hover { border-color: var(--primary); }
  .batch-tag:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
  .batch-results { list-style: none; padding: 0; }
  .batch-results li { padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 14px; }
  .batch-results li:last-child { border-bottom: none; }
  .batch-results .br-name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: block; }
  .batch-results .br-link { color: var(--primary); text-decoration: none; cursor: pointer; transition: color var(--transition); }
  .batch-results .br-link:hover { color: var(--primary-hover); text-decoration: underline; }
  .batch-results .br-detail { font-size: 12px; color: var(--gray); }
  /* ── Dark usage dashboard (Monetir-inspired) ─────────────── */
  /* ── Dark usage dashboard ─────────────────────────────── */
  .usage-dash { background: #0F1117; border: 1px solid rgba(255,255,255,.06); border-radius: 16px; margin-bottom: 16px; padding: 24px; box-shadow: 0 4px 24px rgba(0,0,0,.2); }
  .usage-dash.collapsed .usage-body { display: none; }
  .usage-dash.over-budget { border-color: rgba(239,68,68,.4); box-shadow: 0 0 0 1px rgba(239,68,68,.3), 0 4px 24px rgba(239,68,68,.1); }
  .usage-dash-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
  .usage-dash.collapsed .usage-dash-header { margin-bottom: 0; }
  .usage-dash-left { display: flex; align-items: center; gap: 12px; }
  .usage-dash-title { font-size: 15px; font-weight: 700; color: #F1F5F9; letter-spacing: -0.2px; margin: 0; }
  .usage-dash-right { display: flex; align-items: center; gap: 10px; }
  .usage-dash-live { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: #34D399; text-transform: uppercase; letter-spacing: .5px; }
  .usage-dash-live::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: #34D399; box-shadow: 0 0 8px #34D399; animation: pulse-dot 2s infinite; }
  @keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: .4; } }
  .usage-btn-collapse { background: none; border: none; color: #64748B; cursor: pointer; padding: 4px; border-radius: 4px; display: flex; align-items: center; transition: color .2s, background .2s; }
  .usage-btn-collapse:hover { color: #94A3B8; background: rgba(255,255,255,.05); }
  .usage-btn-collapse svg { transition: transform .2s; }
  .usage-dash.collapsed .usage-btn-collapse svg { transform: rotate(-90deg); }
  .usage-btn-reset { background: none; border: 1px solid rgba(255,255,255,.08); color: #64748B; cursor: pointer; padding: 4px 10px; border-radius: 6px; font-size: 11px; font-weight: 600; font-family: var(--font); transition: color .2s, border-color .2s, background .2s; }
  .usage-btn-reset:hover { color: #EF4444; border-color: rgba(239,68,68,.3); background: rgba(239,68,68,.08); }
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
  .usage-hero-label { font-size: 12px; color: #8B95A5; font-weight: 500; }
  .usage-hero-value { font-size: 24px; font-weight: 800; color: #F1F5F9; font-family: var(--mono); line-height: 1; letter-spacing: -0.5px; transition: color .3s; }
  .usage-hero-sub { font-size: 11px; color: #7B8794; margin-top: 4px; font-family: var(--mono); }
  .usage-dash.over-budget .usage-hero-value { color: #EF4444; }
  .usage-budget-wrap { margin-bottom: 16px; overflow: hidden; transition: max-height .3s ease, opacity .3s ease; max-height: 60px; opacity: 1; }
  .usage-budget-wrap.hidden { max-height: 0; opacity: 0; margin: 0; }
  .usage-budget-header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  .usage-budget-header span { font-size: 12px; color: #8B95A5; }
  .usage-budget-header .over { color: #EF4444; font-weight: 700; }
  .usage-budget-bar { height: 6px; background: #1A1D2B; border-radius: 3px; overflow: hidden; }
  .usage-budget-fill { height: 100%; border-radius: 3px; transition: width .4s ease; background: linear-gradient(90deg, #6366F1, #818CF8); }
  .usage-budget-fill.warn { background: linear-gradient(90deg, #F59E0B, #FBBF24); }
  .usage-budget-fill.critical { background: linear-gradient(90deg, #EA580C, #F97316); }
  .usage-budget-fill.over { background: linear-gradient(90deg, #EF4444, #F87171); }
  .usage-chart-section { background: #1A1D2B; border-radius: 12px; padding: 16px; }
  .usage-chart-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
  .usage-chart-label { font-size: 13px; font-weight: 600; color: #94A3B8; }
  .usage-chart-legend { display: flex; gap: 14px; }
  .usage-chart-legend span { display: flex; align-items: center; gap: 5px; font-size: 11px; color: #8B95A5; }
  .usage-chart-legend span::before { content: ''; width: 8px; height: 8px; border-radius: 50%; }
  .usage-chart-legend .leg-in::before { background: #818CF8; }
  .usage-chart-legend .leg-out::before { background: #34D399; }
  .usage-chart-wrap { position: relative; height: 120px; }
  .usage-chart { width: 100%; height: 100%; display: block; cursor: crosshair; }
  .usage-chart-tooltip { position: absolute; background: #0F1117; border: 1px solid rgba(255,255,255,.1); border-radius: 8px; padding: 8px 12px; font-size: 11px; font-family: var(--mono); color: #E2E8F0; pointer-events: none; white-space: nowrap; opacity: 0; transition: opacity .15s; z-index: 2; box-shadow: 0 4px 12px rgba(0,0,0,.4); }
  .usage-chart-tooltip.visible { opacity: 1; }
  .usage-chart-tooltip .tt-time { color: #8B95A5; margin-bottom: 4px; }
  .usage-chart-tooltip .tt-row { display: flex; align-items: center; gap: 6px; line-height: 1.5; }
  .usage-chart-tooltip .tt-dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
  .usage-chart-empty { text-align: center; color: #64748B; font-size: 13px; padding-top: 30px; }
  .usage-chart-empty svg { display: block; margin: 0 auto 8px; opacity: .3; }
  .usage-nokey { text-align: center; padding: 16px; color: #64748B; font-size: 13px; }
  .usage-nokey a { color: #818CF8; text-decoration: underline; cursor: pointer; }
  @media (max-width: 768px) { .usage-heroes { grid-template-columns: repeat(2, 1fr); } }
  @media (max-width: 480px) { .btn-row { flex-direction: column; } .connect-row { flex-wrap: wrap; } .usage-heroes { grid-template-columns: repeat(2, 1fr); } .usage-hero-value { font-size: 18px; } .usage-hero { padding: 12px; } .usage-dash { padding: 16px; } .usage-chart-wrap { height: 90px; } }
  @media (prefers-reduced-motion: reduce) { .spinner, .spinner-inline { animation: none; } .usage-dash-live::before { animation: none; } * { transition: none !important; } }
  @media (max-width: 640px) { .batch-modal { width: 95vw; } .classify-layout { flex-direction: column; gap: 16px; } .classify-preview { flex: none; } .classify-modal { width: 95vw; } }
</style>
</head>
<body>
<a href="#main-content" class="sr-only">Skip to main content</a>
<main id="main-content">
<div class="container">
  <h1>Auto-Scan</h1>

  <section class="usage-dash" id="usage-dash" aria-labelledby="usage-title">
    <div class="usage-dash-header">
      <div class="usage-dash-left">
        <h2 class="usage-dash-title" id="usage-title">API Usage</h2>
        <div class="usage-dash-live" role="status" aria-label="Live updating"><span class="sr-only">Live</span>Live</div>
      </div>
      <div class="usage-dash-right">
        <button class="usage-btn-reset" onclick="resetUsage()" title="Reset today's counters">Reset</button>
        <button class="usage-btn-collapse" onclick="toggleUsageDash()" aria-label="Collapse usage dashboard" aria-expanded="true"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>
      </div>
    </div>
    <div class="usage-body">
    <div class="usage-nokey" id="usage-nokey" style="display:none">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="opacity:.4;margin-bottom:6px"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg>
      <div>Set your API key to track usage. <a onclick="$('#api-key-modal').classList.add('active')">Configure</a></div>
    </div>
    <div id="usage-content">
    <div class="usage-heroes" role="list">
      <div class="usage-hero" role="listitem">
        <div class="usage-hero-accent tokens"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon tokens"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg></div>
          <div class="usage-hero-label">Tokens</div>
        </div>
        <div class="usage-hero-value" id="usage-tokens" aria-live="polite">--</div>
        <div class="usage-hero-sub" id="usage-tokens-detail">no activity yet</div>
      </div>
      <div class="usage-hero" role="listitem">
        <div class="usage-hero-accent cost"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon cost"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6"/></svg></div>
          <div class="usage-hero-label">Est. Cost</div>
        </div>
        <div class="usage-hero-value" id="usage-cost">$0.00</div>
        <div class="usage-hero-sub" id="usage-cost-detail">$3/1M in, $15/1M out</div>
      </div>
      <div class="usage-hero" role="listitem">
        <div class="usage-hero-accent calls"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon calls"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg></div>
          <div class="usage-hero-label">API Calls</div>
        </div>
        <div class="usage-hero-value" id="usage-calls">0</div>
        <div class="usage-hero-sub" id="usage-calls-detail">today</div>
      </div>
      <div class="usage-hero" role="listitem">
        <div class="usage-hero-accent budget"></div>
        <div class="usage-hero-top">
          <div class="usage-hero-icon budget"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z"/><path d="M12 6v6l4 2"/></svg></div>
          <div class="usage-hero-label">Budget</div>
        </div>
        <div class="usage-hero-value" id="usage-budget-val">Unlimited</div>
        <div class="usage-hero-sub" id="usage-budget-detail">no cap set</div>
      </div>
    </div>
    <div class="usage-budget-wrap hidden" id="usage-budget-section" role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100" aria-label="Token budget usage">
      <div class="usage-budget-header"><span id="usage-budget-left"></span><span id="usage-budget-right"></span></div>
      <div class="usage-budget-bar"><div class="usage-budget-fill" id="usage-budget-fill"></div></div>
    </div>
    <div class="usage-chart-section">
      <div class="usage-chart-header">
        <div class="usage-chart-label">Token History</div>
        <div class="usage-chart-legend"><span class="leg-in">Input</span><span class="leg-out">Output</span></div>
      </div>
      <div class="usage-chart-wrap" id="usage-chart-wrap">
        <div class="usage-chart-empty" id="usage-chart-empty"><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>No API calls yet today</div>
        <canvas class="usage-chart" id="usage-chart" style="display:none" aria-label="Token usage chart showing cumulative tokens over time" role="img"></canvas>
        <div class="usage-chart-tooltip" id="usage-chart-tooltip"></div>
      </div>
    </div>
    </div>
    </div>
  </section>

  <div class="card">
    <h2>Scanner</h2>
    <div class="connect-row">
      <div>
        <label for="scanner-select">Scanner</label>
        <select id="scanner-select" onchange="onScannerSelect()">
          <option value="">-- Select a scanner --</option>
          <option value="__manual__">Enter IP manually</option>
        </select>
        <input type="text" id="scanner-ip" placeholder="192.168.1.x" style="display:none;margin-top:6px">
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
      <label for="daily-budget">Daily Budget $ (0 = unlimited)</label>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="daily-budget" placeholder="0.00" onchange="saveSettings()" style="flex:1">
        <button class="btn btn-secondary" style="width:auto;padding:8px 14px;font-size:13px;white-space:nowrap" onclick="crazyMode()">Unlimited</button>
      </div>
      <div class="field-hint">Blocks API calls when estimated daily spend reaches this amount. Resets at midnight.</div>
    </div>
    <div style="margin-top:14px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="redact-toggle" onchange="saveSettings()" style="width:auto;accent-color:var(--primary)">
        <span>Redact sensitive data before sending to AI</span>
      </label>
      <div class="field-hint">Uses local OCR to detect and black out sensitive patterns in images before they reach the API. Requires <code>tesseract</code> and <code>pytesseract</code>.</div>
      <div style="margin-top:8px"><button class="btn btn-secondary" id="btn-test-ocr" style="width:auto;padding:6px 14px;font-size:13px" onclick="testOCR()">Test OCR</button><span id="ocr-test-result" style="margin-left:10px;font-size:13px"></span></div>
      <div id="redact-patterns" style="display:none;margin-top:8px;padding-left:26px">
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="ssn" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> SSN (US Social Security)</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="ahv" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> AHV/AVS (Swiss)</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="credit_card" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> Credit card numbers</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="iban" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> IBAN</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="phone" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> Phone numbers</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="email" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> Email addresses</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px;cursor:pointer"><input type="checkbox" value="dob" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> Dates of birth</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:13px;cursor:pointer"><input type="checkbox" value="passport" checked onchange="saveSettings()" style="width:auto;accent-color:var(--primary)"> Passport numbers</label>
      </div>
    </div>
    <div style="margin-top:10px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="reckless-toggle" style="width:auto;accent-color:#EF4444">
        <span>Reckless mode <span style="font-size:12px;color:var(--gray)">(skip OCR privacy check, send directly to AI)</span></span>
      </label>
      <div class="field-hint">Skips the local OCR preview and confirmation step. Redaction still runs before sending to the API if enabled above.</div>
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
    <div class="pipeline-wrap" id="scan-progress" style="display:none" aria-live="polite">
      <div class="pipeline" id="pipeline">
        <div class="pipeline-step" id="pipe-scan" data-status="pending">
          <div class="pipe-dot"><span class="pipe-num">1</span></div>
          <div class="pipe-body">
            <div class="pipe-label">Scan</div>
            <div class="pipe-detail" id="pipe-scan-detail"></div>
          </div>
        </div>
        <div class="pipeline-step" id="pipe-ocr" data-status="pending">
          <div class="pipe-dot"><span class="pipe-num">2</span></div>
          <div class="pipe-body">
            <div class="pipe-label">Privacy Check</div>
            <div class="pipe-detail" id="pipe-ocr-detail"></div>
          </div>
        </div>
        <div class="pipeline-step" id="pipe-ai" data-status="pending">
          <div class="pipe-dot"><span class="pipe-num">3</span></div>
          <div class="pipe-body">
            <div class="pipe-label">AI Analysis</div>
            <div class="pipe-detail" id="pipe-ai-detail"></div>
          </div>
        </div>
        <div class="pipeline-step" id="pipe-save" data-status="pending">
          <div class="pipe-dot"><span class="pipe-num">4</span></div>
          <div class="pipe-body">
            <div class="pipe-label">Save</div>
            <div class="pipe-detail" id="pipe-save-detail"></div>
          </div>
        </div>
      </div>
      <div class="redact-alert" id="redact-alert" style="display:none"></div>
    </div>
  </div>

  <div class="card error-card" id="error-card" style="display:none" aria-live="assertive">
    <div class="error-card-icon" id="error-card-icon"></div>
    <h2 id="error-card-title">Scanner Error</h2>
    <p class="error-card-message" id="error-card-message"></p>
    <p class="error-card-hint" id="error-card-hint"></p>
    <div class="error-card-state" id="error-card-state" style="display:none"></div>
    <div class="error-card-actions">
      <button class="btn btn-primary" onclick="retryLastScan()">Try Again</button>
      <button class="btn btn-secondary" onclick="dismissError()">Dismiss</button>
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
    <button class="btn btn-primary btn-scan-next" onclick="scanNext()">Scan Next Documents</button>
  </div>

  <div class="card" id="batch-results-card" style="display:none" aria-live="polite">
    <h2 id="batch-results-title">Batch Complete</h2>
    <ul class="batch-results" id="batch-results-list"></ul>
    <button class="btn btn-primary btn-scan-next" onclick="scanNext()">Scan Next Documents</button>
  </div>

  <div class="card">
    <div class="log-header"><h2>Activity Log</h2><button class="log-copy-btn" id="log-copy-btn" onclick="copyLogs()" title="Copy logs to clipboard"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg><span id="log-copy-label">Copy</span></button></div>
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
        <img id="classify-img" src="" alt="Document preview" onclick="openLightbox(1)" title="Click to preview full size">
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
          <select id="classify-folder"></select>
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
  <div class="lightbox-toolbar" id="lightbox-toolbar">
    <button class="lightbox-tool" onclick="lightboxRotate()" title="Rotate 90\u00b0">&#x21bb; Rotate</button>
    <button class="lightbox-tool" onclick="lightboxStartCrop()" id="btn-crop" title="Crop page">&#x2702; Crop</button>
    <button class="lightbox-tool lightbox-tool-danger" onclick="lightboxDelete()" title="Delete page">&#x2715; Delete</button>
  </div>
  <div class="lightbox-img-wrap" id="lightbox-img-wrap">
    <img id="lightbox-img" src="" alt="Full page preview">
    <div class="crop-overlay" id="crop-overlay" style="display:none">
      <div class="crop-box" id="crop-box"></div>
    </div>
  </div>
  <div class="lightbox-crop-bar" id="lightbox-crop-bar" style="display:none">
    <button class="btn btn-primary" onclick="lightboxApplyCrop()">Apply Crop</button>
    <button class="btn btn-secondary" onclick="lightboxCancelCrop()">Cancel</button>
  </div>
  <div class="lightbox-label" id="lightbox-label"></div>
</div>

<!-- Redaction Preview Modal -->
<div class="lightbox" id="redact-preview" role="dialog" aria-modal="true" aria-label="Redaction preview">
  <button class="lightbox-close" onclick="closeRedactPreview()" aria-label="Close preview">&times;</button>
  <button class="lightbox-nav lightbox-prev" onclick="redactPreviewNav(-1)" aria-label="Previous page">&#8249;</button>
  <button class="lightbox-nav lightbox-next" onclick="redactPreviewNav(1)" aria-label="Next page">&#8250;</button>
  <div class="redact-preview-split">
    <div class="redact-preview-pane">
      <div class="redact-preview-label">Original</div>
      <img id="redact-preview-orig" src="" alt="Original page">
    </div>
    <div class="redact-preview-pane">
      <div class="redact-preview-label">Redacted (sent to AI)</div>
      <img id="redact-preview-redacted" src="" alt="Redacted page">
    </div>
  </div>
  <div class="lightbox-label" id="redact-preview-info"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const _esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
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
    if (s.scanner_ip) {
      // Check if the saved IP matches a discovered scanner option
      const sel = $('#scanner-select');
      const match = [...sel.options].find(o => o.value === s.scanner_ip);
      if (match) {
        sel.value = s.scanner_ip;
      } else {
        sel.value = '__manual__';
        $('#scanner-ip').value = s.scanner_ip;
        $('#scanner-ip').style.display = '';
      }
    }
    if (s.resolution) $('#resolution').value = s.resolution;
    if (s.color_mode) $('#color').value = s.color_mode;
    if (s.scan_source) {
      const radio = document.querySelector('input[name="source"][value="' + s.scan_source + '"]');
      if (radio) radio.checked = true;
    }
    if (s.mode) setMode(s.mode);
    if (s.daily_budget && s.daily_budget !== '0') $('#daily-budget').value = s.daily_budget;
    if (s.redact_enabled) { $('#redact-toggle').checked = true; $('#redact-patterns').style.display = ''; }
    if (s.redact_patterns) {
      const pats = s.redact_patterns.split(',');
      document.querySelectorAll('#redact-patterns input[type="checkbox"]').forEach(cb => { cb.checked = pats.includes(cb.value); });
    }
    if (s.reckless_mode) { $('#reckless-toggle').checked = true; updateRecklessState(true); }
  } catch(e) {}
  // Fallback handled by /api/settings defaults
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (!data.has_api_key) {
      openModal('#api-key-modal'); $('#api-key-input').focus();
      $('#usage-nokey').style.display = ''; $('#usage-content').style.display = 'none';
    } else {
      $('#usage-nokey').style.display = 'none'; $('#usage-content').style.display = '';
    }
  } catch(e) {}
  refreshLog();
  refreshUsage();
})();

$('#redact-toggle').addEventListener('change', function() {
  $('#redact-patterns').style.display = this.checked ? '' : 'none';
});

function updateRecklessState(reckless) {
  const redactSection = $('#redact-toggle').closest('div').parentElement;
  const toggle = $('#redact-toggle');
  const patterns = $('#redact-patterns');
  if (reckless) {
    toggle.disabled = true;
    redactSection.style.opacity = '0.4';
    redactSection.style.pointerEvents = 'none';
  } else {
    toggle.disabled = false;
    redactSection.style.opacity = '';
    redactSection.style.pointerEvents = '';
  }
}
$('#reckless-toggle').addEventListener('change', function() {
  updateRecklessState(this.checked);
  saveSettings();
});

async function testOCR() {
  const btn = $('#btn-test-ocr');
  const result = $('#ocr-test-result');
  btn.disabled = true;
  btn.textContent = 'Testing 3 random documents...';
  result.innerHTML = '';
  try {
    const res = await fetch('/api/test-ocr', { method: 'POST' });
    const data = await res.json();
    let html = '';
    if (data.files && data.files.length > 0) {
      html += '<div style="margin-top:6px">';
      data.files.forEach(f => {
        const icons = {pass: '\u2705', miss: '\u274c', extra: '\u26a0\ufe0f', skipped: '\u23ed\ufe0f', error: '\u274c'};
        const icon = icons[f.status] || '';
        let detail = f.name;
        if (f.status === 'pass' && f.count > 0) detail += ' \u2014 ' + f.count + ' region(s) [' + (f.found||[]).join(', ') + ']';
        else if (f.status === 'pass') detail += ' \u2014 clean (correct)';
        else if (f.status === 'miss') detail += ' \u2014 missed! expected [' + (f.expected||[]).join(', ') + ']';
        else if (f.detail) detail += ' \u2014 ' + f.detail;
        html += '<div style="font-size:12px;margin-bottom:3px">' + icon + ' ' + detail + '</div>';
      });
      html += '</div>';
    }
    if (data.redaction_works) {
      result.innerHTML = '<strong style="color:#16a34a">All good!</strong> <span style="font-size:12px;color:var(--gray)">' + _esc(data.details) + '</span>' + html;
    } else if (data.ocr_works) {
      result.innerHTML = '<strong style="color:#d97706">Partial:</strong> <span style="font-size:12px;color:var(--gray)">' + _esc(data.details) + '</span>' + html;
    } else {
      result.innerHTML = '<strong style="color:#dc2626">Failed:</strong> <span style="font-size:12px">' + _esc(data.details) + '</span>' + html;
    }
  } catch(e) {
    result.innerHTML = '<strong style="color:#dc2626">Request failed:</strong> ' + _esc(e.message);
  }
  btn.disabled = false;
  btn.textContent = 'Test OCR';
}

function saveSettings() {
  const settings = {
    output_dir: $('#output-dir').value,
    scanner_ip: getScannerIP(),
    resolution: $('#resolution').value,
    color_mode: $('#color').value,
    scan_source: document.querySelector('input[name="source"]:checked').value,
    mode: currentMode,
    daily_budget: $('#daily-budget').value.trim() || '0',
    redact_enabled: $('#redact-toggle').checked,
    redact_patterns: [...document.querySelectorAll('#redact-patterns input[type="checkbox"]:checked')].map(cb => cb.value).join(','),
    reckless_mode: $('#reckless-toggle').checked,
  };
  fetch('/api/settings', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(settings) }).catch(() => {});
}

function crazyMode() {
  if (!confirm('Remove daily budget limit? API costs will be uncapped.')) return;
  $('#daily-budget').value = '0';
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
['#output-dir','#scanner-ip','#scanner-select','#resolution','#color'].forEach(s => {
  const el = $(s); if (el) el.addEventListener('change', saveSettings);
});
document.querySelectorAll('input[name="source"]').forEach(r => r.addEventListener('change', saveSettings));
$('#classify-folder').addEventListener('change', function() { this.classList.remove('input-error'); });

function closeApiModal() { closeModal('#api-key-modal'); }
async function saveApiKey() {
  const key = $('#api-key-input').value.trim();
  const err = $('#api-key-error');
  if (!key) { err.textContent = 'Please enter an API key.'; return; }
  err.textContent = 'Saving...';
  try {
    const res = await fetch('/api/save-key', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key}) });
    const data = await res.json();
    if (data.ok) { closeApiModal(); refreshLog(); $('#usage-nokey').style.display = 'none'; $('#usage-content').style.display = ''; refreshUsage(); } else { err.textContent = data.error; }
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
  return { source: document.querySelector('input[name="source"]:checked').value, resolution: $('#resolution').value, color: $('#color').value, output_dir: $('#output-dir').value, scanner_ip: getScannerIP() };
}
function setBusy(busy) {
  ['#btn-classify','#btn-scan','#btn-batch'].forEach(s => { const el = $(s); if (el) { el.disabled = busy; el.setAttribute('aria-busy', busy); el.classList.toggle('busy', busy); }});
  document.querySelectorAll('.busy-text').forEach(el => el.hidden = !busy);
  const prog = $('#scan-progress');
  if (busy) {
    prog.style.display = '';
    resetPipeline();
    setPipeStep('pipe-scan', 'active', 'Starting...');
  } else {
    prog.style.display = 'none';
    $('#redact-alert').style.display = 'none';
  }
}

function setPipeStep(id, status, detail) {
  const el = $('#' + id);
  if (!el) return;
  el.dataset.status = status;
  const detailEl = el.querySelector('.pipe-detail');
  if (detailEl && detail !== undefined) detailEl.textContent = detail;
}

function setPipeDetail(id, html) {
  const el = document.querySelector('#' + id + ' .pipe-detail');
  if (el) el.innerHTML = html;
}

function resetPipeline() {
  ['pipe-scan','pipe-ocr','pipe-ai','pipe-save'].forEach(id => setPipeStep(id, 'pending', ''));
  $('#redact-alert').style.display = 'none';
  _redactWarningShown = false;
}

function updateScanProgress(job) {
  const s = job.status;
  if (s === 'scanning') {
    setPipeStep('pipe-scan', 'active');
    const n = job.pages_scanned || 0;
    if (n > 0) setPipeStep('pipe-scan', 'active', 'Scanning page ' + (n + 1) + '... (' + n + ' done)');
    else setPipeStep('pipe-scan', 'active', 'Scanning pages...');
  } else if (s === 'checking_privacy') {
    setPipeStep('pipe-scan', 'done', (job.pages_scanned || '?') + ' page(s) scanned');
    setPipeStep('pipe-ocr', 'active', 'Running local OCR...');
  } else if (s === 'analyzing') {
    setPipeStep('pipe-scan', 'done', (job.pages_scanned || '?') + ' page(s) scanned');
    // Mark OCR step based on redaction info
    if (job.redaction) {
      const r = job.redaction;
      if (r.status === 'clean') setPipeStep('pipe-ocr', 'done', 'No sensitive data found');
      else if (r.status === 'redacted') {
        setPipeStep('pipe-ocr', 'warning', r.count + ' region(s) redacted (' + (r.types||[]).join(', ') + ')');
      } else setPipeStep('pipe-ocr', 'error', 'Skipped: ' + (r.reason || 'unavailable'));
    } else {
      setPipeStep('pipe-ocr', 'skipped', 'Disabled or reckless mode');
    }
    setPipeStep('pipe-ai', 'active');
    const n = job.pages_scanned || 0;
    setPipeStep('pipe-ai', 'active', 'Analyzing ' + n + ' page' + (n > 1 ? 's' : '') + ' with AI...');
  } else if (s === 'saving') {
    setPipeStep('pipe-scan', 'done');
    setPipeStep('pipe-ai', 'done', 'Classification complete');
    setPipeStep('pipe-save', 'active', 'Saving documents...');
  }
}

let _redactWarningShown = false;
let _pollCancelled = false;
function showRedactWarning(job) {
  if (_redactWarningShown) return;
  _redactWarningShown = true;

  // Update pipeline: scan done, OCR shows results
  setPipeStep('pipe-scan', 'done', (job.pages_scanned || '?') + ' page(s) scanned');
  const r = job.redaction || {};
  if (r.status === 'clean') {
    setPipeStep('pipe-ocr', 'done', 'No sensitive data found');
  } else if (r.status === 'redacted') {
    setPipeStep('pipe-ocr', 'warning');
    setPipeDetail('pipe-ocr', r.count + ' region(s) redacted <a href="#" onclick="openRedactPreview(); return false" style="color:#FDE68A;text-decoration:underline">view</a>');
  } else {
    setPipeStep('pipe-ocr', 'error', 'Could not run');
  }

  // Show confirmation panel below pipeline
  const alertEl = $('#redact-alert');
  if (r.status === 'clean') {
    alertEl.className = 'redact-alert redact-clean';
    alertEl.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>' +
        '<strong>All clear</strong> \u2014 no sensitive patterns detected' +
      '</div>' +
      '<div style="display:flex;gap:8px">' +
        '<button class="btn" style="background:#22C55E;color:#fff;border:none;padding:8px 18px;font-size:13px;font-weight:700;border-radius:8px;cursor:pointer" onclick="confirmSend()">Send to AI</button>' +
        '<button class="btn" style="background:rgba(255,255,255,.08);color:#94A3B8;border:1px solid rgba(255,255,255,.12);padding:8px 18px;font-size:13px;font-weight:600;border-radius:8px;cursor:pointer" onclick="cancelSend()">Cancel</button>' +
      '</div>';
  } else if (r.status === 'redacted') {
    const types = (r.types || []).join(', ');
    // Store redacted page numbers for the preview
    _redactedPages = Object.keys(job.page_redactions || {}).map(Number).sort((a,b) => a - b);
    alertEl.className = 'redact-alert redact-redacted';
    alertEl.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>' +
        '<strong>' + r.count + ' region(s) redacted</strong>' +
        '<a href="#" onclick="openRedactPreview(); return false" style="font-size:12px;color:#93C5FD;margin-left:8px">View redactions \u2192</a>' +
      '</div>' +
      '<div style="font-size:12px;margin-bottom:8px;opacity:.85">Detected: ' + types + ' \u2014 blacked out before sending</div>' +
      '<div style="display:flex;gap:8px">' +
        '<button class="btn" style="background:#F59E0B;color:#fff;border:none;padding:8px 18px;font-size:13px;font-weight:700;border-radius:8px;cursor:pointer" onclick="confirmSend()">Send Redacted to AI</button>' +
        '<button class="btn" style="background:rgba(255,255,255,.08);color:#94A3B8;border:1px solid rgba(255,255,255,.12);padding:8px 18px;font-size:13px;font-weight:600;border-radius:8px;cursor:pointer" onclick="cancelSend()">Cancel</button>' +
      '</div>';
  } else {
    const reason = (r.reason || 'Unknown reason').replace(/</g, '&lt;');
    alertEl.className = 'redact-alert redact-warning';
    alertEl.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 9v4m0 4h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/></svg>' +
        '<strong>Redaction could not run</strong>' +
      '</div>' +
      '<div style="font-size:12px;margin-bottom:4px">' + reason + '</div>' +
      '<div style="font-size:12px;margin-bottom:8px;opacity:.8">Documents may be sent <strong>unredacted</strong> to the AI.</div>' +
      '<div style="display:flex;gap:8px">' +
        '<button class="btn" style="background:#EF4444;color:#fff;border:none;padding:8px 18px;font-size:13px;font-weight:700;border-radius:8px;cursor:pointer" onclick="cancelSend()">Cancel</button>' +
        '<button class="btn" style="background:rgba(255,255,255,.08);color:#94A3B8;border:1px solid rgba(255,255,255,.12);padding:8px 18px;font-size:13px;font-weight:600;border-radius:8px;cursor:pointer" onclick="confirmSend()">Send Anyway</button>' +
      '</div>';
  }
  alertEl.style.display = '';
}

function hideRedactWarning() {
  // NOTE: do NOT reset _redactWarningShown here — that flag prevents the
  // poll loop from re-showing the confirmation after cancel/confirm.
  // It is only reset by resetPipeline() at the start of a new scan.
  const el = $('#redact-alert');
  el.style.display = 'none';
  el.className = 'redact-alert';
}

async function confirmSend() {
  hideRedactWarning();
  setPipeStep('pipe-ocr', 'done');
  setPipeStep('pipe-ai', 'active', 'Sending to AI...');
  try { await fetch('/api/job/confirm', { method: 'POST' }); } catch(e) {}
}

async function cancelSend() {
  hideRedactWarning();
  _pollCancelled = true;
  try { await fetch('/api/job/cancel', { method: 'POST' }); } catch(e) {}
}

function pollJob() {
  _pollCancelled = false;
  return new Promise((resolve, reject) => {
    const interval = setInterval(async () => {
      // Cancel clicked — resolve immediately so the UI resets
      if (_pollCancelled) {
        clearInterval(interval);
        hideRedactWarning();
        refreshLog();
        resolve({status: 'cancelled'});
        return;
      }
      try {
        const res = await fetch('/api/job');
        const job = await res.json();
        if (job.status === 'scanning' || job.status === 'analyzing' || job.status === 'saving' || job.status === 'checking_privacy') {
          updateScanProgress(job);
          refreshLog();
          return; // keep polling
        }
        if (job.status === 'confirm_send') {
          showRedactWarning(job);
          return; // keep polling, waiting for user
        }
        clearInterval(interval);
        hideRedactWarning();
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
      const sel = $('#scanner-select');
      // Keep the first two default options, clear discovered ones
      while (sel.options.length > 2) sel.remove(2);
      data.scanners.forEach(s => {
        const opt = document.createElement('option');
        opt.value = s.ip;
        opt.textContent = s.name + ' (' + s.ip + ')';
        sel.appendChild(opt);
      });
      st.textContent = 'Found ' + data.scanners.length + ' scanner(s) \u2014 select one and click Connect';
      st.className = 'status connected';
      if (data.scanners.length === 1) {
        sel.value = data.scanners[0].ip;
        $('#scanner-ip').style.display = 'none';
      }
    } else if (data.ok) {
      st.textContent = 'No scanners found on the network.'; st.className = 'status error';
    } else {
      st.textContent = 'Error: ' + data.error; st.className = 'status error';
    }
  } catch(e) { st.textContent = 'Discovery failed: ' + e.message; st.className = 'status error'; }
  refreshLog();
}

function getScannerIP() {
  const sel = $('#scanner-select');
  if (sel.value === '__manual__') return $('#scanner-ip').value.trim();
  return sel.value;
}

function onScannerSelect() {
  const sel = $('#scanner-select');
  $('#scanner-ip').style.display = sel.value === '__manual__' ? '' : 'none';
}

async function connect() {
  const ip = getScannerIP();
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
  _lastScanFn = scanOnly; dismissError();
  setBusy(true, 'scanning'); $('#results-card').style.display = 'none';
  try {
    const res = await fetch('/api/scan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), classify: false}) });
    const start = await res.json();
    if (!start.ok) { showScanError({result: {error: start.error, error_type: 'app', hint: start.error}}); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { showScanError(job); }
    else if (job.status === 'duplicate') { alert('Duplicate detected: this document was previously saved as ' + (job.result.previous && job.result.previous.filename || 'unknown')); }
    else if (job.status === 'done' && job.result) { showResult({folder: 'unsorted', tags: [], filename: (job.result.output_path || '').split(/[/\\]/).pop(), summary: 'Saved without classification', path: job.result.output_path}); }
  } catch(e) { showScanError({result: {error: e.message, error_type: 'unknown', hint: 'Connection to the scanner service failed. Check that the app is running.'}}); }
  setBusy(false); refreshLog();
}

function doScan() { return currentMode === 'auto' ? doScanAuto() : doScanAssisted(); }

async function doScanAuto() {
  _lastScanFn = doScanAuto; dismissError();
  setBusy(true, 'scanning'); $('#results-card').style.display = 'none';
  try {
    const res = await fetch('/api/scan', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), classify: true}) });
    const start = await res.json();
    if (!start.ok) { showScanError({result: {error: start.error, error_type: 'app', hint: start.error}}); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { showScanError(job); }
    else if (job.status === 'duplicate') { alert('Duplicate detected: this document was previously saved as ' + (job.result.previous && job.result.previous.filename || 'unknown')); }
    else if (job.status === 'done' && job.result && job.result.classified) {
      const d = job.result;
      showResult({folder: d.category, tags: d.tags || [d.category], filename: d.filename, summary: d.summary, date: d.date, path: d.output_path, riskLevel: d.risk_level, risks: d.risks});
    }
  } catch(e) { showScanError({result: {error: e.message, error_type: 'unknown', hint: 'Connection to the scanner service failed. Check that the app is running.'}}); }
  setBusy(false); refreshLog();
}

async function doScanAssisted() {
  _lastScanFn = doScanAssisted; dismissError();
  setBusy(true, 'scanning'); $('#results-card').style.display = 'none';
  try {
    const res = await fetch('/api/scan-assisted', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(getScanParams()) });
    const start = await res.json();
    if (!start.ok) { showScanError({result: {error: start.error, error_type: 'app', hint: start.error}}); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { showScanError(job); }
    else if (job.status === 'duplicate') { alert('Duplicate detected: this document was previously saved as ' + (job.result.previous && job.result.previous.filename || 'unknown')); }
    else if (job.status === 'done' && job.result && job.result.ok) { showClassifyModal(job.result); }
  } catch(e) { showScanError({result: {error: e.message, error_type: 'unknown', hint: 'Connection to the scanner service failed. Check that the app is running.'}}); }
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
  const pathEl = $('#r-path');
  pathEl.innerHTML = '';
  const pathLink = document.createElement('a');
  pathLink.href = '#';
  pathLink.textContent = 'Saved to: ' + path;
  pathLink.title = 'Reveal in file manager';
  pathLink.style.cssText = 'color:inherit;text-decoration:underline;text-decoration-style:dotted;cursor:pointer;';
  pathLink.onclick = function(e) { e.preventDefault(); revealFile(path); };
  pathEl.appendChild(pathLink);
  pathEl.style.display = '';
  renderRisk($('#r-risk'), riskLevel, risks);
}

function showClassifyModal(data) {
  classifyPageCount = data.pages || 1;
  $('#classify-img').src = 'data:image/jpeg;base64,' + data.preview;
  $('#classify-summary').innerHTML = '<strong>' + _esc(data.summary || '') + '</strong><br>Date: ' + _esc(data.date || 'Unknown');
  $('#classify-fn').value = data.filename || '';

  // All AI-suggested tags start selected
  const aiTags = data.tags || [];
  selectedTags = new Set(aiTags);

  // Populate folder dropdown from all categories
  const folderSelect = $('#classify-folder');
  folderSelect.innerHTML = '';
  (data.all_categories || []).forEach(cat => {
    const opt = document.createElement('option');
    opt.value = cat;
    opt.textContent = cat;
    folderSelect.appendChild(opt);
  });
  folderSelect.value = data.category || 'other';

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

function copyLogs() {
  const text = $('#log').textContent;
  if (!text) return;
  navigator.clipboard.writeText(text).then(function() {
    const btn = $('#log-copy-btn');
    const label = $('#log-copy-label');
    btn.classList.add('copied');
    label.textContent = 'Copied!';
    setTimeout(function() { btn.classList.remove('copied'); label.textContent = 'Copy'; }, 2000);
  });
}

// ── Usage dashboard ────────────────────────────────────────────────
let _chartHistory = []; // cached for resize redraws
let _chartPts = [];     // cached point coords for tooltip hit-testing

function drawUsageChart(history) {
  _chartHistory = history;
  const canvas = $('#usage-chart');
  const empty = $('#usage-chart-empty');
  const tooltip = $('#usage-chart-tooltip');
  if (tooltip) tooltip.classList.remove('visible');
  if (!history || history.length === 0) {
    canvas.style.display = 'none'; empty.style.display = '';
    _chartPts = [];
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
  _chartPts = pts;

  // Horizontal grid lines
  ctx.strokeStyle = 'rgba(148,163,184,.08)';
  ctx.lineWidth = 1;
  const niceSteps = niceYSteps(maxVal);
  niceSteps.forEach(val => {
    const f = val / maxVal;
    if (f <= 0 || f > 1) return;
    const y = pad.t + ch - f * ch;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l + cw, y); ctx.stroke();
  });

  // Smooth curve helper
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

  // Area fill gradient
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

  // Main line
  smoothLine(pts);
  ctx.strokeStyle = '#818CF8';
  ctx.lineWidth = 2.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.stroke();

  // Dots with glow
  pts.forEach(p => {
    ctx.beginPath(); ctx.arc(p.x, p.y, 5, 0, Math.PI * 2); ctx.fillStyle = 'rgba(129,140,248,.2)'; ctx.fill();
    ctx.beginPath(); ctx.arc(p.x, p.y, 3, 0, Math.PI * 2); ctx.fillStyle = '#818CF8'; ctx.fill();
    ctx.beginPath(); ctx.arc(p.x, p.y, 1.5, 0, Math.PI * 2); ctx.fillStyle = '#C7D2FE'; ctx.fill();
  });

  // Y-axis labels (nice intervals)
  ctx.fillStyle = '#64748B';
  ctx.font = '10px ' + mono;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  [0, ...niceSteps].forEach(val => {
    const f = val / maxVal;
    if (f < 0 || f > 1.01) return;
    const y = pad.t + ch - f * ch;
    ctx.fillText(fmtAxis(val), pad.l - 8, y);
  });

  // X-axis time labels
  ctx.fillStyle = '#64748B';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  const labelStep = Math.max(1, Math.floor(pts.length / 6));
  pts.forEach((p, i) => {
    if (i % labelStep === 0 || i === pts.length - 1) ctx.fillText(p.time, p.x, pad.t + ch + 6);
  });
}

function niceYSteps(max) {
  if (max <= 0) return [0];
  const rough = max / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const nice = [1, 2, 2.5, 5, 10].find(n => n * mag >= rough) * mag;
  const steps = [];
  for (let v = nice; v < max * 1.01; v += nice) steps.push(Math.round(v));
  return steps;
}
function fmtAxis(v) { return v >= 1000000 ? (v/1e6).toFixed(1)+'M' : v >= 1000 ? (v/1000).toFixed(v>=10000?0:1)+'k' : v.toString(); }

// Chart tooltip on hover
(function() {
  const canvas = $('#usage-chart');
  const tooltip = $('#usage-chart-tooltip');
  if (!canvas || !tooltip) return;
  canvas.addEventListener('mousemove', function(e) {
    if (_chartPts.length === 0) { tooltip.classList.remove('visible'); return; }
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    let closest = null, minD = Infinity;
    _chartPts.forEach(p => { const d = Math.abs(p.x - mx); if (d < minD) { minD = d; closest = p; } });
    if (!closest || minD > 30) { tooltip.classList.remove('visible'); return; }
    tooltip.innerHTML = '<div class="tt-time">' + closest.time + '</div>' +
      '<div class="tt-row"><span class="tt-dot" style="background:#818CF8"></span>In: ' + fmtNum(closest.input) + '</div>' +
      '<div class="tt-row"><span class="tt-dot" style="background:#34D399"></span>Out: ' + fmtNum(closest.output) + '</div>' +
      '<div class="tt-row" style="color:#F1F5F9;font-weight:600;margin-top:2px">Total: ' + fmtNum(closest.cumulative) + '</div>';
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    let tx = closest.x - tw / 2, ty = closest.y - th - 12;
    if (tx < 0) tx = 4; if (tx + tw > rect.width) tx = rect.width - tw - 4;
    if (ty < 0) ty = closest.y + 16;
    tooltip.style.left = tx + 'px'; tooltip.style.top = ty + 'px';
    tooltip.classList.add('visible');
  });
  canvas.addEventListener('mouseleave', function() { tooltip.classList.remove('visible'); });
})();

// Redraw chart on resize
let _resizeTimer;
window.addEventListener('resize', function() {
  clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(function() { drawUsageChart(_chartHistory); }, 150);
});

function fmtNum(n) { return n >= 1000000 ? (n/1e6).toFixed(2)+'M' : n >= 1000 ? (n/1000).toFixed(1)+'k' : n.toLocaleString(); }
function fmtCost(c) { return c >= 0.01 ? '$' + c.toFixed(4) : c > 0 ? '$' + c.toFixed(4) : '$0.00'; }

function toggleUsageDash() {
  const dash = $('#usage-dash');
  dash.classList.toggle('collapsed');
  const btn = dash.querySelector('.usage-btn-collapse');
  btn.setAttribute('aria-expanded', !dash.classList.contains('collapsed'));
}

async function resetUsage() {
  if (!confirm('Reset all usage counters for today?')) return;
  try { await fetch('/api/usage/reset', { method: 'POST' }); refreshUsage(); } catch(e) {}
}

let _usageIdle = true;
async function refreshUsage() {
  try {
    const res = await fetch('/api/usage');
    const u = await res.json();
    const maxBudget = parseFloat($('#daily-budget').value) || 0;
    const dash = $('#usage-dash');

    // Hero values
    const hasActivity = u.total_tokens > 0;
    $('#usage-tokens').textContent = hasActivity ? fmtNum(u.total_tokens) : '--';
    $('#usage-tokens-detail').textContent = hasActivity ? fmtNum(u.input_tokens) + ' in / ' + fmtNum(u.output_tokens) + ' out' : 'no activity yet';
    $('#usage-cost').textContent = fmtCost(u.estimated_cost);
    $('#usage-cost-detail').textContent = hasActivity ? '$3/1M in, $15/1M out' : '$3/1M in, $15/1M out';
    $('#usage-calls').textContent = u.api_calls;
    $('#usage-calls-detail').textContent = u.api_calls === 0 ? 'today' : u.api_calls === 1 ? '1 call today' : u.api_calls + ' calls today';

    // Budget hero + bar
    const budgetSection = $('#usage-budget-section');
    const budgetVal = $('#usage-budget-val');
    const budgetDetail = $('#usage-budget-detail');
    if (maxBudget > 0) {
      const spent = u.estimated_cost || 0;
      const pct = Math.min(100, (spent / maxBudget) * 100);
      budgetVal.textContent = Math.round(pct) + '%';
      budgetDetail.textContent = pct >= 100 ? 'exceeded!' : '$' + (maxBudget - spent).toFixed(2) + ' remaining';
      budgetSection.classList.remove('hidden');
      budgetSection.setAttribute('aria-valuenow', Math.round(pct));
      const fill = $('#usage-budget-fill');
      fill.style.width = pct + '%';
      fill.className = 'usage-budget-fill' + (pct >= 100 ? ' over' : pct >= 90 ? ' critical' : pct >= 75 ? ' warn' : '');
      dash.classList.toggle('over-budget', pct >= 100);
      $('#usage-budget-left').textContent = '$' + spent.toFixed(2) + ' of $' + maxBudget.toFixed(2);
      const right = $('#usage-budget-right');
      right.innerHTML = pct >= 100 ? '<span class="over">Budget exceeded</span>' : Math.round(pct) + '% used';
    } else {
      budgetVal.textContent = 'Unlimited';
      budgetDetail.textContent = 'no cap set';
      budgetSection.classList.add('hidden');
      dash.classList.remove('over-budget');
    }

    drawUsageChart(u.history || []);
    _usageIdle = !u.api_calls;
  } catch(e) {}
}
// Adaptive polling: 5s during activity, 15s when idle
setInterval(function() { refreshUsage(); }, 5000);
let _slowPoll = setInterval(function() {}, 99999);
(function adaptivePoll() {
  clearInterval(_slowPoll);
  // The 5s fast poll already covers active use; no extra logic needed
  // But we skip re-fetching in the 5s poll if idle by making refreshUsage cheap (it already is)
})();

// ── Classify / Batch scan ───────────────────────────────────────────
let classifyPageCount = 0; // Number of pages in single-doc classify mode
let batchData = [];   // Array of doc objects from API
let batchTags = [];   // Array of Sets, per document
let batchPages = [];  // Array of arrays of 1-indexed page numbers
let allPreviews = []; // Base64 thumbs for every scanned page
let pageRedactions = {}; // {pageNum: {count, types}} from OCR privacy check

async function doBatchScan() {
  _lastScanFn = doBatchScan; dismissError();
  setBusy(true, 'scanning');
  $('#results-card').style.display = 'none';
  $('#batch-results-card').style.display = 'none';
  try {
    // Batch always shows review modal for page rearrangement
    const res = await fetch('/api/scan-batch', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({...getScanParams(), auto: false}) });
    const start = await res.json();
    if (!start.ok) { showScanError({result: {error: start.error, error_type: 'app', hint: start.error}}); setBusy(false); return; }
    const job = await pollJob();
    if (job.status === 'error') { showScanError(job); }
    else if (job.status === 'done' && job.result && job.result.batch) {
      showBatchModal(job.result);
    }
  } catch(e) { showScanError({result: {error: e.message, error_type: 'unknown', hint: 'Connection to the scanner service failed. Check that the app is running.'}}); }
  setBusy(false); refreshLog();
}

function revealFile(path) {
  fetch('/api/reveal', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({path}) }).catch(() => {});
}

function showBatchResults(docs) {
  const card = $('#batch-results-card');
  card.style.display = '';
  $('#batch-results-title').textContent = 'Batch Complete \u2014 ' + docs.length + ' Document' + (docs.length !== 1 ? 's' : '');
  const list = $('#batch-results-list');
  list.innerHTML = '';
  docs.forEach(doc => {
    const li = document.createElement('li');
    const name = doc.filename || (doc.output_path || '').split(/[/\\]/).pop() || 'document';
    const detail = [doc.folder || doc.category, doc.summary].filter(Boolean).join(' \u2014 ');
    const path = doc.output_path || '';

    const link = document.createElement('a');
    link.className = 'br-name br-link';
    link.href = '#';
    link.title = 'Reveal in file manager';
    link.textContent = name + ' ';
    link.insertAdjacentHTML('beforeend', '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px;opacity:.5"><path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>');
    link.addEventListener('click', e => { e.preventDefault(); if (path) revealFile(path); });
    li.appendChild(link);

    if (detail) {
      const br = document.createElement('br');
      const span = document.createElement('span');
      span.className = 'br-detail';
      span.textContent = detail;
      li.appendChild(br);
      li.appendChild(span);
    }
    list.appendChild(li);
  });
}

function showBatchModal(result) {
  const docs = result.documents;
  allPreviews = result.all_previews || [];
  pageRedactions = result.page_redactions || {};
  batchData = docs;
  batchTags = docs.map(d => new Set(d.tags || []));
  batchPages = docs.map(d => [...(d.pages || [])]);
  renderBatchDocs();
  openModal('#batch-modal');
}

function syncBatchEdits() {
  // Sync any user-edited filenames/folders back into batchData before re-rendering
  batchData.forEach((doc, i) => {
    const fnEl = $('#batch-fn-' + i), folderEl = $('#batch-folder-' + i);
    if (fnEl) doc.filename = fnEl.value;
    if (folderEl) doc.category = folderEl.value;
  });
}

function renderBatchDocs(skipSync) {
  if (!skipSync) syncBatchEdits();

  const docsWithPages = batchPages.filter(p => p.length > 0).length;
  $('#batch-count').textContent = docsWithPages;
  const container = $('#batch-docs');
  container.innerHTML = '';
  const esc = s => { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; };
  const numDocs = batchData.length;

  // Build short display names for each document
  function docShortName(doc, idx) {
    const fn = doc.filename || '';
    // Strip .pdf, strip leading date (YYYY-MM-DD_), replace underscores with spaces, capitalize
    let name = fn.replace(/\.pdf$/i, '').replace(/^\d{4}-\d{2}-\d{2}_/, '').replace(/_/g, ' ').trim();
    if (name.length > 40) name = name.substring(0, 40) + '\u2026';
    return name || ('Document ' + (idx + 1));
  }

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
        moveOpts += '<option value="' + d + '"' + (d === i ? ' selected' : '') + '>' + esc(docShortName(batchData[d], d)) + '</option>';
      }
      moveOpts += '<option value="new">+ New doc</option>';
      const pc = (doc.page_confidence && doc.page_confidence[pNum]) || doc.confidence || 100;
      const pcClass = pc >= 85 ? 'high' : pc >= 65 ? 'med' : 'low';
      const pcBadge = pc < 100 ? '<span class="page-confidence ' + pcClass + '">' + pc + '%</span>' : '';
      const pr = pageRedactions[pNum];
      const ocrBadge = pr ? '<span class="ocr-badge" title="' + pr.count + ' region(s) redacted: ' + (pr.types||[]).join(', ') + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>OCR</span>' : '';
      pagesHtml += '<div class="batch-page" draggable="true" data-page="' + pNum + '" data-doc="' + i + '">' +
        pcBadge + ocrBadge +
        '<div class="page-actions">' +
          '<button class="page-action-btn" onclick="rotatePage(' + pNum + ',' + i + ')" title="Rotate 90\u00b0">\u21bb</button>' +
          '<button class="page-action-btn" onclick="deletePage(' + pNum + ',' + i + ')" title="Delete page">\u2715</button>' +
        '</div>' +
        '<img src="data:image/jpeg;base64,' + preview + '" alt="Page ' + pNum + '" onclick="openLightbox(' + pNum + ',' + i + ')" title="Click to preview" style="cursor:zoom-in" draggable="false">' +
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

    const fn = doc.filename || '';
    const folder = doc.category || 'other';

    const conf = doc.confidence || 100;
    const confClass = conf >= 85 ? 'high' : conf >= 65 ? 'med' : 'low';
    // Count how many pages in this document were redacted by OCR
    const docRedactedPages = pages.filter(p => pageRedactions[p]).length;
    const ocrDocBadge = docRedactedPages > 0
      ? '<span class="ocr-doc-badge" title="' + docRedactedPages + ' page(s) had sensitive data redacted by local OCR"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>OCR protected</span>'
      : '';
    card.innerHTML =
      '<div class="batch-doc-head">' +
        '<div class="batch-doc-title">' +
          '<span class="batch-doc-label">' + esc(docShortName(doc, i)) + (doc.summary ? ' \u2014 ' + esc(doc.summary) : '') + '</span>' +
          '<span class="confidence-badge confidence-' + confClass + '" title="AI confidence in document grouping">' + conf + '%</span>' +
          ocrDocBadge +
          '<button class="btn-remove-doc" onclick="removeBatchDoc(' + i + ')">Remove</button>' +
        '</div>' +
      '</div>' +
      '<div class="batch-page-grid" data-doc="' + i + '">' + pagesHtml + '</div>' +
      '<div class="batch-fields">' +
        '<label>Filename</label><input type="text" id="batch-fn-' + i + '" value="' + esc(fn) + '">' +
        '<label>Folder</label><select id="batch-folder-' + i + '">' + (doc.all_categories || []).map(function(cat) { return '<option value="' + esc(cat) + '"' + (cat === folder ? ' selected' : '') + '>' + esc(cat) + '</option>'; }).join('') + '</select>' +
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
  const pages = batchPages[idx] || [];
  if (pages.length > 0) {
    if (!confirm('Delete this document and its ' + pages.length + ' page(s)? The pages will be removed.')) return;
  }
  syncBatchEdits();
  batchData.splice(idx, 1);
  batchTags.splice(idx, 1);
  batchPages.splice(idx, 1);
  renderBatchDocs(true);
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
  syncBatchEdits();
  const documents = [];
  batchData.forEach((doc, i) => {
    if (!batchPages[i] || batchPages[i].length === 0) return;
    documents.push({
      pages: batchPages[i],
      folder: doc.category || 'other',
      tags: [...(batchTags[i] || [])],
      filename: doc.filename,
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

// ── Error handling ─────────────────────────────────────────────────
let _lastScanFn = null; // Store last scan function for retry

function showScanError(job) {
  const result = job.result || {};
  const errorType = result.error_type || 'unknown';
  const icons = {jam: '\u26d4', empty: '\ud83d\udce5', mispick: '\u26a0\ufe0f', busy: '\u23f3', stopped: '\ud83d\uded1', scanner: '\u26a0\ufe0f', app: '\u2139\ufe0f', unknown: '\u274c'};
  const titles = {jam: 'Paper Jam', empty: 'Feeder Empty', mispick: 'Paper Mispick', busy: 'Scanner Busy', stopped: 'Scanner Stopped', scanner: 'Scanner Error', app: 'Error', unknown: 'Error'};

  $('#error-card-icon').textContent = icons[errorType] || icons.unknown;
  $('#error-card-title').textContent = titles[errorType] || 'Scanner Error';
  $('#error-card-message').textContent = result.error || 'Unknown error';
  $('#error-card-hint').textContent = result.hint || 'Check the scanner and try again.';

  const stateEl = $('#error-card-state');
  if (result.scanner_state) {
    stateEl.textContent = 'Scanner: ' + result.scanner_state + (result.adf_state ? ' | ADF: ' + result.adf_state : '');
    stateEl.style.display = '';
  } else {
    stateEl.style.display = 'none';
  }

  $('#error-card').style.display = '';
  $('#error-card').scrollIntoView({behavior: 'smooth', block: 'center'});
}

function dismissError() {
  $('#error-card').style.display = 'none';
}

function retryLastScan() {
  dismissError();
  if (_lastScanFn) _lastScanFn();
}

function scanNext() {
  dismissError();
  $('#results-card').style.display = 'none';
  $('#batch-results-card').style.display = 'none';
  // Reset result fields
  ['#r-folder','#r-tags','#r-filename','#r-summary','#r-date'].forEach(s => { const el = $(s); if (el) el.textContent = '--'; });
  const riskEl = $('#r-risk'); if (riskEl) { riskEl.style.display = 'none'; riskEl.innerHTML = ''; }
  const pathEl = $('#r-path'); if (pathEl) { pathEl.style.display = 'none'; pathEl.innerHTML = ''; }
  $('#batch-results-list').innerHTML = '';
  // Clear stale data from previous scan
  classifyPageCount = 0;
  batchData = [];
  batchPages = [];
  batchTags = [];
  allPreviews = [];
  pageRedactions = {};
  window.scrollTo({top: 0, behavior: 'smooth'});
}

// ── Lightbox (fullscreen page preview) ──────────────────────────────
let lightboxPages = []; // Ordered list of page numbers available in lightbox
let lightboxIdx = 0;    // Current index within lightboxPages
let lightboxDocIdx = null; // Which document owns the current lightbox view

function openLightbox(pageNum, docIdx) {
  lightboxDocIdx = docIdx != null ? docIdx : null;
  // Navigate only within the document's own pages
  if (docIdx != null && batchPages[docIdx]) {
    lightboxPages = [...batchPages[docIdx]].sort((a, b) => a - b);
  } else if (batchPages.length > 0) {
    // Batch mode fallback: all pages across documents
    lightboxPages = [];
    batchPages.forEach(pages => pages.forEach(p => { if (!lightboxPages.includes(p)) lightboxPages.push(p); }));
    lightboxPages.sort((a, b) => a - b);
  } else if (classifyPageCount > 0) {
    // Single-doc classify mode: pages 1..N
    lightboxPages = Array.from({length: classifyPageCount}, (_, i) => i + 1);
  } else {
    lightboxPages = [1];
  }
  lightboxIdx = lightboxPages.indexOf(pageNum);
  if (lightboxIdx === -1) lightboxIdx = 0;
  showLightboxPage();
  $('#lightbox').classList.add('active'); mainContent.setAttribute('inert', '');
}

function showLightboxPage() {
  const pNum = lightboxPages[lightboxIdx];
  $('#lightbox-img').src = '/api/page-image/' + pNum + '?t=' + Date.now();
  $('#lightbox-img').alt = 'Page ' + pNum;
  $('#lightbox-label').textContent = 'Page ' + pNum + ' (' + (lightboxIdx + 1) + '/' + lightboxPages.length + ')';
  // Show toolbar only when there are pending images to edit (batch or classify mode)
  const showTools = batchPages.length > 0 || classifyPageCount > 0;
  $('#lightbox-toolbar').style.display = showTools ? 'flex' : 'none';
  lightboxCancelCrop();
}

function closeLightbox() {
  lightboxCancelCrop();
  $('#lightbox').classList.remove('active');
  if (!document.querySelector('.modal-overlay.active')) mainContent.removeAttribute('inert');
}

function lightboxNav(dir) {
  lightboxIdx = (lightboxIdx + dir + lightboxPages.length) % lightboxPages.length;
  showLightboxPage();
}

// ── Page editing (rotate, crop, delete) ──────────────────────────
async function rotatePage(pageNum, docIdx) {
  try {
    const res = await fetch('/api/rotate-page', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({page_num: pageNum, degrees: 90}) });
    const data = await res.json();
    if (data.ok && data.preview) {
      allPreviews[pageNum - 1] = data.preview;
      renderBatchDocs();
    }
  } catch(e) {}
}

function deletePage(pageNum, docIdx) {
  if (!confirm('Delete page ' + pageNum + '?')) return;
  const idx = batchPages[docIdx].indexOf(pageNum);
  if (idx !== -1) batchPages[docIdx].splice(idx, 1);
  renderBatchDocs();
}

async function lightboxRotate() {
  const pNum = lightboxPages[lightboxIdx];
  try {
    const res = await fetch('/api/rotate-page', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({page_num: pNum, degrees: 90}) });
    const data = await res.json();
    if (data.ok) {
      if (data.preview) allPreviews[pNum - 1] = data.preview;
      showLightboxPage();
      if (batchPages.length > 0) renderBatchDocs();
    }
  } catch(e) {}
}

function lightboxDelete() {
  const pNum = lightboxPages[lightboxIdx];
  if (!confirm('Delete page ' + pNum + '?')) return;
  // Remove from whichever document owns it
  if (lightboxDocIdx != null && batchPages[lightboxDocIdx]) {
    const idx = batchPages[lightboxDocIdx].indexOf(pNum);
    if (idx !== -1) batchPages[lightboxDocIdx].splice(idx, 1);
  } else {
    // Find it in any doc
    for (let d = 0; d < batchPages.length; d++) {
      const idx = batchPages[d].indexOf(pNum);
      if (idx !== -1) { batchPages[d].splice(idx, 1); break; }
    }
  }
  lightboxPages.splice(lightboxIdx, 1);
  if (lightboxPages.length === 0) {
    closeLightbox();
  } else {
    if (lightboxIdx >= lightboxPages.length) lightboxIdx = lightboxPages.length - 1;
    showLightboxPage();
  }
  if (batchPages.length > 0) renderBatchDocs();
}

// ── Crop ──────────────────────────────────────────────────────────
let _cropping = false;
let _cropStart = null;
let _cropBox = null;

function lightboxStartCrop() {
  _cropping = true;
  _cropBox = null;
  const overlay = $('#crop-overlay');
  const box = $('#crop-box');
  overlay.style.display = 'block';
  box.style.cssText = 'display:none';
  $('#lightbox-crop-bar').style.display = 'flex';
  $('#btn-crop').style.background = 'rgba(59,130,246,.6)';

  overlay.onmousedown = function(e) {
    const rect = overlay.getBoundingClientRect();
    _cropStart = {x: e.clientX - rect.left, y: e.clientY - rect.top};
    box.style.display = 'block';
    box.style.left = _cropStart.x + 'px';
    box.style.top = _cropStart.y + 'px';
    box.style.width = '0'; box.style.height = '0';
  };
  overlay.onmousemove = function(e) {
    if (!_cropStart) return;
    const rect = overlay.getBoundingClientRect();
    const cx = Math.max(0, Math.min(e.clientX - rect.left, rect.width));
    const cy = Math.max(0, Math.min(e.clientY - rect.top, rect.height));
    const x = Math.min(_cropStart.x, cx), y = Math.min(_cropStart.y, cy);
    const w = Math.abs(cx - _cropStart.x), h = Math.abs(cy - _cropStart.y);
    box.style.left = x + 'px'; box.style.top = y + 'px';
    box.style.width = w + 'px'; box.style.height = h + 'px';
    _cropBox = {x, y, w, h, ow: rect.width, oh: rect.height};
  };
  overlay.onmouseup = function() { _cropStart = null; };
}

function lightboxCancelCrop() {
  _cropping = false; _cropStart = null; _cropBox = null;
  const overlay = $('#crop-overlay');
  if (overlay) { overlay.style.display = 'none'; overlay.onmousedown = null; overlay.onmousemove = null; overlay.onmouseup = null; }
  const bar = $('#lightbox-crop-bar');
  if (bar) bar.style.display = 'none';
  const btn = $('#btn-crop');
  if (btn) btn.style.background = '';
}

async function lightboxApplyCrop() {
  if (!_cropBox || _cropBox.w < 5 || _cropBox.h < 5) { alert('Draw a crop area first.'); return; }
  const pNum = lightboxPages[lightboxIdx];
  const left = _cropBox.x / _cropBox.ow;
  const top = _cropBox.y / _cropBox.oh;
  const right = (_cropBox.x + _cropBox.w) / _cropBox.ow;
  const bottom = (_cropBox.y + _cropBox.h) / _cropBox.oh;
  try {
    const res = await fetch('/api/crop-page', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({page_num: pNum, left, top, right, bottom}) });
    const data = await res.json();
    if (data.ok) {
      if (data.preview) allPreviews[pNum - 1] = data.preview;
      lightboxCancelCrop();
      showLightboxPage();
      if (batchPages.length > 0) renderBatchDocs();
    } else { alert('Crop failed: ' + data.error); }
  } catch(e) { alert('Crop failed: ' + e.message); }
}

// ── Redaction preview (before/after comparison) ────────────────────
let _redactedPages = []; // page numbers that have redactions
let _redactPreviewIdx = 0;

function openRedactPreview() {
  if (_redactedPages.length === 0) return;
  _redactPreviewIdx = 0;
  showRedactPreviewPage();
  $('#redact-preview').classList.add('active');
  mainContent.setAttribute('inert', '');
}

function closeRedactPreview() {
  $('#redact-preview').classList.remove('active');
  if (!document.querySelector('.modal-overlay.active') && !$('#lightbox').classList.contains('active'))
    mainContent.removeAttribute('inert');
}

function showRedactPreviewPage() {
  const pNum = _redactedPages[_redactPreviewIdx];
  $('#redact-preview-orig').src = '/api/original-image/' + pNum;
  $('#redact-preview-redacted').src = '/api/redacted-image/' + pNum;
  $('#redact-preview-info').textContent = 'Page ' + pNum + ' (' + (_redactPreviewIdx + 1) + '/' + _redactedPages.length + ')';
}

function redactPreviewNav(dir) {
  _redactPreviewIdx = (_redactPreviewIdx + dir + _redactedPages.length) % _redactedPages.length;
  showRedactPreviewPage();
}

$('#redact-preview').addEventListener('click', e => { if (e.target === $('#redact-preview')) closeRedactPreview(); });

// Click image to zoom fullscreen, click again to unzoom
document.querySelectorAll('#redact-preview .redact-preview-pane img').forEach(img => {
  img.addEventListener('click', e => {
    e.stopPropagation();
    if (img.classList.contains('zoomed')) {
      img.classList.remove('zoomed');
    } else {
      // Unzoom any other zoomed image first
      document.querySelectorAll('#redact-preview img.zoomed').forEach(z => z.classList.remove('zoomed'));
      img.classList.add('zoomed');
    }
  });
});
// Escape also unzooms
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const zoomed = document.querySelector('#redact-preview img.zoomed');
    if (zoomed) { zoomed.classList.remove('zoomed'); e.stopImmediatePropagation(); }
  }
}, true);

// Click lightbox background to close (but not on image or buttons)
$('#lightbox').addEventListener('click', e => { if (e.target === $('#lightbox') && !_cropping) closeLightbox(); });

// Keyboard: Escape closes modals / lightbox, arrows navigate lightbox
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if ($('#redact-preview').classList.contains('active')) { closeRedactPreview(); }
    else if ($('#lightbox').classList.contains('active')) { closeLightbox(); }
    else if ($('#batch-modal').classList.contains('active')) { cancelBatch(); }
    else if ($('#classify-modal').classList.contains('active')) { cancelClassify(); }
    else if ($('#api-key-modal').classList.contains('active')) { closeApiModal(); }
  }
  if ($('#redact-preview').classList.contains('active')) {
    if (e.key === 'ArrowLeft') redactPreviewNav(-1);
    if (e.key === 'ArrowRight') redactPreviewNav(1);
  } else if ($('#lightbox').classList.contains('active')) {
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
    global _server_port
    port = 8470
    _server_port = port

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
