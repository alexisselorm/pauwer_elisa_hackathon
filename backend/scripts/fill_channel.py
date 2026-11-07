"""Fill a channel's history with everyday chatter, so a demo take starts on a lived-in channel
and the earlier test threads scroll out of view.

Each message is a top-level post from a colleague with an initials avatar in their own
colour, never the same person twice in a row. The
chatter names no node, site or fix and never says "fixed", "resolved" or the like, so it
triggers no capture; press Reset in the console within CAPTURE_IDLE_MINUTES (10) of posting
and the graph never even triages it.

Run from backend/ (reads SLACK_BOT_TOKEN and SLACK_CAPTURE_CHANNELS from .env):
    uv run python scripts/fill_channel.py [--count 30] [--delay 1.2] [--dry-run]
    uv run python scripts/fill_channel.py --channel noc-transport noc-alarms
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

from dotenv import dotenv_values
from slack_sdk import WebClient
from slack_sdk.http_retry.builtin_handlers import RateLimitErrorRetryHandler

# Each colleague and their avatar colour (muted, readable with white initials).
PEOPLE = {
    "Aino Laakso": "5B8DEF",
    "Mikko Salminen": "2BA88C",
    "Jenni Heikkilä": "E07A5F",
    "Tuomas Rantanen": "8E6CC9",
    "Laura Mäkinen": "D4A017",
    "Antti Koskinen": "3D7EA6",
    "Riikka Nieminen": "C95D8A",
    "Janne Lehtinen": "6B8E23",
}


def avatar(name: str) -> str:
    """A round avatar with the person's initials on their colour (ui-avatars.com)."""
    query = urlencode({"name": name, "background": PEOPLE[name], "color": "fff", "size": 128})
    return f"https://ui-avatars.com/api/?{query}&rounded=true&bold=true"


def speakers(count: int) -> list[str]:
    """Random colleagues, never the same one twice in a row, so the colours alternate."""
    out: list[str] = []
    for _ in range(count):
        out.append(random.choice([p for p in PEOPLE if not out or p != out[-1]]))  # noqa: S311
    return out


CHATTER = [
    "Morning all ☕",
    "Who's on the night shift this week?",
    "Reminder: team meeting moved to 14:00 today.",
    "Anyone else's VPN slow this morning?",
    "The coffee machine on the 3rd floor is out of beans again.",
    "Handover notes are in the usual place.",
    "I'll be on a training day tomorrow, Mikko covers for me.",
    "Lunch at 11:30? The Thai place is open again.",
    "Can someone share the on-call rota for December?",
    "Thanks for the help yesterday!",
    "Updated my phone number in the contact list.",
    "Heads up, the parking garage card readers are being replaced on Thursday.",
    "Is the quarterly safety training mandatory for contractors too?",
    "Back from holiday, catching up on emails 🙂",
    "Who has the spare laptop charger?",
    "Friday afternoon cake in the kitchen 🎂",
    "Reminder to log your overtime hours before the end of the month.",
    "Has anyone booked the big meeting room for Wednesday?",
    "New hire starts on Monday, please say hi.",
    "I'm working from home today, ping me on chat.",
    "The office wifi password changed, check the intranet.",
    "Good weekend everyone!",
    "Anyone going to the team sauna evening?",
    "The printer on the 2nd floor jammed again, I left a note.",
    "Shift swap: can anyone take my Saturday?",
    "Survey link from HR is in your inbox, takes 5 minutes.",
    "Snow tyres on the service car are on now.",
    "Sorry, missed the call, calling back in 10.",
]


def settings() -> tuple[str | None, str | None]:
    env = {**dotenv_values(Path(__file__).resolve().parents[1] / ".env"), **os.environ}
    channels = [c.strip() for c in (env.get("SLACK_CAPTURE_CHANNELS") or "").split(",")]
    return env.get("SLACK_BOT_TOKEN"), next((c for c in channels if c), None)


def channel_id(client: WebClient, name_or_id: str) -> str:
    """A channel id from `#noc-alarms`, `noc-alarms` or an id (`C0123…`)."""
    name = name_or_id.lstrip("#")
    if name[:1] in "CG" and name.isupper():
        return name
    for page in client.conversations_list(types="public_channel,private_channel", limit=200):
        for c in page["channels"]:
            if c["name"] == name:
                return str(c["id"])
    sys.exit(f"No channel named #{name} visible to the bot (invite it first).")


def chatter(count: int) -> list[tuple[str, str]]:
    """`count` (speaker, line) pairs; no line repeats until all of them ran."""
    deck = random.sample(CHATTER, len(CHATTER))
    return list(zip(speakers(count), [deck[i % len(deck)] for i in range(count)], strict=True))


def main() -> int:
    token, default_channel = settings()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--channel",
        nargs="+",
        default=[default_channel] if default_channel else [],
        help="channels to fill, names or ids (default: the first capture channel)",
    )
    p.add_argument("--count", type=int, default=30, help="messages per channel")
    p.add_argument("--delay", type=float, default=1.2, help="seconds between messages")
    p.add_argument("--dry-run", action="store_true", help="print the messages, post nothing")
    args = p.parse_args()

    if args.dry_run:
        for channel in args.channel or ["(no channel)"]:
            print(f"--- {channel}")
            for name, text in chatter(args.count):
                print(f"{name}: {text}")
        return 0
    if not token or not args.channel:
        sys.exit("Need SLACK_BOT_TOKEN and a channel (--channel or SLACK_CAPTURE_CHANNELS).")

    client = WebClient(token=token)
    client.retry_handlers.append(RateLimitErrorRetryHandler(max_retry_count=5))
    for channel in args.channel:
        cid = channel_id(client, channel)
        print(f"--- {channel} ({cid})")
        for i, (name, text) in enumerate(chatter(args.count)):
            if i:
                time.sleep(args.delay)
            client.chat_postMessage(channel=cid, text=text, username=name, icon_url=avatar(name))
            print(f"{i + 1}/{args.count} {name}: {text}")
    print("\nDone. Press Reset in the console within 10 minutes so none of it is triaged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
