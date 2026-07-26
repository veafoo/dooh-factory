# Security

The Google Sheet driving this tool is **untrusted input**. It is edited by whoever holds the
link — a colleague, an agency, an intern — and its cells reach a filesystem path resolver, an
FFmpeg argument list, a headless browser and a generated VAST document. The threat model
follows from that.

## Design guarantees

| Surface | Guarantee |
|---|---|
| FFmpeg invocation | `subprocess.run` with an **argument list**, never a shell string. No `shell=True`, no `os.system` anywhere in the codebase. |
| Output filenames | Every stem goes through `naming.sanitize()`, which keeps only `[A-Za-z0-9_-]`. A cell cannot inject a path separator or a traversal sequence into an output path. |
| Asset references | Confined to the assets directory, symlinks resolved (see below). |
| Config | `yaml.safe_load` only. |
| Credentials | Service-account JSON path comes from config, is never logged, and is git-ignored along with `config.yml`. |
| VAST output | URL elements are wrapped in CDATA with the `]]>` sequence neutralised (see below). |
| No dynamic evaluation | No `eval`, `exec`, `pickle`, or `__import__` of sheet-derived names. |

## Audit fixes (2026-07-26)

Both were found in a dedicated review pass, fixed, and locked by tests
(`tests/test_unit.py::TestAssetPathConfinement`, `::TestCdataInjection`).

### Path traversal through an asset reference — fixed

`contract.resolve_asset()` joined a sheet cell against the assets directory and **let absolute
paths through by design**. A cell reading `../../../.ssh/id_rsa` or `/etc/passwd` was therefore
resolved, read, and — this is what makes it more than a read primitive — potentially **stitched
into a creative that then leaves the building**.

The resolver now resolves symlinks (`os.path.realpath`) and rejects anything that does not sit
under the assets directory, raising `AssetOutsideBase` with an actionable message. Absolute
references are rejected for the same reason.

Escape hatch for a legitimate local run where assets live elsewhere:
`DOOH_ALLOW_ASSETS_OUTSIDE_BASE=1`. It is an environment variable rather than a config key on
purpose — that makes the exception explicit per invocation, instead of something a checked-in
config could carry silently forever.

### CDATA injection in generated VAST — fixed

Tracking URLs from the sheet were inserted verbatim into `<![CDATA[...]]>`. A URL containing
`]]>` closed the CDATA section early: at best the XML was malformed and the tag rejected by the
player, at worst the remainder was parsed as markup and **injected elements into the VAST**
served to end users.

The sequence is now split across two CDATA sections (`]]]]><![CDATA[>`), the standard fix. The
test asserts the property that matters — the document parses, no element is injected, and the
URL text round-trips verbatim — rather than a fragile string comparison.

## Operational notes

- **Mode 2 loads pages in a headless browser.** A template reference may be an `http(s)://`
  URL, so it is executed as web content. Point it at creatives you control.
- **Output directory is config-driven**, not sheet-driven. A cell cannot redirect where files
  are written.
- **Failure is loud.** Missing clips are detected before FFmpeg is invoked, and a rejected
  asset reference stops the row with an explicit message rather than rendering something wrong.

## Reporting

Private personal repository. Security remarks go directly to its author.
