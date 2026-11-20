# -*- coding: utf-8 -*-
"""⑪ 실험 바구니(REV) 규칙 엔진 v2 (2026-11-20 사용자 승인 규칙).
가상 실험. 입력: Notion 행(JSON), data/latest.json, data/screen.csv, store/ohlcv.parquet, store/fundamentals.parquet
출력: plan.json {updates:[{page_id,properties}], creates:[{properties}], message, summary, checks}
사용: python3 -I rev_engine.py ROWS_JSON DATA_DIR STORE_DIR OUT_JSON
"""
import sys, json, math
import pandas as pd, numpy as np

VERSION = "v2"
HOLD_DAYS = 20
STOP_K, STOP_MIN, STOP_MAX = 2.5, 8.0, 15.0     # 손절선 = clamp(2.5 x ATR20%, 8, 15)
MIN_DAYS_LEFT = 5                                # 교체 매수는 남은 보유일 5일 이상일 때만
MAX_NEW_BUYS = 2                                 # 하루 교체 매수 최대 2종목
SECTOR_CAP = 3
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
            "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}

def r2(x): return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 2)
def r6(x): return round(float(x), 6)
def nz(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))
def sec(s):
    s = str(s or "")
    return "Technology" if s in ("Technology", "Information Technology") else s
def dprop(name, d): return {f"date:{name}:start": d, f"date:{name}:is_datetime": 0}

rows_path, DATA, STORE, OUT = sys.argv[1:5]
raw = json.load(open(rows_path))
rows = raw["results"] if isinstance(raw, dict) and "results" in raw else raw
latest = json.load(open(f"{DATA}/latest.json"))
as_of = latest["meta"]["as_of"]
fxrow = latest["tickers"].get("KRW=X") or {}
sc = pd.read_csv(f"{DATA}/screen.csv")
sc_by = sc.set_index("ticker")

def g(r, k):
    v = r.get(k)
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else v

# ---------- 상태 분류 ----------
summ_rows = [r for r in rows if g(r, "구분") == "일일 합계"]
summ_rows.sort(key=lambda r: g(r, "date:기준일:start") or "")
last_summ = summ_rows[-1] if summ_rows else None
pos_rows = [r for r in rows if g(r, "구분") in ("보유", "매도 대기", "정리", "종료")]
cur_round = int(max([g(r, "회차") or 0 for r in pos_rows] + [0]))
pos_rows = [r for r in pos_rows if int(g(r, "회차") or 0) == cur_round]
active = [r for r in pos_rows if g(r, "구분") in ("보유", "매도 대기")]
if not active:
    json.dump({"updates": [], "creates": [], "summary": "REV 보유 행 없음",
               "message": "[가상 실험] REV 보유 행이 없습니다 — 직전 회차 종료 때 다음 회차 종목이 만들어지지 않았습니다. Notion을 확인해 주세요.\n가상 실험이며 투자 권유가 아닙니다",
               "checks": {}}, open(OUT, "w"), ensure_ascii=False)
    print("보유 행 없음"); raise SystemExit(0)

def kind(r):
    k = g(r, "편입")
    if k: return k
    return "SPY 대기" if g(r, "티커") == "SPY" else "초기"

tickers = sorted({g(r, "티커") for r in pos_rows if g(r, "티커")} | {"SPY"})
# 교체 후보 OHLCV는 필요할 때 다시 읽음
ohlcv_all_path = f"{STORE}/ohlcv.parquet"
oh = pd.read_parquet(ohlcv_all_path, filters=[("ticker", "in", tickers)])
oh["date"] = pd.to_datetime(oh["date"]).dt.strftime("%Y-%m-%d")
oh = oh.sort_values(["ticker", "date"])
spy_dates = sorted(oh[oh.ticker == "SPY"].date.unique())
D = min(spy_dates[-1], as_of)                     # 기준일 = 데이터상 최신 마감일
stale_note = None

def px(t, d, col="close"):
    s = oh[(oh.ticker == t) & (oh.date == d)]
    return None if s.empty else float(s.iloc[0][col])

def last_close(t, d):
    s = oh[(oh.ticker == t) & (oh.date <= d)]
    return (None, None) if s.empty else (float(s.iloc[-1]["close"]), s.iloc[-1]["date"])

def next_tdays(d, n):
    out, x = [], pd.Timestamp(d)
    while len(out) < n:
        x += pd.Timedelta(days=1)
        if x.weekday() < 5 and x.strftime("%Y-%m-%d") not in HOLIDAYS:
            out.append(x.strftime("%Y-%m-%d"))
    return out

