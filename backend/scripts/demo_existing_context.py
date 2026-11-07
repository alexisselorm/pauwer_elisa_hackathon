"""Demo chat 1: a thread about knowledge kaiku ALREADY has.

Veikko is about to drive to Kemi and asks whether the gate is still on Pekka's padlock.
Erkki answers with what the graph already holds about the Kemi site: the gate was on
Pekka's padlock, and since 2026-08-03 it is a code lock that Heikki Lehtonen (site
security) gives out per visit, recorded by an email of 2026-08-18. That knowledge is in
the demo baseline: the console's Reset loads it at once, and `kaiku backfill` (demo
profile) yields it once the capture loop has triaged the Kemi threads. The thread ends
with "All good now", so kaiku triages it right away. Expect the card to corroborate the
existing rule ("+1 source") rather than add a new one.

Then ask the bot yourself, as a real user, in the same channel:
    @kaiku How do I get through the gate at the Kemi site?

Run from backend/ (reads SLACK_BOT_TOKEN and SLACK_CAPTURE_CHANNELS from .env):
    uv run python scripts/demo_existing_context.py [--channel C0123] [--delay 4] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from slack_sdk import WebClient

FIELD = (":construction_worker:", "Veikko Vikkelä")
EXPERT = (":mage:", "Erkki Expert")

CHAT: list[tuple[tuple[str, str], str]] = [
    (
        FIELD,
        "Heading up to Kemi this afternoon to look at TRN-042. Is the transport room gate "
        "still on Pekka's padlock? Don't want to drive two hours to a locked gate.",
    ),
    (
        EXPERT,
        "Not anymore. They put a code lock on that gate in August, there's no key at all now.",
    ),
    (FIELD, "Ok. Who has the code then?"),
    (
        EXPERT,
        "Heikki Lehtonen from site security. Call him before you leave, he gives the code out "
        "per visit.",
    ),
    (FIELD, "Called Heikki and got the code. Gate opened, I'm in. All good now 👍"),
]

FOLLOW_UP = "@kaiku How do I get through the gate at the Kemi site?"


def settings() -> tuple[str | None, str | None]:
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    channels = [c.strip() for c in (env.get("SLACK_CAPTURE_CHANNELS") or "").split(",")]
    return env.get("SLACK_BOT_TOKEN"), next((c for c in channels if c), None)


def main() -> int:
    token, default_channel = settings()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", default=default_channel, help="channel id to post into")
    p.add_argument("--delay", type=float, default=4.0, help="seconds between messages")
    p.add_argument("--dry-run", action="store_true", help="print the chat, post nothing")
    args = p.parse_args()

    if args.dry_run:
        for (_, name), text in CHAT:
            print(f"{name}: {text}")
        print(f"\nThen type: {FOLLOW_UP}")
        return 0
    if not token or not args.channel:
        sys.exit("Need SLACK_BOT_TOKEN and a channel (--channel or SLACK_CAPTURE_CHANNELS).")

    client = WebClient(token=token)
    root: str | None = None
    for i, ((icon, name), text) in enumerate(CHAT):
        if i:
            time.sleep(args.delay)
        out = client.chat_postMessage(
            channel=args.channel, text=text, thread_ts=root, username=name, icon_emoji=icon
        )
        root = root or out["ts"]
        print(f"{i + 1}. {name}: {text.splitlines()[0][:70]}")
    print(f"\nPosted in {args.channel}, thread {root}.")
    print(f"Now ask the bot yourself in that channel:\n    {FOLLOW_UP}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
