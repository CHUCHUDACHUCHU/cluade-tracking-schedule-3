"""
펀더멘털·실적 추정치 수집기 (collect.py 다음에 실행)

무엇을 모으나(yfinance, 종목당)
  - 기본: 시가총액, 업종, 선행·후행 P/E, EV/매출, EV/EBITDA, 영업이익률, FCF, 부채비율, 공매도 비율, 애널리스트 목표가·의견
  - 분기 실적(최근 6~8분기): 매출, 영업이익, 순이익, 희석 EPS, FCF → 매출 성장률(전년 대비)·직전 분기 성장률·가속·이익률 추이
  - 연간 실적(최근 4년): 매출, 희석 EPS, 회계연도 말 → 연도별 P/E(저장소 종가 ÷ EPS)
  - 실적 일정: 다음 실적일, 직전 실적일, 최근 4분기 EPS 서프라이즈
  - 추정치: 올해·내년 EPS·매출 컨센서스, 다음 해 EPS 성장률, 추정치 변화(현재 vs 7·30·60·90일 전), 상향·하향 수(7·30일)
  - 목표가·등급 변경(최근 90일): 목표가 상향·하향 건수, 등급 상향·하향 건수

누구를 모으나(하루 시간 예산 안에서)
  - A(매일): core.txt + 기술적 사전 순위 상위 N + 실적 발표 전후 종목 → 전체 항목
  - B(순환): 나머지를 '가장 오래전에 받은 순'으로 → 전체 항목. 약 2주에 한 바퀴.
  - 레이트 리밋이 연속되면 그날은 멈추고 다음 날 이어서 받음(받은 값은 저장).

저장
  Release 'data': fundamentals.parquet(종목당 최신 1행), fund_history.parquet(받을 때마다 1행 누적 — 추정치 변화 추적),
                  financials.parquet(분기·연간 실적 long, 갱신)
  git data/screen.csv: 핵심 펀더멘털 칸을 덧붙이고 사전 순위(pre_rank) 기록
  git data/fund_log.json: 이번 실행 통계

사용
  python scripts/fundamentals.py --store store [--budget-min 35] [--top 150]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS = ROOT / "tickers"
PAUSE = 0.35            # 요청 사이 쉬는 시간(초)
RL_STOP = 4             # 레이트 리밋 연속 횟수 → 그날 중단
A_MAX = 220             # A 묶음 최대 종목 수
FULL_EVERY_DAYS = 6     # 재무제표·실적 일정은 이 기간마다(또는 실적 전후) 다시 받음, 그 사이엔 가벼운 갱신
NON_STOCK = {"SPY", "KRW=X", "XLF", "SMH", "GRID", "XLK", "XLI", "ITA", "IGV", "XLE", "IYZ", "XBI", "XLC", "PAVE",
             "XLV", "XLB", "KRE", "XLU", "XLY", "URA", "XLP", "XHB", "XLRE", "UUP", "GLD", "USO", "CPER", "EEM", "IWM", "RSP", "VIXY"}  # ETF·환율은 펀더멘털 없음

INFO_KEYS = {
    "marketCap": "mcap", "enterpriseValue": "ev", "forwardPE": "fwd_pe", "trailingPE": "ttm_pe",
    "priceToSalesTrailing12Months": "ps_ttm", "enterpriseToRevenue": "ev_rev", "enterpriseToEbitda": "ev_ebitda",
    "forwardEps": "fwd_eps", "trailingEps": "ttm_eps", "revenueGrowth": "info_rev_growth",
    "earningsQuarterlyGrowth": "info_eq_growth", "grossMargins": "gross_margin", "operatingMargins": "op_margin",
    "profitMargins": "net_margin", "freeCashflow": "fcf_ttm", "totalRevenue": "revenue_ttm",
    "debtToEquity": "debt_to_equity", "shortPercentOfFloat": "short_pct_float", "heldPercentInstitutions": "inst_pct",
    "beta": "beta", "targetMeanPrice": "target_mean", "targetHighPrice": "target_high", "targetLowPrice": "target_low",
    "recommendationMean": "rec_mean", "numberOfAnalystOpinions": "n_analysts", "sharesOutstanding": "shares_out",
    "sector": "yf_sector", "industry": "yf_industry", "quoteType": "quote_type",
}


def log(*a):
    print(*a, flush=True)


def warn(*a):
    print("[warn]", *a, file=sys.stderr, flush=True)


def num(x):
    try:
        x = float(x)
        return None if math.isnan(x) or math.isinf(x) else x
    except (TypeError, ValueError):
        return None


def pct(a, b):
    a, b = num(a), num(b)
    return None if a is None or b is None or b == 0 else (a / abs(b) - 1) * 100 if b > 0 else None


def rnd(x, nd=2):
    x = num(x)
    return None if x is None else round(x, nd)


class RateLimited(Exception):
    pass


def is_rate_limit(e: Exception) -> bool:
    s = f"{type(e).__name__} {e}".lower()
    return "ratelimit" in s or "too many requests" in s or "429" in s


def safe(fn, default=None):
    try:
        v = fn()
        return default if v is None else v
    except Exception as e:
        if is_rate_limit(e):
            raise RateLimited(str(e))
        return default


def row_series(df: pd.DataFrame, names: list[str]) -> pd.Series | None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce")
            s.index = pd.to_datetime(s.index, errors="coerce")
            return s.dropna().sort_index()  # 오래된 → 최근
    return None


# ---------------- 종목 하나 ----------------
def fetch_one(tk: str, prices: pd.DataFrame | None, light: bool = False) -> tuple[dict, list[dict]]:
    """light=True: 시세성 항목(info·추정치 변화·목표가 변경)만. 재무제표·실적 일정은 직전 값 유지."""
    import yfinance as yf
    t = yf.Ticker(tk)
    out: dict = {"ticker": tk}
    fin_rows: list[dict] = []
    got = []
    if light:
        return fetch_light(t, out, got), fin_rows

    info = safe(lambda: t.info, {}) or {}
    time.sleep(PAUSE)
    if info:
        got.append("info")
        for k, v in INFO_KEYS.items():
            val = info.get(k)
            out[v] = val if isinstance(val, str) else num(val)

    return fetch_full_rest(t, tk, prices, out, fin_rows, got), fin_rows


def fetch_light(t, out, got) -> dict:
    info = safe(lambda: t.info, {}) or {}
    time.sleep(PAUSE)
    if info:
        got.append("info")
        for k, v in INFO_KEYS.items():
            val = info.get(k)
            out[v] = val if isinstance(val, str) else num(val)
    estimates_and_ratings(t, out, got)
    out["fund_fields_ok"] = "+".join(got)
    out["fetch_kind"] = "light"
    return out


def fetch_full_rest(t, tk, prices, out, fin_rows, got) -> dict:
    # 분기 실적
    q = safe(lambda: t.quarterly_income_stmt, pd.DataFrame())
    time.sleep(PAUSE)
    qcf = safe(lambda: t.quarterly_cashflow, pd.DataFrame())
    time.sleep(PAUSE)
    rev = row_series(q, ["Total Revenue", "Operating Revenue"])
    opi = row_series(q, ["Operating Income", "Total Operating Income As Reported"])
    ni = row_series(q, ["Net Income", "Net Income Common Stockholders"])
    eps = row_series(q, ["Diluted EPS", "Basic EPS"])
    fcf = row_series(qcf, ["Free Cash Flow"])
    if rev is not None and len(rev):
        got.append("quarterly")
        out["q_last_end"] = rev.index[-1].strftime("%Y-%m-%d")
        out["q_revenue"] = num(rev.iloc[-1])

        def yoy(s, k):  # k=0 최신 분기, 1 직전 분기 — 4분기 전과 비교(분기 간격 확인)
            if s is None or len(s) < 5 + k:
                return None
            cur, prev = s.index[-1 - k], s.index[-5 - k]
            if not (330 <= (cur - prev).days <= 400):
                return None
            return pct(s.iloc[-1 - k], s.iloc[-5 - k])

        out["rev_growth_q"] = rnd(yoy(rev, 0))
        out["rev_growth_prev_q"] = rnd(yoy(rev, 1))
        if out["rev_growth_q"] is not None and out["rev_growth_prev_q"] is not None:
            out["rev_accel_pp"] = rnd(out["rev_growth_q"] - out["rev_growth_prev_q"])
        if opi is not None and len(opi) >= 5:
            def margin(d):
                o, rv_ = num(opi.get(d)), num(rev.get(d))
                return o / rv_ * 100 if (o is not None and rv_) else None
            m_now, m_ago = margin(opi.index[-1]), margin(opi.index[-5])
            out["op_margin_q"] = rnd(m_now)
            out["op_margin_q_yago"] = rnd(m_ago)
            if m_now is not None and m_ago is not None:
                out["op_margin_chg_pp"] = rnd(m_now - m_ago)
        if fcf is not None and len(fcf) >= 5:
            out["fcf_q"] = num(fcf.iloc[-1])
            out["fcf_q_yago"] = num(fcf.iloc[-5])
        for d in rev.index[-8:]:
            fin_rows.append({"ticker": tk, "freq": "q", "period_end": d.strftime("%Y-%m-%d"),
                             "revenue": num(rev.get(d)), "op_income": num(opi.get(d)) if opi is not None else None,
                             "net_income": num(ni.get(d)) if ni is not None else None,
                             "diluted_eps": num(eps.get(d)) if eps is not None else None,
                             "fcf": num(fcf.get(d)) if fcf is not None else None})

    # 연간 실적 + 연도별 P/E(저장소 종가)
    a = safe(lambda: t.income_stmt, pd.DataFrame())
    time.sleep(PAUSE)
    arev = row_series(a, ["Total Revenue", "Operating Revenue"])
    aeps = row_series(a, ["Diluted EPS", "Basic EPS"])
    pe_hist = {}
    if aeps is not None and len(aeps):
        got.append("annual")
        for d in aeps.index[-5:]:
            e = num(aeps.get(d))
            px = None
            if prices is not None and not prices.empty:
                p = prices[prices["date"] <= d.strftime("%Y-%m-%d")]
                if not p.empty and (d - pd.Timestamp(p["date"].iloc[-1])).days <= 7:
                    px = num(p["close"].iloc[-1])
            if e and e > 0 and px:
                pe_hist[d.strftime("%Y")] = round(px / e, 2)
            fin_rows.append({"ticker": tk, "freq": "a", "period_end": d.strftime("%Y-%m-%d"),
                             "revenue": num(arev.get(d)) if arev is not None else None, "op_income": None,
                             "net_income": None, "diluted_eps": e, "fcf": None, "price_at_end": px})
        out["pe_hist"] = json.dumps(pe_hist) if pe_hist else None
        vals = list(pe_hist.values())
        out["pe_hist_median"] = rnd(float(np.median(vals))) if vals else None

    # 실적 일정·서프라이즈
    cal = safe(lambda: t.calendar, {}) or {}
    time.sleep(PAUSE)
    ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
    if ed:
        ed = ed if isinstance(ed, (list, tuple)) else [ed]
        ds = sorted(pd.to_datetime(x).date() for x in ed if x is not None)
        if ds:
            got.append("calendar")
            fut = [d for d in ds if d >= date.today() - timedelta(days=1)]
            past = [d for d in ds if d < date.today() - timedelta(days=1)]
            out["next_earnings"] = fut[0].isoformat() if fut else None   # 아직 발표 예정일이 안 올라온 경우 비움
            if past:
                out["last_earnings_date"] = past[-1].isoformat()
    eh = safe(lambda: t.earnings_history, pd.DataFrame())
    time.sleep(PAUSE)
    if isinstance(eh, pd.DataFrame) and not eh.empty:
        got.append("earn_hist")
        eh = eh.copy()
        eh.index = pd.to_datetime(eh.index, errors="coerce")
        eh = eh.sort_index()
        sp = pd.to_numeric(eh.get("surprisePercent"), errors="coerce")
        if sp is not None and len(sp.dropna()):
            v = sp.dropna()
            out["last_surprise_pct"] = rnd(v.iloc[-1] * (100 if abs(v.iloc[-1]) < 5 else 1))
            out["beats_4q"] = int((v.tail(4) > 0).sum())
        out["last_earnings_q"] = eh.index[-1].strftime("%Y-%m-%d") if pd.notna(eh.index[-1]) else None

    estimates_and_ratings(t, out, got, full=True)
    out["fund_fields_ok"] = "+".join(got)
    out["fetch_kind"] = "full"
    return out


def estimates_and_ratings(t, out, got, full=False):
    # 추정치
    ee = safe(lambda: t.earnings_estimate, pd.DataFrame())
    time.sleep(PAUSE)
    if isinstance(ee, pd.DataFrame) and not ee.empty:
        got.append("estimates")
        g = lambda p, c: num(ee.loc[p, c]) if (p in ee.index and c in ee.columns) else None
        out["eps_est_0y"], out["eps_est_1y"] = g("0y", "avg"), g("+1y", "avg")
        out["eps_est_0q"], out["eps_est_1q"] = g("0q", "avg"), g("+1q", "avg")
        out["eps_yago_0y"] = g("0y", "yearAgoEps")
        out["n_analysts_eps"] = g("0y", "numberOfAnalysts")
        if out["eps_est_0y"] and out["eps_est_1y"] and out["eps_est_0y"] > 0:
            out["eps_growth_next_y_pct"] = rnd((out["eps_est_1y"] / out["eps_est_0y"] - 1) * 100)
        if out["eps_est_0y"] and out.get("eps_yago_0y") and out["eps_yago_0y"] > 0:
            out["eps_growth_this_y_pct"] = rnd((out["eps_est_0y"] / out["eps_yago_0y"] - 1) * 100)
    re_ = safe(lambda: t.revenue_estimate, pd.DataFrame()) if full else pd.DataFrame()
    if full:
        time.sleep(PAUSE)
    if isinstance(re_, pd.DataFrame) and not re_.empty:
        g = lambda p, c: num(re_.loc[p, c]) if (p in re_.index and c in re_.columns) else None
        out["rev_est_0y"], out["rev_est_1y"] = g("0y", "avg"), g("+1y", "avg")
        out["rev_growth_est_0y_pct"] = rnd((g("0y", "growth") or 0) * 100) if g("0y", "growth") is not None else None
        out["rev_growth_est_1y_pct"] = rnd((g("+1y", "growth") or 0) * 100) if g("+1y", "growth") is not None else None
    tr = safe(lambda: t.eps_trend, pd.DataFrame())
    time.sleep(PAUSE)
    if isinstance(tr, pd.DataFrame) and not tr.empty:
        got.append("eps_trend")
        for per, tag in [("0y", "0y"), ("+1y", "1y")]:
            if per in tr.index:
                cur = num(tr.loc[per].get("current"))
                for ago in ["7daysAgo", "30daysAgo", "90daysAgo"]:
                    out[f"eps_{tag}_chg_{ago.replace('daysAgo', 'd')}_pct"] = rnd(pct(cur, tr.loc[per].get(ago)))
    rv = safe(lambda: t.eps_revisions, pd.DataFrame())
    time.sleep(PAUSE)
    if isinstance(rv, pd.DataFrame) and not rv.empty:
        cols = {c.lower(): c for c in rv.columns}
        for per, tag in [("0y", "0y"), ("+1y", "1y")]:
            if per in rv.index:
                for k, nm in [("uplast30days", "up30"), ("downlast30days", "down30"), ("uplast7days", "up7"), ("downlast7days", "down7")]:
                    if k in cols:
                        out[f"rev_{tag}_{nm}"] = num(rv.loc[per, cols[k]])

    # 목표가·등급 변경(최근 90일)
    ud = safe(lambda: t.upgrades_downgrades, pd.DataFrame())
    time.sleep(PAUSE)
    if isinstance(ud, pd.DataFrame) and not ud.empty:
        got.append("ratings")
        u = ud.copy()
        u.index = pd.to_datetime(u.index, errors="coerce", utc=True)
        u = u[u.index >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=90)]
        pta = u.get("priceTargetAction", pd.Series(dtype=str)).astype(str).str.lower()
        act = u.get("Action", pd.Series(dtype=str)).astype(str).str.lower()
        out["pt_raise_90d"] = int(pta.str.contains("raise").sum())
        out["pt_cut_90d"] = int(pta.str.contains("lower").sum())
        out["upgrade_90d"] = int((act == "up").sum())
        out["downgrade_90d"] = int((act == "down").sum())


# ---------------- 누적 분기로 성장률 다시 계산 ----------------
def recompute_growth(df: pd.DataFrame, fins: pd.DataFrame) -> pd.DataFrame:
    """Yahoo는 분기 실적을 5개만 줘서 '직전 분기 전년 대비'(6개 필요)가 비는 경우가 많다.
    지금까지 쌓은 financials.parquet의 분기를 합쳐 다시 계산한다(분기가 쌓일수록 채워짐)."""
    if fins.empty:
        return df
    q = fins[fins["freq"] == "q"].copy()
    q["period_end"] = pd.to_datetime(q["period_end"], errors="coerce")
    q = q.dropna(subset=["period_end", "revenue"]).sort_values(["ticker", "period_end"])
    grp = dict(tuple(q.groupby("ticker")))
    out = df.copy()
    for i, tk in out["ticker"].items():
        g = grp.get(tk)
        if g is None or len(g) < 5:
            continue
        idx = list(g["period_end"])
        rev = dict(zip(idx, g["revenue"]))

        def yoy(k):
            if len(idx) < 5 + k:
                return None
            cur = idx[-1 - k]
            cands = [d for d in idx if 330 <= (cur - d).days <= 400]
            return rnd(pct(rev[cur], rev[cands[-1]])) if cands else None

        g0, g1 = yoy(0), yoy(1)
        if g0 is not None:
            out.at[i, "rev_growth_q"] = g0
        if g1 is not None:
            out.at[i, "rev_growth_prev_q"] = g1
        if g0 is not None and g1 is not None:
            out.at[i, "rev_accel_pp"] = rnd(g0 - g1)
        out.at[i, "q_count"] = len(idx)
    return out


# ---------------- 대상 고르기 ----------------
def pre_rank(screen: pd.DataFrame) -> pd.Series:
    """수집 우선순위용 기술적 사전 점수(투자 판단 아님). 유동성 미달은 NaN."""
    s = screen.copy()
    liq = (s["avg_dollar_vol_20d"].fillna(0) >= 2e7) & (s["close"].fillna(0) >= 5) & (s["history_days"].fillna(0) >= 120)
    rk = lambda c: s[c].rank(pct=True)
    score = (0.4 * rk("rs63_vs_spy_pct").fillna(0) + 0.2 * rk("ret_126d_pct").fillna(0)
             + 0.2 * rk("rs21_vs_sector_pct").fillna(0) + 0.1 * (s["vs_sma50_pct"].fillna(-1) > 0)
             + 0.1 * (s["from_high_pct"].fillna(-100) > -25))
    return score.where(liq)


def choose(screen: pd.DataFrame, old: pd.DataFrame, core: list[str], top: int, today: date):
    s = screen.set_index("ticker")
    pr = pre_rank(screen).values
    s["pre_score"] = pr
    s["pre_rank"] = s["pre_score"].rank(ascending=False, method="first")
    etf_like = NON_STOCK
    A = [t for t in core if t in s.index and t not in etf_like]
    A += s[s["pre_rank"] <= top].sort_values("pre_rank").index.tolist()
    if not old.empty:  # 실적 발표 전후(지난 실적 후 5일 이내, 다음 실적 3일 이내)
        o = old.set_index("ticker")
        ne = pd.to_datetime(o.get("next_earnings"), errors="coerce")
        near = ne[(ne.dt.date >= today - timedelta(days=5)) & (ne.dt.date <= today + timedelta(days=3))].index
        A += [t for t in near if t in s.index]
    A = list(dict.fromkeys(A))[:A_MAX]
    rest = [t for t in s.index if t not in set(A) and t not in etf_like and t != "KRW=X"]
    if not old.empty and "fetched" in old.columns:
        fa = old.set_index("ticker")["fetched"]
        rest.sort(key=lambda t: (str(fa.get(t)) if t in fa.index and pd.notna(fa.get(t)) else "0000"))
    return A, rest, s


# ---------------- 메인 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="Release 파일 폴더(ohlcv.parquet 등)")
    ap.add_argument("--budget-min", type=float, default=35)
    ap.add_argument("--top", type=int, default=150)
    args = ap.parse_args()
    t0 = time.time()
    st = Path(args.store)
    today = datetime.now(timezone.utc).date()

    screen = pd.read_csv(DATA / "screen.csv")
    core = [l.split("#")[0].strip().upper() for l in (TICKERS / "core.txt").read_text(encoding="utf-8").splitlines() if l.split("#")[0].strip()]
    fp, hp, fnp = st / "fundamentals.parquet", st / "fund_history.parquet", st / "financials.parquet"
    old = pd.read_parquet(fp) if fp.exists() else pd.DataFrame()
    hist = pd.read_parquet(hp) if hp.exists() else pd.DataFrame()
    fins = pd.read_parquet(fnp) if fnp.exists() else pd.DataFrame()
    prices = pd.read_parquet(st / "ohlcv.parquet", columns=["date", "ticker", "close"]) if (st / "ohlcv.parquet").exists() else None
    pg = dict(tuple(prices.groupby("ticker"))) if prices is not None else {}

    A, B, sidx = choose(screen, old, core, args.top, today)
    oldmap = {r["ticker"]: r for r in old.to_dict("records")} if not old.empty else {}
    log(f"대상: A(매일) {len(A)}개, B(순환 대기) {len(B)}개, 예산 {args.budget_min}분")

    new_rows, new_fin, done, failed, rl = [], [], [], [], 0
    stopped = None
    for tier, lst in (("A", A), ("B", B)):
        for tk in lst:
            if time.time() - t0 > args.budget_min * 60:
                stopped = "시간 예산"
                break
            prev = oldmap.get(tk)
            light = False
            if prev is not None and str(prev.get("fetch_kind")) in ("full", "light") and "quarterly" in str(prev.get("fund_fields_ok", "")):
                lf = pd.to_datetime(prev.get("last_full"), errors="coerce")
                ne = pd.to_datetime(prev.get("next_earnings"), errors="coerce")
                near = pd.notna(ne) and (today - timedelta(days=5) <= ne.date() <= today + timedelta(days=3))
                light = pd.notna(lf) and (today - lf.date()).days < FULL_EVERY_DAYS and not near
            try:
                row, fr = fetch_one(tk, pg.get(tk), light=light)
                rl = 0
                if light:
                    light_ok = bool(row.get("fund_fields_ok"))
                    row = {**{k: v for k, v in prev.items() if not (isinstance(v, float) and math.isnan(v)) and v is not None},
                           **{k: v for k, v in row.items() if v is not None}}
                    row["fetch_kind"] = "light"
                    row["fund_fields_ok"] = prev.get("fund_fields_ok") if light_ok else ""
                else:
                    row["last_full"] = today.isoformat()
            except RateLimited as e:
                rl += 1
                warn(f"레이트 리밋 {rl}/{RL_STOP} ({tk}): {e}")
                failed.append(tk)
                if rl >= RL_STOP:
                    stopped = "레이트 리밋 연속"
                    break
                time.sleep(60 * rl)
                continue
            except Exception as e:
                warn(f"{tk} 실패: {type(e).__name__}: {e}")
                failed.append(tk)
                continue
            if row.get("fund_fields_ok"):
                row.update(fetched=today.isoformat(), tier=tier)
                new_rows.append(row)
                new_fin += fr
                done.append(tk)
            else:
                failed.append(tk)
        if stopped:
            break

    if new_fin:
        nf = pd.DataFrame(new_fin)
        fins = pd.concat([fins, nf], ignore_index=True).drop_duplicates(["ticker", "freq", "period_end"], keep="last")
    newdf = pd.DataFrame(new_rows)
    if not newdf.empty:
        newdf = recompute_growth(newdf, fins)
        latest = pd.concat([old[~old["ticker"].isin(newdf["ticker"])] if not old.empty else old, newdf], ignore_index=True)
        hist = pd.concat([hist, newdf], ignore_index=True).drop_duplicates(["ticker", "fetched"], keep="last")
    else:
        latest = old

    # 검증 후 저장(이전보다 종목 수가 줄면 저장 안 함)
    errs = []
    if not old.empty and len(latest) < len(old):
        errs.append(f"종목 수 감소 {len(old)} → {len(latest)}")
    if not errs and not latest.empty:
        for df, path in ((latest, fp), (hist, hp), (fins, fnp)):
            df = df.copy()
            for c in df.columns:
                if df[c].dtype == object and c not in ("ticker", "tier", "fetched"):
                    df[c] = df[c].astype("string")
            tmp = path.with_suffix(".tmp.parquet")
            df.to_parquet(tmp, index=False, compression="zstd")
            tmp.replace(path)

    # screen.csv에 핵심 칸 병합
    keep = ["ticker", "fetched", "mcap", "fwd_pe", "ttm_pe", "pe_hist_median", "ev_rev", "rev_growth_q", "rev_growth_prev_q",
            "rev_accel_pp", "op_margin_q", "op_margin_chg_pp", "fcf_q", "fcf_q_yago", "eps_growth_this_y_pct",
            "eps_growth_next_y_pct", "rev_growth_est_1y_pct", "eps_0y_chg_30d_pct", "eps_1y_chg_30d_pct",
            "eps_1y_chg_90d_pct", "rev_1y_up30", "rev_1y_down30", "pt_raise_90d", "pt_cut_90d", "upgrade_90d",
            "downgrade_90d", "next_earnings", "last_earnings_date", "last_earnings_q", "q_count", "last_surprise_pct", "beats_4q", "target_mean",
            "rec_mean", "n_analysts", "short_pct_float", "yf_industry"]
    sc = sidx.reset_index()
    sc = sc[[c for c in sc.columns if c not in keep[1:]]]
    if not latest.empty:
        sc = sc.merge(latest[[c for c in keep if c in latest.columns]].rename(columns={"fetched": "fund_fetched"}),
                      on="ticker", how="left")
        if "mcap" in sc.columns:
            sc["mcap_b"] = (pd.to_numeric(sc["mcap"], errors="coerce") / 1e9).round(2)
            sc = sc.drop(columns="mcap")
        if "target_mean" in sc.columns:
            sc["target_upside_pct"] = ((pd.to_numeric(sc["target_mean"], errors="coerce") / sc["close"] - 1) * 100).round(1)
    sc["pre_score"] = sc["pre_score"].round(3)
    sc.to_csv(DATA / "screen.csv", index=False)

    covered = int(latest["ticker"].nunique()) if not latest.empty else 0
    stat = {"date": today.isoformat(), "A": len(A), "B_waiting": len(B), "fetched": len(done),
            "fetched_A": sum(1 for r in new_rows if r["tier"] == "A"), "failed": len(failed), "failed_sample": failed[:30],
            "stopped": stopped, "coverage": covered, "universe": int(len(sidx)),
            "coverage_pct": round(covered / max(1, len(sidx)) * 100, 1), "save_errors": errs,
            "minutes": round((time.time() - t0) / 60, 1)}
    (DATA / "fund_log.json").write_text(json.dumps(stat, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"펀더멘털: 받음 {len(done)}(A {stat['fetched_A']}) / 실패 {len(failed)} / 누적 {covered}종목({stat['coverage_pct']}%) "
        f"/ 중단: {stopped or '없음'} / {stat['minutes']}분")
    if errs:
        sys.exit(2)


if __name__ == "__main__":
    main()