def tdays_between(a, b):  # a..b 포함 거래일 수(과거 구간)
    return len([x for x in spy_dates if a <= x <= b])

def atr_pct(t, d):
    s = oh[(oh.ticker == t) & (oh.date <= d)].tail(21)
    if len(s) < 21: return None
    pc = s.close.shift()
    tr = pd.concat([s.high - s.low, (s.high - pc).abs(), (s.low - pc).abs()], axis=1).max(axis=1).iloc[1:]
    return float(tr.mean() / s.close.iloc[-1] * 100)

def prev_tday(d):
    s = [x for x in spy_dates if x < d]
    return s[-1] if s else None

# FX
fx = fxrow.get("close"); fx_date = fxrow.get("date"); fx_note = None
if not fx:
    fx = g(last_summ, "매수 환율") if last_summ else None; fx_note = "환율 추정"

init_rows = [r for r in pos_rows if kind(r) == "초기"]
round_start = min(g(r, "date:매수일:start") for r in init_rows)
end_date = g(init_rows[0], "date:종료 예정:start")

updates, creates, events, changes = {}, [], [], []
def upd(r, **props):
    pid = r["url"].rstrip("/").split("/")[-1].split("?")[0][-32:]
    updates.setdefault(pid, {}).update(props)
    for k, v in props.items(): r[k] = v
def P(**kw): return kw
def pid_of(r): return r["url"].rstrip("/").split("/")[-1].split("?")[0][-32:]   # 재원 칸에는 URL 대신 page id(32자)를 씀

# ---------- 0. 회차 시작 매수가 채움(초기·교체 공통: 매수가 빈 보유 행) ----------
sold_today = {}   # source url -> proceeds
# 0a. 매도 대기 체결 (매도일 시가)
for r in [x for x in active if g(x, "구분") == "매도 대기"]:
    sd = g(r, "date:매도일:start"); t = g(r, "티커")
    op = px(t, sd, "open") if sd else None
    if op is None:
        events.append(f"{t} 매도 대기(데이터 없음, {sd})"); continue
    reason = g(r, "정리 사유") or ""
    q = float(g(r, "수량")); inv = float(g(r, "투입(USD)"))
    if reason.startswith("부분익절"):
        qs = r6(q / 2); qk = r6(q - qs); inv_s = round(inv * qs / q, 2)
        proceeds = qs * op
        creates.append(P(**{"기록": f"{cur_round}회차 {t} 부분익절", "구분": "정리", "회차": cur_round, "티커": t,
            "섹터": g(r, "섹터"), "편입": kind(r), **dprop("매수일", g(r, "date:매수일:start")),
            "매수가(USD)": g(r, "매수가(USD)"), "수량": qs, "투입(USD)": inv_s, "투입(원)": round((g(r, "투입(원)") or 0) * qs / q),
            "매수 환율": g(r, "매수 환율"), "SPY 매수가": g(r, "SPY 매수가"), **dprop("매도일", sd), "매도가(USD)": r2(op),
            "실현(USD)": r2(proceeds), "수익률(%)": r2((op / g(r, "매수가(USD)") - 1) * 100), "정리 사유": reason,
            "손절선(%)": g(r, "손절선(%)"), **dprop("종료 예정", end_date), **dprop("기준일", sd),
            "메모": f"{sd} 시가 {op:.2f}에 절반 매도(가상)"}))
        upd(r, **{"구분": "보유", "수량": qk, "투입(USD)": round(inv - inv_s, 2),
                  "투입(원)": round((g(r, "투입(원)") or 0) - round((g(r, "투입(원)") or 0) * qs / q)),
                  "부분익절": "__YES__", "정리 사유": None, "date:매도일:start": None})
        sold_today[pid_of(r)] = sold_today.get(pid_of(r), 0) + proceeds
        changes.append(f"{t} 절반 매도 체결 {op:.2f}")
    else:
        proceeds = q * op
        upd(r, **{"구분": "정리", "매도가(USD)": r2(op), "실현(USD)": r2(proceeds), "현재가(USD)": r2(op),
                  "평가(USD)": r2(proceeds), "평가(원)": round(proceeds * fx) if fx else None,
                  "수익률(%)": r2((op / g(r, "매수가(USD)") - 1) * 100), **dprop("기준일", sd)})
        sold_today[pid_of(r)] = sold_today.get(pid_of(r), 0) + proceeds
        changes.append(f"{t} 매도 체결 {op:.2f}({reason.split(':')[0]})")

