"""Entrypoint.

    python run.py --job mode1 --once          # one job, once (cron-friendly)
    python run.py --job mode1 --dry-run       # resolve + plan, render nothing
    python run.py --job mode1 --csv rows.csv  # drive from a local CSV, no Sheet
    python run.py --daemon                    # every job on its cadence
    python run.py --print-cron                # crontab lines to review/install

Exit code is non-zero when any row errored, so cron/CI notices.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys

import yaml

if __package__ in (None, ""):  # `python src/main.py` still has to work
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src import mode1_stitch, mode2_html5, scheduler, sheets
else:
    from . import mode1_stitch, mode2_html5, scheduler, sheets

log = logging.getLogger("dooh.main")

MODES = {"stitch": mode1_stitch.run, "html5": mode2_html5.run}


def load_config(path: str = "config/config.yml") -> dict:
    if not os.path.isfile(path):
        example = os.path.join(os.path.dirname(path) or ".", "config.example.yml")
        hint = f" (copy {example} to {path} and fill it in)" if os.path.isfile(example) else ""
        raise SystemExit(f"config not found: {path}{hint}")
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    for key in ("paths", "jobs"):
        if key not in cfg:
            raise SystemExit(f"config is missing the '{key}' block: {path}")
    return cfg


def read_csv_rows(path: str) -> list[dict]:
    """Local CSV as a stand-in for the sheet: same columns, same contract.

    Lets a whole job be validated end-to-end before any Google credentials
    exist, and makes reproducing a bad render a one-file affair.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = []
        for i, rec in enumerate(csv.DictReader(f), start=2):
            clean = {(k or "").strip(): (v or "").strip()
                     for k, v in rec.items() if (k or "").strip()}
            if any(clean.values()):
                clean["_row"] = i
                rows.append(clean)
        return rows


def run_job(cfg: dict, name: str, *, csv_path: str | None = None, **opts):
    """Run one configured job. Returns the runner Summary."""
    jobs = cfg.get("jobs") or {}
    if name not in jobs:
        raise SystemExit(f"unknown job '{name}' - configured: {', '.join(jobs) or '(none)'}")
    job = jobs[name]
    mode = job.get("mode")
    if mode not in MODES:
        raise SystemExit(f"job '{name}' has unknown mode '{mode}' - use: {', '.join(MODES)}")

    if csv_path:
        rows, write_back = read_csv_rows(csv_path), sheets.null_write_back
        log.info("job '%s': %d row(s) from %s (no write-back)", name, len(rows), csv_path)
    else:
        google = cfg.get("google") or {}
        ws = sheets.open_worksheet(
            google.get("service_account_json"), google.get("spreadsheet_id"), job["worksheet"])
        rows = sheets.read_rows(ws)
        write_back = sheets.make_write_back(ws)
        log.info("job '%s': %d row(s) from worksheet '%s'", name, len(rows), job["worksheet"])

    return MODES[mode](cfg, rows, write_back, job, **opts)


def _setup_logging(verbose: bool, log_file: str | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> int:
    ap = argparse.ArgumentParser(prog="dooh-factory")
    ap.add_argument("--job", help="job name from the config")
    ap.add_argument("--once", action="store_true",
                    help="explicit single run (default when --job is given)")
    ap.add_argument("--daemon", action="store_true", help="run every job on its cadence")
    ap.add_argument("--print-cron", action="store_true", help="print crontab lines and exit")
    ap.add_argument("--config", default="config/config.yml")
    ap.add_argument("--csv", help="read rows from a local CSV instead of the Sheet")
    ap.add_argument("--force", action="store_true", help="re-render rows already marked done")
    ap.add_argument("--dry-run", action="store_true", help="resolve and plan, render nothing")
    ap.add_argument("--limit", type=int, help="stop after N rendered rows")
    ap.add_argument("--workers", type=int, default=1,
                    help="render N rows concurrently (default 1; each Mode 2 worker "
                         "runs its own browser, each Mode 1 worker its own ffmpeg)")
    ap.add_argument("--log-file")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    _setup_logging(args.verbose, args.log_file)
    cfg = load_config(args.config)

    if args.print_cron:
        print("\n".join(scheduler.crontab_lines(cfg, config_path=args.config)))
        return 0

    if args.daemon:
        scheduler.run_daemon(cfg, lambda name: run_job(cfg, name))
        return 0

    if not args.job:
        ap.error("give --job <name>, --daemon, or --print-cron")

    if args.workers < 1:
        ap.error("--workers must be >= 1")

    summary = run_job(cfg, args.job, csv_path=args.csv, force=args.force,
                      dry_run=args.dry_run, limit=args.limit, workers=args.workers)
    print(summary)
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
