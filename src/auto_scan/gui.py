"""Web-based GUI for auto-scan using Flask."""

from __future__ import annotations

import os
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template_string, request

from auto_scan import AutoScanError
from auto_scan.analyzer import DocumentInfo, analyze_document
from auto_scan.config import Config, load_config
from auto_scan.organizer import save_document, save_unclassified
from auto_scan.scanner.discovery import ScannerInfo, discover_scanner, scanner_info_from_ip
from auto_scan.scanner.escl import ESCLClient, ScanSettings

app = Flask(__name__)

# ── App state ────────────────────────────────────────────────────────

state = {
    "scanner_info": None,
    "config": None,
    "logs": [],
}


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    state["logs"].append(f"[{ts}] {msg}")
    if len(state["logs"]) > 200:
        state["logs"] = state["logs"][-200:]


def _get_config(**overrides) -> Config:
    return load_config(**overrides)


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

    # Find .env file
    env_path = None
    for candidate in [Path(".env"), Path(__file__).resolve().parents[2] / ".env"]:
        if candidate.exists():
            env_path = candidate
            break
    if env_path is None:
        env_path = Path(".env")

    # Update or create .env
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
            "ok": True,
            "name": info.name,
            "ip": info.ip,
            "state": status.state,
            "adf": status.adf_state,
            "sources": caps.sources,
            "resolutions": caps.resolutions,
            "color_modes": caps.color_modes,
        })
    except Exception as e:
        _log(f"Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/scan", methods=["POST"])
