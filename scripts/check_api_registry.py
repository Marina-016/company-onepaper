#!/usr/bin/env python3
"""
接口注册表体检：把 a_share_fetch_data.ALL_API_NAMES 里的每个 nameEn 拿到
生产实际使用的 META_BASE 上解一遍，解不出来的直接报出来。

为什么需要它
------------
接口名只在真跑一次公司一页纸时才会被验证，而注册表被裁剪时不会有人通知——
`batchGetReportContent` 被拆成 Domestic/Foreign 之后，旧名字在 warm 列表里躺了
很久没人发现（缺失的 URL 只会让 meta 里少一条，不报错、不中断）。
`tests/` 下的回归覆盖的是写作与溯源逻辑，不触网解析接口名，替代不了本检查。

用法：
    DATAYES_TOKEN=... python3 scripts/check_api_registry.py
    python3 scripts/check_api_registry.py --token <TOKEN>

全部解析成功退出码 0，有缺失退出码 1（可直接挂 CI / pre-release 检查）。
"""
from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # robowork 侧叫 a_share_fetch_data，上游 aladdin-llm-skills 侧叫 fetch_data
    import a_share_fetch_data as F  # noqa: E402
except ImportError:  # pragma: no cover
    import fetch_data as F  # noqa: E402


WHITELIST_BASE = "https://gw.datayes.com/aladdin_llm_mgmt/web/whitelist/api"


def _resolve_on(base: str, name: str, token: str) -> tuple[str, str]:
    """→ (httpUrl, error)。空 url 表示该注册表没有这个 nameEn。"""
    rj, _, err = F.call("GET", base, token, params={"nameEn": name})
    if err:
        return "", str(err)[:160]
    data = (rj or {}).get("data") or {}
    url = data.get("httpUrl") or ""
    if url:
        return url, ""
    return "", f"code={(rj or {}).get('code')} message={(rj or {}).get('message')}"


def resolve(name: str, token: str) -> tuple[str, str, str, bool]:
    """→ (name, httpUrl, error, on_whitelist)。

    除了本 skill 实际使用的 META_BASE，额外在 whitelist 上解一次：mgr 是 whitelist
    的超集，只在 mgr 上有的接口意味着「本机（机构账号）能调、换个账号类型可能不能」，
    也意味着一旦 META_BASE 切回 whitelist 就会断。这类名字要报出来但不算失败。
    """
    url, err = _resolve_on(F.META_BASE, name, token)
    if F.META_BASE == WHITELIST_BASE:
        return name, url, err, bool(url)
    wl_url, _ = _resolve_on(WHITELIST_BASE, name, token)
    return name, url, err, bool(wl_url)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", default=os.environ.get("DATAYES_TOKEN"))
    args = parser.parse_args()
    if not args.token:
        print("缺少 token：设置 DATAYES_TOKEN 或传 --token", file=sys.stderr)
        return 2

    names = list(F.ALL_API_NAMES)
    print(f"注册表: {F.META_BASE}")
    print(f"待检 nameEn: {len(names)} 个\n")

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda n: resolve(n, args.token), names))

    missing = [(n, e) for n, u, e, _ in results if not u]
    mgr_only = [n for n, u, _, wl in results if u and not wl]
    for name, url, err, on_wl in sorted(results):
        if not url:
            tag = "MISS"
        elif on_wl:
            tag = "OK  "
        else:
            tag = "MGR!"
        print(f"  {tag} {name:44s} {url or err}")

    print()
    print(f"解析成功 {len(names) - len(missing)}/{len(names)}")
    if mgr_only:
        print(
            f"\n⚠️ {len(mgr_only)} 个 nameEn 只在 mgr 上有、不在 whitelist 上："
            f"{', '.join(sorted(mgr_only))}",
        )
        print(
            "   这些接口在本机（机构账号）能调通，但权限按账号类型分级，"
            "且 META_BASE 一旦切回 whitelist 就会断。",
        )
    if missing:
        print("\n以下 nameEn 解不出来，需要改名或从 ALL_API_NAMES 移除：", file=sys.stderr)
        for name, err in missing:
            print(f"  - {name}: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
