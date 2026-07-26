"""VAST 4.2 InLine generation.

The factory renders the assets; this is what a buyer actually traffics. One
creative becomes one InLine tag carrying every rendition as a `<MediaFile>`, so
the player picks what it can decode at the bandwidth it has.

Every tag is validated against the official IAB VAST 4.2 XSD in the test suite.
The schema is ordered, and several of its orderings are not the intuitive ones,
so they were determined from the schema rather than guessed:

    InLine        AdSystem, Error, Impression, AdServingId, AdTitle, ..., Creatives
    Creative      Linear, then UniversalAdId
    Linear        TrackingEvents, then Duration, then MediaFiles, then VideoClicks
    VideoClicks   ClickTracking, then ClickThrough
    MediaFiles    MediaFile+, then Mezzanine

The document also carries the `http://www.iab.com/VAST` namespace: the 4.2 XSD
declares a targetNamespace, so a tag without it has no matching global
declaration and fails at the root. Players are lenient about this; the schema
is not.

One thing the schema cannot express: `Impression` and `Error` are typed
`xs:anyURI`, which forbids square brackets, so a URL carrying `[CACHEBUSTING]`
or `[ERRORCODE]` is never schema-valid. The macros are mandated by the VAST
spec itself and every ad server emits them, so they stay - the XSD is simply
stricter than the standard it encodes. The validation tests use macro-free URLs
and that gap is deliberate.

Deliberate choices, each one a known failure mode if skipped:

- **`<Error>` with `[ERRORCODE]`.** Without it a rejected creative is invisible:
  the player drops the ad and nobody learns why.
- **`<Mezzanine>`** on channels that declare it. Server-side ad insertion
  re-encodes from the mezzanine; its absence is VAST error 406, i.e. no ad
  served on SSAI CTV inventory.
- **`<UniversalAdId>`** (VAST 4, required) and **`<AdServingId>`** (4.1+).
  `idRegistry` defaults to `unknown`, which is the correct value until the
  creative is registered with Ad-ID or Clearcast - putting a fake registry
  there is worse than admitting there is none.
- **`[CACHEBUSTING]`** left in tracking URLs for the player to substitute. A
  URL without it double-counts or gets served from cache.
- **No `<VideoClicks>` on DOOH.** There is no click on a street panel; an
  advertised ClickThrough that cannot fire skews click reporting.

Tracking event names come from the VAST vocabulary exactly (`firstQuartile`,
not `first_quartile`): a misspelled event is silently never fired, which shows
up as a hole in completion reporting rather than as an error.
"""
from __future__ import annotations

import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from xml.dom import minidom

log = logging.getLogger("dooh.vast")

VAST_VERSION = "4.2"
# The 4.2 XSD declares targetNamespace with elementFormDefault="qualified", so a
# tag without this namespace has no matching global declaration and fails at the
# root. Real players are lenient about it; the schema is not.
VAST_NS = "http://www.iab.com/VAST"


def _q(tag: str) -> str:
    return f"{{{VAST_NS}}}{tag}"

# Counting events. DOOH panels have no meaningful quartile playback pattern, so
# the channel decides which of these are worth emitting.
QUARTILE_EVENTS = ("start", "firstQuartile", "midpoint", "thirdQuartile", "complete")
DOOH_EVENTS = ("creativeView", "start", "complete")


def duration_hms(seconds: float) -> str:
    """VAST wants HH:MM:SS(.mmm)."""
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}" if ms else base


def asset_url(path: str, base_url: str) -> str:
    """Public URL of a rendered file. Falls back to a file:// URL when no CDN
    base is configured, so a locally generated tag is still playable in a
    validator instead of pointing at nothing."""
    if not base_url:
        return "file://" + os.path.abspath(path)
    return base_url.rstrip("/") + "/" + os.path.basename(path)


def _cdata(parent: ET.Element, tag: str, url: str, **attrs) -> ET.Element:
    el = ET.SubElement(parent, _q(tag), {k: str(v) for k, v in attrs.items() if v not in (None, "")})
    el.text = url
    return el


