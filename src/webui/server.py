"""Flask web UI server for StopLiga."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, render_template


def create_app(state_file: Path) -> Flask:
    """Create and return the Flask application."""

    app = Flask(__name__, template_folder="templates")
    app.config["state_file"] = state_file

    @app.route("/api/state")
    def api_state():
        sf: Path = app.config["state_file"]
        if not sf.exists():
            return jsonify({"status": "pending", "age_seconds": None})
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return jsonify({"status": "error", "message": str(exc), "age_seconds": None})

        age_seconds = None
        last_success = data.get("last_success_at")
        if last_success:
            try:
                ts = datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
                age_seconds = int((datetime.now(timezone.utc) - ts).total_seconds())
            except ValueError:
                pass
        data["age_seconds"] = age_seconds
        return jsonify(data)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app


def start_server(state_file: Path, host: str, port: int) -> None:
    """Start the Flask server. Intended to run in a daemon thread."""

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        app = create_app(state_file)
        app.run(host=host, port=port, debug=False, use_reloader=False)
    except Exception as exc:
        logging.getLogger("stopliga.webui").error("webui_server_failed: %s", exc, exc_info=True)