# 0b. 매수가 빈 보유 행 채움
for r in [x for x in pos_rows if g(x, "구분") == "보유" and g(x, "매수가(USD)") is None]:
    t = g(r, "티커"); bd = g(r, "date:매수일:start")
    op = px(t, bd, "open"); spy_op = px("SPY", bd, "open")
    if op is None or (kind(r) == "초기" and spy_op is None):
        events.append(f"{t} 매수 대기({bd})"); continue
    inv = g(r, "투입(USD)")
    if inv is None:   # 교체/대기 매수: 재원 체결 금액
        srcs = [s.rstrip("/").split("/")[-1].split("?")[0][-32:] for s in (g(r, "재원") or "").replace("-", "").split() if s]
        inv = round(sum(sold_today.get(s, 0) for s in srcs), 2)
        if inv <= 0:
            events.append(f"{t} 매수 대기(재원 미체결)"); continue
    q = r6(inv / op)
    stop = min(STOP_MAX, max(STOP_MIN, STOP_K * (atr_pct(t, prev_tday(bd)) or 4.0))) if t != "SPY" else None
    props = {"매수가(USD)": r2(op), "수량": q, "투입(USD)": round(inv, 2), "최고 종가": r2(op)}
    if kind(r) == "초기":
        props["SPY 매수가"] = r2(spy_op)
    if stop: props["손절선(%)"] = r2(stop)
    rate = fx
    props["매수 환율"] = rate
    if fx_date != bd: props["메모"] = (g(r, "메모") or "") + f" · 매수 환율은 {fx_date} 값(매수일 값 없음)"
    if g(r, "투입(원)") is None and rate: props["투입(원)"] = round(inv * rate)
    upd(r, **props)
    changes.append(f"{t} 매수가 채움 {op:.2f}")

# ---------- 1. 보유 평가 ----------
held = [r for r in pos_rows if g(r, "구분") in ("보유", "매도 대기") and g(r, "매수가(USD)") is not None]
if not held:
    out = {"updates": [{"page_id": k, "properties": v} for k, v in updates.items()], "creates": creates,
           "message": f"[가상 실험] REV {cur_round}회차 매수 대기 · 매수일 시가 데이터가 아직 없습니다(as_of {as_of[5:].replace('-', '/')})\n가상 실험이며 투자 권유가 아닙니다",
           "summary": f"REV {cur_round}회차 매수 대기", "checks": {}}
    json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1); print(out["message"]); raise SystemExit(0)
spy_buy = g(init_rows[0], "SPY 매수가")
spy_c = px("SPY", D)
spy_ret = (spy_c / spy_buy - 1) * 100 if spy_buy else None
day_n = tdays_between(round_start, D)
days_left = HOLD_DAYS - day_n

def row_view(r):
    t = g(r, "티커"); c, cd = last_close(t, D)
    bp = float(g(r, "매수가(USD)")); q = float(g(r, "수량"))
    bd = g(r, "date:매수일:start")
    hist = oh[(oh.ticker == t) & (oh.date >= bd) & (oh.date <= D)]
    peak = float(hist.close.max()) if not hist.empty else c
    pc = px(t, prev_tday(D)) if prev_tday(D) else None
    return dict(t=t, c=c, cd=cd, bp=bp, q=q, v=q * c, ret=(c / bp - 1) * 100, peak=peak,
                peak_ret=(peak / bp - 1) * 100, d1=((c / pc - 1) * 100) if pc else None, bd=bd)

views = {}
for r in held:
    v = row_view(r); views[r["url"]] = v
    s = sc_by.loc[v["t"]] if v["t"] in sc_by.index else None
    props = {**dprop("기준일", D), "현재가(USD)": r2(v["c"]), "평가(USD)": r2(v["v"]),
             "평가(원)": round(v["v"] * fx) if fx else None, "수익률(%)": r2(v["ret"]),
             "SPY 수익률(%)": r2(spy_ret), "SPY 대비(%p)": r2(v["ret"] - spy_ret),
             "보유 거래일": tdays_between(v["bd"], D), "최고 종가": r2(v["peak"])}
    if s is not None and v["t"] != "SPY":
        props["30일 추정치 변화(%)"] = r2(s.get("eps_1y_chg_30d_pct"))
    if g(r, "편입") is None: props["편입"] = kind(r)
    if g(r, "손절선(%)") is None and v["t"] != "SPY":
        props["손절선(%)"] = r2(min(STOP_MAX, max(STOP_MIN, STOP_K * (atr_pct(v["t"], prev_tday(v["bd"])) or 4.0))))
    if v["cd"] != D: props["메모"] = (g(r, "메모") or "") + f" · {D} 데이터 없음, {v['cd']} 종가 사용"
    upd(r, **props)

