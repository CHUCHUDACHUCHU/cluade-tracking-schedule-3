# 채점 v3.2 종목 모드 (2026-11-10 사용자 승인 — v3.1에서 '혼합 후보 풀' 반영). 사용법: python3 score_v3_2.py config.json
# v3.1 대비 변경(이것만):
#   - sector_basket "기타"(① 비중확대·전환 대기 섹터 밖에서 고른 상위 종목) 허용 — 예전엔 '없음'이면 제외였음.
#     '기타'는 기대수익 관문을 15%로(성장 조정과 같음) 두고, 비중은 리포터가 절반으로 줄인다.
#   - "없음"은 여전히 제외.
# 나머지(품질 점수 0~100, 매수 후보 관문 품질 60, 남은 기술 조건, 손익비·기대수익·약세·확실성 관문, 정렬)는 v3.1과 같다.
# 입력: {"mode":"stock","asof","bench_csv"(date,SPY,섹터 ETF...),"out_csv", "items":[{
#   "t","kind"(stock|etf),"sector_etf","sector_basket"(추세|성장 조정|성장 조정 대기|기타|없음),"prices_csv"(date,close),
#   "rev_growth","rev_growth_prev"(선택),"guide"(상향|유지|하향),"guide_beat"(선택),"est_up"(-3~3),"margin_trend"(선택 -1|0|1),
#   "mcap_b","adv_musd","entry","stop","scen"{bull:[가격,확률],base:[..],bear:[..]},"cyclical","norm_pe","norm_pe_limit",
#   "earn_days","post_er_ok","src_primary","price_xcheck","est_consistent"}]}
import json, sys
import pandas as pd, numpy as np

def rsi(s, n=14):
    d = s.diff(); u = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100/(1+u/l)

def tech(s):
    s = s.dropna(); c = s.iloc[-1]
    ma20 = s.rolling(20).mean(); ma50 = s.rolling(50).mean()
    e12 = s.ewm(span=12, adjust=False).mean(); e26 = s.ewm(span=26, adjust=False).mean()
    m = e12-e26; h = m - m.ewm(span=9, adjust=False).mean()
    r = rsi(s).iloc[-1]
    up20 = ma20.iloc[-1] > ma20.iloc[-6]
    m50 = ma50.iloc[-1] if len(s) >= 50 else np.nan
    if c > ma20.iloc[-1] and up20 and h.iloc[-1] > 0: timing = "전환 확인"
    elif c < ma20.iloc[-1] and (np.isnan(m50) or ma20.iloc[-1] < m50) and h.iloc[-1] < 0 and r < 45: timing = "하락 지속"
    else: timing = "중립"
    n = min(63, len(s)-1); r3 = s.iloc[-1]/s.iloc[-1-n]-1
    hi = s.tail(252).max()
    d20 = (c/ma20.iloc[-1]-1)*100
    oh = int(r >= 70) + int(c >= 0.97*hi and r3 >= 0.25) + int(d20 >= 8)
    return dict(close=c, rsi=round(r,1), timing=timing, d20=round(d20,1), overheat=oh >= 2,
                up20=bool(up20), above20=bool(c > ma20.iloc[-1]), from_hi=round((c/hi-1)*100,1),
                above50=None if np.isnan(m50) else bool(c > m50))

def load_bench(path, asof):
    b = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    b = b[b.index <= asof]
    daily = b[b["type"] == "D"].drop(columns=["type"]) if "type" in b else b
    allp = b.drop(columns=["type"]) if "type" in b else b
    return daily, allp

