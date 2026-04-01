"""
Monique File Manager — Render entry point.

Endpoints:
  GET /health  → 200 health check
  POST /scan   → trigger sovereign-dispatch full ingestion
"""

import logging
import threading

from flask import Flask, jsonify

import config
from monique_file_manager import run_sovereign_dispatch

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("monique")

app = Flask(__name__)

# Track scan state so /health can report it
_scan_state = {"status": "idle", "result": None}


@app.route("/")
def index():
    return "Monique File Manager — Sovereign Dispatch Active"


@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "gemini_model": config.GEMINI_MODEL,
        "sovereign_dispatch": config.SOVEREIGN_DISPATCH["mode"],
        "scan_state": _scan_state["status"],
    }), 200


@app.route("/scan", methods=["POST"])
def scan():
    """Kick off sovereign-dispatch in a background thread."""
    if _scan_state["status"] == "running":
        return jsonify({"error": "scan already running"}), 409

    def _run():
        _scan_state["status"] = "running"
        try:
            _scan_state["result"] = run_sovereign_dispatch()
            _scan_state["status"] = "complete"
        except Exception as exc:
            logger.exception("Sovereign-dispatch failed")
            _scan_state["status"] = "error"
            _scan_state["result"] = str(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    return jsonify({
        "message": "Sovereign-dispatch started",
        "mode": config.SOVEREIGN_DISPATCH["mode"],
        "workspace_accounts": config.DWD_WORKSPACE_ACCOUNTS,
        "gmail_accounts": config.OAUTH2_GMAIL_ACCOUNTS,
    }), 202


@app.route("/scan/status")
def scan_status():
    return jsonify(_scan_state), 200


if __name__ == "__main__":
    app.run()