# ---------- 2. 정리 규칙 판단 (종가 판단 → 다음 거래일 시가 실행) ----------
def headroom(t, c):
    """더 오를 여지 점수 0~5"""
    if t not in sc_by.index: return 0, []
    s = sc_by.loc[t]; pts, why = 0, []
    if nz(s.eps_1y_chg_30d_pct) and s.eps_1y_chg_30d_pct > 0: pts += 1; why.append("추정치↑")
    if nz(s.rev_1y_up30) and nz(s.rev_1y_down30) and s.rev_1y_up30 > s.rev_1y_down30: pts += 1; why.append("상향>하향")
    tu = (s.target_mean / c - 1) * 100 if nz(s.target_mean) else None
    if tu is not None and tu >= 10: pts += 1; why.append(f"목표가+{tu:.0f}%")
    if nz(s.sma20) and c > s.sma20: pts += 1; why.append("20일선 위")
    if nz(s.rsi14) and s.rsi14 < 75: pts += 1; why.append("과열 아님")
    return pts, why

exec_day = next_tdays(D, 1)[0]
is_end = day_n >= HOLD_DAYS
exits = []   # (row, reason, partial)
if not is_end:
    for r in held:
        v = views[r["url"]]; t = v["t"]
        if t == "SPY" or g(r, "구분") != "보유": continue
        s = sc_by.loc[t] if t in sc_by.index else None
        R = float(g(r, "손절선(%)") or 10)
        hs, hwhy = headroom(t, v["c"])
        upd(r, **{"여지 점수": hs})
        atr_now = (s.atr20_pct if s is not None and nz(s.atr20_pct) else atr_pct(t, D)) or 4.0
        tu = ((s.target_mean / v["c"] - 1) * 100) if s is not None and nz(s.target_mean) else None
        ne = s.next_earnings if s is not None and nz(s.next_earnings) else None
        soon = set(next_tdays(D, 2))
        reason = None; partial = False
        # A. 손절
        if v["ret"] <= -R:
            reason = f"손절: {v['ret']:.1f}% ≤ -{R:.1f}%(2.5×ATR)"
        # B. 근거 소멸
        elif s is not None and nz(s.eps_1y_chg_30d_pct) and s.eps_1y_chg_30d_pct < 0 and (
                s.eps_1y_chg_30d_pct <= -2 or (nz(s.rev_1y_down30) and s.rev_1y_down30 >= (s.rev_1y_up30 or 0))):
            reason = f"근거 소멸: 30일 추정치 {s.eps_1y_chg_30d_pct:.1f}%, 상향 {int(s.rev_1y_up30 or 0)}/하향 {int(s.rev_1y_down30 or 0)}"
        # C. 실적 전 위험 축소
        elif ne in soon and v["ret"] <= -0.5 * R and (s.beats_4q if nz(s.beats_4q) else 0) <= 2:
            reason = f"실적 전 위험 축소: {ne} 실적, {v['ret']:.1f}%·최근 4분기 상회 {int(s.beats_4q or 0)}회"
        # D. 추적 손절(+1R 도달 후)
        elif v["peak_ret"] >= R:
            k = 3.0 if hs >= 4 else (2.5 if hs == 3 else 2.0)
            level = max(v["bp"], v["peak"] * (1 - k * atr_now / 100))
            if v["c"] <= level:
                reason = f"추적 손절: 최고 {v['peak']:.2f}에서 {k}×ATR 하락(여지 {hs}/5)"
        # E. 절반 익절(1회)
        if reason is None and g(r, "부분익절") not in ("__YES__", True, 1):
            if v["ret"] >= 2 * R and hs <= 2:
                reason, partial = f"부분익절(50%): +{v['ret']:.1f}% ≥ 2R, 여지 {hs}/5", True
            elif v["ret"] >= R and tu is not None and tu <= 0:
                reason, partial = f"부분익절(50%): 목표가 도달(+{v['ret']:.1f}%)", True
            elif ne in soon and v["ret"] >= R:
                reason, partial = f"부분익절(50%): {ne} 실적 전 이익 확정(+{v['ret']:.1f}%)", True
        if reason:
            exits.append((r, reason, partial))
            upd(r, **{"구분": "매도 대기", "정리 사유": reason, **dprop("매도일", exec_day)})
            changes.append(f"{t} → {exec_day} 시가 {'절반 ' if partial else ''}매도 예정 ({reason.split(':')[0]})")
        if ne and ne in set(next_tdays(D, 5)):
            events.append(f"{t} 실적 {ne}")

