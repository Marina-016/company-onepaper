#!/usr/bin/env python3
"""Run only the getMaterialsV2 multi-query retrieval for a company."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys

from fetch_materials import (
    DatayesClient,
    choose_company,
    collect_materials_v2,
    find_token,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch focused getMaterialsV2 snippets for a HK/US one-pager.")
    parser.add_argument("--company", default="", help="Company name, e.g. 智谱 or NVIDIA")
    parser.add_argument("--ticker", default="", help="Ticker, e.g. 02513.HK or NVDA")
    parser.add_argument("--market", default="auto", choices=["auto", "HK", "US", "hk", "us"])
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--size", type=int, default=10)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    errors: list[dict[str, str]] = []
    client = DatayesClient(find_token())
    target = choose_company(args, client, errors)
    data = {
        "__meta__": {
            "company": target["company"],
            "ticker": target["ticker"],
            "market": target["market"],
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        },
        "materials_v2": collect_materials_v2(
            client,
            target["company"],
            target["ticker"],
            target["market"],
            errors,
            args.days,
            args.size,
        ),
        "errors": errors,
    }

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
