"""
① 섹터 단계 (2026-11-10 v5): 데이터로 섹터 입력값 일부를 계산하고 score_v3.py 섹터 모드를 돌린다.

입력
  --data DIR        : screen.csv, core_prices.csv 가 있는 폴더(저장소 data/ 를 받은 곳)
  --inputs FILE     : 현재 ① DB 입력값 JSON 목록 [{"name","etf","rev","demand","leaders","macro","commodity_peak","val_rich"}]
  --apply-data      : 데이터로 계산한 rev(추정치 방향)·leaders(리더 밀도)를 입력값에 반영(주간 재조사 때만)
  --asof YYYY-MM-DD : 판정 기준일(기본: latest.json meta.as_of)
출력
  --out FILE(JSON)  : {"asof", "sectors":[섹터별 데이터 계산값 + 스크립트 판정]}, 화면에는 요약표만

데이터 계산 규칙(섹터 소속 = GICS 섹터 ETF, 테마는 Yahoo 세부 업종으로 연결 — common.py)
  - 대상: 유동성 기준(20일 평균 거래대금 2천만 달러·주가 5달러·이력 120일) + 시총 20억 달러 이상 + 펀더멘털 수집 21일 이내
  - 추정치 방향(rev_data): 대상 종목의 '내년 EPS 추정치 30일 변화' 중앙값 > +1% → +1, < −1% → −1, 그 사이 0 (대상 5개 미만이면 없음)
  - 리더 밀도(leaders_data): 최근 분기 매출 +30% 이상 + 내년 EPS 추정치 30일 상향(>0) 종목 수 → 0개=0, 1~2=1, 3~4=2, 5개 이상=3
  - 커버리지(fund_cov_pct): 유동성 통과 종목 중 펀더멘털이 21일 이내로 수집된 비율(테마 ETF는 상위 GICS 섹터 기준).
    60% 미만이거나 대상이 5개 미만이면 data_ok=false(반영하지 않음)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import GICS_ETFS, SECTORS, THEME_PARENT, liquid, sector_members  # noqa: E402

HERE = Path(__file__).resolve().parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--inputs", required=True)
    ap.add_argument("--apply-data", action="store_true")
    ap.add_argument("--asof")
    ap.add_argument("--out", default="sector_stage.json")
    a = ap.parse_args()
    d = Path(a.data)
    screen = pd.read_csv(d / "screen.csv")
    asof = a.asof or json.load(open(d / "latest.json"))["meta"]["as_of"]
    cutoff = (pd.Timestamp(asof) - pd.Timedelta(days=21)).strftime("%Y-%m-%d")
    inputs = {x["etf"]: x for x in json.load(open(a.inputs))}

    mem = sector_members(screen)
    rows = []
    for name, etf in SECTORS:
        m = liquid(mem[etf])
        big = m[m["mcap_b"].fillna(0) >= 2] if "mcap_b" in m else m.iloc[0:0]
        fresh = big[big["fund_fetched"].fillna("") >= cutoff] if "fund_fetched" in big else big.iloc[0:0]
        cm = m if etf in GICS_ETFS else liquid(pd.concat([mem[p_] for p_ in THEME_PARENT[etf]]).drop_duplicates("ticker"))
        cov = round(len(cm[cm["fund_fetched"].fillna("") >= cutoff]) / len(cm) * 100, 1) if len(cm) else 0.0
        med = fresh["eps_1y_chg_30d_pct"].median() if len(fresh) else np.nan
        rev_data = None if len(fresh) < 5 or pd.isna(med) else (1 if med > 1 else (-1 if med < -1 else 0))
        lead = fresh[(fresh["rev_growth_q"] >= 30) & (fresh["eps_1y_chg_30d_pct"] > 0)]
        n = len(lead)
        leaders_data = 0 if n == 0 else (1 if n <= 2 else (2 if n <= 4 else 3))
        data_ok = cov >= 60 and len(fresh) >= 5
        rows.append(dict(name=name, etf=etf, members_liquid=int(len(m)), fresh_big=int(len(fresh)), fund_cov_pct=cov,
                         eps_rev_30d_median=None if pd.isna(med) else round(float(med), 2), rev_data=rev_data,
                         leaders_n=n, leaders_data=leaders_data, data_ok=bool(data_ok),
                         leader_tickers=lead.sort_values("rev_growth_q", ascending=False)["ticker"].head(8).tolist()))

    # 판정 입력 구성
    sectors_cfg = []
    for r in rows:
        base = dict(inputs.get(r["etf"], {}))
        if not base:
            print(f"[warn] {r['etf']} 입력값 없음 → 중립값(rev 0, demand 3, leaders 0)", file=sys.stderr)
            base = {"name": r["name"], "etf": r["etf"], "rev": 0, "demand": 3, "leaders": 0, "macro": 0,
                    "commodity_peak": False, "val_rich": False}
        used = {"rev": base.get("rev", 0), "leaders": base.get("leaders", 0)}
        if a.apply_data and r["data_ok"]:
            if r["rev_data"] is not None:
                used["rev"] = r["rev_data"]
            used["leaders"] = r["leaders_data"]
        r["rev_used"], r["leaders_used"] = used["rev"], used["leaders"]
        r["rev_prev"], r["leaders_prev"] = base.get("rev"), base.get("leaders")
        sectors_cfg.append({**base, "name": r["name"], "etf": r["etf"], **used})

    # bench CSV(일별 종가)
    cp = pd.read_csv(d / "core_prices.csv")
    etfs = ["SPY"] + [e for _, e in SECTORS]
    b = cp[cp["ticker"].isin(etfs)].pivot(index="date", columns="ticker", values="close")
    missing = [e for e in etfs if e not in b.columns]
    if missing:
        raise SystemExit(f"bench에 없는 ETF: {missing}")
    b = b[etfs].sort_index()
    b.insert(0, "type", "D")
    b.to_csv("bench.csv")
    cfg = {"mode": "sector", "asof": asof, "bench_csv": "bench.csv", "out_csv": "sector_v3_out.csv", "sectors": sectors_cfg}
    json.dump(cfg, open("sector_cfg.json", "w"), ensure_ascii=False)
    subprocess.run([sys.executable, str(HERE / "score_v3.py"), "sector_cfg.json"], check=True, stdout=subprocess.DEVNULL)
    res = pd.read_csv("sector_v3_out.csv").set_index("etf")
    for r in rows:
        x = res.loc[r["etf"]]
        for k in ["rs61", "rs3", "mom", "G", "timing", "rsi", "d20", "bucket", "verdict", "note", "demand", "macro", "cp", "val_rich"]:
            v = x[k]
            r[k] = None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, "item") else v)
    order = {e: i for i, e in enumerate(res.index)}
    rows.sort(key=lambda r: order[r["etf"]])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    json.dump({"asof": asof, "applied_data": a.apply_data, "sectors": rows}, open(a.out, "w"), ensure_ascii=False, indent=1)

    cols = ["rank", "etf", "verdict", "bucket", "timing", "mom", "G", "rev_used", "rev_data", "leaders_used", "leaders_data",
            "fund_cov_pct", "data_ok", "d20"]
    t = pd.DataFrame(rows)[cols + ["leader_tickers"]]
    t["leader_tickers"] = t["leader_tickers"].map(lambda x: ",".join(x[:5]))
    pd.set_option("display.width", 250)
    print(t.to_string(index=False))


if __name__ == "__main__":
    main()
