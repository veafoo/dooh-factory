"""Mode 2: inject variable params into an HTML5 creative and capture each variant.

Primary use case: drive-to-store / repiquage d'adresse - one variant per store,
address and store name swapped in, captured to a flat asset the DOOH player can
run. The browser is opened once per batch, not once per row.

Sheet contract (on top of the reserved columns in `contract.py`):

    variant_id       row id (used as the naming fallback)
    template         HTML5 creative: a file under templates_dir, or an http(s) URL
    format           'WxH' viewport, e.g. 1080x1920. Falls back to config.
    capture          static | video | frames | auto   (default: job setting)
    inject           query | window_var | postmessage | dom  (default: job setting)
    duration_s       animation length; 0/empty means a still
    wait_selector    optional CSS selector to wait for before capturing
    wait_ms          optional extra settle delay
    <anything else>  injected as a creative param

Injection modes:
    query        params go in the URL query string
    window_var   params land in `window.__DATA__` *before* any page script runs
                 (add_init_script), so the creative can read them synchronously
    postmessage  params are posted to the page after load
    dom          every `[data-dooh="<key>"]` element gets the value - textContent,
                 or src/href when the element is an img/source/a. Zero-JS
                 templates, which is what most designers actually hand over.

Capture modes:
    static   one screenshot -> jpg/png. Deterministic.
    video    Chromium records in real time -> ffmpeg normalizes. Correct timing
             for CSS animations; this is the safe default for animated creatives.
    frames   screenshot loop -> ffmpeg. Exact only when the template exposes a
             seek hook `window.__seek(ms)`; without it, frames are captured on
             the wall clock and the result is approximate. Warned about at run
             time rather than silently shipped.
    auto     static when duration_s is empty, otherwise the job's animated engine.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import tempfile
import time
from urllib.parse import urlencode, urlparse, urlunparse

from . import contract, ffmpeg_utils, renditions, runner

log = logging.getLogger("dooh.mode2")

# Row columns that steer the render instead of being injected into the creative.
CONTROL_COLS = {
    "variant_id", "creative_id", "template", "format", "capture", "inject",
    "duration_s", "fps", "wait_selector", "wait_ms", "device_scale_factor",
    "naming_template", "output_name", "static_format", "channel", "audio_track",
}

_SEEK_PROBE = "typeof window.__seek === 'function'"


def _num(value, default=None):
    s = str(value or "").strip().replace(",", ".")
    if not s:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _pick(row: dict, job: dict, key: str, default=None):
    """Row cell wins over job config wins over default."""
    v = str(row.get(key, "") or "").strip()
    if v:
        return v
    v = job.get(key)
    return v if v not in (None, "") else default


def extract_params(row: dict) -> dict:
    """The creative's variable params: every non-control, non-reserved cell."""
    return {
        k: str(v) for k, v in row.items()
        if k not in CONTROL_COLS and k not in contract.RESERVED and str(v or "").strip()
    }


def resolve_template(ref: str, templates_dir: str) -> tuple[str, str | None]:
    """-> (url, local_path or None). Local templates become file:// URLs."""
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError("row has no template")
    if ref.startswith(("http://", "https://", "file://")):
        return ref, None
    path = contract.resolve_asset(ref, templates_dir)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"template not found: {path}")
    return "file://" + os.path.abspath(path), os.path.abspath(path)


def build_url(url: str, params: dict, inject: str) -> str:
    """Append params to the URL when injection is query-based, else pass through."""
    if inject != "query" or not params:
        return url
    parts = urlparse(url)
    query = "&".join(filter(None, [parts.query, urlencode(params)]))
    return urlunparse(parts._replace(query=query))


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #

