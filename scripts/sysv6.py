"""sysv6.py — 체계적 매매 엔진 v6 (백테스트·모의매매·향후 자동매매가 같은 코드를 쓴다)
규칙(2026-11-13, 2023-11~2026-11 시험 근거 — 장기 자료로 재확인 예정)
  선정: 사전 지정 6요인(12-1개월 모멘텀·200일선 대비·52주 고점 근접·연간 매출 성장 가속·연간 EPS 성장·이익수익률 E/P)을
        섹터 안 백분위로 바꿔 합산 → 유동 종목(20일 평균 거래대금 2천만 달러↑, 5달러↑) 중 상위 10%
        + 종가 > 20일선 & 20일선 5일 전보다 상승 + 섹터 ETF 20일 수익 − SPY ≤ 3%p(추격 금지)
  비중: 종목당 계좌의 3.3%(최대 30종목), 섹터당 30% 상한, 과열 주의(시장 폭 상위 20% 또는 200일선 위 70%↑)면 1.65%
  신규 금지: 200일선 위 종목 < 40%(약세장) / 계좌 고점 대비 −10%면 21거래일 쉼(뒤 고점 재설정)
  −15%면 수익률 낮은 절반 정리
  청산: 종가 ≥ 3개월 목표(진입 × (1 + 0.5 × 20일 변동성 × √63)) / 종가 ≤ 재난 손절(진입 × (1 − 5×ATR, 15~35%)) / 63거래일 경과
  체결: 신호 다음 거래일 시가, 비용 편도 0.15%
"""
import numpy as np, pandas as pd

PRIOR = ["mom_12_1", "trend200", "hi52", "rev_accel", "eps_g", "ep"]
C0 = dict(top=0.9, wmax=0.033, maxpos=30, secmax=0.30, dd1=0.10, dd2=0.15, pause=21, cost=0.0015, hold=63,
          sdm=5.0, sdlo=0.15, sdhi=0.35, chase=0.03, b200_min=0.40, hot_b200=0.70, hot_breadth=0.8, hot_mult=0.5)


def features(ohlcv: pd.DataFrame, fin: pd.DataFrame, screen: pd.DataFrame):
    o = ohlcv.copy(); o["date"] = pd.to_datetime(o["date"])
    pv = lambda c: o.pivot(index="date", columns="ticker", values=c).sort_index()
    CL, OP, HI, LO, VO = pv("close"), pv("open"), pv("high"), pv("low"), pv("volume")
    keep = CL.notna().mean(axis=1) > 0.5
    CL, OP, HI, LO, VO = CL[keep], OP[keep], HI[keep], LO[keep], VO[keep]
    sc = screen.set_index("ticker")
    core = set(sc.index[sc["sources"] == "core"]) | {"SPY"}
    stocks = [t for t in CL.columns if t in sc.index and t not in core and pd.notna(sc.loc[t, "sector_etf"])]
    P = CL[stocks].ffill(limit=3); spy = CL["SPY"].ffill()
    r = P.pct_change(fill_method=None); dv = (P * VO[stocks]).rolling(20).mean()
    liq = (dv >= 20e6) & (P >= 5) & (P.notna().rolling(126).sum() >= 120)
    F = {}
    F["mom_12_1"] = P.shift(21) / P.shift(252) - 1
    F["trend200"] = P / P.rolling(200).mean() - 1
    F["hi52"] = P / P.rolling(252, min_periods=200).max()
    fa = fin[fin["freq"] == "a"].copy(); fa["period_end"] = pd.to_datetime(fa["period_end"])
    fa = fa.sort_values(["ticker", "period_end"]); fa["known"] = fa["period_end"] + pd.Timedelta(days=90)
    g = fa.groupby("ticker")
    fa["rev_g"] = g["revenue"].pct_change(fill_method=None)
    fa["eps_g"] = (fa["diluted_eps"] - g["diluted_eps"].shift()) / g["diluted_eps"].shift().abs()
    fa["rev_accel"] = fa["rev_g"] - fa.groupby("ticker")["rev_g"].shift()

    def asof(col):
        out = pd.DataFrame(np.nan, index=P.index, columns=stocks)
        for t, d in fa[fa["ticker"].isin(stocks)].groupby("ticker"):
            s = d.set_index("known")[col].dropna(); s = s[~s.index.duplicated(keep="last")]
            if len(s): out[t] = s.reindex(P.index.union(s.index)).ffill().reindex(P.index).values
        return out
    F["ep"] = asof("diluted_eps") / P
    F["eps_g"] = asof("eps_g").clip(-3, 3)
    F["rev_accel"] = asof("rev_accel").clip(-2, 2)
    sec = sc.loc[stocks, "sector_etf"]
    XR = {k: F[k].where(liq & np.isfinite(F[k])).T.groupby(sec).rank(pct=True).T for k in PRIOR}
    comp = sum((XR[k] - 0.5).fillna(0) for k in PRIOR).where(liq)
    crank = comp.rank(axis=1, pct=True)
    ma20 = P.rolling(20).mean(); up20 = ma20 > ma20.shift(5)
    sig = r.rolling(20).std()
    H, L = HI[stocks], LO[stocks]
    tr = np.maximum(H - L, np.maximum((H - P.shift()).abs(), (L - P.shift()).abs())); atr = tr.rolling(20).mean() / P
    e20 = CL.pct_change(20, fill_method=None)
    ex20 = pd.DataFrame({t: (e20[sec[t]] - e20["SPY"]) if sec[t] in e20 else np.nan for t in stocks})
    b200 = ((P > P.rolling(200).mean()) & liq).sum(axis=1) / liq.sum(axis=1)
    ewi = (1 + r.where(liq).mean(axis=1).fillna(0)).cumprod()
    br = (ewi / spy).pct_change(20)
    bp = br.rolling(400, min_periods=60).apply(lambda s: (s <= s[-1]).mean(), raw=True)
    return dict(P=P, O=OP[stocks].reindex(P.index), spy=spy, liq=liq, sec=sec, crank=crank, ma20=ma20, up20=up20,
                sig=sig, atr=atr, ex20=ex20, b200=b200, bp=bp, XR=XR, dates=P.index, cols=P.columns)