# ---------- 3. 리밸런싱 후보 ----------
def universe(base_date):
    f = pd.read_parquet(f"{STORE}/fundamentals.parquet", columns=["ticker", "eps_est_1y", "quote_type"])
    u = sc.merge(f, on="ticker", how="left")
    ff = pd.to_datetime(u.fund_fetched, errors="coerce")
    u = u[(u.mcap_b >= 10) & (u.avg_dollar_vol_20d >= 2e7) & (u.n_analysts >= 5) & (u.eps_est_1y.abs() >= 0.5)
          & (ff >= pd.Timestamp(base_date) - pd.Timedelta(days=21)) & (u.quote_type != "ETF")
          & (u.stale != True) & u.eps_1y_chg_90d_pct.notna()].copy()
    u["sec"] = u.sector.map(sec)
    return u

def pick_replacements(n, held_rows, exclude):
    if n <= 0 or days_left < MIN_DAYS_LEFT:
        return [], "남은 보유일 5일 미만"
    u = universe(D)
    cut = u.eps_1y_chg_90d_pct.quantile(0.9)
    top = u[(u.eps_1y_chg_90d_pct >= cut) & (u.eps_1y_chg_30d_pct > 0)].copy()
    nxt5 = set(next_tdays(D, 6))
    gate = top[(top.rev_1y_up30 >= 3) & (top.rev_1y_up30 >= 2 * top.rev_1y_down30.fillna(0))
               & (top.eps_0y_chg_30d_pct.fillna(0) >= 0)
               & (top.close > top.sma50) & (top.sma20_rising == True)
               & (top.rsi14 <= 70) & (top.vs_sma20_pct <= 2 * top.atr20_pct)
               & (top.target_upside_pct >= 10)
               & (~top.next_earnings.fillna("").isin(nxt5))
               ].copy()
    n_pass = len(gate); gate = gate[~gate.ticker.isin(exclude)].copy()
    if n_pass and gate.empty: return [], f"품질 통과 {n_pass}종목이 모두 이번 회차 보유·정리 종목"
    if gate.empty: return [], f"REV 상위 {len(top)}종목 중 품질 조건 통과 없음"
    gate["q"] = (0.5 * gate.eps_1y_chg_90d_pct.rank(pct=True) + 0.3 * gate.eps_1y_chg_30d_pct.rank(pct=True)
                 + 0.2 * gate.rs63_vs_spy_pct.rank(pct=True))
    gate = gate.sort_values("q", ascending=False)
    cnt = {}
    for r in held_rows:
        if g(r, "티커") != "SPY": cnt[sec(g(r, "섹터"))] = cnt.get(sec(g(r, "섹터")), 0) + 1
    out = []
    for _, c in gate.iterrows():
        if cnt.get(c.sec, 0) >= SECTOR_CAP: continue
        out.append(c); cnt[c.sec] = cnt.get(c.sec, 0) + 1
        if len(out) >= n: break
    if not out: return out, f"품질 통과 {len(gate)}종목 모두 섹터 한도(3개)에 걸림"
    return out, (None if len(out) >= n else "품질 통과 후보 부족")

