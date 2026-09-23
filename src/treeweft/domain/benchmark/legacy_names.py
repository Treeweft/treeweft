"""Read pre-rebrand agentic results under current names — pure, no I/O.

Results written before the Treeloom -> Treeweft rename name the arm
"treeloom" (and "treeloom-facet", ...) and key comparison fields
"treeloom_*". Rewriting those on load keeps old rows and _summary.json files
aggregating with new ones. Only dict keys and whole-string arm names are
rewritten; free text (answers, file paths) is left alone.
"""
from __future__ import annotations

import re

_LEGACY_KEY = re.compile(r"^treeloom(?=$|[-_])")
_LEGACY_ARM = re.compile(r"^treeloom(-[a-z-]+)?$")


def upgrade_legacy_names(obj):
    """Return a copy of `obj` with legacy treeloom arm names/keys renamed."""
    if isinstance(obj, dict):
        return {
            (_LEGACY_KEY.sub("treeweft", k) if isinstance(k, str) else k): upgrade_legacy_names(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [upgrade_legacy_names(v) for v in obj]
    if isinstance(obj, str) and _LEGACY_ARM.match(obj):
        return "treeweft" + obj[len("treeloom"):]
    return obj
