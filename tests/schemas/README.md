# Vendored schemas

`vast_4.2.xsd` is the official IAB Tech Lab VAST 4.2 schema, from
https://github.com/InteractiveAdvertisingBureau/vast (`vast_4.2.xsd`, version
4.2.0). Vendored so `tests/test_vast_schema.py` runs offline and stays
reproducible: a validation suite that needs the network is a validation suite
that gets skipped.

Refresh it from upstream when moving to another VAST version.