def prepare(row: dict, cfg: dict, job: dict | None = None) -> dict:
    """Resolve template, params, capture spec and output name for one variant."""
    job = job or {}
    paths, ff = cfg["paths"], cfg.get("ffmpeg", {})
    url, local = resolve_template(row.get("template", ""), paths.get("templates_dir", "templates"))
    params = extract_params(row)

    default_wh = (int(ff.get("width", 1080)), int(ff.get("height", 1920)))
    width, height = contract.parse_format(row.get("format"), default_wh)
    fps = int(_num(row.get("fps"), None) or _num(job.get("fps"), None) or ff.get("fps", 25))
    duration = _num(row.get("duration_s"), None) or _num(job.get("duration_s"), None)

    inject = str(_pick(row, job, "inject", "query")).lower()
    capture = str(_pick(row, job, "capture", "auto")).lower()
    if capture == "auto":
        capture = "static" if not duration else str(job.get("animated_engine", "video")).lower()
    if capture in ("video", "frames") and not duration:
        duration = float(ff.get("default_duration_s", 10))

    static_format = str(_pick(row, job, "static_format", "jpg")).lower().lstrip(".")
    ext = "mp4" if capture in ("video", "frames") else static_format

    channel = str(_pick(row, job, "channel", "dooh")).lower()
    audio_track = str(row.get("audio_track", "") or job.get("audio_track", "") or "").strip()
    if audio_track:
        audio_track = contract.resolve_asset(audio_track, paths.get("assets_dir", "."))
        if not os.path.isfile(audio_track):
            raise FileNotFoundError(f"audio track not found: {audio_track}")

    stem = contract.output_stem(row, job.get("naming_fallback", "{variant_id}"))
    ladder = str((cfg.get("delivery") or {}).get("channels", {}).get(channel, ""))
    spec = "|".join(str(x) for x in [
        width, height, fps, duration, inject, capture, ext,
        ff.get("video_codec"), ff.get("crf"), ff.get("bitrate"),
        ff.get("profile"), ff.get("level"), ff.get("gop"),
        job.get("device_scale_factor"), job.get("wait_until"), job.get("settle_ms"),
        channel, ladder, audio_track,
    ])
    files = ([local] if local else []) + ([audio_track] if audio_track else [])
    return {
        "stem": stem,
        "files": files,
        "hash": contract.compute_input_hash(row, files, spec),
        "channel": channel,
        "audio_track": audio_track or None,
        "title": row.get("campaign") or stem,
        "url": build_url(url, params, inject),
        "params": params,
        "inject": inject,
        "capture": capture,
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "wait_selector": _pick(row, job, "wait_selector", ""),
        "wait_ms": _num(_pick(row, job, "wait_ms", None), None),
        "device_scale_factor": float(_num(_pick(row, job, "device_scale_factor", None), 1.0)),
        "dst": os.path.join(paths["output_dir"], f"{stem}.{ext}"),
    }


# --------------------------------------------------------------------------- #
# browser session
# --------------------------------------------------------------------------- #