if not is_end:
    remaining = [r for r in held if g(r, "구분") == "보유" or (g(r, "구분") == "매도 대기" and
                 any(e[0] is r and e[2] for e in exits))]
    exclude = {g(r, "티커") for r in pos_rows}
    slots = [(e[0], e[2]) for e in exits]
    spy_wait = [r for r in held if g(r, "티커") == "SPY" and g(r, "구분") == "보유"]
    n_want = min(MAX_NEW_BUYS, len(slots) + len(spy_wait))
    picks, why_none = pick_replacements(n_want, remaining, exclude) if (slots or spy_wait) else ([], None)
    pi = 0
    for (src, partial) in slots:
        t_src = g(src, "티커")
        if pi < len(picks):
            c = picks[pi]; pi += 1
            creates.append(P(**{"기록": f"{cur_round}회차 {c.ticker}(교체)", "구분": "보유", "편입": "교체", "회차": cur_round,
                "티커": c.ticker, "섹터": c.sector, **dprop("신호일", D), **dprop("매수일", exec_day),
                **dprop("종료 예정", end_date), "재원": pid_of(src), "90일 추정치 변화(%)": r2(c.eps_1y_chg_90d_pct),
                "30일 추정치 변화(%)": r2(c.eps_1y_chg_30d_pct),
                "메모": f"{t_src} {'절반 ' if partial else ''}매도 대금으로 교체 매수. 품질점수 {c.q:.2f}, 목표가 여지 +{c.target_upside_pct:.0f}%, RSI {c.rsi14:.0f}"}))
            changes.append(f"교체 매수 예정 {c.ticker}({sec(c.sector)}, 90일 +{c.eps_1y_chg_90d_pct:.0f}%) ← {t_src}")
        else:
            creates.append(P(**{"기록": f"{cur_round}회차 SPY 대기(← {t_src})", "구분": "보유", "편입": "SPY 대기", "회차": cur_round,
                "티커": "SPY", "섹터": "ETF", **dprop("신호일", D), **dprop("매수일", exec_day), **dprop("종료 예정", end_date),
                "재원": pid_of(src), "메모": f"{t_src} 매도 대금 대기: {why_none or '하루 교체 2종목 한도'}"}))
            changes.append(f"{t_src} 대금 → SPY 대기({why_none or '하루 교체 2종목 한도'})")
    for w in spy_wait:
        if pi < len(picks):
            c = picks[pi]; pi += 1
            upd(w, **{"구분": "매도 대기", "정리 사유": "교체 매수 재원(SPY 대기 해제)", **dprop("매도일", exec_day)})
            creates.append(P(**{"기록": f"{cur_round}회차 {c.ticker}(교체)", "구분": "보유", "편입": "교체", "회차": cur_round,
                "티커": c.ticker, "섹터": c.sector, **dprop("신호일", D), **dprop("매수일", exec_day),
                **dprop("종료 예정", end_date), "재원": pid_of(w), "90일 추정치 변화(%)": r2(c.eps_1y_chg_90d_pct),
                "30일 추정치 변화(%)": r2(c.eps_1y_chg_30d_pct),
                "메모": f"SPY 대기 자금으로 교체 매수. 품질점수 {c.q:.2f}, 목표가 여지 +{c.target_upside_pct:.0f}%"}))
            changes.append(f"SPY 대기 → {c.ticker} 교체 예정")

# ---------- 4. 합계·원안(A) ----------
held_now = [r for r in pos_rows if g(r, "구분") in ("보유", "매도 대기") and g(r, "매수가(USD)") is not None]
B_val = sum(views[r["url"]]["v"] for r in held_now if r["url"] in views)
pending_cash = sum(v for k, v in sold_today.items()
                   if not any(g(x, "재원") and k in g(x, "재원").replace("-", "") and g(x, "매수가(USD)") is not None for x in pos_rows))
B_val += pending_cash
cap_B = round(sum(float(g(r, "투입(USD)") or 0) for r in init_rows), 2)
cap_B = float(g(last_summ, "투입(USD)")) if last_summ and int(g(last_summ, "회차") or 0) == cur_round else cap_B
cap_B_krw = float(g(last_summ, "투입(원)")) if last_summ and int(g(last_summ, "회차") or 0) == cur_round else \
    sum(float(g(r, "투입(원)") or 0) for r in init_rows)
B_ret = (B_val / cap_B - 1) * 100
if last_summ and int(g(last_summ, "회차") or 0) == cur_round and g(last_summ, "원안 투입(USD)"):
    cap_A = float(g(last_summ, "원안 투입(USD)"))
elif last_summ and int(g(last_summ, "회차") or 0) == cur_round - 1 and g(last_summ, "원안 평가(USD)"):
    cap_A = float(g(last_summ, "원안 평가(USD)"))
else:
    cap_A = cap_B
orig = {}
for r in init_rows:
    if g(r, "매수가(USD)") is not None: orig[g(r, "티커")] = float(g(r, "매수가(USD)"))
A_val = sum(cap_A / len(orig) * (last_close(t, D)[0] / bp) for t, bp in orig.items())
A_ret = (A_val / cap_A - 1) * 100

