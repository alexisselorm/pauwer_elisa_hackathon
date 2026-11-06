#!/usr/bin/env python3
"""Generate the full mock_data dataset, in dependency order.

    python3 mock_data/generate.py [--with-external]

Deterministic (seed 20261107). Standard library only.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "generators"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-external", action="store_true",
                    help="also fetch real FMI weather + Nord Pool FI prices (needs internet)")
    args = ap.parse_args()

    import generate_network
    import generate_telemetry
    import generate_context
    import generate_structured
    import generate_alarm_banks
    import generate_knowledge_graph
    import generate_answer_key

    if args.with_external:
        scripts = os.path.join(HERE, "scripts")
        sys.path.insert(0, scripts)
        import fetch_external
        t = time.time()
        fetch_external.main()
        print(f"external: {time.time() - t:.0f}s")

    steps = [
        ("network", generate_network.main),
        ("telemetry", generate_telemetry.main),
        ("context (teams/email/pdf/kb/gold)", generate_context.main),
        ("structured (itsm/cmdb/jira/...)", generate_structured.main),
        ("alarm banks", generate_alarm_banks.main),
        ("knowledge graph helpers", generate_knowledge_graph.main),
        ("answer key (documented vs not documented)", generate_answer_key.main),
    ]
    for name, fn in steps:
        t = time.time()
        fn()
        print(f"[{name}] done in {time.time() - t:.1f}s", flush=True)

    # CI-grade integrity checks
    sys.path.insert(0, os.path.join(HERE, "scripts"))
    import verify_msg
    rc = verify_msg.main(HERE)
    if rc:
        print("ERROR: .msg integrity check failed")
        return rc
    import verify_temporal
    rc = verify_temporal.main(HERE)
    if rc:
        print("ERROR: valid_from/valid_to check failed")
        return rc

    total = 0
    for dirpath, _dirs, files in os.walk(HERE):
        for f in files:
            total += os.path.getsize(os.path.join(dirpath, f))
    print(f"mock_data: {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
