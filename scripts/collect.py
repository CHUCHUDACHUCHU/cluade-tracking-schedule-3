"""
미국 주식 일봉 수집기 v2 — 약 2,800종목(S&P 600 + 러셀 2000 + 러셀 미드캡) + 핵심 목록

저장 구조
  [GitHub Release 'data' 첨부 파일 — git 이력에 안 쌓임]
    ohlcv.parquet                  전체 시계열(long: date,ticker,open,high,low,close,adj_close,volume)
                                   처음 3년 백필 → 매일 누적, 지우지 않음
    ohlcv_backup_YYYY-MM-DD.parquet  토요일(미국 금요일 장) 실행마다 백업, 최근 4개 유지(워크플로가 관리)
  [git: data/]
    screen.csv        전 종목 1행 요약 지표(②의 1차 거르기용)
    latest.json       핵심 목록(core.txt) 최신 지표(①②③ 호환)
    core_prices.csv   핵심 목록 최근 400거래일(①②③ 호환)
    collect_log.json  이번 실행 통계(받은 수·실패·백필·분할 감지)
  [git: tickers/]
    core.txt          핵심 목록(사용자 관리: SPY·섹터 ETF·환율·대형주 후보)
    universe.csv      자동 생성 목록(ticker,name,sector,sources) — 토요일마다 갱신, 실패 시 직전 목록 사용

사용
  python scripts/collect.py --store store/ohlcv.parquet [--refresh-universe]
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TICKERS = ROOT / "tickers"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/csv,application/json,text/html;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
NASDAQ_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=10000&offset=0&download=true"
SMID_MCAP = (3e8, 5e10)   # 나스닥 대체 목록: 시총 3억~500억 달러(러셀 2000+미드캡 근사)
UNIVERSE_ERRORS: list[str] = []

KEEP_DAYS = 400
BACKFILL_PERIOD = "3y"
UPDATE_PERIOD = "1mo"
CHUNK = 100
COLS = ["date", "ticker", "open", "high", "low", "close", "adj_close", "volume"]

ISHARES = {  # 보유 종목 CSV(섹터 포함)
    "r2000": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf/1467271812596.ajax?fileType=csv&fileName=IWM_holdings&dataType=fund",
    "rmid": "https://www.ishares.com/us/products/239718/ishares-russell-midcap-etf/1467271812596.ajax?fileType=csv&fileName=IWR_holdings&dataType=fund",
}
SP600_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"

SECTOR_ETF = [  # GICS 섹터명(일부 문자열) → 섹터 ETF
    ("information technology", "XLK"), ("technology", "XLK"), ("health", "XLV"), ("financ", "XLF"),
    ("industrial", "XLI"), ("consumer discretionary", "XLY"), ("consumer staples", "XLP"),
    ("energy", "XLE"), ("material", "XLB"), ("real estate", "XLRE"), ("utilit", "XLU"),
    ("communication", "XLC"), ("telecom", "XLC"), ("finance", "XLF"),
]


def log(*a):
    print(*a, flush=True)


def warn(*a):
    print("[warn]", *a, file=sys.stderr, flush=True)


# ---------------- 목록 ----------------
def norm_ticker(t: str) -> str | None:
    t = str(t).strip().upper()
    if t == "KRW=X":
        return t
    t = t.replace(".", "-").replace("/", "-").replace(" ", "-")
    return t if re.fullmatch(r"[A-Z][A-Z0-9\-]{0,9}", t) else None


def read_core() -> list[str]:
    out = []
    for line in (TICKERS / "core.txt").read_text(encoding="utf-8").splitlines():
        t = norm_ticker(line.split("#", 1)[0]) if line.split("#", 1)[0].strip() else None
        if t:
            out.append(t)
    return list(dict.fromkeys(out))


def http_get(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers=UA)
    raw = urllib.request.urlopen(req, timeout=timeout).read()
    return raw.decode("utf-8-sig", errors="replace")


def parse_ishares(text: str) -> pd.DataFrame:
    """iShares 보유 종목 CSV → ticker,name,sector (주식만)."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("Ticker,") or l.startswith('"Ticker",'))
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])), on_bad_lines="skip", dtype=str)
    df.columns = [c.strip() for c in df.columns]
    if "Asset Class" in df.columns:
        df = df[df["Asset Class"].str.strip().str.lower() == "equity"]
    df = pd.DataFrame({"ticker": df["Ticker"].map(norm_ticker), "name": df.get("Name"),
                       "sector": df.get("Sector")})
    return df.dropna(subset=["ticker"]).drop_duplicates("ticker")


