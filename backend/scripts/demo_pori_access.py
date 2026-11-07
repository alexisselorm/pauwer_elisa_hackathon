"""Demo video, scene 2: new site-access knowledge for Pori, with an end date.

Veikko (field) asks Erkki (expert) about the Pori transport room (TRN-017). Street works
close the front gate until a Friday about two weeks out; until then you get in through the
back gate, whose key box code the NOC duty phone gives out. Nothing in the corpus covers
Pori access, so kaiku captures a NEW assertion bound to TRN-017 with valid_to = that
Friday (00:00 Europe/Helsinki, end exclusive). "That solved it" makes kaiku triage the
thread at once: the card lands in the thread and the console graph adds the node live,
within about a minute, with no refresh.

The window always covers next Monday, the visit day of scene 3's question, which this
script prints at the end so you can paste it in the asking channel.

Run from backend/ (reads SLACK_BOT_TOKEN and SLACK_CAPTURE_CHANNELS from .env):
    uv run python scripts/demo_pori_access.py [--channel C0123] [--delay 5] [--dry-run]
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import dotenv_values
from slack_sdk import WebClient

FIELD = (":construction_worker:", "Veikko Vikkelä")
EXPERT = (":mage:", "Erkki Expert")


def _day(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B}"  # "Friday 20 November"


def dates(today: date) -> tuple[date, date]:
    """Next Monday (the scene 3 visit, never today) and the Friday of the week after."""
    monday = today + timedelta(days=7 - today.weekday())
    return monday, monday + timedelta(days=11)


def chat(today: date) -> list[tuple[tuple[str, str], str]]:
    _, reopen = dates(today)
    return [
        (
            FIELD,
            "I have to go to the Pori transport room (TRN-017) next week. "
            "Is the front gate open as usual?",
        ),
        (
            EXPERT,
            f"No, street works have closed the front gate until {_day(reopen)}. "
            "Use the back gate from the parking lot instead.",
        ),
        (FIELD, "Is the back gate locked?"),
        (
            EXPERT,
            "Yes, there's a key box on the back gate. Call the NOC duty phone and they give "
            "you the key box code. Don't try the front gate, the contractor has fenced it off.",
        ),
        (FIELD, "Got it, that solved it. Thanks!"),
    ]


def scene3_question(today: date) -> str:
    monday, _ = dates(today)
    return (
        f"@kaiku I'm driving to the Pori node on {_day(monday)} at 8:00 because port 1/1/2 "
        "has had no traffic since its SFP was replaced. What should I do on site and what "
        "should I avoid, does the live telemetry actually match that, how do I get into the "
        f"building on {_day(monday)}, and has that fix been confirmed anywhere?"
    )


def settings() -> tuple[str | None, str | None]:
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    channels = [c.strip() for c in (env.get("SLACK_CAPTURE_CHANNELS") or "").split(",")]
    return env.get("SLACK_BOT_TOKEN"), next((c for c in channels if c), None)


def main() -> int:
    token, default_channel = settings()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--channel", default=default_channel, help="channel id to post into")
    p.add_argument("--delay", type=float, default=5.0, help="seconds between messages")
    p.add_argument("--dry-run", action="store_true", help="print the chat, post nothing")
    args = p.parse_args()

    today = date.today()
    messages = chat(today)
    if args.dry_run:
        for (_, name), text in messages:
            print(f"{name}: {text}")
        print(f"\nScene 3, in the asking channel:\n    {scene3_question(today)}")
        return 0
    if not token or not args.channel:
        sys.exit("Need SLACK_BOT_TOKEN and a channel (--channel or SLACK_CAPTURE_CHANNELS).")

    client = WebClient(token=token)
    root: str | None = None
    for i, ((icon, name), text) in enumerate(messages):
        if i:
            time.sleep(args.delay)
        out = client.chat_postMessage(
            channel=args.channel, text=text, thread_ts=root, username=name, icon_emoji=icon
        )
        root = root or out["ts"]
        print(f"{i + 1}. {name}: {text[:70]}")
    print(f"\nPosted in {args.channel}, thread {root}. The card and the graph node follow shortly.")
    print(f"Scene 3, in the asking channel:\n    {scene3_question(today)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