class Session:
    """One Chromium instance for a whole batch. Use as a context manager."""

    def __init__(self, cfg: dict, job: dict | None = None):
        self.cfg = cfg
        self.job = job or {}
        self._pw = None
        self.browser = None

    def __enter__(self) -> "Session":
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - environment problem, not logic
            raise RuntimeError(
                "playwright is not installed - `pip install playwright && playwright install chromium`"
            ) from e
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(
            headless=self.job.get("headless", True),
            args=["--hide-scrollbars", "--force-color-profile=srgb",
                  "--disable-lcd-text", "--font-render-hinting=none"],
        )
        return self

    def __exit__(self, *exc) -> None:
        if self.browser:
            self.browser.close()
        if self._pw:
            self._pw.stop()

    # -- page setup -------------------------------------------------------- #

    def _open(self, plan: dict, ctx_kwargs: dict):
        ctx = self.browser.new_context(
            viewport={"width": plan["width"], "height": plan["height"]},
            device_scale_factor=plan["device_scale_factor"],
            reduced_motion="no-preference",
            **ctx_kwargs,
        )
        page = ctx.new_page()
        if plan["inject"] == "window_var":
            var = self.job.get("window_var", "__DATA__")
            # add_init_script runs before any page script: the creative can read
            # window.__DATA__ synchronously at parse time.
            page.add_init_script(f"window.{var} = {json.dumps(plan['params'])};")
        page.goto(plan["url"], wait_until=self.job.get("wait_until", "load"),
                  timeout=int(self.job.get("timeout_ms", 30000)))
        if plan["inject"] == "postmessage":
            page.evaluate("d => window.postMessage(d, '*')", plan["params"])
        elif plan["inject"] == "dom":
            page.evaluate(_DOM_INJECT, plan["params"])
        self._settle(page, plan)
        return ctx, page

    def _settle(self, page, plan: dict) -> None:
        if plan["wait_selector"]:
            page.wait_for_selector(plan["wait_selector"],
                                   timeout=int(self.job.get("timeout_ms", 30000)))
        try:
            page.evaluate("() => document.fonts ? document.fonts.ready : true")
        except Exception:  # noqa: BLE001 - font API missing is not fatal
            pass
        wait_ms = plan["wait_ms"]
        if wait_ms is None:
            wait_ms = float(self.job.get("settle_ms", 300))
        if wait_ms > 0:
            page.wait_for_timeout(wait_ms)

    # -- render ------------------------------------------------------------ #

    def render(self, plan: dict, cfg: dict, job: dict | None = None) -> str:
        os.makedirs(os.path.dirname(plan["dst"]) or ".", exist_ok=True)
        how = plan["capture"]
        if how == "static":
            return self._capture_static(plan)
        if how not in ("frames", "video"):
            raise ValueError(f"unknown capture mode: {how}")

        path = self._capture_frames(plan) if how == "frames" else self._capture_video(plan)
        # A browser capture has no audio track of its own, so a CTV or mobile
        # variant gets its sound from the row's `audio_track` at rendition time.
        plan["extra"] = renditions.package(path, plan, cfg, job)
        return path

    def _capture_static(self, plan: dict) -> str:
        ctx, page = self._open(plan, {})
        tmp = tempfile.mkdtemp(prefix="dooh-shot-")
        try:
            shot = os.path.join(tmp, "shot.png")
            page.screenshot(path=shot, type="png")
            if plan["dst"].lower().endswith(".png") and plan["device_scale_factor"] == 1.0:
                shutil.move(shot, plan["dst"])
            else:
                ffmpeg_utils.convert_image(
                    shot, plan["dst"], width=plan["width"], height=plan["height"],
                    quality=int(self.job.get("jpeg_quality", 90)), ff=self.cfg.get("ffmpeg", {}),
                )
        finally:
            ctx.close()
            shutil.rmtree(tmp, ignore_errors=True)
        return plan["dst"]

    def _capture_frames(self, plan: dict) -> str:
        """Screenshot loop. Exact if the creative exposes `window.__seek(ms)`."""
        ctx, page = self._open(plan, {})
        tmp = tempfile.mkdtemp(prefix="dooh-frames-")
        try:
            fps, duration = plan["fps"], plan["duration"]
            n = max(1, int(round(fps * duration)))
            step = 1.0 / fps
            seekable = bool(page.evaluate(f"() => {_SEEK_PROBE}"))
            if not seekable:
                log.warning(
                    "%s: template exposes no window.__seek(ms); frames are captured on the "
                    "wall clock and timing is approximate - use capture=video for CSS animation",
                    plan["stem"],
                )
            t0 = time.monotonic()
            late = 0
            for i in range(n):
                if seekable:
                    page.evaluate("ms => window.__seek(ms)", i * step * 1000.0)
                else:
                    target = t0 + i * step
                    remaining = target - time.monotonic()
                    if remaining > 0:
                        page.wait_for_timeout(remaining * 1000.0)
                    elif i:
                        late += 1
                page.screenshot(path=os.path.join(tmp, f"{i:05d}.png"), type="png")
            if late:
                log.warning("%s: %d/%d frames captured late (screenshots slower than %.0f fps)",
                            plan["stem"], late, n, fps)
            ffmpeg_utils.frames_to_video(
                os.path.join(tmp, "*.png"), plan["dst"], fps,
                ff=self.cfg.get("ffmpeg", {}), max_duration=duration,
            )
        finally:
            ctx.close()
            shutil.rmtree(tmp, ignore_errors=True)
        return plan["dst"]

    def _capture_video(self, plan: dict) -> str:
        """Real-time Chromium recording -> ffmpeg. Right choice for CSS animation."""
        tmp = tempfile.mkdtemp(prefix="dooh-video-")
        try:
            started = time.monotonic()
            ctx, page = self._open(plan, {
                "record_video_dir": tmp,
                "record_video_size": {"width": plan["width"], "height": plan["height"]},
            })
            # Recording began at context creation; everything before this point
            # is load + settle and must be trimmed off the front.
            offset = time.monotonic() - started
            page.wait_for_timeout(plan["duration"] * 1000.0)
            ctx.close()  # flushes the webm to disk

            raw = sorted(glob.glob(os.path.join(tmp, "*.webm")))
            if not raw:
                raise RuntimeError("Chromium produced no recording")
            ffmpeg_utils.transcode_capture(
                raw[0], plan["dst"], width=plan["width"], height=plan["height"],
                fps=plan["fps"], ff=self.cfg.get("ffmpeg", {}),
                start=offset, duration=plan["duration"],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return plan["dst"]


_DOM_INJECT = """
(params) => {
  const asSrc = new Set(['IMG', 'SOURCE', 'VIDEO', 'IFRAME']);
  for (const [key, value] of Object.entries(params)) {
    for (const el of document.querySelectorAll(`[data-dooh="${CSS.escape(key)}"]`)) {
      if (asSrc.has(el.tagName)) el.setAttribute('src', value);
      else if (el.tagName === 'A') el.setAttribute('href', value);
      else el.textContent = value;
    }
  }
}
"""


# --------------------------------------------------------------------------- #
# entrypoints
# --------------------------------------------------------------------------- #

def render_row(row: dict, cfg: dict, job: dict | None = None) -> str:
    """Build one variant (opens its own browser). Convenience for one-offs/tests."""
    with Session(cfg, job) as sess:
        return sess.render(prepare(row, cfg, job), cfg, job)


def run(cfg: dict, rows, write_back, job: dict | None = None, **opts) -> runner.Summary:
    """Render every pending variant, reusing browsers instead of relaunching.

    Serial runs share one browser for the whole batch. Parallel runs give each
    worker its own, created and closed inside that worker's thread - Playwright's
    sync API is bound to the thread that started it.
    """
    job = job or {}
    rows = list(rows)
    if opts.get("dry_run"):
        # No capture happens, so do not pay for a Chromium launch.
        return runner.run_rows(rows, write_back, cfg, job, prepare=prepare,
                               render=lambda *a: "", **opts)

    if int(opts.get("workers", 1) or 1) > 1:
        return runner.run_rows(rows, write_back, cfg, job, prepare=prepare,
                               render=None, render_context=lambda: Session(cfg, job), **opts)

    with Session(cfg, job) as sess:
        return runner.run_rows(rows, write_back, cfg, job,
                               prepare=prepare, render=sess.render, **opts)