def fetch_nasdaq_smid() -> pd.DataFrame:
    """대체 목록: 나스닥 스크리너(NYSE·NASDAQ·AMEX 전 종목)에서 미국 기업 중 시총 3억~500억 달러."""
    j = json.loads(http_get(NASDAQ_URL))
    rows = j["data"]["rows"]
    d = pd.DataFrame(rows)
    d["mcap"] = pd.to_numeric(d["marketCap"].astype(str).str.replace(",", ""), errors="coerce")
    d = d[(d["mcap"] >= SMID_MCAP[0]) & (d["mcap"] <= SMID_MCAP[1])]
    if "country" in d.columns:
        d = d[d["country"].fillna("").str.contains("United States")]
    # 보통주만: 워런트·유닛·권리·우선주·스팩(인수 목적 회사) 제외
    name = d["name"].fillna("")
    sym = d["symbol"].astype(str)
    bad = (name.str.contains(r"\b(?:Warrants?|Units?|Rights?|Preferred|Depositary Shares|Notes due)\b", case=False, regex=True)
           | name.str.contains(r"Acquisition Corp|Acquisition Co\b|SPAC", case=False, regex=True)
           | sym.str.contains(r"\^|[/.\-](?:WS|W|U|R)$|-P[A-Z]?$", regex=True))
    d = d[~bad]
    return pd.DataFrame({"ticker": d["symbol"].map(norm_ticker), "name": d["name"],
                         "sector": d["sector"]}).dropna(subset=["ticker"]).drop_duplicates("ticker")


def fetch_sp600() -> pd.DataFrame:
    tables = pd.read_html(io.StringIO(http_get(SP600_URL)))
    t = tables[0]
    sym = [c for c in t.columns if "Symbol" in str(c) or "Ticker" in str(c)][0]
    sec = [c for c in t.columns if "Sector" in str(c)]
    nm = [c for c in t.columns if "Security" in str(c) or "Company" in str(c)]
    return pd.DataFrame({"ticker": t[sym].map(norm_ticker),
                         "name": t[nm[0]] if nm else None,
                         "sector": t[sec[0]] if sec else None}).dropna(subset=["ticker"])


def build_universe(refresh: bool) -> pd.DataFrame:
    path = TICKERS / "universe.csv"
    old = pd.read_csv(path, dtype=str) if path.exists() else pd.DataFrame(columns=["ticker", "name", "sector", "sources"])
    if not refresh and not old.empty:
        return old
    parts, ok = [], []
    for src, fn in [("sp600", fetch_sp600)] + [(k, (lambda u=u: parse_ishares(http_get(u)))) for k, u in ISHARES.items()]:
        for attempt in range(3):
            try:
                d = fn()
                if len(d) < 100:
                    raise ValueError(f"{len(d)}개뿐")
                d["sources"] = src
                parts.append(d)
                ok.append(f"{src}:{len(d)}")
                break
            except Exception as e:
                warn(f"목록 {src} 실패 {attempt + 1}/3: {e}")
                if attempt == 2:
                    UNIVERSE_ERRORS.append(f"{src}: {type(e).__name__}: {str(e)[:150]}")
                time.sleep(5 * (attempt + 1))
    # 러셀(iShares)이 하나라도 실패하면 나스닥 대체 목록으로 보충
    if any(not any(p["sources"].iat[0] == k for p in parts) for k in ISHARES):
        try:
            d = fetch_nasdaq_smid()
            if len(d) >= 100:
                d["sources"] = "nasdaq_smid"
                parts.append(d)
                ok.append(f"nasdaq_smid:{len(d)}")
        except Exception as e:
            warn(f"나스닥 대체 목록 실패: {e}")
            UNIVERSE_ERRORS.append(f"nasdaq_smid: {type(e).__name__}: {str(e)[:150]}")
    if not parts:
        warn("목록을 하나도 못 받음 → 직전 universe.csv 사용")
        return old
    new = pd.concat(parts, ignore_index=True)
    new = (new.groupby("ticker", as_index=False)
              .agg({"name": "first", "sector": "first", "sources": lambda s: "+".join(sorted(set(s)))}))
    # 일부 출처만 성공했으면 직전 목록의 나머지를 보존(목록이 갑자기 줄지 않게)
    if len(ok) < 1 + len(ISHARES) and not old.empty:
        keep = old[~old["ticker"].isin(new["ticker"])]
        new = pd.concat([new, keep], ignore_index=True)
    if not old.empty and len(new) < 0.8 * len(old):
        warn(f"새 목록 {len(new)}개가 직전 {len(old)}개의 80% 미만 → 직전 목록 사용")
        return old
    new.sort_values("ticker").to_csv(path, index=False)
    log(f"목록 갱신: {', '.join(ok)} → 합계 {len(new)}개")
    return new


