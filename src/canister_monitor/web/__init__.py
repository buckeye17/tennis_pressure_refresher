"""LAN web dashboard and JSON API (PLAN.md section 9.3)."""

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from flask import Flask, g, render_template

from canister_monitor import __version__, db
from canister_monitor.config import Config


def create_app(
    config: Config, db_path: str | Path | None = None, clock: Callable[[], float] = time.time
) -> Flask:
    """Build the Flask app. ``clock`` returns UTC epoch seconds (injectable for tests)."""
    app = Flask(__name__)
    app.json.sort_keys = False
    app.extensions["canister"] = {
        "config": config,
        "db_path": Path(db_path) if db_path is not None else config.collector.db_path,
        "clock": clock,
    }
    # Apply migrations once at startup so requests never take a write lock for it.
    db.connect(app.extensions["canister"]["db_path"]).close()

    from canister_monitor.web.api import api

    app.register_blueprint(api)

    @app.get("/")
    def index():
        return render_template("index.html", config=config, version=__version__)

    @app.teardown_appcontext
    def close_db(_exc: BaseException | None) -> None:
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    return app


def get_db() -> sqlite3.Connection:
    """The request's database connection, opened on first use."""
    from flask import current_app

    if "db" not in g:
        g.db = db.connect(current_app.extensions["canister"]["db_path"], migrate_schema=False)
    return g.db