def new_state(cash=100000.0, start=None):
    return dict(cash=cash, init=cash, pos={}, pending=[], peak=cash, pause_until=None, equity=[], trades=[],
                start=start, last_entry_week=None, ver="v6")


def _px(X, d, t):
    try:
        v = X.at[d, t]; return None if pd.isna(v) else float(v)
    except KeyError:
        return None


def step(S, X, i, C=None, entry_today=None):
    """X 특성, i = 오늘(신호일) 인덱스. 1) 어제 낸 주문을 오늘 시가로 체결 2) 오늘 종가로 평가 3) 내일 시가 주문 생성."""
    C = {**C0, **(C or {})}; d = X["dates"][i]; ds = str(d.date()); fills = []
    # 1) 체결
    for od in S["pending"]:
        t = od["t"]; o = _px(X["O"], d, t) or _px(X["P"], d, t)
        if o is None: continue
        if od["side"] == "sell" and t in S["pos"]:
            p = S["pos"].pop(t); amt = p["sh"] * o * (1 - C["cost"]); S["cash"] += amt
            S["trades"].append(dict(t=t, d0=p["d0"], d1=ds, e=p["e"], x=round(o, 4), ret=round(amt / p["cost_amt"] - 1, 4), spy=round(float(X["spy"].loc[d]) / p.get("spy0", float(X["spy"].loc[d])) - 1, 4), why=od["why"]))
            fills.append(dict(side="sell", t=t, px=round(o, 2), sh=round(p["sh"], 4), why=od["why"]))
        elif od["side"] == "buy" and t not in S["pos"]:
            amt = min(od["amt"], S["cash"])
            if amt <= 0: continue
            sh = amt / (o * (1 + C["cost"])); S["cash"] -= amt
            S["pos"][t] = dict(sh=sh, e=o, d0=ds, cost_amt=amt, last=o, sec=od["sec"],
                               stop=o * (1 - od["sd"]), t3=o * (1 + od["up"]), sd=od["sd"], spy0=float(X["spy"].loc[d]))
            fills.append(dict(side="buy", t=t, px=round(o, 2), sh=round(sh, 4), amt=round(amt, 2)))
    S["pending"] = []
    # 2) 평가
    for t, p in S["pos"].items():
        c = _px(X["P"], d, t)
        if c is not None: p["last"] = c
    val = S["cash"] + sum(p["sh"] * p["last"] for p in S["pos"].values())
    if S["pause_until"] is not None and ds >= S["pause_until"] and not S.get("reset_done") == S["pause_until"]:
        S["peak"] = val; S["reset_done"] = S["pause_until"]  # 쉼이 끝나면 기준 고점 재설정
    S["peak"] = max(S["peak"], val); dd = val / S["peak"] - 1
    S["equity"].append([ds, round(val, 2), round(float(X["spy"].iloc[i]), 4)])
    orders = []
    # 3-1) 청산 신호
    sells = []
    for t, p in S["pos"].items():
        age = i - int(X["dates"].searchsorted(pd.Timestamp(p["d0"])))  # 거래일 수
        why = "손절" if p["last"] <= p["stop"] else "3개월 목표" if p["last"] >= p["t3"] else "기한" if age >= C["hold"] else None
        if why: sells.append((t, why))
    paused = S["pause_until"] is not None and ds < S["pause_until"]
    if dd <= -C["dd2"]:
        rest = sorted([t for t in S["pos"] if t not in dict(sells)], key=lambda t: S["pos"][t]["last"] / S["pos"][t]["e"])
        sells += [(t, "계좌 −15% 정리") for t in rest[:len(rest) // 2]]
        S["peak"] = val; S["pause_until"] = str(X["dates"][min(i + C["pause"], len(X["dates"]) - 1)].date()) if i + C["pause"] < len(X["dates"]) else _add_bd(d, C["pause"])
        paused = True
    elif dd <= -C["dd1"] and not paused:
        S["pause_until"] = str(X["dates"][i + C["pause"]].date()) if i + C["pause"] < len(X["dates"]) else _add_bd(d, C["pause"])
        paused = True
    for t, why in sells:
        orders.append(dict(side="sell", t=t, why=why))
    # 3-2) 신규(주 1회)
    status = dict(dd=round(dd * 100, 2), b200=round(float(X["b200"].iloc[i]) * 100, 1), breadth_pct=None if pd.isna(X["bp"].iloc[i]) else round(float(X["bp"].iloc[i]) * 100))
    wk = f"{d.isocalendar()[0]}-{d.isocalendar()[1]}"
    do_entry = (S["last_entry_week"] != wk) if entry_today is None else entry_today
    gate = None
    if paused: gate = "계좌 낙폭 휴식(%s까지)" % S["pause_until"]
    elif X["b200"].iloc[i] < C["b200_min"]: gate = "약세장(200일선 위 %.0f%%)" % (X["b200"].iloc[i] * 100)
    mult = C["hot_mult"] if (X["b200"].iloc[i] > C["hot_b200"] or (X["bp"].iloc[i] >= C["hot_breadth"])) else 1.0
    status.update(gate=gate, mult=mult, entry_day=bool(do_entry))
    if do_entry and gate is None:
        S["last_entry_week"] = wk
        cr = X["crank"].iloc[i]
        m = (cr >= C["top"]) & (X["P"].iloc[i] > X["ma20"].iloc[i]) & X["up20"].iloc[i] & ~(X["ex20"].iloc[i] > C["chase"])
        m &= ~cr.index.isin(list(S["pos"]))
        cs = cr[m].sort_values(ascending=False)
        secw = {}
        for t, p in S["pos"].items(): secw[p["sec"]] = secw.get(p["sec"], 0) + p["sh"] * p["last"] / val
        cash = S["cash"] + sum(S["pos"][t]["sh"] * S["pos"][t]["last"] for t, _ in sells if t in S["pos"])
        npos = len(S["pos"]) - len(sells)
        for t in cs.index:
            if npos >= C["maxpos"]: break
            j = X["cols"].get_loc(t); a = X["atr"].iat[i, j]; s = X["sig"].iat[i, j]
            if np.isnan(a) or np.isnan(s): continue
            w = C["wmax"] * mult; sc_ = X["sec"][t]
            if secw.get(sc_, 0) + w > C["secmax"]: continue
            amt = min(w * val, cash)
            if amt < 0.005 * val: break
            sd = min(max(C["sdm"] * a, C["sdlo"]), C["sdhi"]); up = 0.5 * s * np.sqrt(63)
            c = float(X["P"].iat[i, j])
            orders.append(dict(side="buy", t=t, amt=round(amt, 2), sec=sc_, sd=round(sd, 4), up=round(up, 4), ref_close=round(c, 2),
                               est_stop=round(c * (1 - sd), 2), est_t3=round(c * (1 + up), 2), factor_pct=round(float(cr[t]) * 100, 1)))
            cash -= amt; npos += 1; secw[sc_] = secw.get(sc_, 0) + w
    S["pending"] = orders
    return dict(date=ds, value=round(val, 2), cash=round(S["cash"], 2), n_pos=len(S["pos"]), fills=fills, orders=orders, status=status)


def _add_bd(d, n):
    return str((pd.Timestamp(d) + pd.tseries.offsets.BDay(n)).date())


def backtest(X, start_i=260, C=None, cash=1.0, off=0):
    S = new_state(cash)
    for i in range(start_i, len(X["dates"])):
        step(S, X, i, C, entry_today=((i + off) % 5 == 0))
    return S