# ---------------- 다운로드 ----------------
def _yf():
    import yfinance as yf
    return yf


def download(tickers: list[str], period: str | None = None, start: str | None = None,
             chunk: int = CHUNK, pause: float = 2.0) -> pd.DataFrame:
    """일봉 long DF. 덩어리 단위 재시도 + 끝에 빠진 티커만 작은 덩어리로 한 번 더."""
    if not tickers:
        return pd.DataFrame(columns=COLS)

    def one(part):
        yf = _yf()
        for attempt in range(1, 4):
            try:
                raw = yf.download(part, period=None if start else period, start=start, interval="1d",
                                  auto_adjust=False, group_by="ticker", threads=True, progress=False)
                return raw
            except Exception as e:
                warn(f"다운로드 실패 {attempt}/3 ({len(part)}개): {e}")
                time.sleep(15 * attempt)
        return None

    def to_long(raw, part):
        out = []
        if raw is None or raw.empty:
            return out
        for t in part:
            try:
                d = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            d = d.rename(columns=lambda c: str(c).lower()).rename(columns={"adj close": "adj_close"})
            need = ["open", "high", "low", "close", "adj_close", "volume"]
            if not set(need).issubset(d.columns):
                continue
            d = d[need].dropna(subset=["close"])
            if d.empty:
                continue
            d = d.reset_index()
            d = d.rename(columns={d.columns[0]: "date"})
            d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
            d.insert(1, "ticker", t)
            out.append(d[COLS])
        return out

    frames = []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        frames += to_long(one(part), part)
        time.sleep(pause)
    got = {f["ticker"].iat[0] for f in frames}
    miss = [t for t in tickers if t not in got]
    if miss and len(miss) < len(tickers):  # 전부 실패면 차단 상황이니 반복하지 않음
        log(f"빠진 {len(miss)}개 재시도")
        for i in range(0, len(miss), 20):
            part = miss[i:i + 20]
            frames += to_long(one(part), part)
            time.sleep(pause)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLS)


def fill_from_intraday(daily: pd.DataFrame, tickers: list[str], have: set) -> tuple[pd.DataFrame, dict]:
    """핵심 목록만: 일봉이 늦게 생기는 날(11/9 확인) 15분봉으로 그날 일봉을 만든다(장 마감 15:45 봉까지 있는 날만)."""
    try:
        raw = _yf().download(tickers, period="5d", interval="15m", auto_adjust=False,
                             group_by="ticker", threads=True, progress=False, prepost=False)
    except Exception as e:
        warn(f"15분봉 실패: {e}")
        return daily, {}
    if raw is None or raw.empty:
        return daily, {}
    rows, filled = [], {}
    for t in tickers:
        if t == "KRW=X":
            continue
        try:
            g = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        g = g.rename(columns=lambda c: str(c).lower()).dropna(subset=["close"])
        if g.empty:
            continue
        idx = g.index.tz_convert("America/New_York") if g.index.tz is not None else g.index
        g = g.set_axis(idx).between_time("09:30", "15:59")
        for d, day in g.groupby(g.index.date):
            ds = d.strftime("%Y-%m-%d")
            last = day.index[-1]
            if (ds, t) in have or last.hour * 60 + last.minute < 15 * 60 + 45:
                continue
            c = float(day["close"].iloc[-1])
            rows.append({"date": ds, "ticker": t, "open": float(day["open"].iloc[0]), "high": float(day["high"].max()),
                         "low": float(day["low"].min()), "close": c, "adj_close": c, "volume": float(day["volume"].sum())})
            filled.setdefault(t, []).append(ds)
    if rows:
        daily = pd.concat([daily, pd.DataFrame(rows)[COLS]], ignore_index=True)
    return daily, filled