# ---------- 5. 회차 종료 ----------
end_msg = []
if is_end:
    wins = 0
    for r in held_now:
        v = views[r["url"]]
        upd(r, **{"구분": "종료", **dprop("매도일", D), "매도가(USD)": r2(v["c"]), "실현(USD)": r2(v["v"]),
                  "정리 사유": "회차 종료(20거래일 종가)"})
    for t, bp in orig.items():
        if (last_close(t, D)[0] / bp - 1) * 100 > spy_ret: wins += 1
    end_msg.append(f"🏁 {cur_round}회차 종료: 규칙 바구니 {B_ret:+.2f}% · 원안 {A_ret:+.2f}% · SPY {spy_ret:+.2f}% · 원안 종목 중 SPY 이긴 수 {wins}/10")
    # 다음 회차 선정(원안 규칙 그대로)
    u = universe(D)
    cut = u.eps_1y_chg_90d_pct.quantile(0.9)
    top = u[(u.eps_1y_chg_90d_pct >= cut) & (u.eps_1y_chg_30d_pct > 0)].sort_values("eps_1y_chg_90d_pct", ascending=False)
    cnt, picks = {}, []
    for _, c in top.iterrows():
        if cnt.get(c.sec, 0) >= SECTOR_CAP: continue
        picks.append(c); cnt[c.sec] = cnt.get(c.sec, 0) + 1
        if len(picks) == 10: break
    nb = next_tdays(D, 1)[0]; ne_ = next_tdays(D, HOLD_DAYS)[-1]
    per = round(B_val / max(1, len(picks)), 2)
    if len(picks) < 10: end_msg.append(f"⚠️ 다음 회차 조건 통과 {len(picks)}종목뿐 — 있는 종목으로 나눠 담음")
    for c in picks:
        creates.append(P(**{"기록": f"{cur_round+1}회차 {c.ticker}", "구분": "보유", "편입": "초기", "회차": cur_round + 1,
            "티커": c.ticker, "섹터": c.sector, **dprop("신호일", D), **dprop("매수일", nb), **dprop("종료 예정", ne_),
            "투입(USD)": per, "투입(원)": round(per * fx) if fx else None,
            "90일 추정치 변화(%)": r2(c.eps_1y_chg_90d_pct), "30일 추정치 변화(%)": r2(c.eps_1y_chg_30d_pct),
            "메모": f"신호 {D}, {nb} 시가 매수 예정"}))
    end_msg.append(f"다음 {cur_round+1}회차({nb} 시가 매수, 종목당 ${per}): " +
                   ", ".join(f"{c.ticker}(+{c.eps_1y_chg_90d_pct:.0f}%)" for c in picks))

summary_props = {"기록": f"{cur_round}회차 합계 {D}", "구분": "일일 합계", "회차": cur_round, **dprop("기준일", D),
    **dprop("매수일", round_start), **dprop("종료 예정", end_date),
    "투입(USD)": cap_B, "투입(원)": round(cap_B_krw), "평가(USD)": r2(B_val), "평가(원)": round(B_val * fx) if fx else None,
    "수익률(%)": r2(B_ret), "SPY 수익률(%)": r2(spy_ret), "SPY 대비(%p)": r2(B_ret - spy_ret),
    "원안 투입(USD)": round(cap_A, 2), "원안 평가(USD)": r2(A_val), "원안 수익률(%)": r2(A_ret), "원안 대비(%p)": r2(B_ret - A_ret),
    "보유 거래일": day_n, "매수 환율": fx, "현재가(USD)": r2(spy_c), "SPY 매수가": spy_buy}
krw_ret = ((B_val * fx) / cap_B_krw - 1) * 100 if fx else None
movers = sorted([views[r["url"]] for r in held_now if r["url"] in views and views[r["url"]]["d1"] is not None and views[r["url"]]["t"] != "SPY"],
                key=lambda v: v["d1"])
memo = [f"규칙 {VERSION}. 원안(그냥 보유) {A_ret:+.2f}%. 원화 수익률 {krw_ret:+.2f}%(환율 {fx:,.2f}, 환율 요인 약 {krw_ret - B_ret:+.2f}%p)" if fx else "환율 없음"]
if movers: memo.append(f"오늘 최고 {movers[-1]['t']} {movers[-1]['d1']:+.2f}%, 최저 {movers[0]['t']} {movers[0]['d1']:+.2f}%")
if changes: memo.append("변경: " + "; ".join(changes))
memo.append(f"수수료·배당 무시. 매수 환율 칸=오늘 환율, 현재가·SPY 매수가 칸=SPY. 데이터 as_of {as_of[5:].replace('-', '/')}")
if fx_note: memo.append(fx_note)
summary_props["메모"] = ". ".join(memo)
if last_summ and g(last_summ, "date:기준일:start") == D:
    upd(last_summ, **summary_props)
