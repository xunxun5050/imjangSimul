#!/usr/bin/env python3
"""
K-apt(공동주택관리정보시스템) 단지정보로 listings.json 보강

    export KAPT_KEY="발급받은 디코딩 서비스키"
    python3 enrich_kapt.py --in listings.json --out listings.json

채워지는 값:
    units             세대수            (기본정보 kaptdaCnt)
    dongCnt           동수              (기본정보 kaptDongCnt)
    buildYear         사용승인일 기준    (기본정보 kaptUsedate, 실거래 건축년도보다 정확)
    parkingPerUnit    세대당 주차대수    (상세정보 kaptdPcnt + kaptdPcntu)
    subwayStation     인접 역명          (상세정보 subwayStation)
    subwayWalkMin     역까지 도보 분      (상세정보 kaptdWtimesub)
    heatName          난방방식
    totalFloors       실거래 최대 관측층에서 추정 (K-apt에는 최고층 정보가 없음)

필요한 활용신청 2건:
    국토교통부_공동주택 단지 목록제공 서비스   (data.go.kr/data/15057332)
    국토교통부_공동주택 기본 정보제공 서비스   (data.go.kr/data/15058453)

주의: 이 두 서비스는 엔드포인트 버전이 여러 번 바뀐 이력이 있다(AptListService2/3,
AptBasisInfoService1/V2/V3 등). 아래 후보를 순서대로 찔러보고 되는 걸 캐시하지만,
전부 실패하면 포털의 활용가이드 문서에서 현재 경로를 확인해 BASES 상수를 고칠 것.
"""

import argparse
import difflib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

LIST_BASES = [
    "https://apis.data.go.kr/1613000/AptListService3",
    "https://apis.data.go.kr/1613000/AptListService2",
]
LIST_OPS = ["getSigunguAptList", "getLegaldongAptList"]

INFO_BASES = [
    "https://apis.data.go.kr/1613000/AptBasisInfoServiceV3",
    "https://apis.data.go.kr/1613000/AptBasisInfoServiceV2",
    "https://apis.data.go.kr/1613000/AptBasisInfoService1",
]
BASS_OPS = ["getAphusBassInfoV3", "getAphusBassInfoV2", "getAphusBassInfoV1", "getAphusBassInfo"]
DTL_OPS = ["getAphusDtlInfoV3", "getAphusDtlInfoV2", "getAphusDtlInfoV1", "getAphusDtlInfo"]

RESOLVED = {}   # 통한 경로 캐시


