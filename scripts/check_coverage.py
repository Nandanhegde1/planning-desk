"""Map every stated requirement to the tool that serves it.

python scripts/check_coverage.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.mcp_host import MCPHost

MATRIX = [
    ("A1.1 outdoor/indoor match on X DateTime feasible", ["feasibility__check_activity_window"]),
    ("A1.2 travel to location on X DateTime feasible", ["feasibility__check_travel_plan"]),
    (
        "A1.2 ... what are the other better alternatives",
        ["feasibility__suggest_better_windows", "feasibility__suggest_alternative_destinations"],
    ),
    (
        "A1.3 destinations by cost and season",
        [
            "discovery__find_destinations_for_month",
            "discovery__shortlist_destinations",
            "discovery__rate_destination_season",
        ],
    ),
    (
        "A2.1 USD to INR 2 years into spreadsheet or doc",
        ["fx__get_rate_series", "fx__export_spreadsheet", "fx__export_document"],
    ),
    ("A2.2 trendline on that rate", ["fx__compute_trendline"]),
    ("A2.3 INR/GBP + INR/EUR, comparable trendline", ["fx__compare_pairs"]),
    ("A2.4 a different graph as deemed fit", ["fx__render_chart"]),
    ("Both: external API for live information", ["discovery__web_search"]),
]


async def main():
    async with MCPHost() as host:
        have = set(host.tool_names)
        print(f"{'REQUIREMENT':<52} TOOLS")
        print("-" * 96)
        ok = True
        for need, tools in MATRIX:
            missing = [t for t in tools if t not in have]
            ok &= not missing
            mark = "yes" if not missing else "MISSING " + ", ".join(missing)
            print(f"{need:<52} {mark}")
            for t in tools:
                if t in have:
                    print(f"{'':<52}   {t}")
        print("-" * 96)
        print("all requirements mapped to a live tool:", ok)


asyncio.run(main())