def stock(cfg):
    asof = pd.Timestamp(cfg["asof"])
    daily, allp = load_bench(cfg["bench_csv"], asof)
    out = []
    for it in cfg["items"]:
        p = pd.read_csv(it["prices_csv"], parse_dates=["date"]).set_index("date")["close"].sort_index()
        p = p[p.index <= asof].dropna()
        T = tech(p)
        etf = daily[it["sector_etf"]].dropna().reindex(p.index).ffill()
        rl = (p/etf).dropna()
        rs20 = (rl.iloc[-1]/rl.iloc[-21]-1)*100 if len(rl) > 21 else np.nan
        rs_hi = bool(len(rl) > 21 and rl.iloc[-1] >= 0.995*rl.tail(21).max())
        g = it.get("rev_growth", 0) or 0
        q = {"rev": 35*min(max(g/60, 0), 1)}
        gd = it.get("guide")
        q["guide"] = 0 if gd == "하향" else (20 if (gd == "상향" and it.get("guide_beat")) else (15 if gd == "상향" else 8))
        q["est"] = (max(-3, min(3, it.get("est_up", 0))) + 3)/6*20
        qmax = 75; miss = []
        if it.get("rev_growth_prev") is not None:
            q["accel"] = 10*min(max((g - it["rev_growth_prev"])/10, 0), 1); qmax += 10
        else: miss.append("가속")
        if it.get("margin_trend") is not None:
            q["margin"] = {-1: 0, 0: 7.5, 1: 15}[it["margin_trend"]]; qmax += 15
        else: miss.append("수익성")
        quality = round(sum(q.values())/qmax*100, 1)
        q_note = ("환산(미입력: %s)" % "·".join(miss)) if miss else "전 항목"
        lead = int(g >= 20) + int(g >= 40) + int(it.get("guide") == "상향") + int(it.get("est_up", 0) >= 2) + int(rs20 > 0 if not np.isnan(rs20) else 0)
        e, s = it["entry"], it["stop"]
        sc = it["scen"]; pr = sum(v[1] for v in sc.values())
        E = sum(v[1]/pr*(v[0]/e-1) for v in sc.values())
        risk = (e-s)/e; rr = (sc["base"][0]-e)/(e-s); bear = sc["bear"][0]/e-1
        k = sum([bool(it.get("src_primary")), bool(it.get("price_xcheck")), bool(it.get("est_consistent"))])
        cert, coef = {3: ("높음", 1.0), 2: ("중간", 0.75)}.get(k, ("낮음", 0.5))
        rank = round(E/risk*coef, 2) if risk > 0 else None
        basket = it.get("sector_basket", "없음")
        minE = 0.15 if (basket.startswith("성장 조정") or basket == "기타") else 0.10
        lim = it.get("norm_pe_limit") or 25
        hard, wait = [], []
        if it.get("mcap_b", 0) < 2 or it.get("adv_musd", 0) < 20: hard.append("제외:유동성·규모")
        if basket == "없음": hard.append("제외:섹터 바구니 없음")
        if it.get("cyclical") and (it.get("norm_pe") is None or it["norm_pe"] > lim): wait.append("경기순환 고점 이익(정상화 P/E %s)" % it.get("norm_pe"))
        if quality < 60 and it.get("kind") != "etf": wait.append("품질 점수<60(%.0f)" % quality)
        tw = []
        if not T["above20"]: tw.append("20일선 아래")
        if not T["up20"]: tw.append("20일선 하락 중")
        # v5.7(2026-11-13): "섹터 대비 20일 상대강도 음수" 관문 삭제 — 20년 시험에서 섹터보다 강한/약한 종목의 3개월 초과수익 차이 +0.3%p(t 0.4), 효과 없음. rs20은 리더 점수·기록에만 씀.
        if T["rsi"] >= 70 or T["d20"] >= 8: tw.append("과열(RSI %.0f, 20일선 %+.1f%%)" % (T["rsi"], T["d20"]))
        if it.get("earn_days", 99) <= 3 and not it.get("post_er_ok"): tw.append("실적 3거래일 이내")
        wait += tw
        if not it.get("price_xcheck"): wait.append("기준일 종가 미교차확인")
        if rr < 2: wait.append("손익비<2(%.1f)" % rr)
        if E < minE: wait.append("기대수익<%d%%" % (minE*100))
        if bear < -0.30: wait.append("약세 시나리오 -30% 초과")
        if cert == "낮음": wait.append("확실성 낮음")
        if hard: status = "제외"
        elif wait or rank is None or rank < 1.5: status = "관찰"
        else: status = "매수 후보"
        out.append(dict(t=it["t"], status=status, basket=basket, quality=quality, q_note=q_note, ready_left=len(tw), lead=lead, rank=rank, E=round(E*100,1),
                        risk=round(risk*100,1), rr=round(rr,2), bear=round(bear*100,1), cert=cert, timing=T["timing"],
                        rsi=T["rsi"], d20=T["d20"], from_hi=T["from_hi"], rs20=None if np.isnan(rs20) else round(rs20,1),
                        rs_hi=rs_hi, gates="; ".join(hard+wait) or "통과"))
    df = pd.DataFrame(out).sort_values(["status", "quality", "ready_left"], ascending=[True, False, True])
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
    print(df.to_string(index=False))
    df.to_csv(cfg.get("out_csv", "stock_v3_2_out.csv"), index=False)

if __name__ == "__main__":
    c = json.load(open(sys.argv[1]))
    if c.get("mode") == "sector":
        raise SystemExit("섹터 모드는 score_v3.py를 쓰세요")
    stock(c)