# ---------------- 저장소 갱신 ----------------
def update_store(store: pd.DataFrame, tickers: list[str], core: list[str]) -> tuple[pd.DataFrame, dict]:
    stat = {}
    counts = store.groupby("ticker").size() if not store.empty else pd.Series(dtype=int)
    known = set(counts[counts >= 30].index)
    need_full = [t for t in tickers if t not in known]
    need_inc = [t for t in tickers if t in known]
    log(f"대상 {len(tickers)}개 — 새로 3년 백필 {len(need_full)}개, 증분 {len(need_inc)}개")

    inc = download(need_inc, period=UPDATE_PERIOD)
    full = download(need_full, period=BACKFILL_PERIOD)
    new = pd.concat([inc, full], ignore_index=True)

    have = set(zip(new["date"], new["ticker"]))
    new, filled = fill_from_intraday(new, [t for t in core if t in set(tickers)], have)

    # 분할 등으로 과거 종가가 바뀐 티커 → 기존 시작일부터 다시 받기
    rebf = []
    if not store.empty and not inc.empty:
        m = store[store["ticker"].isin(need_inc)][["date", "ticker", "close"]].merge(
            inc[["date", "ticker", "close"]], on=["date", "ticker"], suffixes=("_o", "_n"))
        bad = m[(m["close_n"] / m["close_o"] - 1).abs() > 0.02]
        rebf = sorted(bad["ticker"].unique().tolist())
    if rebf:
        first = store[store["ticker"].isin(rebf)].groupby("ticker")["date"].min().min()
        log(f"과거 종가 변경 감지(분할 등) {len(rebf)}개 → {first}부터 재수집: {rebf[:20]}")
        redo = download(rebf, start=first)
        ok = set(redo["ticker"].unique())
        store = store[~store["ticker"].isin(ok)]
        new = pd.concat([new[~new["ticker"].isin(ok)], redo], ignore_index=True)

    merged = pd.concat([store, new], ignore_index=True)
    merged = merged.drop_duplicates(["date", "ticker"], keep="last").sort_values(["ticker", "date"], ignore_index=True)
    got = set(new["ticker"].unique())
    stat.update(requested=len(tickers), received=len(got), missing=sorted(set(tickers) - got),
                backfilled=len([t for t in need_full if t in got]), backfill_failed=sorted(set(need_full) - got),
                split_refetched=rebf, intraday_filled=filled, new_rows=int(len(new)))
    return merged, stat


def validate(old: pd.DataFrame, new: pd.DataFrame) -> list[str]:
    """업로드 전 안전 점검 — 문제가 있으면 업로드하지 않는다."""
    errs = []
    if new.empty:
        return ["새 저장소가 비어 있음"]
    if not old.empty:
        if len(new) < 0.99 * len(old):
            errs.append(f"행 수 감소 {len(old)} → {len(new)}")
        ot, nt = old["ticker"].nunique(), new["ticker"].nunique()
        if nt < 0.98 * ot:
            errs.append(f"티커 수 감소 {ot} → {nt}")
        if new["date"].max() < old["date"].max():
            errs.append(f"최신 날짜 후퇴 {old['date'].max()} → {new['date'].max()}")
    if new.duplicated(["date", "ticker"]).any():
        errs.append("(date,ticker) 중복")
    if (new["close"] <= 0).any():
        errs.append("0 이하 종가")
    return errs