def api_scan():
    data = request.json or {}
    classify = data.get("classify", True)
    source = data.get("source", "Feeder")
    resolution = int(data.get("resolution", 300))
    color = data.get("color", "RGB24")
    output_dir = data.get("output_dir", "")

    try:
        overrides = {
            "scan_source": source,
            "resolution": resolution,
            "color_mode": color,
        }
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
                source=source,
                color_mode=color,
                resolution=resolution,
                document_format=config.scan_format,
            )
            images = client.scan(settings)

        _log(f"Scanned {len(images)} page(s)")

        result = {"ok": True, "pages": len(images)}

        if classify:
            _log("Analyzing with Claude Vision...")
            doc_info = analyze_document(images, config)
            _log(f"Classified as: {doc_info.category}")

            output_path = save_document(images, doc_info, config)
            _log(f"Saved: {output_path}")

            result.update({
                "classified": True,
                "category": doc_info.category,
                "filename": doc_info.filename,
                "summary": doc_info.summary,
                "date": doc_info.date,
                "key_fields": doc_info.key_fields,
                "output_path": str(output_path),
            })
        else:
            output_path = save_unclassified(images, config)
            _log(f"Saved: {output_path}")
            result.update({
                "classified": False,
                "output_path": str(output_path),
            })

        return jsonify(result)

    except Exception as e:
        _log(f"Error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


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
  :root { --bg: #f8f9fa; --card: #fff; --border: #dee2e6; --primary: #0d6efd; --primary-hover: #0b5ed7; --gray: #6c757d; --green: #198754; --red: #dc3545; --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; --mono: "SF Mono", Menlo, Monaco, monospace; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: var(--font); background: var(--bg); color: #212529; line-height: 1.5; }
  .container { max-width: 740px; margin: 0 auto; padding: 24px 16px; }
  h1 { font-size: 24px; font-weight: 700; margin-bottom: 20px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 20px; margin-bottom: 16px; }
  .card h2 { font-size: 15px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--gray); margin-bottom: 12px; }
  label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 4px; color: #495057; }
  input[type="text"], select { width: 100%; padding: 8px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; font-family: var(--font); }
  input:focus, select:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(13,110,253,.15); }
  .row { display: flex; gap: 12px; margin-bottom: 10px; }
  .row > * { flex: 1; }
  .radio-group { display: flex; gap: 16px; padding: 6px 0; }
  .radio-group label { display: flex; align-items: center; gap: 6px; font-weight: 400; cursor: pointer; }
  .btn-row { display: flex; gap: 10px; }
  .btn { display: inline-flex; align-items: center; justify-content: center; padding: 10px 20px; border: none; border-radius: 8px; font-size: 15px; font-weight: 600; font-family: var(--font); cursor: pointer; transition: background .15s; width: 100%; }
  .btn:disabled { opacity: .5; cursor: not-allowed; }
  .btn-primary { background: var(--primary); color: #fff; }
  .btn-primary:hover:not(:disabled) { background: var(--primary-hover); }
  .btn-secondary { background: #e9ecef; color: #495057; }
  .btn-secondary:hover:not(:disabled) { background: #dee2e6; }
  .btn-connect { padding: 8px 16px; width: auto; font-size: 14px; }
  .connect-row { display: flex; gap: 8px; align-items: flex-end; }
  .connect-row > :first-child { flex: 1; }
  .status { font-size: 13px; padding: 6px 0; }
  .status.connected { color: var(--green); font-weight: 600; }
  .status.disconnected { color: var(--gray); }
  .status.error { color: var(--red); }
  .results-grid { display: grid; grid-template-columns: 100px 1fr; gap: 4px 12px; font-size: 14px; }
  .results-grid dt { font-weight: 600; color: #495057; }
  .results-grid dd { color: #212529; word-break: break-word; }
  .log { background: #1e1e1e; color: #d4d4d4; border-radius: 8px; padding: 12px; font-family: var(--mono); font-size: 12px; height: 180px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }
  .spinner { display: none; width: 18px; height: 18px; border: 2px solid #fff4; border-top-color: #fff; border-radius: 50%; animation: spin .6s linear infinite; margin-right: 8px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .busy .spinner { display: inline-block; }
  .output-path { margin-top: 10px; padding: 8px 12px; background: #d1e7dd; border-radius: 6px; font-size: 13px; word-break: break-all; }
  /* Modal */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.active { display: flex; }
  .modal { background: #fff; border-radius: 14px; padding: 28px; width: 480px; max-width: 90vw; box-shadow: 0 12px 40px rgba(0,0,0,.2); }
  .modal h2 { font-size: 18px; font-weight: 700; margin-bottom: 8px; color: #212529; text-transform: none; letter-spacing: 0; }
  .modal p { font-size: 14px; color: var(--gray); margin-bottom: 16px; line-height: 1.6; }
  .modal a { color: var(--primary); }
  .modal input[type="password"] { width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 6px; font-size: 14px; font-family: var(--mono); margin-bottom: 6px; }
  .modal input[type="password"]:focus { outline: none; border-color: var(--primary); box-shadow: 0 0 0 3px rgba(13,110,253,.15); }
  .modal-btns { display: flex; gap: 8px; justify-content: flex-end; margin-top: 16px; }
  .modal-btns .btn { width: auto; }
  .modal-error { color: var(--red); font-size: 13px; min-height: 20px; }
</style>
</head>
<body>
<div class="container">
  <h1>Auto-Scan</h1>

  <!-- Scanner Connection -->
  <div class="card">
    <h2>Scanner</h2>
    <div class="connect-row">
      <div>
        <label for="scanner-ip">Scanner IP (leave blank for auto-discover)</label>
        <input type="text" id="scanner-ip" placeholder="192.168.1.x">
      </div>
      <button class="btn btn-primary btn-connect" onclick="connect()">Connect</button>
    </div>
    <div class="status disconnected" id="scanner-status">Not connected</div>
  </div>

  <!-- Settings -->
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
      <div>
        <label for="resolution">Resolution</label>
        <select id="resolution">
          <option value="150">150 DPI</option>
          <option value="200">200 DPI</option>
          <option value="300" selected>300 DPI</option>
          <option value="600">600 DPI</option>
        </select>
      </div>
      <div>
        <label for="color">Color Mode</label>
        <select id="color">
          <option value="RGB24">Color</option>
          <option value="Grayscale8">Grayscale</option>
        </select>
      </div>
    </div>
    <div>
      <label for="output-dir">Output Directory</label>
      <input type="text" id="output-dir" value="">
    </div>
  </div>

  <!-- Action Buttons -->
  <div class="card">
    <div class="btn-row">
      <button class="btn btn-primary" id="btn-classify" onclick="scan(true)" disabled>
        <span class="spinner"></span>Scan &amp; Classify
      </button>
      <button class="btn btn-secondary" id="btn-scan" onclick="scan(false)" disabled>
        <span class="spinner"></span>Scan Only
      </button>
    </div>
  </div>

  <!-- Results -->
  <div class="card" id="results-card" style="display:none">
    <h2>Classification Results</h2>
    <dl class="results-grid">
      <dt>Category</dt><dd id="r-category">--</dd>
      <dt>Filename</dt><dd id="r-filename">--</dd>
      <dt>Summary</dt><dd id="r-summary">--</dd>
      <dt>Date</dt><dd id="r-date">--</dd>
    </dl>
    <div class="output-path" id="r-path" style="display:none"></div>
  </div>

  <!-- Log -->
  <div class="card">
    <h2>Activity Log</h2>
    <div class="log" id="log"></div>
  </div>
</div>

<!-- API Key Modal -->
<div class="modal-overlay" id="api-key-modal">
  <div class="modal">
    <h2>Anthropic API Key Required</h2>
    <p>An API key is needed for AI document classification.<br>
       Get one at <a href="https://console.anthropic.com" target="_blank">console.anthropic.com</a></p>
    <label for="api-key-input">API Key</label>
    <input type="password" id="api-key-input" placeholder="sk-ant-...">
    <div class="modal-error" id="api-key-error"></div>
    <div class="modal-btns">
      <button class="btn btn-secondary" onclick="closeModal()">Skip</button>
      <button class="btn btn-primary" onclick="saveApiKey()">Save</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);

// Init: check API key and show modal if missing
(async function init() {
  if (!$('#output-dir').value) {
    $('#output-dir').value = '~/Documents/Scans';
  }
  try {
    const res = await fetch('/api/config');
    const data = await res.json();
    if (!data.has_api_key) {
      $('#api-key-modal').classList.add('active');
      $('#api-key-input').focus();
    }
  } catch(e) {}
  refreshLog();
})();

// Modal functions
function closeModal() {
  $('#api-key-modal').classList.remove('active');
}

async function saveApiKey() {
  const key = $('#api-key-input').value.trim();
  const err = $('#api-key-error');

  if (!key) {
    err.textContent = 'Please enter an API key.';
    return;
  }
  if (!key.startsWith('sk-ant-')) {
    err.textContent = 'Key should start with sk-ant-...';
    return;
  }

  err.textContent = 'Saving...';
  try {
    const res = await fetch('/api/save-key', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key})
    });
    const data = await res.json();
    if (data.ok) {
      closeModal();
      refreshLog();
    } else {
      err.textContent = data.error || 'Failed to save.';
    }
  } catch(e) {
    err.textContent = 'Network error: ' + e.message;
  }
}

// Handle Enter key in modal
$('#api-key-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') saveApiKey();
});

function getSource() {
  return document.querySelector('input[name="source"]:checked').value;
}

function setBusy(busy) {
  $('#btn-classify').disabled = busy;
  $('#btn-scan').disabled = busy;
  if (busy) {
    $('#btn-classify').classList.add('busy');
    $('#btn-scan').classList.add('busy');
  } else {
    $('#btn-classify').classList.remove('busy');
    $('#btn-scan').classList.remove('busy');
  }
}

async function connect() {
  const ip = $('#scanner-ip').value.trim();
  const st = $('#scanner-status');
  st.textContent = 'Connecting...';
  st.className = 'status disconnected';

  try {
    const res = await fetch('/api/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ip})
    });
    const data = await res.json();
    if (data.ok) {
      st.textContent = data.name + ' \u2014 ' + data.state;
      st.className = 'status connected';
      $('#btn-classify').disabled = false;
      $('#btn-scan').disabled = false;
    } else {
      st.textContent = 'Error: ' + data.error;
      st.className = 'status error';
    }
  } catch(e) {
    st.textContent = 'Connection failed: ' + e.message;
    st.className = 'status error';
  }
  refreshLog();
}

async function scan(classify) {
  setBusy(true);
  $('#results-card').style.display = 'none';

  try {
    const res = await fetch('/api/scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        classify,
        source: getSource(),
        resolution: $('#resolution').value,
        color: $('#color').value,
        output_dir: $('#output-dir').value,
        scanner_ip: $('#scanner-ip').value.trim(),
      })
    });
    const data = await res.json();

    if (data.ok) {
      if (data.classified) {
        $('#results-card').style.display = '';
        $('#r-category').textContent = data.category;
        $('#r-filename').textContent = data.filename;
        $('#r-summary').textContent = data.summary;
        $('#r-date').textContent = data.date || '--';
      }
      if (data.output_path) {
        $('#r-path').textContent = 'Saved to: ' + data.output_path;
        $('#r-path').style.display = '';
        if (!data.classified) {
          $('#results-card').style.display = '';
          $('#r-category').textContent = 'unsorted';
          $('#r-filename').textContent = data.output_path.split('/').pop();
          $('#r-summary').textContent = 'Saved without classification';
          $('#r-date').textContent = '--';
        }
      }
    } else {
      alert('Error: ' + data.error);
    }
  } catch(e) {
    alert('Request failed: ' + e.message);
  }

  setBusy(false);
  refreshLog();
}

async function refreshLog() {
  try {
    const res = await fetch('/api/logs');
    const logs = await res.json();
    const el = $('#log');
    el.textContent = logs.join('\n');
    el.scrollTop = el.scrollHeight;
  } catch(e) {}
}

setInterval(refreshLog, 2000);
</script>
</body>
</html>"""


def main() -> None:
    port = 8470

    try:
        config = load_config()
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
