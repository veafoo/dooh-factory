"""Mode-agnostic batch runner.

Both modes are the same loop: decide whether a row still needs rendering,
build it, report the result back to the sheet, keep the batch alive when one
row fails. Only two things differ per mode, so that is all a mode provides:

    prepare(row, cfg, job) -> plan dict   # resolve inputs, name, fingerprint
    render(plan, cfg, job) -> output path # actually build the asset

A plan must carry at least `stem` (output filename stem), `files` (resolved
input paths) and `hash` (input fingerprint). `prepare` is the deterministic
gate: it fails loud on a missing asset before anything expensive starts.

The run is two phases. **Planning is serial** - prepare, skip decisions and the
output-name collision guard all happen in sheet order, so the same sheet always
produces the same decisions and the same error messages. **Rendering can be
concurrent** (`workers > 1`). Write-backs are serialized behind a lock.

Measured on an M1, 2026-07-26 - worth knowing before reaching for `--workers`:

    Mode 2, 6 static captures:  3.9s serial -> 2.1s on 3 workers  (~1.9x)
    Mode 1, 8 stitched creatives: 7.0s serial -> 6.8s on 4 workers (nothing)

Mode 2 gains because each row is mostly browser latency - page load and settle
waits - so the CPU is idle in between. Mode 1 gains nothing: x264 already
threads across every core, and running four encodes concurrently just splits
the same CPU four ways. Use workers for Mode 2; leave Mode 1 at 1 unless the
rushes are small enough that ffmpeg process startup dominates.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field

from . import contract

log = logging.getLogger("dooh.runner")


@dataclass
class Summary:
    total: int = 0
    rendered: int = 0
    skipped: int = 0
    errors: int = 0
    outputs: list[str] = field(default_factory=list)
    seconds: float = 0.0

    def __str__(self) -> str:
        return (f"{self.rendered} rendered, {self.skipped} skipped, "
                f"{self.errors} errors ({self.total} rows, {self.seconds:.1f}s)")


def _row_id(row: dict) -> str:
    return row.get("creative_id") or row.get("variant_id") or f"row {row.get('_row')}"


def run_rows(
    rows,
    write_back,
    cfg: dict,
    job: dict,
    *,
    prepare,
    render,
    force: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    workers: int = 1,
    render_context=None,
) -> Summary:
    """Render every row that needs it. One failing row never stops the batch.

    `render_context` is an optional zero-arg context-manager factory for modes
    whose renderer owns a resource that is bound to the thread using it - Mode 2
    and its browser. Each worker enters and exits its own, in its own thread.
    """
    started = time.monotonic()
    s = Summary()
    lock = threading.Lock()
    claimed: dict[str, int] = {}  # stem -> sheet row, guards silent overwrites
    todo: list[tuple[dict, dict, str]] = []

    def report(row, path, status, msg="", input_hash=None, **extra):
        with lock:
            write_back(row, path, status, msg, input_hash=input_hash, **extra)

    # -- phase 1: plan, serially and in sheet order --------------------------
    for row in rows:
        s.total += 1
        if limit is not None and len(todo) + s.errors >= limit:
            s.skipped += 1
            continue

        # Cheap gate first: a done row in a sheet without an input_hash column
        # can be skipped without touching the filesystem.
        if not force and contract.is_settled(row):
            s.skipped += 1
            continue

        try:
            plan = prepare(row, cfg, job)
        except Exception as e:  # noqa: BLE001 - report per-row, keep going
            s.errors += 1
            report(row, "", "error", str(e))
            log.error("%s: prepare failed: %s", _row_id(row), e)
            continue

        if force:
            reason = "force"
        else:
            needed, reason = contract.needs_render(row, plan["hash"])
            if not needed:
                s.skipped += 1
                continue

        stem = plan["stem"]
        if stem in claimed:
            s.errors += 1
            msg = f"output name collision on '{stem}' (already claimed by sheet row {claimed[stem]})"
            report(row, "", "error", msg)
            log.error("%s: %s", _row_id(row), msg)
            continue
        claimed[stem] = row.get("_row", s.total)

        if dry_run:
            s.skipped += 1
            log.info("[dry-run] %s -> %s (%s), inputs: %s",
                     _row_id(row), stem, reason, ", ".join(plan.get("files", [])) or "-")
            continue

        todo.append((row, plan, reason))

    # -- phase 2: render -----------------------------------------------------
    def build(item, render_fn) -> None:
        row, plan, reason = item
        try:
            path = render_fn(plan, cfg, job)
            # A mode may attach delivery artefacts (vast_path, renditions) to
            # the plan while rendering; they go back to the sheet with the row.
            report(row, path, "done", "", input_hash=plan["hash"],
                   **plan.get("extra", {}))
            with lock:
                s.rendered += 1
                s.outputs.append(path)
            log.info("%s: done (%s) -> %s", _row_id(row), reason, path)
        except Exception as e:  # noqa: BLE001
            with lock:
                s.errors += 1
            report(row, "", "error", str(e))
            log.error("%s: %s", _row_id(row), e)

    if workers > 1 and len(todo) > 1:
        log.info("rendering %d row(s) on %d workers", len(todo), workers)
        pending: queue.Queue = queue.Queue()
        for item in todo:
            pending.put(item)

        def worker() -> None:
            # A thread-bound renderer is created and torn down inside the very
            # thread that uses it - Playwright's sync API requires it.
            if render_context is None:
                drain(render)
                return
            with render_context() as ctx:
                drain(ctx.render)

        def drain(render_fn) -> None:
            while True:
                try:
                    item = pending.get_nowait()
                except queue.Empty:
                    return
                build(item, render_fn)

        threads = [threading.Thread(target=worker, name=f"dooh-w{i}", daemon=True)
                   for i in range(min(workers, len(todo)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for item in todo:
            build(item, render)

    flush = getattr(write_back, "flush", None)
    if flush:
        try:
            flush()
        except Exception as e:  # noqa: BLE001 - a write-back failure must be loud
            log.error("write-back flush failed: %s", e)
            raise

    s.seconds = time.monotonic() - started
    log.info("summary: %s", s)
    return s