def build_inline(
    *,
    ad_id: str,
    ad_serving_id: str,
    title: str,
    duration_s: float,
    renditions: list[dict],
    vast_cfg: dict | None = None,
    channel: str = "dooh",
    base_url: str = "",
) -> str:
    """Return a VAST 4.2 InLine document as a string."""
    cfg = vast_cfg or {}
    if not renditions:
        raise ValueError("a VAST tag needs at least one rendition")

    media = [r for r in renditions if r.get("role") != "mezzanine"]
    mezzanine = next((r for r in renditions if r.get("role") == "mezzanine"), None)
    if not media:
        raise ValueError("a VAST tag needs at least one non-mezzanine rendition")

    ET.register_namespace("", VAST_NS)
    root = ET.Element(_q("VAST"), {"version": cfg.get("version", VAST_VERSION)})
    ad = ET.SubElement(root, _q("Ad"), {"id": ad_id, "conditionalAd": "false"})
    inline = ET.SubElement(ad, _q("InLine"))

    # The XSD sequence is ordered and xmllint rejects any other arrangement:
    # AdSystem, Error, Impression, AdServingId, AdTitle, ..., Creatives.
    # Verified against the IAB VAST 4.2 XSD and their own Inline_Simple sample.
    ET.SubElement(inline, _q("AdSystem"), {"version": "1.0"}).text = \
        cfg.get("ad_system", "dooh-factory")

    if cfg.get("error_url"):
        _cdata(inline, "Error", cfg["error_url"])
    # Impression is minOccurs=1 in the schema. With none configured the element
    # still has to be there, empty: an ad with no impression node is not a VAST
    # document at all.
    impressions = _as_list(cfg.get("impression_url")) or [""]
    for i, url in enumerate(impressions, start=1):
        _cdata(inline, "Impression", url, id=f"imp-{i}")

    ET.SubElement(inline, _q("AdServingId")).text = ad_serving_id
    ET.SubElement(inline, _q("AdTitle")).text = title

    if cfg.get("advertiser"):
        ET.SubElement(inline, _q("Advertiser")).text = cfg["advertiser"]

    creatives = ET.SubElement(inline, _q("Creatives"))
    creative = ET.SubElement(creatives, _q("Creative"), {"id": ad_id, "sequence": "1"})

    # Linear before UniversalAdId, again because the sequence is ordered.
    linear = ET.SubElement(creative, _q("Linear"))

    # And inside Linear: TrackingEvents before Duration.
    events = DOOH_EVENTS if channel == "dooh" else QUARTILE_EVENTS
    tracking_cfg = cfg.get("tracking") or {}
    tracked = {e: tracking_cfg[e] for e in events if tracking_cfg.get(e)}
    if tracked:
        te = ET.SubElement(linear, _q("TrackingEvents"))
        for event, url in tracked.items():
            _cdata(te, "Tracking", url, event=event)

    ET.SubElement(linear, _q("Duration")).text = duration_hms(duration_s)

    files = ET.SubElement(linear, _q("MediaFiles"))
    for i, r in enumerate(media, start=1):
        mime = r.get("mime", "video/mp4")
        attrs = {
            "id": f"media-{i}",
            "delivery": "progressive",
            "type": mime,
            # An audio ad has no picture. VAST still requires the attributes, and
            # 0x0 is how you say so.
            "width": r["width"],
            "height": r["height"],
            "codec": _vast_codec(r.get("codec")),
        }
        if not mime.startswith("audio/"):
            # Scaling hints are meaningless on an audio rendition.
            attrs["scalable"] = "true"
            attrs["maintainAspectRatio"] = "true"
        if r.get("bitrate_kbps"):
            attrs["bitrate"] = r["bitrate_kbps"]
        _cdata(files, "MediaFile", asset_url(r["path"], base_url), **attrs)

    if mezzanine:
        _cdata(files, "Mezzanine", asset_url(mezzanine["path"], base_url),
               id="mezzanine-1", delivery="progressive",
               type=mezzanine.get("mime", "video/mp4"),
               width=mezzanine.get("width") or media[0]["width"],
               height=mezzanine.get("height") or media[0]["height"])

    # OOH has no click surface; advertising one distorts click reporting.
    click_through = cfg.get("click_through")
    if click_through and channel != "dooh":
        clicks = ET.SubElement(linear, _q("VideoClicks"))
        # ClickTracking before ClickThrough: counter-intuitive, but that is the
        # order the 4.2 schema imposes. Verified empirically against the XSD.
        for i, url in enumerate(_as_list(cfg.get("click_tracking")), start=1):
            _cdata(clicks, "ClickTracking", url, id=f"ctrk-{i}")
        _cdata(clicks, "ClickThrough", click_through, id="ct-1")

    ET.SubElement(creative, _q("UniversalAdId"),
                  {"idRegistry": cfg.get("id_registry", "unknown")}).text = ad_id

    xml = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(xml).toprettyxml(indent="  ")
    # minidom emits its own declaration; keep one, and drop the blank lines it
    # sprinkles between nodes.
    body = "\n".join(ln for ln in pretty.splitlines() if ln.strip())
    return _wrap_urls_in_cdata(body) + "\n"


# Elements whose content is a URL. Every real tag wraps these in CDATA: an
# unescaped `&` in a tracking URL is invalid XML, and an escaped one (`&amp;`)
# is what a player then requests, which breaks the query string.
_URL_ELEMENTS = ("Impression", "Error", "Tracking", "ClickThrough", "ClickTracking",
                 "MediaFile", "Mezzanine", "VASTAdTagURI")
_URL_RE = re.compile(
    r"<(" + "|".join(_URL_ELEMENTS) + r")([^>]*)>([^<]+)</\1>")


def _cdata_safe(text: str) -> str:
    """Neutralise a `]]>` sequence inside CDATA content.

    A tracking URL is untrusted input (it comes from the sheet). A `]]>` in it
    would close the CDATA section early: best case the XML is malformed and the
    tag is rejected, worst case the remainder is parsed as markup and injects
    elements into the VAST a player receives. The standard fix is to split the
    sequence across two CDATA sections.
    """
    return text.replace("]]>", "]]]]><![CDATA[>")


def _wrap_urls_in_cdata(xml: str) -> str:
    def repl(m: re.Match) -> str:
        tag, attrs, body = m.group(1), m.group(2), m.group(3)
        return f"<{tag}{attrs}><![CDATA[{_cdata_safe(html.unescape(body.strip()))}]]></{tag}>"

    return _URL_RE.sub(repl, xml)


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [value] if isinstance(value, str) else list(value)


def _vast_codec(codec: str | None) -> str:
    """ffmpeg encoder name -> the codec label players expect in a tag."""
    return {"libx264": "H.264", "libx265": "H.265", "h264": "H.264",
            "libmp3lame": "mp3", "aac": "AAC", "libfdk_aac": "AAC"}.get(
        (codec or "").lower(), codec or "H.264")


def write(path: str, xml: str) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(xml)
    return path
