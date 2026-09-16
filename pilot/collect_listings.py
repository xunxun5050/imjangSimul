#!/usr/bin/env python3
"""
국토교통부 아파트 매매 실거래가 상세 자료 → listings.json

    export MOLIT_KEY="발급받은 디코딩 서비스키"
    python3 collect_listings.py --lawd 11350 --months 12 --out listings.json

의존성 없음(표준 라이브러리만 사용).

처리 순서:
    1. 법정동코드(--lawd) + 최근 N개월(--months)의 실거래를 월별로 수집
    2. 해제여부=O(취소 거래) 제외
    3. (법정동, 단지명, round(전용면적)) 로 그룹핑, 평형당 거래금액 중앙값 산출
    4. 평형당 표본이 --min-deals 미만이면 버림
    5. listings.json 으로 저장 (§4.3 스키마)

DONG_META 는 법정동별 x, y, t, edu, inf, env, fut 를 손으로 채운 표다.
절차적 모드의 REGIONS 레코드를 그대로 대체하므로, 새 구를 추가할 때
유일하게 사람이 판단해야 하는 부분이다. README.md §5 참고.

주의: 이 API도 K-apt 서비스들처럼 엔드포인트·태그명이 몇 차례 바뀐 이력이 있다.
아래 후보를 순서대로 찔러보고 되는 걸 쓰지만, 전부 실패하면 포털 활용가이드에서
현재 스펙을 확인해 BASES/ALIAS 상수를 고칠 것.
"""

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import date

BASES = [
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev",
    "https://apis.data.go.kr/1613000/RTMSDataSvcAptTrade",
]
OP = "getRTMSDataSvcAptTradeDev"
OP_FALLBACK = "getRTMSDataSvcAptTrade"

# 법정동별 인적 판단값. 새 구를 추가하려면 이 표에 항목을 더한다 (README §5).
DONG_META = {
    "11350": {  # 노원구
        "_zone": "서울", "_gu": "노원구",
        "상계동": {"x": 5, "y": 10, "t": 1.12, "edu": 3, "inf": 3, "env": 4, "fut": 3},
        "중계동": {"x": 6, "y": 8, "t": 1.10, "edu": 5, "inf": 3, "env": 4, "fut": 3},
        "하계동": {"x": 6, "y": 7, "t": 1.08, "edu": 4, "inf": 3, "env": 3, "fut": 3},
        "공릉동": {"x": 7, "y": 6, "t": 1.08, "edu": 3, "inf": 3, "env": 3, "fut": 3},
        "월계동": {"x": 4, "y": 7, "t": 1.10, "edu": 2, "inf": 2, "env": 3, "fut": 4},
    },
}

# 논리 필드 -> 실제 응답 태그 후보. 앞에서부터 찾아써서 버전 차이를 흡수한다.
ALIAS = {
    "dong":   ["umdNm", "법정동"],
    "apt":    ["aptNm", "aptDong", "아파트"],
    "area":   ["excluUseAr", "전용면적"],
    "amount": ["dealAmount", "거래금액"],
    "year":   ["dealYear", "년"],
    "month":  ["dealMonth", "월"],
    "day":    ["dealDay", "일"],
    "floor":  ["floor", "층"],
    "build":  ["buildYear", "건축년도"],
    "cancel": ["cdealType", "해제여부"],
}


