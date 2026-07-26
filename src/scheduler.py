"""Scheduling. Two paths, same job definitions:

- cron   : the OS invokes `run.py --job <name> --once`. Recommended for
           once/daily/hourly - nothing to keep alive, nothing to babysit.
- daemon : one long-running process (APScheduler) re-reading the sheet each
           tick. Only worth it at minute cadence.

Cadence grammar (the `cadence` key of a job):

    once                 run when invoked, never scheduled
    hourly               top of every hour
    daily@08:00          every day at 08:00 local time
    every_5_minutes      every 5 minutes (also: every_5m, every_30_seconds)
    cron:*/5 9-19 * * 1-5   raw 5-field crontab expression, for anything else

Rad's rule, enforced here rather than documented and forgotten: never render
more often than the source data changes. `parse_cadence` refuses an interval
below `min_interval_seconds` (config, default 60) so a typo cannot turn into a
per-second Sheets API hammering.
"""
from __future__ import annotations

import logging
import re
import shlex
import sys

log = logging.getLogger("dooh.scheduler")

_DAILY = re.compile(r"^daily@(\d{1,2}):(\d{2})$")
_EVERY = re.compile(r"^every_(\d+)_?(second|seconds|s|minute|minutes|min|m|hour|hours|h)$")
_UNIT_SECONDS = {"second": 1, "seconds": 1, "s": 1,
                 "minute": 60, "minutes": 60, "min": 60, "m": 60,
                 "hour": 3600, "hours": 3600, "h": 3600}


class CadenceError(ValueError):
    """Unparseable or unsafely frequent cadence."""


def parse_cadence(cadence: str, *, min_interval_seconds: int = 60) -> dict:
    """'daily@08:00' -> {'type': 'cron', 'hour': 8, 'minute': 0}, etc.

    Returns a trigger spec: {'type': 'once'} | {'type': 'interval', 'seconds': N}
    | {'type': 'cron', ...APScheduler cron fields}.
    """
    s = str(cadence or "").strip().lower()
    if not s or s == "once":
        return {"type": "once"}
    if s == "hourly":
        return {"type": "cron", "minute": 0}
    if s == "daily":
        return {"type": "cron", "hour": 0, "minute": 0}

    m = _DAILY.match(s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise CadenceError(f"invalid time in cadence: {cadence}")
        return {"type": "cron", "hour": hour, "minute": minute}

    m = _EVERY.match(s)
    if m:
        seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
        if seconds <= 0:
            raise CadenceError(f"invalid interval: {cadence}")
        if seconds < min_interval_seconds:
            raise CadenceError(
                f"cadence '{cadence}' = {seconds}s, below the {min_interval_seconds}s floor; "
                "do not render more often than the source data changes"
            )
        return {"type": "interval", "seconds": seconds}

    if s.startswith("cron:"):
        fields = s[len("cron:"):].split()
        if len(fields) != 5:
            raise CadenceError(f"cron expression needs 5 fields, got {len(fields)}: {cadence}")
        keys = ("minute", "hour", "day", "month", "day_of_week")
        return {"type": "cron", **dict(zip(keys, fields))}

    raise CadenceError(f"unparseable cadence: {cadence}")


def scheduled_jobs(cfg: dict) -> list[tuple[str, dict, dict]]:
    """[(job_name, job_cfg, trigger_spec)] for every job with a real cadence."""
    floor = int(cfg.get("scheduler", {}).get("min_interval_seconds", 60))
    out = []
    for name, job in (cfg.get("jobs") or {}).items():
        spec = parse_cadence(job.get("cadence", "once"), min_interval_seconds=floor)
        if spec["type"] == "once":
            log.info("job '%s': cadence once - not scheduled", name)
            continue
        out.append((name, job, spec))
    return out


def crontab_lines(cfg: dict, *, config_path: str = "config/config.yml",
                  project_dir: str | None = None, python: str | None = None) -> list[str]:
    """Crontab lines for every scheduled job. Print them, review, install by hand.

    Deliberately not auto-installed: editing a user's crontab is exactly the
    kind of thing that should stay a copy-paste decision.
    """
    import os

    project_dir = os.path.abspath(project_dir or os.getcwd())
    python = python or sys.executable
    lines = [f"# dooh-factory - generated, review before installing",
             f"# cd {project_dir}"]
    for name, _job, spec in scheduled_jobs(cfg):
        if spec["type"] == "interval":
            seconds = spec["seconds"]
            if seconds % 3600 == 0:
                sched = f"0 */{seconds // 3600} * * *"
            elif seconds % 60 == 0:
                sched = f"*/{seconds // 60} * * * *"
            else:
                lines.append(f"# job '{name}': {seconds}s interval is sub-minute - use --daemon")
                continue
        else:
            sched = " ".join(str(spec.get(k, "*")) for k in
                             ("minute", "hour", "day", "month", "day_of_week"))
        cmd = (f"cd {shlex.quote(project_dir)} && {shlex.quote(python)} run.py "
               f"--config {shlex.quote(config_path)} --job {shlex.quote(name)} --once")
        lines.append(f"{sched} {cmd} >> logs/{name}.log 2>&1")
    return lines


def run_daemon(cfg: dict, run_job) -> None:
    """Register every scheduled job with APScheduler and block.

    `run_job(name)` is injected so the daemon has no import cycle with main and
    stays trivially testable.
    """
    try:
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as e:  # pragma: no cover - environment problem
        raise RuntimeError("apscheduler is not installed - `pip install apscheduler`") from e

    jobs = scheduled_jobs(cfg)
    if not jobs:
        raise RuntimeError("no job has a schedulable cadence - nothing for the daemon to do")

    sched = BlockingScheduler(timezone=cfg.get("scheduler", {}).get("timezone"))
    for name, _job, spec in jobs:
        if spec["type"] == "interval":
            trigger = IntervalTrigger(seconds=spec["seconds"])
        else:
            trigger = CronTrigger(**{k: v for k, v in spec.items() if k != "type"})

        def tick(job_name=name):
            # A single failing tick must never take the daemon down with it.
            try:
                run_job(job_name)
            except Exception as e:  # noqa: BLE001
                log.error("job '%s' failed: %s", job_name, e)

        # coalesce + max_instances=1: a tick that overruns its slot is skipped,
        # not queued up behind itself.
        sched.add_job(tick, trigger, id=name, name=name,
                      coalesce=True, max_instances=1, misfire_grace_time=300)
        log.info("scheduled job '%s': %s", name, spec)

    log.info("daemon up, %d job(s). Ctrl-C to stop.", len(jobs))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("daemon stopped")
