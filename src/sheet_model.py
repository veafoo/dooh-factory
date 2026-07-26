"""The Google Sheet model: three tabs, one source of truth.

    mode1        stitched creatives - one row per rush combination
    mode2        HTML5 variants - one row per variant (store, offer, audience...)
    media_dv360  the trafficking plan - one row per line item x creative

Kept as data, separately from the script that pushes it to Google, so the model
can be unit-tested, exported to CSV and diffed without touching the network.

Why so many columns on the creative tabs: this is a DCO matrix, not a file list.
A creative varies along axes - market, language, audience, daypart, offer, store
- and every axis has to be a column, because it is both an input to the render
(which rush, which text) and a component of the output name. A column you did
not model is a variation you cannot produce.

Columns the engine reads are documented in `docs/SHEET_MODEL.md`. Anything else
on mode2 is injected into the creative as a param, so adding a column there is
how you add a new variable - no code change.
"""
from __future__ import annotations

from . import contract, mode2_html5

# --------------------------------------------------------------------------- #
# mode1 - stitched creatives
# --------------------------------------------------------------------------- #

# DCO axes: what the creative varies along. Free text; they feed both the rush
# selection (via the expander) and the naming template.
MODE1_AXES = [
    "creative_id", "campaign", "advertiser", "market", "language",
    "audience", "daypart", "product", "offer", "price", "cta", "version",
]
MODE1_RENDER = [
    "channel", "format",
    "clip_1", "clip_1_dur", "clip_2", "clip_2_dur", "clip_3", "clip_3_dur",
    "clip_4", "clip_4_dur", "clip_5", "clip_5_dur", "clip_6", "clip_6_dur",
    "transition", "transition_dur", "audio_track",
    "max_duration_s", "still_duration_s",
    "naming_template", "output_name",
]
MODE1_HEADER = MODE1_AXES + MODE1_RENDER + list(contract.WRITE_BACK_COLS)

# --------------------------------------------------------------------------- #
# mode2 - HTML5 variants
# --------------------------------------------------------------------------- #

# Everything here that is not a control column is injected into the template.
MODE2_AXES = [
    "variant_id", "campaign", "advertiser", "market", "language",
    "audience", "daypart", "product", "offer", "price", "cta", "claim",
    "store_id", "store_name", "address", "postal_code", "city",
    "phone", "opening_hours", "logo_url", "image_url",
]
MODE2_RENDER = [
    "template", "channel", "format", "capture", "inject", "duration_s",
    "audio_track", "wait_selector", "wait_ms",
    "naming_template", "output_name",
]
MODE2_HEADER = MODE2_AXES + MODE2_RENDER + list(contract.WRITE_BACK_COLS)

# --------------------------------------------------------------------------- #
# media_dv360 - the trafficking plan
# --------------------------------------------------------------------------- #

# DV360 concepts, in the order a trader fills them. This is a PLANNING tab: it
# is not a Structured Data File. SDF headers are version-specific (v7, v8, ...)
# and must be taken from the SDF template your DV360 instance exports - do not
# assume these names map one to one.
MEDIA_HEADER = [
    "line_id", "status",
    "advertiser_id", "advertiser_name",
    "campaign_id", "campaign_name",
    "io_id", "io_name",
    "line_item_id", "line_item_name", "line_item_type",
    "channel", "environment",
    "start_date", "end_date",
    "budget_type", "budget_amount", "pacing", "pacing_rate",
    "bid_strategy", "bid_amount", "frequency_cap",
    "geo_targeting", "audience_targeting", "daypart_targeting",
    "inventory_source", "deal_id",
    # The bridge back to the creative tabs.
    "creative_source",       # hosted | third_party
    "creative_tab",          # mode1 | mode2
    "creative_id",           # matches creative_id / variant_id on that tab
    "creative_name", "creative_type", "duration_s", "dimensions",
    "hosted_asset",          # the rendition to upload into DV360
    "third_party_vast_url",  # the tag to traffic instead
    "notes",
]

# --------------------------------------------------------------------------- #
# vocabularies (dropdowns)
# --------------------------------------------------------------------------- #