# ---------------- 지표 ----------------
def r(x, nd=2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(x) or np.isinf(x) else round(x, nd)


def pct(a, b):
    return (a / b - 1) * 100 if (b and pd.notna(a) and pd.notna(b) and b != 0) else None


def rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    v = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    return float(v.iloc[-1]) if len(v) and pd.notna(v.iloc[-1]) else float("nan")


def indicators(g: pd.DataFrame) -> dict:
    g = g.sort_values("date")
    c, a = g["close"].reset_index(drop=True), g["adj_close"].reset_index(drop=True)
    h, l, v = g["high"].reset_index(drop=True), g["low"].reset_index(drop=True), g["volume"].reset_index(drop=True)
    n, last = len(c), c.iloc[-1]
    sma = lambda k: c.rolling(k).mean().iloc[-1] if n >= k else np.nan
    s20, s50, s200 = sma(20), sma(50), sma(200)
    s20_5 = c.rolling(20).mean().iloc[-6] if n >= 25 else np.nan
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(20).mean().iloc[-1] if n >= 21 else np.nan
    ret = lambda k: pct(a.iloc[-1], a.iloc[-1 - k]) if n > k else None
    hi = c.iloc[-252:].max()
    vol20 = v.rolling(20).mean().iloc[-1] if n >= 20 else np.nan
    return {
        "date": g["date"].iloc[-1], "close": r(last, 4), "prev_close": r(c.iloc[-2], 4) if n > 1 else None,
        "chg_1d_pct": r(pct(last, c.iloc[-2])) if n > 1 else None,
        "high": r(h.iloc[-1], 4), "low": r(l.iloc[-1], 4),
        "sma20": r(s20), "sma50": r(s50), "sma200": r(s200),
        "vs_sma20_pct": r(pct(last, s20)), "vs_sma50_pct": r(pct(last, s50)), "vs_sma200_pct": r(pct(last, s200)),
        "sma20_rising": None if np.isnan(s20_5) else bool(s20 > s20_5),
        "rsi14": r(rsi(c)), "atr20_pct": r(atr / last * 100) if pd.notna(atr) else None,
        "ret_5d_pct": r(ret(5)), "ret_21d_pct": r(ret(21)), "ret_63d_pct": r(ret(63)), "ret_126d_pct": r(ret(126)),
        "ret_252d_pct": r(ret(252)),
        "ret_6m_ex1m_pct": r(pct(a.iloc[-22], a.iloc[-127])) if n > 127 else None,
        "high_252d": r(hi, 4), "from_high_pct": r(pct(last, hi)),
        "vol_ratio_20d": r(v.iloc[-1] / vol20) if pd.notna(vol20) and vol20 else None,
        "avg_dollar_vol_20d": r((c * v).rolling(20).mean().iloc[-1], 0) if n >= 20 else None,
        "history_days": n, "history_from": g["date"].iloc[0],
    }


def sector_etf(sector) -> str | None:
    s = str(sector or "").lower()
    for key, etf in SECTOR_ETF:
        if key in s:
            return etf
    return None


# ---------------- 메인 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="ohlcv.parquet 경로(없으면 새로 만듦)")
    ap.add_argument("--refresh-universe", action="store_true")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    t0 = time.time()

    core = read_core()
    uni = build_universe(args.refresh_universe or not (TICKERS / "universe.csv").exists())
    uni_t = [t for t in uni["ticker"].map(norm_ticker).dropna().tolist() if t not in core]
    tickers = core + uni_t

    sp = Path(args.store)
    old = pd.read_parquet(sp) if sp.exists() else pd.DataFrame(columns=COLS)
    log(f"기존 저장소: {len(old):,}행, {old['ticker'].nunique() if not old.empty else 0}종목, "
        f"{old['date'].min() if not old.empty else '-'} ~ {old['date'].max() if not old.empty else '-'}")

    store, stat = update_store(old, tickers, core)
    errs = validate(old, store)
    stat["validate_errors"] = errs
    if errs:
        warn("검증 실패 → 저장소를 덮어쓰지 않음:", errs)
    else:
        for col in ["open", "high", "low", "close", "adj_close"]:
            store[col] = store[col].astype("float64").round(4)
        store["volume"] = store["volume"].fillna(0).astype("int64")
        tmp = sp.with_suffix(".tmp.parquet")
        store[COLS].to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(sp)
        log(f"저장소 저장: {len(store):,}행, {store['ticker'].nunique()}종목, "
            f"{store['date'].min()} ~ {store['date'].max()}, {sp.stat().st_size / 1e6:.1f}MB")

    base = store if not errs else old
    spy = base[base["ticker"] == "SPY"]["date"]
    as_of = spy.max() if not spy.empty else base["date"].max()

    # 핵심 목록 출력(①②③ 호환)
    cdf = base[base["ticker"].isin(core)].copy()
    keep = sorted(cdf["date"].unique())[-KEEP_DAYS:]
    cdf[cdf["date"].isin(keep)].to_csv(DATA / "core_prices.csv", index=False, float_format="%.4f")

    ind_all = {}
    for t, g in base.groupby("ticker"):
        if len(g) >= 2:
            ind_all[t] = indicators(g)

    latest = {t: {**ind_all[t], "stale": ind_all[t]["date"] != as_of and t != "KRW=X"} for t in core if t in ind_all}
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "as_of": as_of,
        "source": "Yahoo Finance via yfinance (close = 분할 반영·배당 미반영, adj_close = 배당까지 반영)",
        "missing_today": [t for t in core if t in stat["missing"]],
        "intraday_filled": stat["intraday_filled"],
        "stale": sorted(t for t, v in latest.items() if v["stale"]),
        "count": len(latest),
        "store": "GitHub Release 'data' / ohlcv.parquet",
    }
    (DATA / "latest.json").write_text(json.dumps({"meta": meta, "tickers": latest}, ensure_ascii=False, indent=1),
                                      encoding="utf-8")

    # 전 종목 스크린
    info = uni.set_index("ticker")
    etf_ret21 = {e: ind_all[e]["ret_21d_pct"] for _, e in SECTOR_ETF if e in ind_all}
    spy63 = ind_all.get("SPY", {}).get("ret_63d_pct")
    rows = []
    for t, ind in ind_all.items():
        sec = info["sector"].get(t) if t in info.index else None
        etf = sector_etf(sec)
        r21 = ind["ret_21d_pct"]
        rows.append({
            "ticker": t, "name": info["name"].get(t) if t in info.index else None, "sector": sec,
            "sector_etf": etf, "sources": info["sources"].get(t) if t in info.index else "core",
            **{k: v for k, v in ind.items()},
            "rs21_vs_sector_pct": r(r21 - etf_ret21[etf]) if (etf in etf_ret21 and r21 is not None and etf_ret21[etf] is not None) else None,
            "rs63_vs_spy_pct": r(ind["ret_63d_pct"] - spy63) if (ind["ret_63d_pct"] is not None and spy63 is not None) else None,
            "stale": ind["date"] != as_of,
        })
    sdf = pd.DataFrame(rows).sort_values("ticker")
    sdf.to_csv(DATA / "screen.csv", index=False)
    # 현행 ② 지침(일요일 새 리더 발굴) 호환용: 핵심 목록 밖 종목만
    sdf[sdf["sources"] != "core"].to_csv(DATA / "universe_latest.csv", index=False)

    stat["universe_errors"] = UNIVERSE_ERRORS
    stat["universe_sources"] = uni["sources"].value_counts().to_dict() if "sources" in uni else {}
    stat.update(as_of=as_of, universe=len(uni_t), core=len(core), screen_rows=len(rows),
                store_rows=int(len(base)), store_tickers=int(base["ticker"].nunique()),
                store_from=str(base["date"].min()), seconds=round(time.time() - t0))
    stat["missing_count"] = len(stat["missing"])
    stat["missing"] = stat["missing"][:200]
    (DATA / "collect_log.json").write_text(json.dumps(stat, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"core: {len(latest)}개, 기준일 {as_of} / screen {len(rows)}행 / 받음 {stat['received']}/{stat['requested']} "
        f"/ 백필 {stat['backfilled']} / 분할 재수집 {len(stat['split_refetched'])} / {stat['seconds']}초")

    if errs:
        sys.exit(2)
    if stat["received"] < 0.5 * stat["requested"]:
        sys.exit("[error] 절반 이상 못 받음 — Yahoo 차단 가능성(저장소는 저장됨)")


if __name__ == "__main__":
    main()