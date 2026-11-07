"""When a thread is worth reading (plan 001 WS2, 002 §3.2): a resolution signal in a message
or a reaction, or the thread going idle. Deterministic, so trigger precision (ST-03) and the
60 s card latency (ST-01) are testable without an LLM.

A 👍 alone is not a signal: people thumbs-up lunch plans (held-out H-04). "Traffic is back 👍"
is, because of the words.
"""

from __future__ import annotations

import re

_RESOLUTION = re.compile(
    r"\b(traffic is back|traffic('s| is) (flowing|moving)|counters (are )?(moving|increasing)"
    r"|fixed( it| now)?|resolved|solved|works now|working again|that did it|that worked"
    r"|back (up|online)|all good now|toimii)\b",
    re.IGNORECASE,
)
# Slack reaction names, and the emoji themselves (Teams exports carry the character).
RESOLUTION_REACTIONS = frozenset({"white_check_mark", "heavy_check_mark", "tada", "✅", "✔️", "🎉"})


def is_resolution(text: str) -> bool:
    return bool(_RESOLUTION.search(text or ""))


def is_resolution_reaction(name: str) -> bool:
    return name.split("::")[0] in RESOLUTION_REACTIONS  # "white_check_mark::skin-tone-2"
