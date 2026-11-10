"""
미국 주식 종가 수집기 (yfinance → CSV/JSON)

출력 (data/)
  core_prices.csv   : 핵심 목록 일별 OHLCV, 최근 400거래일 (date,ticker,open,high,low,close,adj_close,volume)
  latest.json       : 핵심 목록 티커별 최신 지표 (리포터가 이것만 읽어도 됨)
  universe_latest.csv : 넓은 목록(S&P 600 등) 스크리닝용 요약 1행/티커  (--universe 실행 시)

사용
  python scripts/fetch_closes.py              # 핵심 목록만
  python scripts/fetch_closes.py --universe   # 핵심 + 넓은 목록
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS = ROOT / "tickers"
KEEP_DAYS = 400          # core_prices.csv 보관 거래일 수
FETCH_PERIOD = "2y"      # 지표 계산용 다운로드 기간 (200일선·52주 고점 여유)


# ---------- 목록 읽기 ----------
def read_list(path: Path) -> list[str]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        t = line.split("#", 1)[0].strip().upper()
        if t:
            out.append(t.replace(".", "-") if t != "KRW=X" else t)  # BRK.B → BRK-B
    return list(dict.fromkeys(out))


def sp600_from_wikipedia() -> list[str]:
    try:
        tables = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_600_companies")
        col = [c for c in tables[0].columns if "Symbol" in str(c) or "Ticker" in str(c)][0]
        return [str(s).strip().replace(".", "-") for s in tables[0][col]]
    except Exception as e:  # 위키 형식 변경 대비
        print(f"[warn] S&P 600 목록 실패: {e}", file=sys.stderr)
        return []


# ---------- 다운로드 ----------
def download(tickers: list[str], period: str, chunk: int = 50, retries: int = 3) -> pd.DataFrame:
    """long 형식 DataFrame(date,ticker,open,high,low,close,adj_close,volume) 반환."""
    import yfinance as yf

    frames = []
    for i in range(0, len(tickers), chunk):
        part = tickers[i : i + chunk]
        for attempt in range(1, retries + 1):
            try:
                raw = yf.download(
                    part, period=period, interval="1d", auto_adjust=False,
                    group_by="ticker", threads=True, progress=False,
                )
                break
            except Exception as e:
                print(f"[warn] 다운로드 실패 {attempt}/{retries}: {e}", file=sys.stderr)
                time.sleep(10 * attempt)
        else:
            continue
        for t in part:
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.rename(columns=str.lower).rename(columns={"adj close": "adj_close"})
            need = ["open", "high", "low", "close", "adj_close", "volume"]
            if not set(need).issubset(df.columns):
                continue
            df = df[need].dropna(subset=["close"])
            if df.empty:
                continue
            df = df.reset_index().rename(columns={"Date": "date", "index": "date"})
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df.insert(1, "ticker", t)
            frames.append(df)
        time.sleep(1)
    if not frames:
        return pd.DataFrame(columns=["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"])
    return pd.concat(frames, ignore_index=True)


# ---------- 지표 ----------
def rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    v = 100 - 100 / (1 + rs)
    return float(v.iloc[-1]) if len(v) and pd.notna(v.iloc[-1]) else float("nan")


def pct(a, b):
    return (a / b - 1) * 100 if (b and pd.notna(a) and pd.notna(b)) else None


def r(x, nd=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), nd)


def indicators(g: pd.DataFrame) -> dict:
    g = g.sort_values("date")
    c, a = g["close"].reset_index(drop=True), g["adj_close"].reset_index(drop=True)
    h, l = g["high"].reset_index(drop=True), g["low"].reset_index(drop=True)
    n = len(c)
    last = c.iloc[-1]
    sma = lambda s, k: s.rolling(k).mean().iloc[-1] if n >= k else np.nan
    sma20, sma50, sma200 = sma(c, 20), sma(c, 50), sma(c, 200)
    sma20_5ago = c.rolling(20).mean().iloc[-6] if n >= 25 else np.nan
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr20 = tr.rolling(20).mean().iloc[-1] if n >= 21 else np.nan
    ret = lambda k: pct(a.iloc[-1], a.iloc[-1 - k]) if n > k else None
    hi252 = c.iloc[-252:].max()
    return {
        "date": g["date"].iloc[-1],
        "close": r(last, 4),
        "prev_close": r(c.iloc[-2], 4) if n > 1 else None,
        "chg_1d_pct": r(pct(last, c.iloc[-2])) if n > 1 else None,
        "high": r(h.iloc[-1], 4), "low": r(l.iloc[-1], 4),
        "sma20": r(sma20), "sma50": r(sma50), "sma200": r(sma200),
        "vs_sma20_pct": r(pct(last, sma20)), "vs_sma50_pct": r(pct(last, sma50)),
        "sma20_rising": None if np.isnan(sma20_5ago) else bool(sma20 > sma20_5ago),
        "rsi14": r(rsi(c)),
        "atr20_pct": r(atr20 / last * 100) if pd.notna(atr20) else None,
        "ret_5d_pct": r(ret(5)), "ret_21d_pct": r(ret(21)), "ret_63d_pct": r(ret(63)),
        "ret_126d_pct": r(ret(126)),
        "ret_6m_ex1m_pct": r(pct(a.iloc[-22], a.iloc[-127])) if n > 127 else None,
        "high_252d": r(hi252, 4), "from_high_pct": r(pct(last, hi252)),
        "avg_dollar_vol_20d": r((c * g["volume"].reset_index(drop=True)).rolling(20).mean().iloc[-1], 0) if n >= 20 else None,
        "history_days": n,
    }


# ---------- 메인 ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", action="store_true", help="넓은 목록 요약도 갱신")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)

    core = read_list(TICKERS / "core.txt")
    df = download(core, FETCH_PERIOD)
    got = set(df["ticker"].unique())
    missing = [t for t in core if t not in got]

    # 기존 파일과 합쳐 과거 행 보존(다운로드 실패 티커의 이력이 사라지지 않게)
    out_csv = DATA / "core_prices.csv"
    if out_csv.exists():
        old = pd.read_csv(out_csv)
        df = pd.concat([old, df], ignore_index=True).drop_duplicates(["date", "ticker"], keep="last")
    df = df.sort_values(["ticker", "date"])
    keep_dates = sorted(df["date"].unique())[-KEEP_DAYS:]
    df[df["date"].isin(keep_dates)].to_csv(out_csv, index=False, float_format="%.4f")

    # 기준일 = SPY 최신 거래일
    spy_dates = df.loc[df["ticker"] == "SPY", "date"]
    as_of = spy_dates.max() if not spy_dates.empty else df["date"].max()

    latest = {}
    for t, g in df.groupby("ticker"):
        if len(g) < 2:
            continue
        ind = indicators(g)
        ind["stale"] = ind["date"] != as_of and t != "KRW=X"
        latest[t] = ind
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of": as_of,
        "source": "Yahoo Finance via yfinance (close = 미조정 종가, 수익률은 adj_close 기준)",
        "missing_today": missing,
        "stale": sorted(t for t, v in latest.items() if v["stale"]),
        "count": len(latest),
    }
    (DATA / "latest.json").write_text(
        json.dumps({"meta": meta, "tickers": latest}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"core: {len(latest)}개, 기준일 {as_of}, 누락 {missing}")

    if args.universe:
        uni = read_list(TICKERS / "universe.txt") or sp600_from_wikipedia()
        uni = [t for t in uni if t not in core]
        udf = download(uni, "1y")
        rows = []
        for t, g in udf.groupby("ticker"):
            if len(g) >= 30:
                rows.append({"ticker": t, **indicators(g)})
        pd.DataFrame(rows).to_csv(DATA / "universe_latest.csv", index=False)
        print(f"universe: {len(rows)}/{len(uni)}개")

    if len(missing) > len(core) // 2:
        sys.exit("[error] 절반 이상 누락 — Yahoo 차단 가능성")


if __name__ == "__main__":
    main()