def call(url, params, timeout=20):
    qs = urllib.parse.urlencode(params, safe="")
    req = urllib.request.Request(url + "?" + qs, headers={"User-Agent": "imjang-sim/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "replace")


def rows_of(raw):
    """JSON/XML 어느 쪽으로 오든 item 딕셔너리 리스트로 정규화. (enrich_kapt.py 와 동일 로직)"""
    raw = raw.strip()
    if raw.startswith("{"):
        d = json.loads(raw)
        body = (d.get("response") or {}).get("body") or d.get("body") or {}
        items = body.get("items") or body.get("item") or []
        if isinstance(items, dict):
            items = items.get("item", [])
        if isinstance(items, dict):
            items = [items]
        return items or []
    root = ET.fromstring(raw)
    code = root.findtext(".//resultCode") or root.findtext(".//returnReasonCode")
    if code not in (None, "00", "000"):
        msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or "?"
        raise RuntimeError(f"{code}: {msg}")
    out = []
    for it in root.findall(".//item"):
        out.append({c.tag: (c.text or "").strip() for c in it})
    return out


def field(row, key, default=""):
    for tag in ALIAS[key]:
        if tag in row and row[tag] not in (None, ""):
            return row[tag]
    return default


def to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(v):
    f = to_float(v)
    return int(f) if f is not None else None


def recent_months(n):
    """오늘 기준 최근 n개월을 YYYYMM 문자열 오름차순으로."""
    today = date.today()
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    return list(reversed(out))


def fetch_month(lawd, ymd, key):
    """월 1건, 페이지네이션까지 처리. Dev 엔드포인트 실패 시 일반 엔드포인트로 폴백."""
    op_by_base = {BASES[0]: OP, BASES[1]: OP_FALLBACK}
    last_err = None
    for base in BASES:
        op = op_by_base[base]
        try:
            rows, page = [], 1
            while True:
                raw = call(f"{base}/{op}",
                           {"LAWD_CD": lawd, "DEAL_YMD": ymd, "pageNo": page,
                            "numOfRows": 1000, "serviceKey": key})
                got = rows_of(raw)
                rows += got
                if len(got) < 1000:
                    break
                page += 1
                time.sleep(0.15)
            return rows
        except Exception as e:
            last_err = e
    raise RuntimeError(f"{ymd} 조회 실패 (모든 엔드포인트): {last_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lawd", default="11350", help="법정동코드 앞 5자리")
    ap.add_argument("--months", type=int, default=12, help="최근 N개월")
    ap.add_argument("--min-deals", type=int, default=3, help="평형당 최소 거래건수")
    ap.add_argument("--out", default="listings.json")
    ap.add_argument("--gu", default=None, help="표시용 구 이름. 생략하면 DONG_META의 _gu를 쓴다")
    ap.add_argument("--ask-premium", type=float, default=1.05)
    ap.add_argument("--key", default=os.environ.get("MOLIT_KEY"))
    args = ap.parse_args()

    if not args.key:
        raise SystemExit("서비스키가 없습니다. MOLIT_KEY 환경변수 또는 --key 를 주세요.")

    meta_table = DONG_META.get(args.lawd)
    if not meta_table:
        print(f"경고: DONG_META에 {args.lawd} 항목이 없습니다. "
              f"dongMeta가 빈 채로 저장되고, 그 동의 거래는 게임에서 조용히 버려집니다. "
              f"README.md §5 참고해서 채워 넣으세요.", file=sys.stderr)
        meta_table = {"_zone": "서울", "_gu": ""}
    zone = meta_table.get("_zone", "서울")
    gu = args.gu or meta_table.get("_gu", "")
    dong_meta = {k: v for k, v in meta_table.items() if not k.startswith("_")}

    months = recent_months(args.months)
    print(f"[1/2] 실거래 수집: {args.lawd} 최근 {args.months}개월", file=sys.stderr)
    all_rows = []
    for ymd in months:
        rows = fetch_month(args.lawd, ymd, args.key)
        print(f"  {ymd}   {len(rows)}건", file=sys.stderr)
        all_rows += rows
        time.sleep(0.1)

    valid = [r for r in all_rows if field(r, "cancel").strip() != "O"]
    print(f"      유효 거래 {len(valid):,}건", file=sys.stderr)

    print("[2/2] 단지·평형 그룹핑", file=sys.stderr)
    groups = {}  # (dong, apt, round(m2)) -> list[row]
    for r in valid:
        dong = field(r, "dong")
        apt = field(r, "apt")
        m2 = to_float(field(r, "area"))
        amt = to_int(field(r, "amount"))
        if not dong or not apt or m2 is None or amt is None:
            continue
        if dong not in dong_meta:
            continue  # DONG_META에 없는 법정동은 조용히 버린다
        key = (dong, apt, round(m2))
        groups.setdefault(key, []).append({
            "m2": m2, "amount": amt,
            "floor": to_int(field(r, "floor")),
            "build": to_int(field(r, "build")),
            "ym": f"{field(r,'year')}{int(field(r,'month') or 0):02d}" if field(r, "year") else "",
        })

    complexes = {}
    for (dong, apt, _), rows in groups.items():
        if len(rows) < args.min_deals:
            continue
        amounts = sorted(r["amount"] for r in rows)
        n = len(amounts)
        median = amounts[n // 2] if n % 2 else round((amounts[n // 2 - 1] + amounts[n // 2]) / 2)
        m2_avg = round(sum(r["m2"] for r in rows) / n, 1)
        floors = sorted({r["floor"] for r in rows if r["floor"] is not None})
        last_ym = max((r["ym"] for r in rows if r["ym"]), default="")
        builds = [r["build"] for r in rows if r["build"]]
        build_year = Counter(builds).most_common(1)[0][0] if builds else None

        key = (dong, apt)
        c = complexes.setdefault(key, {
            "id": f"{args.lawd}-{dong}-{apt}",
            "apt": apt, "dong": dong, "buildYear": build_year, "sizes": [],
        })
        c["sizes"].append({
            "m2": m2_avg, "py": round(m2_avg / 3.305785, 1), "n": n,
            "median": median, "min": amounts[0], "max": amounts[-1],
            "floors": floors, "lastYm": last_ym,
        })

    doc = {
        "meta": {
            "source": "molit-rtms-apt-trade-dev",
            "lawdCd": args.lawd, "gu": gu, "zone": zone,
            "months": args.months, "minDeals": args.min_deals,
            "askPremium": args.ask_premium,
            "generatedAt": date.today().isoformat(),
        },
        "dongMeta": dong_meta,
        "complexes": list(complexes.values()),
    }
    json.dump(doc, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    n_sizes = sum(len(c["sizes"]) for c in doc["complexes"])
    print(f"완료 → {args.out}  (단지 {len(doc['complexes'])}개 / 평형 {n_sizes}종)", file=sys.stderr)
    if not doc["complexes"]:
        print("  단지가 0개입니다. --min-deals를 낮추거나 DONG_META/법정동코드를 확인하세요.", file=sys.stderr)


if __name__ == "__main__":
    main()
