"""Demo chat 2: a thread that creates NEW knowledge, with a validity window.

Sanna (facilities) tells Veikko that the Lappeenranta transport room (TRN-019) is being
renovated: from next Monday, for eleven days, the main entrance is closed and you get in
through the loading dock with a guard-booth badge and a contractor escort. Nothing in the
corpus covers this, so kaiku captures a new assertion bound to TRN-019 / Lappeenranta,
with valid_from = next Monday and valid_to = the day the main entrance reopens (00:00
Europe/Helsinki, end exclusive). The dates are computed when you run it, so the window
is always in the near future. "That solved it" makes kaiku triage the thread right away.

Show it afterwards: in the console (http://localhost:8080), drag the "As of" slider into
and out of the window. Or ask the bot: @kaiku How do I get into the Lappeenranta site
next week?

Run from backend/ (reads SLACK_BOT_TOKEN and SLACK_CAPTURE_CHANNELS from .env):
    uv run python scripts/demo_new_knowledge.py [--channel C0123] [--delay 4] [--dry-run]
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
FACILITIES = (":office:", "Sanna Korhonen")


def _day(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B}"  # "Monday 9 November"


def chat(today: date) -> list[tuple[tuple[str, str], str]]:
    start = today + timedelta(days=7 - today.weekday())  # next Monday, never today
    reopen = start + timedelta(days=11)  # the Friday after: first day back to normal
    last = reopen - timedelta(days=1)
    return [
        (
            FIELD,
            "I need to get into the Lappeenranta transport room next week to check TRN-019. "
            "Anything I should know about access?",
        ),
        (
            FACILITIES,
            f"Yes, heads up: the building is being renovated from {_day(start)} through "
            f"{_day(last)}. The main entrance is closed for that whole time and your key card "
            "won't open it.",
        ),
        (FIELD, "So how do I get in?"),
        (
            FACILITIES,
            "Use the loading dock at the back of the building. Pick up a visitor badge at the "
            "guard booth first, and inside you need an escort from the renovation contractor. "
            "Book the escort at least one day ahead through the contractor's site office.",
        ),
        (FIELD, "And after that it's back to normal?"),
        (
            FACILITIES,
            f"Yes, from {_day(reopen)} the main entrance and the normal key card work again.",
        ),
        (FIELD, "Great, that solved it. Escort booked for Tuesday. Thanks Sanna!"),
    ]


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

    messages = chat(date.today())
    if args.dry_run:
        for (_, name), text in messages:
            print(f"{name}: {text}")
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
        print(f"{i + 1}. {name}: {text.splitlines()[0][:70]}")
    print(f"\nPosted in {args.channel}, thread {root}. The context card should follow shortly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
