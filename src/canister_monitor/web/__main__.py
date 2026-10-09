"""Run the dashboard with waitress: python -m canister_monitor.web [--config PATH]."""

import argparse
import logging
import sys
from pathlib import Path

from canister_monitor.config import ConfigError, load_config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m canister_monitor.web", description=__doc__)
    p.add_argument("--config", default="config.toml", help="config file (default: config.toml)")
    p.add_argument("--db", help="override the database path from config")
    p.add_argument(
        "--simulate",
        action="store_true",
        help="show the simulator's database (<db_path stem>-sim.db) instead of real data",
    )
    args = p.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=config.collector.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = Path(args.db) if args.db else config.collector.db_path
    if args.simulate and not args.db:
        db_path = db_path.with_name(f"{db_path.stem}-sim{db_path.suffix}")

    from waitress import serve

    from canister_monitor.web import create_app

    app = create_app(config, db_path=db_path)
    logging.getLogger("canister_monitor.web").info(
        "serving %s on http://%s:%d", db_path, config.web.host, config.web.port
    )
    serve(app, host=config.web.host, port=config.web.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