DROPDOWNS = {
    "status": ["pending", "done", "error"],
    "channel": ["dooh", "ctv", "mobile", "web"],
    "format": ["1080x1920", "1920x1080", "3840x2160", "2160x3840", "1080x1080",
               "1280x720", "640x360"],
    "transition": ["fade", "dissolve", "wipeleft", "wiperight",
                   "slideup", "slidedown", "circleopen", "smoothleft"],
    "capture": ["auto", "static", "video", "frames"],
    "inject": ["dom", "query", "window_var", "postmessage"],
    "creative_source": ["hosted", "third_party"],
    "creative_tab": ["mode1", "mode2"],
    "creative_type": ["video", "audio", "display", "native"],
    "line_item_type": ["Display", "Video", "Audio", "Digital out of home"],
    "environment": ["dooh", "ctv", "mobile_app", "mobile_web", "desktop"],
    "budget_type": ["Amount", "Impressions"],
    "pacing": ["Flight", "Daily"],
    "pacing_rate": ["ASAP", "Even", "Ahead"],
    "bid_strategy": ["Fixed", "Maximize conversions", "Maximize viewable impressions",
                     "Beat the deal"],
    "creative_source_col": ["hosted", "third_party"],
}

WIDTHS = {
    "address": 200, "store_name": 170, "opening_hours": 170, "naming_template": 230,
    "output_path": 260, "renditions": 300, "vast_path": 240, "message": 260,
    "input_hash": 130, "rendered_at": 170, "template": 170, "campaign": 140,
    "claim": 150, "wait_selector": 150, "logo_url": 180, "image_url": 180,
    "third_party_vast_url": 300, "hosted_asset": 280, "geo_targeting": 180,
    "audience_targeting": 190, "daypart_targeting": 180, "inventory_source": 180,
    "line_item_name": 200, "campaign_name": 170, "io_name": 170, "notes": 240,
    "creative_name": 190, "advertiser_name": 170,
}
DEFAULT_WIDTH = 115


# --------------------------------------------------------------------------- #
# example rows
# --------------------------------------------------------------------------- #

def _row(header: list[str], **values) -> list[str]:
    """Build a row in header order; anything unset stays empty."""
    unknown = [k for k in values if k not in header]
    assert not unknown, f"example row has columns not in the header: {unknown}"
    return [str(values.get(col, "")) for col in header]


MODE1_EXAMPLES = [
    _row(MODE1_HEADER, creative_id="FR-young-30", campaign="Soldes Été",
         advertiser="Demo Brand", market="FR", language="fr", audience="young",
         offer="-30%", cta="En magasin", version="a", channel="dooh",
         format="1080x1920", clip_1="intro_FR.mp4", clip_2="product_young.mp4",
         clip_3="cta_30.mp4", clip_3_dur="2",
         naming_template="{campaign}_{market}_{audience}_{offer}_{version}",
         status="pending"),
    _row(MODE1_HEADER, creative_id="FR-family-50", campaign="Soldes Été",
         advertiser="Demo Brand", market="FR", language="fr", audience="family",
         offer="-50%", cta="En magasin", version="a", channel="dooh",
         format="1080x1920", clip_1="intro_FR.mp4", clip_2="product_family.mp4",
         clip_3="cta_50.mp4", transition="fade", transition_dur="0.5",
         naming_template="{campaign}_{market}_{audience}_{offer}_{version}",
         status="pending"),
    _row(MODE1_HEADER, creative_id="FR-ctv-30", campaign="Soldes Été",
         advertiser="Demo Brand", market="FR", language="fr", audience="young",
         offer="-30%", version="a", channel="ctv", format="1920x1080",
         clip_1="intro_FR.mp4", clip_2="product_young.mp4", clip_3="cta_30.mp4",
         audio_track="vo_fr.m4a", max_duration_s="15",
         naming_template="{campaign}_{market}_{audience}_{offer}_ctv",
         status="pending"),
]

MODE2_EXAMPLES = [
    _row(MODE2_HEADER, variant_id="lyon3", campaign="Drive to Store",
         advertiser="Demo Brand", market="FR", language="fr", audience="proximity",
         claim="à 5 minutes", store_id="FR-069-003", store_name="Magasin Lyon 3",
         address="12 rue Garibaldi", postal_code="69003", city="Lyon",
         phone="04 72 00 00 00", opening_hours="9h-19h",
         template="example_store.html", channel="dooh", format="1080x1920",
         capture="static", inject="dom",
         naming_template="{campaign}_{store_id}", status="pending"),
    _row(MODE2_HEADER, variant_id="paris11", campaign="Drive to Store",
         advertiser="Demo Brand", market="FR", language="fr", audience="proximity",
         claim="à 3 minutes", store_id="FR-075-011", store_name="Magasin Paris 11",
         address="8 bd Voltaire", postal_code="75011", city="Paris",
         phone="01 40 00 00 00", opening_hours="9h-20h",
         template="example_store.html", channel="dooh", format="1080x1920",
         capture="static", inject="dom",
         naming_template="{campaign}_{store_id}", status="pending"),
    _row(MODE2_HEADER, variant_id="lille1-ctv", campaign="Drive to Store",
         advertiser="Demo Brand", market="FR", language="fr", audience="proximity",
         claim="à 8 minutes", store_id="FR-059-001", store_name="Magasin Lille Centre",
         address="3 rue Faidherbe", postal_code="59000", city="Lille",
         template="example_store.html", channel="ctv", format="1920x1080",
         capture="video", inject="dom", duration_s="10", audio_track="vo_fr.m4a",
         naming_template="{campaign}_{store_id}_ctv", status="pending"),
]

