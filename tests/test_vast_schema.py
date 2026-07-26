"""Validate generated tags against the official IAB VAST 4.2 XSD.

Well-formed is not schema-valid, and the difference was four real defects: a
missing namespace, and three element orderings that are not the intuitive ones.
This suite is what stops them coming back.

Needs `xmllint` (libxml2). Skipped, loudly, when it is absent.

Known and deliberate gap: `Impression` and `Error` are typed `xs:anyURI`, which
forbids square brackets, so a URL carrying `[CACHEBUSTING]` or `[ERRORCODE]` can
never validate. Those macros are mandated by the VAST spec itself and every ad
server emits them - the XSD is stricter than the standard it encodes. These
tests therefore use macro-free URLs, and `test_macros_are_kept_even_though_the_schema_rejects_them`
pins that trade-off so nobody "fixes" it by dropping the macros.
"""
from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from src import vast

XSD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schemas", "vast_4.2.xsd")

pytestmark = pytest.mark.skipif(shutil.which("xmllint") is None,
                                reason="xmllint (libxml2) is not installed")


def _rendition(**over):
    base = {"path": "/o/spot_1920x1080.mp4", "width": 1920, "height": 1080,
            "bitrate_kbps": 8000, "codec": "libx264", "mime": "video/mp4",
            "bytes": 1, "role": "media"}
    base.update(over)
    return base


# Macro-free on purpose: see the module docstring.
CFG = {
    "ad_system": "dooh-factory",
    "advertiser": "Demo Brand",
    "impression_url": "https://ad.test/imp",
    "error_url": "https://ad.test/err",
    "tracking": {e: f"https://ad.test/{e}" for e in vast.QUARTILE_EVENTS},
}


def validate(xml: str, tmp_path) -> None:
    path = tmp_path / "tag.xml"
    path.write_text(xml, encoding="utf-8")
    p = subprocess.run(["xmllint", "--noout", "--schema", XSD, str(path)],
                       capture_output=True, text=True)
    assert p.returncode == 0, f"schema validation failed:\n{p.stderr.strip()}"


def build(channel="dooh", renditions=None, **cfg):
    return vast.build_inline(
        ad_id="spot", ad_serving_id="spot-abc-123", title="Soldes Ete",
        duration_s=10.0, renditions=renditions or [_rendition()],
        vast_cfg={**CFG, **cfg}, channel=channel, base_url="https://cdn.test/c")


def test_dooh_tag_is_schema_valid(tmp_path):
    validate(build("dooh", [_rendition(width=1080, height=1920)]), tmp_path)


def test_ctv_tag_with_ladder_mezzanine_and_clicks_is_schema_valid(tmp_path):
    rends = [_rendition(),
             _rendition(path="/o/spot_1280x720.mp4", width=1280, height=720,
                        bitrate_kbps=4000),
             _rendition(path="/o/spot.mp4", role="mezzanine", bitrate_kbps=0)]
    validate(build("ctv", rends, click_through="https://brand.test/store",
                   click_tracking=["https://ad.test/ck1", "https://ad.test/ck2"]), tmp_path)


def test_mobile_tag_is_schema_valid(tmp_path):
    validate(build("mobile", [_rendition(path="/o/s_640x360.mp4", width=640,
                                         height=360, bitrate_kbps=600)],
                   click_through="https://brand.test/x"), tmp_path)


def test_radio_tag_is_schema_valid(tmp_path):
    validate(build("radio", [_rendition(path="/o/spot.m4a", width=0, height=0,
                                        mime="audio/mp4", codec="aac",
                                        bitrate_kbps=192)]), tmp_path)


def test_a_tag_with_no_configuration_at_all_is_still_schema_valid(tmp_path):
    """Impression is minOccurs=1: with nothing configured the element still has
    to be emitted, empty, or the document is not VAST."""
    xml = vast.build_inline(ad_id="spot", ad_serving_id="s-1", title="T",
                            duration_s=6.0, renditions=[_rendition()],
                            vast_cfg={}, channel="dooh", base_url="")
    assert "<Impression" in xml
    validate(xml, tmp_path)


def test_tag_declares_the_iab_namespace():
    assert 'xmlns="http://www.iab.com/VAST"' in build()
    assert "noNamespaceSchemaLocation" not in build()


def test_macros_are_kept_even_though_the_schema_rejects_them(tmp_path):
    """Pins the trade-off: the VAST spec mandates macros, xs:anyURI forbids the
    brackets they need. The macros win, because a tag without them miscounts."""
    xml = build(impression_url="https://ad.test/i?cb=[CACHEBUSTING]",
                error_url="https://ad.test/e?code=[ERRORCODE]")
    assert "[CACHEBUSTING]" in xml and "[ERRORCODE]" in xml
    path = tmp_path / "macro.xml"
    path.write_text(xml, encoding="utf-8")
    p = subprocess.run(["xmllint", "--noout", "--schema", XSD, str(path)],
                       capture_output=True, text=True)
    assert p.returncode != 0
    assert "anyURI" in p.stderr, "expected the failure to be the anyURI type, nothing else"
