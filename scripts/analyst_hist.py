"""실적 서프라이즈 이력 + 애널리스트 투자의견·목표가 변경 이력 수집기(yfinance).

- earnings_hist.parquet: 종목별 과거 실적 발표일, EPS 추정치, 실제 EPS, 서프라이즈(%)
- ratings_hist.parquet : 종목별 과거 투자의견 변경(증권사, 이전→이후 의견, 상향/하향), 목표가 변경(있으면)
- 하루 시간 예산(--budget-min) 안에서, 가장 오래전에 받은 종목부터 순환한다(첫 백필은 며칠 걸림).
- 이미 있는 행과 합치고 중복은 지운다(과거 기록은 지우지 않음).

사용: python scripts/analyst_hist.py --out data_out --budget-min 40
"""
import argparse, json, os, time, datetime as dt, random
import pandas as pd

try:
    import yfinance as yf
except ImportError:  # 로컬 점검용
    yf = None


def norm_earn(t: str, df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    d = df.reset_index()
    d.columns = [str(c) for c in d.columns]
    datecol = next((c for c in d.columns if "date" in c.lower()), d.columns[0])
    pick = lambda *names: next((d[c] for c in d.columns for n in names if c.lower().replace(" ", "") == n), pd.Series([None] * len(d)))
    out = pd.DataFrame({"ticker": t,
                        "earnings_dt": pd.to_datetime(d[datecol], errors="coerce", utc=True),
                        "eps_est": pd.to_numeric(pick("epsestimate"), errors="coerce"),
                        "eps_actual": pd.to_numeric(pick("reportedeps", "epsactual"), errors="coerce"),
                        "surprise_pct": pd.to_numeric(pick("surprise(%)", "surprisepercent"), errors="coerce")})
    out["earnings_date"] = out.earnings_dt.dt.tz_convert("America/New_York").dt.tz_localize(None)
    return out.drop(columns="earnings_dt").dropna(subset=["earnings_date"])


def norm_rat(t: str, df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    d = df.reset_index()
    d.columns = [str(c) for c in d.columns]
    g = lambda c: d[c] if c in d.columns else pd.Series([None] * len(d))
    datecol = next((c for c in d.columns if "date" in c.lower()), d.columns[0])
    return pd.DataFrame({"ticker": t, "date": pd.to_datetime(d[datecol], errors="coerce", utc=True).dt.tz_localize(None),
                         "firm": g("firm"), "to_grade": g("toGrade"), "from_grade": g("fromGrade"), "action": g("action"),
                         "pt_action": g("priceTargetAction"), "pt_now": pd.to_numeric(g("currentPriceTarget"), errors="coerce"),
                         "pt_prior": pd.to_numeric(g("priorPriceTarget"), errors="coerce")}).dropna(subset=["date"])


def fetch_one(t: str, limit: int):
    tk = yf.Ticker(t)
    e = r = None; err = []
    try:
        e = tk.get_earnings_dates(limit=limit)
    except Exception as x:
        err.append(f"earn:{type(x).__name__}")
    try:
        r = tk.upgrades_downgrades
    except Exception as x:
        err.append(f"rat:{type(x).__name__}")
    return norm_earn(t, e), norm_rat(t, r), err


def merge(old_path, new: pd.DataFrame, keys):
    old = pd.read_parquet(old_path) if os.path.exists(old_path) else pd.DataFrame()
    allx = pd.concat([old, new], ignore_index=True) if len(new) else old
    if len(allx):
        allx = allx.sort_values("fetched").drop_duplicates(subset=keys, keep="last").reset_index(drop=True)
    return allx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data_out"); ap.add_argument("--screen", default="data/screen.csv")
    ap.add_argument("--budget-min", type=float, default=40); ap.add_argument("--limit", type=int, default=60)
    ap.add_argument("--refresh-days", type=int, default=7); ap.add_argument("--sleep", type=float, default=0.6)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    sp = os.path.join(a.out, "analyst_state.json")
    state = json.load(open(sp)) if os.path.exists(sp) else {}
    sc = pd.read_csv(a.screen)
    tickers = sc.ticker.astype(str).str.upper().tolist()
    if "quote_type" in sc.columns:   # ETF는 실적·의견이 없음
        tickers = sc[sc.quote_type.fillna("EQUITY").str.upper() != "ETF"].ticker.astype(str).str.upper().tolist()
    today = dt.date.today()
    due = [t for t in tickers if (today - dt.date.fromisoformat(state.get(t, "2000-01-01"))).days >= a.refresh_days]
    due.sort(key=lambda t: state.get(t, "2000-01-01"))
    t0 = time.time(); E, Rt, errs, n = [], [], {}, 0
    stamp = dt.datetime.utcnow().strftime("%Y-%m-%d")
    for t in due:
        if time.time() - t0 > a.budget_min * 60:
            break
        e, r, err = fetch_one(t, a.limit); n += 1
        if len(e): E.append(e.assign(fetched=stamp))
        if len(r): Rt.append(r.assign(fetched=stamp))
        if err:
            errs[t] = err
            if sum(1 for v in errs.values()) >= 25 and n <= 30:   # 초반부터 다 실패하면 차단된 것 — 중단
                print("초반 오류가 많아 중단(요청 차단 가능성)"); break
        else:
            state[t] = today.isoformat()
        time.sleep(a.sleep + random.random() * 0.4)
    ep = os.path.join(a.out, "earnings_hist.parquet"); rp = os.path.join(a.out, "ratings_hist.parquet")
    ea = merge(ep, pd.concat(E, ignore_index=True) if E else pd.DataFrame(), ["ticker", "earnings_date"])
    ra = merge(rp, pd.concat(Rt, ignore_index=True) if Rt else pd.DataFrame(), ["ticker", "date", "firm", "to_grade", "from_grade", "pt_now"])
    if len(ea): ea.to_parquet(ep, index=False)
    if len(ra): ra.to_parquet(rp, index=False)
    json.dump(state, open(sp, "w"))
    log = dict(updated_utc=dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), processed=n, due=len(due), errors=len(errs),
               error_sample=dict(list(errs.items())[:10]), earnings_rows=int(len(ea)), ratings_rows=int(len(ra)),
               covered=sum(1 for t in tickers if t in state), universe=len(tickers))
    json.dump(log, open(os.path.join(a.out, "analyst_log.json"), "w"), ensure_ascii=False, indent=1)
    print("완료:", log)


if __name__ == "__main__":
    main()
