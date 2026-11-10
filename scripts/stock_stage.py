"""
② 종목 단계 A·B (2026-11-10 v5). 웹 조회 없이 2,700종목 → 40개 → v3.2 채점.

입력
  --data DIR       : screen.csv, latest.json (저장소 data/)
  --store DIR      : ohlcv.parquet, fundamentals.parquet, financials.parquet (Release 'data')
  --sectors FILE   : ① 현재 판정 JSON [{"etf","verdict","bucket"}] (① DB 21행)
  --overrides FILE : (선택) C단계 웹 확인 결과 {티커: {"guide":"상향|유지|하향","guide_beat":bool,"src_primary":bool,
                     "cyclical":bool,"checked":"YYYY-MM-DD","note":"..."}} — ② DB '펀더멘털 근거'에서 만든다. 100일 지나면 무시
  --holdings T1,T2 : 보유 종목(항상 포함)
  --keep T1,T2     : 직전 ② DB 후보(점수가 떨어져도 이번에 다시 채점해 상태를 갱신)
  --in-sector N --other N : 혼합 풀 크기(기본 30·10)
출력
  --out FILE(JSON) : {"asof","stage_a":{통계},"items":[종목별 입력값·근거·채점 결과·needs_web]}

A단계(거르기, 순서대로)
  1) 주식만, 유동성(20일 평균 거래대금 2천만 달러·주가 5달러·이력 120일), 시총 20억 달러 이상, 기준일 종가 있음
  2) 펀더멘털 21일 이내 수집 + 최근 분기 매출 성장률 있음
  3) 섹터 바구니 = 소속 섹터(GICS + 테마) 중 ① 판정이 가장 좋은 것:
       비중확대+추세 → "추세" / 비중확대+성장 조정 → "성장 조정" / 중립+성장 조정(전환 대기) → "성장 조정 대기" / 그 외 → "기타"
       단, ① 판정이 비중축소·제외인 섹터에만 속한 종목은 "없음"(후보 제외)
  4) 사전 품질 점수(score_v3_2와 같은 공식, 가이던스는 '유지'로 가정) 높은 순 → 남은 기술 조건 적은 순
     ① 섹터(추세·성장 조정·성장 조정 대기)에서 N개, 기타 섹터에서 품질 70 이상 중 M개
B단계(채점 입력값 자동 생성 — 규칙은 시스템 페이지 v5와 같다)
  - rev_growth = 분기 매출 전년 대비, rev_growth_prev = 직전 분기(누적 분기로 계산, 없으면 비움)
  - est_up = 데이터 신호(30일 내년 EPS 추정치 변화 ±2%, 30일 상향−하향 애널리스트 ±2명, 90일 목표가 상향−하향 ±2건)
  - margin_trend = 영업이익률 전년 대비 +1%p↑ 개선 / −1%p↓ 악화 / 그 외 유지
  - guide = C단계 확인값(없으면 '유지'), src_primary = C단계 1차 자료 확인 여부(없으면 false)
  - 다음 12개월 EPS(NTM) = 올해 EPS 추정 × 올해 회계연도 남은 비중 + 내년 EPS 추정 × 나머지(하나만 있으면 그 값, 없으면 선행 EPS)
  - 자기 P/E = 회계연도 말 후행 P/E(최근 3~4년)를 지금의 선행/후행 비율(0.4~1.1로 제한)로 선행 기준 환산
       기본 배수 = 중앙값, 성장주(내년 EPS 성장 20% 이상)는 (중앙값 + 최근 2년 평균) ÷ 2, 강세 배수 = 최근 2년 최고
       80배 넘는 해(이익이 막 생긴 해)는 빼고, 남은 값이 2개 미만이면 같은 세부 업종(없으면 섹터) 선행 P/E 중앙값. 기본 배수 상한 60, 강세 75
  - 이익이 너무 작거나 없으면(NTM ≤0, 선행 P/E >60) 매출 방식: 기본 = 종가 × (동종 EV/매출 중앙 ÷ 본인 EV/매출) × (내년 매출 ÷ 최근 12개월 매출, 0.8~2.5),
       강세 = 기본×1.25, 약세 = 기본×0.6
  - 시나리오: 기본 = NTM × 기본 배수, 강세 = NTM×1.10 × 강세 배수(없으면 기본×1.25), 약세 = NTM×0.85 × 최저 배수(없으면 기본×0.75), 확률 25/50/25
  - 진입 = 종가가 20일선 위면 종가, 아래면 20일선(회복 지점). 손절 = 50일선·최근 20일 저점 바로 아래 중 진입가에서 6~15% 사이의
         가장 가까운 값, 없으면 진입가 −12%
  - 경기순환 = 업종 목록(common.py) — 정상화 P/E = 종가 ÷ 연간 EPS 평균(3~5년)
  - earn_days = 다음 실적일까지 거래일, post_er_ok = 직전 실적일 이후 5거래일 이내이고 실적 전날 대비 종가가 플러스
  - price_xcheck = 기준일 종가 있음(CSV 확정), est_consistent = est_up ≥ 0 이고 매출 성장 > 0
needs_web(C단계 대상) = 매수 후보이거나, 품질 60↑ + 남은 기술 조건 ≤1 + 손익비 ≥1.5 — 최대 10개(품질 순)
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CYCLICAL_INDUSTRY, CYCLICAL_TICKERS, GICS_ETFS, NON_STOCK, est_up_data,  # noqa: E402
                    margin_trend_data, quality_pre, themes_of)

HERE = Path(__file__).resolve().parent
BASKET_ORDER = {"추세": 0, "성장 조정": 1, "성장 조정 대기": 2, "기타": 3, "없음": 4}


def nz(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else x


def r2(x, nd=2):
    x = nz(x)
    return None if x is None else round(float(x), nd)


def basket_for(etfs: list[str], sect: dict) -> tuple[str, str]:
    best, best_etf, any_ok = "기타", None, False
    for e in etfs:
        s = sect.get(e)
        if not s:
            continue
        v, b = s.get("verdict"), s.get("bucket")
        if v not in ("비중축소", "제외"):
            any_ok = True
        cand = "추세" if (v == "비중확대" and b == "추세") else "성장 조정" if (v == "비중확대" and b == "성장 조정") \
            else "성장 조정 대기" if (v == "중립" and b == "성장 조정") else None
        if cand and BASKET_ORDER[cand] < BASKET_ORDER[best]:
            best, best_etf = cand, e
    if best == "기타" and not any_ok and etfs:
        return "없음", etfs[0]
    if best == "기타":
        gics = [e for e in etfs if e in GICS_ETFS]
        best_etf = gics[0] if gics else (etfs[0] if etfs else "SPY")
    return best, best_etf


def tech_left(r) -> int:
    n = 0
    n += 0 if (r.get("vs_sma20_pct") or -1) > 0 else 1
    n += 0 if bool(r.get("sma20_rising")) else 1
    n += 0 if (r.get("rs21_vs_sector_pct") or -1) > 0 else 1
    n += 1 if ((r.get("rsi14") or 0) >= 70 or (r.get("vs_sma20_pct") or 0) >= 8) else 0
    return n


def bdays(a, b) -> int:
    return int(np.busday_count(pd.Timestamp(a).date(), pd.Timestamp(b).date()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--store", required=True)
    ap.add_argument("--sectors", required=True)
    ap.add_argument("--overrides")
    ap.add_argument("--holdings", default="")
    ap.add_argument("--keep", default="")
    ap.add_argument("--in-sector", type=int, default=30)
    ap.add_argument("--other", type=int, default=10)
    ap.add_argument("--out", default="stock_stage.json")
    a = ap.parse_args()
    d, st = Path(a.data), Path(a.store)
    asof = json.load(open(d / "latest.json"))["meta"]["as_of"]
    sect = {x["etf"]: x for x in json.load(open(a.sectors))}
    ovr = json.load(open(a.overrides)) if a.overrides and Path(a.overrides).exists() else {}
    hold = [t.strip().upper() for t in a.holdings.split(",") if t.strip()]
    keep = [t.strip().upper() for t in a.keep.split(",") if t.strip()]
    sc = pd.read_csv(d / "screen.csv")
    fund = pd.read_parquet(st / "fundamentals.parquet").set_index("ticker")
    cutoff = (pd.Timestamp(asof) - pd.Timedelta(days=21)).strftime("%Y-%m-%d")

    # ---------- A단계 ----------
    stat = {"screen": len(sc)}
    x = sc[~sc["ticker"].isin(NON_STOCK)].copy()
    x = x[(x["avg_dollar_vol_20d"].fillna(0) >= 2e7) & (x["close"].fillna(0) >= 5) & (x["history_days"].fillna(0) >= 120)
          & (x["date"] == asof)]
    stat["liquid"] = len(x)
    x = x[x["mcap_b"].fillna(0) >= 2]
    stat["mcap2b"] = len(x)
    x = x[(x["fund_fetched"].fillna("") >= cutoff) & x["rev_growth_q"].notna()]
    stat["fund_fresh"] = len(x)
    x["etfs"] = x.apply(lambda r: [e for e in ([r["sector_etf"]] if pd.notna(r["sector_etf"]) else []) + sorted(themes_of(r.get("yf_industry")))], axis=1)
    bk = x["etfs"].map(lambda es: basket_for(es, sect))
    x["basket"], x["basket_etf"] = bk.map(lambda t: t[0]), bk.map(lambda t: t[1])
    x = x[x["basket"] != "없음"]
    stat["after_sector"] = len(x)
    qp = x.apply(lambda r: quality_pre(r.to_dict()), axis=1)
    x["q_pre"] = qp.map(lambda t: t[0])
    x["tech_left"] = x.apply(lambda r: tech_left(r.to_dict()), axis=1)
    x = x.sort_values(["q_pre", "tech_left"], ascending=[False, True])
    ins = x[x["basket"] != "기타"].head(a.in_sector)
    oth = x[(x["basket"] == "기타") & (x["q_pre"] >= 70)].head(a.other)
    must = sc[sc["ticker"].isin(hold + keep) & ~sc["ticker"].isin(NON_STOCK)]
    pick = list(dict.fromkeys(ins["ticker"].tolist() + oth["ticker"].tolist() + must["ticker"].tolist()))
    stat.update(in_sector=len(ins), other=len(oth), forced=len(set(must["ticker"]) - set(ins["ticker"]) - set(oth["ticker"])),
                picked=len(pick), basket_counts=x[x["ticker"].isin(pick)]["basket"].value_counts().to_dict())

    # ---------- B단계 ----------
    rows = sc.set_index("ticker")
    xi = x.set_index("ticker")
    px = pd.read_parquet(st / "ohlcv.parquet", filters=[("ticker", "in", pick + ["SPY"] + [e for e in sect])])
    fin = pd.read_parquet(st / "financials.parquet")
    fin = fin[fin["ticker"].isin(pick)]
    Path("prices").mkdir(exist_ok=True)
    bench = px[px["ticker"].isin(["SPY"] + list(sect))].pivot(index="date", columns="ticker", values="close").sort_index()
    bench.to_csv("bench_stock.csv")
    # 동종 선행 P/E 중앙값(세부 업종 → 섹터)
    allf = sc[(sc["mcap_b"].fillna(0) >= 2) & sc["fwd_pe"].between(1, 200)]
    peer_ind = allf.groupby("yf_industry")["fwd_pe"].median()
    peer_sec = allf.groupby("sector_etf")["fwd_pe"].median()
    fe = fund.reset_index()[["ticker", "ev_rev"]].merge(sc[["ticker", "yf_industry", "sector_etf", "mcap_b"]], on="ticker")
    fe = fe[(fe["mcap_b"].fillna(0) >= 2) & pd.to_numeric(fe["ev_rev"], errors="coerce").between(0.1, 60)]
    fe["ev_rev"] = pd.to_numeric(fe["ev_rev"], errors="coerce")
    peer_evr = fe.groupby("yf_industry")["ev_rev"].median()
    peer_evr_sec = fe.groupby("sector_etf")["ev_rev"].median()

    items, info = [], {}
    for t in pick:
        r = rows.loc[t].to_dict()
        f = fund.loc[t].to_dict() if t in fund.index else {}
        p = px[px["ticker"] == t].sort_values("date")
        if len(p) < 60:
            continue
        p[["date", "close"]].to_csv(f"prices/{t}.csv", index=False)
        close = float(p["close"].iloc[-1])
        bkt = xi.loc[t, "basket"] if t in xi.index else "기타"
        betf = xi.loc[t, "basket_etf"] if t in xi.index else (r.get("sector_etf") or "SPY")
        if betf not in bench.columns:
            betf = "SPY"
        o = ovr.get(t, {})
        if o.get("checked") and (pd.Timestamp(asof) - pd.Timestamp(o["checked"])).days > 100:
            o = {}
        # NTM EPS — 회계연도 진행도로 올해·내년 추정치를 가중(회계연도 말은 연간 실적 날짜로 추정)
        e0, e1 = nz(f.get("eps_est_0y")), nz(f.get("eps_est_1y"))
        ann = fin[(fin["ticker"] == t) & (fin["freq"] == "a")]["period_end"].dropna()
        w0 = 0.5
        if len(ann):
            fy_end = pd.Timestamp(sorted(ann)[-1])
            nxt = fy_end
            while nxt <= pd.Timestamp(asof):
                nxt = nxt + pd.DateOffset(years=1)
            w0 = min(max((nxt - pd.Timestamp(asof)).days / 365.0, 0), 1)   # 올해 회계연도에 남은 비중
        ntm = (w0 * e0 + (1 - w0) * e1) if (e0 is not None and e1 is not None) else (e1 if e1 is not None else (e0 if e0 is not None else nz(f.get("fwd_eps"))))
        g_next = nz(r.get("eps_growth_next_y_pct"))
        growth = g_next is not None and g_next >= 20
        # 자기 P/E(선행 환산)
        fwd, ttm = nz(f.get("fwd_pe")), nz(f.get("ttm_pe"))
        ratio = min(max(fwd / ttm, 0.4), 1.1) if (fwd and ttm and fwd > 0 and ttm > 0) else 0.8
        hist = {}
        try:
            hist = json.loads(f.get("pe_hist") or "{}") if isinstance(f.get("pe_hist"), str) else {}
        except Exception:
            hist = {}
        hv = [v * ratio for _, v in sorted(hist.items()) if v and 0 < v <= 80]   # 80배 초과(이익이 막 생긴 해)는 기준에서 뺌
        src = "자기 P/E(연도별 후행 %s × 선행/후행 %.2f)" % (",".join(f"{k}:{v}" for k, v in sorted(hist.items())), ratio)
        if len(hv) >= 2:
            med = float(np.median(hv)); rec = hv[-2:]
            base_m = (med + float(np.mean(rec))) / 2 if growth else med
            bull_m, bear_m = max(rec), min(hv)
        else:
            peer = peer_ind.get(r.get("yf_industry")) if pd.notna(r.get("yf_industry")) else None
            peer = peer if peer is not None and not np.isnan(peer) else peer_sec.get(r.get("sector_etf"))
            base_m = float(peer) if peer is not None and not np.isnan(peer) else (fwd or 15.0)
            bull_m, bear_m = base_m * 1.25, base_m * 0.75
            src = "동종 선행 P/E 중앙값(%s)" % (r.get("yf_industry") or r.get("sector_etf"))
        base_m = min(base_m, 60.0); bull_m = min(bull_m, 75.0)
        method = "EPS×P/E"
        evr, rv1, rvt = nz(f.get("ev_rev")), nz(f.get("rev_est_1y")), nz(f.get("revenue_ttm"))
        use_rev = (ntm is None or ntm <= 0 or (fwd is not None and fwd > 60) or (ntm and close / ntm > 60))
        if use_rev:
            peer_r = peer_evr.get(r.get("yf_industry")) if pd.notna(r.get("yf_industry")) else None
            if peer_r is None or np.isnan(peer_r):
                peer_r = peer_evr_sec.get(r.get("sector_etf"))
            if evr and evr > 0 and rv1 and rvt and rvt > 0 and peer_r and not np.isnan(peer_r):
                gf = min(max(rv1 / rvt, 0.8), 2.5)
                base = close * (float(peer_r) / evr) * gf
                scen = {"bull": [round(base * 1.25, 2), 25], "base": [round(base, 2), 50], "bear": [round(base * 0.6, 2), 25]}
                method = "매출×동종 EV/매출(%.1f배, 본인 %.1f배, 내년 매출/최근 12개월 %.2f)" % (peer_r, evr, gf)
                src = method
            else:
                continue  # 이익·매출 근거 모두 부족 → 이번 채점 제외
        else:
            base = ntm * base_m
            scen = {"bull": [round(ntm * 1.10 * bull_m, 2), 25], "base": [round(base, 2), 50],
                    "bear": [round(ntm * 0.85 * bear_m, 2), 25]}
        # 목표가 신뢰성: 기본 시나리오가 종가의 0.4배 미만·3배 초과면 이 방식이 맞지 않는 종목(초기 단계·일회성 이익 등)
        val_flag = None
        if base < 0.4 * close or base > 3 * close:
            val_flag = "목표가 산정 부적합(기본 %.0f vs 종가 %.0f) — 웹 확인 전 판단 보류" % (base, close)
        # 진입·손절
        sma20, sma50 = nz(r.get("sma20")), nz(r.get("sma50"))
        entry = close if (sma20 and close >= sma20) else (sma20 or close)
        low20 = float(p["low"].tail(20).min())
        cands = [v * 0.99 for v in (sma50, low20) if v and v < entry]
        cands = [c for c in cands if 0.06 <= (entry - c) / entry <= 0.15]
        stop = max(cands) if cands else entry * 0.88
        # 실적
        ne = nz(f.get("next_earnings"))
        earn_days = bdays(asof, ne) if ne and ne >= asof else 99
        le = nz(f.get("last_earnings_date")) or nz(f.get("last_earnings_q"))
        post_ok = False
        if le and bdays(le, asof) <= 5:
            pb = p[p["date"] < le]
            post_ok = bool(len(pb) and close > float(pb["close"].iloc[-1]))
        # 경기순환
        ind = r.get("yf_industry")
        cyc = bool(o.get("cyclical")) if "cyclical" in o else (str(ind) in CYCLICAL_INDUSTRY or t in CYCLICAL_TICKERS)
        aeps = fin[(fin["ticker"] == t) & (fin["freq"] == "a")]["diluted_eps"].dropna()
        norm_pe = round(close / aeps.mean(), 1) if (cyc and len(aeps) >= 3 and aeps.mean() > 0) else None
        est = est_up_data(r)
        it = {
            "t": t, "kind": "stock", "sector_etf": betf, "sector_basket": bkt, "prices_csv": f"prices/{t}.csv",
            "rev_growth": r2(r.get("rev_growth_q")), "rev_growth_prev": r2(r.get("rev_growth_prev_q")),
            "guide": o.get("guide", "유지"), "guide_beat": bool(o.get("guide_beat", False)),
            "est_up": est if est is not None else 0, "margin_trend": margin_trend_data(r),
            "mcap_b": r2(r.get("mcap_b")), "adv_musd": r2((r.get("avg_dollar_vol_20d") or 0) / 1e6),
            "entry": round(entry, 2), "stop": round(stop, 2), "scen": scen,
            "cyclical": cyc, "norm_pe": norm_pe, "earn_days": earn_days, "post_er_ok": post_ok,
            "src_primary": bool(o.get("src_primary", False)), "price_xcheck": r.get("date") == asof,
            "est_consistent": bool((est or 0) >= 0 and (r.get("rev_growth_q") or 0) > 0) and val_flag is None,
        }
        if it["rev_growth_prev"] is None:
            it.pop("rev_growth_prev")
        if it["margin_trend"] is None:
            it.pop("margin_trend")
        items.append(it)
        info[t] = {
            "name": r.get("name"), "sector": r.get("sector"), "industry": ind, "basket": bkt, "basket_etf": betf,
            "close": round(close, 2), "sma20": r2(sma20), "sma50": r2(sma50), "q_pre": r2(xi.loc[t, "q_pre"]) if t in xi.index else None,
            "ntm_eps": r2(ntm), "ntm_weight_this_fy": r2(w0), "valuation_method": method, "valuation_flag": val_flag, "eps_growth_next_y_pct": r2(g_next), "growth_multiple": growth, "base_multiple": r2(base_m),
            "bull_multiple": r2(bull_m), "bear_multiple": r2(bear_m), "multiple_source": src,
            "fwd_pe": r2(fwd), "ttm_pe": r2(ttm), "next_earnings": ne, "last_earnings": le,
            "eps_1y_chg_30d_pct": r2(r.get("eps_1y_chg_30d_pct")), "rev_1y_up30": nz(r.get("rev_1y_up30")),
            "rev_1y_down30": nz(r.get("rev_1y_down30")), "pt_raise_90d": nz(r.get("pt_raise_90d")),
            "pt_cut_90d": nz(r.get("pt_cut_90d")), "op_margin_chg_pp": r2(r.get("op_margin_chg_pp")),
            "target_mean": r2(f.get("target_mean")), "rec_mean": r2(f.get("rec_mean")), "fund_fetched": r.get("fund_fetched"),
            "override": o or None, "holding": t in hold,
        }

    cfg = {"mode": "stock", "asof": asof, "bench_csv": "bench_stock.csv", "out_csv": "stock_v3_2_out.csv", "items": items}
    json.dump(cfg, open("stock_cfg.json", "w"), ensure_ascii=False)
    subprocess.run([sys.executable, str(HERE / "score_v3_2.py"), "stock_cfg.json"], check=True, stdout=subprocess.DEVNULL)
    res = pd.read_csv("stock_v3_2_out.csv")
    out = []
    for _, s in res.iterrows():
        t = s["t"]
        it = next(i for i in items if i["t"] == t)
        rec = {k: (None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, "item") else v)) for k, v in s.items()}
        rec.update(inputs={k: v for k, v in it.items() if k not in ("t", "prices_csv")}, data=info[t])
        rec["needs_web"] = False
        out.append(rec)
    out.sort(key=lambda r: ({"매수 후보": 0, "관찰": 1, "제외": 2}.get(r["status"], 3), -(r["quality"] or 0), r["ready_left"]))
    n = 0
    for r in out:
        if n >= 10:
            break
        if r["data"].get("valuation_flag"):
            continue
        if r["status"] == "매수 후보" or ((r["quality"] or 0) >= 60 and r["ready_left"] <= 1 and (r["rr"] or 0) >= 1.5):
            r["needs_web"] = True
            n += 1
    stat["scored"] = len(out)
    stat["status_counts"] = pd.Series([r["status"] for r in out]).value_counts().to_dict() if out else {}
    stat["needs_web"] = n
    json.dump({"asof": asof, "stage_a": stat, "items": out}, open(a.out, "w"), ensure_ascii=False, indent=1, default=str)

    t = pd.DataFrame([{k: r[k] for k in ["t", "status", "basket", "quality", "ready_left", "rank", "E", "rr", "bear", "cert", "needs_web"]}
                      | {"base_x": r["data"]["base_multiple"], "gx": r["data"]["growth_multiple"], "close": r["data"]["close"],
                         "entry": r["inputs"]["entry"], "base": r["inputs"]["scen"]["base"][0]} for r in out])
    pd.set_option("display.width", 250)
    print(json.dumps(stat, ensure_ascii=False))
    print(t.to_string(index=False))


if __name__ == "__main__":
    main()
