"""htrace — Hermes Agent trace builder.

Reads Hermes session storage (state.db per profile) and shell-hook JSONL
captures, normalises them into one trace document (``htrace/1``) that the
static viewer in this repo renders.

Stdlib only. No network. No writes outside the output path.
"""

__version__ = "0.1.0"
SCHEMA = "htrace/1"
