"""Round 56: text from outside (channel names, video titles, transcripts, names) made safe to
put in a caption file, an .srt, an email subject or a page.

Found by running the "big list of naughty strings" through every text sink (tests/test_round56.py):
markup was already escaped everywhere, but raw control characters (NUL, escape codes, vertical
tab) and Unicode direction overrides passed straight into .ass/.srt files and email subjects. A
NUL can cut a caption file short; a right-to-left override flips how the rest of a line reads.
"""
from __future__ import annotations

import re

# C0/C1 control characters except tab and newline (handled by the caller), and DEL.
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Explicit direction overrides and isolates (LRE RLE PDF LRO RLO, LRI RLI FSI PDI) and the BOM.
# Zero-width joiners stay: emoji and many scripts need them.
_DIRECTION = re.compile(r"[‪-‮⁦-⁩﻿]")


def strip_controls(text: object, keep_newlines: bool = False) -> str:
    """``text`` without control characters or direction overrides. Control characters become a
    space (so words don't run together); newlines and tabs become spaces unless ``keep_newlines``."""
    value = _DIRECTION.sub("", str(text if text is not None else ""))
    if not keep_newlines:
        value = value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    else:
        value = value.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROLS.sub(" ", value)