# The media tab pulls the rendered asset straight from the creative tab, so a
# trader never copies a path by hand and a re-render is picked up automatically.
_VAST_LOOKUP = ('=IFERROR(VLOOKUP($AH{r},INDIRECT("\'"&$AG{r}&"\'!A:ZZ"),'
                'MATCH("vast_path",INDIRECT("\'"&$AG{r}&"\'!A1:ZZ1"),0),FALSE),"")')
_ASSET_LOOKUP = ('=IFERROR(VLOOKUP($AH{r},INDIRECT("\'"&$AG{r}&"\'!A:ZZ"),'
                 'MATCH("renditions",INDIRECT("\'"&$AG{r}&"\'!A1:ZZ1"),0),FALSE),"")')


def media_examples() -> list[list[str]]:
    """Two lines: one trafficking a third-party VAST tag, one uploading the asset."""
    rows = []
    common = dict(advertiser_name="Demo Brand", campaign_name="Soldes Été 2026",
                  io_name="IO - Drive to Store FR", start_date="2026-09-01",
                  end_date="2026-09-30", budget_type="Amount", budget_amount="25000",
                  pacing="Flight", pacing_rate="Even", bid_strategy="Fixed",
                  geo_targeting="FR", audience_targeting="proximity 5km",
                  status="pending")
    rows.append(_row(MEDIA_HEADER, line_id="1",
                     line_item_name="LI - DOOH panels FR", line_item_type="Digital out of home",
                     channel="dooh", environment="dooh", bid_amount="12",
                     daypart_targeting="07:00-21:00", inventory_source="DOOH exchange",
                     creative_source="third_party", creative_tab="mode1",
                     creative_id="FR-young-30", creative_name="Soldes FR young -30",
                     creative_type="video", duration_s="10", dimensions="1080x1920",
                     notes="VAST tag pulled from the mode1 tab", **common))
    rows.append(_row(MEDIA_HEADER, line_id="2",
                     line_item_name="LI - CTV FR", line_item_type="Video",
                     channel="ctv", environment="ctv", bid_amount="28",
                     inventory_source="Programmatic Guaranteed", deal_id="",
                     creative_source="hosted", creative_tab="mode1",
                     creative_id="FR-ctv-30", creative_name="Soldes FR CTV -30",
                     creative_type="video", duration_s="15", dimensions="1920x1080",
                     notes="upload the 1920x1080 rendition into DV360", **common))
    # Fill the two bridge columns with the lookups, on the right sheet rows.
    vast_i = MEDIA_HEADER.index("third_party_vast_url")
    asset_i = MEDIA_HEADER.index("hosted_asset")
    for offset, row in enumerate(rows):
        r = offset + 2  # row 1 is the header
        row[vast_i] = _VAST_LOOKUP.format(r=r)
        row[asset_i] = _ASSET_LOOKUP.format(r=r)
    return rows


TABS = {
    "mode1": {"header": MODE1_HEADER, "examples": MODE1_EXAMPLES, "formulas": False},
    "mode2": {"header": MODE2_HEADER, "examples": MODE2_EXAMPLES, "formulas": False},
    "media_dv360": {"header": MEDIA_HEADER, "examples": None, "formulas": True},
}


def tab(name: str) -> dict:
    spec = dict(TABS[name])
    if spec["examples"] is None:
        spec["examples"] = media_examples()
    return spec


def check_model_matches_code() -> None:
    """Guard against the model drifting from what the engine actually reads.

    Two ways this goes wrong silently: a write-back column missing from a
    creative tab (the result is never recorded), and a Mode 2 control column
    missing from its header (it stops steering the render and starts being
    injected into the creative as a param).
    """
    for name in ("mode1", "mode2"):
        header = TABS[name]["header"]
        missing = [c for c in contract.WRITE_BACK_COLS if c not in header]
        assert not missing, f"{name}: missing write-back columns {missing}"
        assert len(header) == len(set(header)), f"{name}: duplicate columns"
    # These are job-level settings, not per-row columns, so they may be absent.
    job_level = {"creative_id", "static_format", "device_scale_factor", "fps"}
    missing = [c for c in mode2_html5.CONTROL_COLS - job_level if c not in MODE2_HEADER]
    assert not missing, f"mode2: missing control columns {missing}"
    for name in TABS:
        assert len(TABS[name]["header"]) == len(set(TABS[name]["header"])), name