# --------------------------------------------------------------------------- 통신
def call(url, params, timeout=20):
    qs = urllib.parse.urlencode(params, safe="")
    req = urllib.request.Request(url + "?" + qs, headers={"User-Agent": "imjang-sim/1.1"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "replace")


def rows_of(raw):
    """JSON/XML 어느 쪽으로 오든 item 딕셔너리 리스트로 정규화."""
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
    if not out:
        # 단건 응답은 item 없이 body 바로 아래에 오기도 한다
        body = root.find(".//body")
        if body is not None:
            d = {c.tag: (c.text or "").strip() for c in body if len(c) == 0}
            if d:
                out.append(d)
    return out


def probe(kind, bases, ops, params, key):
    """되는 base+op 조합을 찾아 캐시한다."""
    if kind in RESOLVED:
        base, op = RESOLVED[kind]
        return rows_of(call(f"{base}/{op}", dict(params, serviceKey=key)))
    errors = []
    for base in bases:
        for op in ops:
            try:
                rows = rows_of(call(f"{base}/{op}", dict(params, serviceKey=key)))
                RESOLVED[kind] = (base, op)
                print(f"      경로 확정 [{kind}] {base}/{op}", file=sys.stderr)
                return rows
            except Exception as e:
                errors.append(f"{op}: {e}")
    raise RuntimeError(f"[{kind}] 모든 후보 실패\n  " + "\n  ".join(errors[:6]))


# --------------------------------------------------------------------------- 매칭
SUFFIX = re.compile(r"(아파트|APT|apt)$")
PAREN = re.compile(r"\(.*?\)")


def norm(name):
    s = PAREN.sub("", name or "")
    s = re.sub(r"[\s\-_·,.]", "", s)
    s = SUFFIX.sub("", s)
    s = s.replace("차", "차").replace("단지", "단지")
    return s


def best_match(target, candidates, threshold):
    """candidates: [(norm_name, raw)] → raw or None"""
    t = norm(target)
    for n, raw in candidates:
        if n == t:
            return raw, 1.0
    names = [n for n, _ in candidates]
    hit = difflib.get_close_matches(t, names, n=1, cutoff=threshold)
    if hit:
        for n, raw in candidates:
            if n == hit[0]:
                return raw, difflib.SequenceMatcher(None, t, n).ratio()
    return None, 0.0


# --------------------------------------------------------------------------- 파싱
def walk_min(v):
    """'5분이내' / '10분이내' / '15분이상' → 분"""
    if not v:
        return None
    m = re.search(r"(\d+)", str(v))
    if not m:
        return None
    n = int(m.group(1))
    return n + 1 if "이상" in str(v) else n


def to_int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 메인
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default="listings.json")
    ap.add_argument("--out", dest="dst", default="listings.json")
    ap.add_argument("--key", default=os.environ.get("KAPT_KEY") or os.environ.get("MOLIT_KEY"))
    ap.add_argument("--cache", default=".kapt_cache.json")
    ap.add_argument("--threshold", type=float, default=0.82, help="단지명 퍼지매칭 컷오프")
    args = ap.parse_args()

    if not args.key:
        raise SystemExit("서비스키가 없습니다. KAPT_KEY 환경변수 또는 --key 를 주세요.")

    doc = json.load(open(args.src, encoding="utf-8"))
    lawd = doc["meta"]["lawdCd"]
    comps = doc["complexes"]
    print(f"[1/3] 대상 단지 {len(comps)}개 ({doc['meta'].get('gu','')})", file=sys.stderr)

    cache = {}
    if os.path.exists(args.cache):
        cache = json.load(open(args.cache, encoding="utf-8"))

    # --- 단지 목록 ---
    print("[2/3] K-apt 단지 목록 조회", file=sys.stderr)
    catalog = cache.get("catalog")
    if not catalog:
        catalog, page = [], 1
        while True:
            rows = probe("list", LIST_BASES, LIST_OPS,
                         {"sigunguCode": lawd, "pageNo": page, "numOfRows": 1000}, args.key)
            catalog += [{"code": r.get("kaptCode"), "name": r.get("kaptName"),
                         "addr": r.get("as3") or r.get("bjdCode") or ""} for r in rows if r.get("kaptCode")]
            if len(rows) < 1000:
                break
            page += 1
            time.sleep(0.2)
        cache["catalog"] = catalog
    print(f"      K-apt 등록 단지 {len(catalog)}개", file=sys.stderr)

    cands = [(norm(c["name"]), c) for c in catalog if c.get("name")]

    # --- 단지별 상세 ---
    print("[3/3] 기본정보·상세정보 조회", file=sys.stderr)
    matched = weak = 0
    details = cache.setdefault("detail", {})

    for i, c in enumerate(comps, 1):
        hit, score = best_match(c["apt"], cands, args.threshold)
        if not hit:
            continue
        matched += 1
        if score < 0.95:
            weak += 1
        code = hit["code"]

        if code not in details:
            d = {}
            try:
                for r in probe("bass", INFO_BASES, BASS_OPS, {"kaptCode": code}, args.key):
                    d.update(r)
                time.sleep(0.15)
                for r in probe("dtl", INFO_BASES, DTL_OPS, {"kaptCode": code}, args.key):
                    d.update(r)
                time.sleep(0.15)
            except Exception as e:
                print(f"      ! {c['apt']} ({code}) 조회 실패: {e}", file=sys.stderr)
                d = {}
            details[code] = d

        d = details.get(code) or {}
        units = to_int(d.get("kaptdaCnt"))
        pg, pu = to_int(d.get("kaptdPcnt")) or 0, to_int(d.get("kaptdPcntu")) or 0
        usedate = str(d.get("kaptUsedate") or "")

        c["kaptCode"] = code
        c["kaptName"] = d.get("kaptName") or hit.get("name")
        c["matchScore"] = round(score, 3)
        if units:
            c["units"] = units
        if to_int(d.get("kaptDongCnt")):
            c["dongCnt"] = to_int(d.get("kaptDongCnt"))
        if len(usedate) >= 4 and usedate[:4].isdigit():
            c["buildYear"] = int(usedate[:4])
        if units and (pg + pu):
            c["parkingPerUnit"] = round((pg + pu) / units, 2)
        if d.get("subwayStation"):
            c["subwayStation"] = d["subwayStation"].strip()
        if walk_min(d.get("kaptdWtimesub")):
            c["subwayWalkMin"] = walk_min(d.get("kaptdWtimesub"))
        if d.get("codeHeatNm"):
            c["heatName"] = d["codeHeatNm"]

        # K-apt에 최고층 정보가 없어 실거래 관측 최대층으로 추정한다
        obs = max((f for s in c["sizes"] for f in s.get("floors", [])), default=0)
        if obs and not c.get("totalFloors"):
            c["totalFloors"] = obs + 2
            c["totalFloorsEstimated"] = True

        if i % 25 == 0:
            print(f"      {i}/{len(comps)}", file=sys.stderr)
            json.dump(cache, open(args.cache, "w", encoding="utf-8"), ensure_ascii=False)

    json.dump(cache, open(args.cache, "w", encoding="utf-8"), ensure_ascii=False)

    doc["meta"]["kaptEnrichedAt"] = time.strftime("%Y-%m-%d")
    doc["meta"]["kaptMatched"] = matched
    json.dump(doc, open(args.dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    rate = matched / len(comps) * 100 if comps else 0
    print(f"\n완료 → {args.dst}", file=sys.stderr)
    print(f"  매칭 {matched}/{len(comps)} ({rate:.0f}%) · 그중 퍼지매칭 {weak}건", file=sys.stderr)
    if weak:
        print(f"  matchScore 가 낮은 단지는 listings.json 에서 직접 확인하세요.", file=sys.stderr)
    if rate < 60:
        print("  매칭률이 낮습니다. --threshold 를 0.75 정도로 낮춰보세요.", file=sys.stderr)


if __name__ == "__main__":
    main()
