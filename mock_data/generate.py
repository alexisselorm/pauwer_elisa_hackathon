#!/usr/bin/env python3
"""Generate the full mock_data dataset, in dependency order.

    python3 mock_data/generate.py

Deterministic (seed 20261107): two runs produce byte-identical files. Standard library only.
"""
from __future__ import annotations

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "generators"))


def main() -> int:
    import generate_network
    import generate_telemetry
    import generate_context
    import generate_structured
    import generate_knowledge_graph
    import generate_answer_key

    steps = [
        ("network", generate_network.main),
        ("telemetry", generate_telemetry.main),
        ("context (teams/email/pdf/kb/gold)", generate_context.main),
        ("structured (itsm/cmdb/jira/...)", generate_structured.main),
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
        print("ERROR: temporal check failed (validity bounds / record time = event time)")
        return rc

    total = 0
    for dirpath, _dirs, files in os.walk(HERE):
        for f in files:
            total += os.path.getsize(os.path.join(dirpath, f))
    print(f"mock_data: {total / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