else:
    creates.append(summary_props)

# ---------- 6. 알림 ----------
mmdd = lambda d: d[5:].replace("-", "/")
L = []
if as_of < D or spy_dates[-1] < D: L.append(f"데이터 미갱신(as_of {mmdd(as_of)})")
krw_val = round(B_val * fx) if fx else 0
L.append(f"[가상 실험] REV {cur_round}회차 {day_n}일째 · 규칙 바구니 {B_ret:+.2f}% vs 원안(그냥 보유) {A_ret:+.2f}% vs SPY {spy_ret:+.2f}% (SPY 대비 {B_ret - spy_ret:+.2f}%p, 달러 기준) · 평가 {krw_val:,}원")
lead = "앞서지만" if B_ret > spy_ret else "뒤지지만"
rule_eff = ("규칙 바구니와 그냥 보유는 아직 같음" if abs(B_ret - A_ret) < 0.005 else
            "규칙이 그냥 보유보다 " + (f"{B_ret - A_ret:+.2f}%p 나음" if B_ret > A_ret else f"{B_ret - A_ret:+.2f}%p 못함"))
L.append(f"REV(애널리스트 EPS 추정치가 최근 크게 오른 종목을 사는 방식)가 SPY 대비(같은 날 같은 돈으로 SPY를 샀을 때와의 차이) {lead}, {day_n}/20일·표본 1개라 판단은 이릅니다. {rule_eff}.")
lines = []
for r in sorted(held_now, key=lambda r: -views[r["url"]]["ret"] if r["url"] in views else 0):
    if r["url"] not in views: continue
    v = views[r["url"]]; tag = ""
    if g(r, "구분") == "매도 대기": tag = " ⏳매도 예정"
    if kind(r) == "교체": tag += " (교체)"
    if kind(r) == "SPY 대기": tag += " (대기 자금)"
    R = g(r, "손절선(%)")
    lines.append(f"{v['t']} {v['ret']:+.1f}% (SPY 대비 {v['ret'] - spy_ret:+.1f}%p{f', 손절 -{R:.1f}%' if R else ''}){tag}")
L += lines
closed = [r for r in pos_rows if g(r, "구분") == "정리" and g(r, "티커") != "SPY"]
if closed:
    L.append("정리 완료: " + ", ".join(f"{g(r, '티커')} {g(r, '수익률(%)'):+.1f}%({(g(r, '정리 사유') or '').split(':')[0]})" for r in closed if g(r, "수익률(%)") is not None))
L.append("오늘 바뀐 것: " + ("; ".join(changes) if changes else "규칙 발동 없음"))
big = [f"{v['t']} {v['d1']:+.1f}%" for v in movers if abs(v["d1"]) >= 5]
ev = (["큰 변동 " + ", ".join(big)] if big else []) + events
if ev: L.append(" · ".join(ev))
L += end_msg
L.append(f"가상 실험이며 투자 권유가 아닙니다 · 데이터 Yahoo Finance via GitHub(as_of {mmdd(as_of)})")

checks = {"B_val": round(B_val, 4), "sum_views": round(sum(views[r['url']]['v'] for r in held_now if r['url'] in views), 4),
          "pending_cash": round(pending_cash, 4), "cap_B": cap_B, "cap_A": cap_A, "A_val": round(A_val, 4),
          "n_orig": len(orig), "day_n": day_n, "D": D, "spy_ret": r2(spy_ret)}
out = {"updates": [{"page_id": k, "properties": v} for k, v in updates.items()], "creates": creates,
       "message": "\n".join(L), "summary": f"REV {cur_round}회차 {day_n}일째 바구니 {B_ret:+.2f}% / 원안 {A_ret:+.2f}% / SPY {spy_ret:+.2f}%",
       "checks": checks}
json.dump(out, open(OUT, "w"), ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
print(out["message"]); print(json.dumps(checks, ensure_ascii=False))
print(f"updates={len(out['updates'])} creates={len(creates)}")