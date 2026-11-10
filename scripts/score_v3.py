# 채점 v3 (2026-11-04). 사용법: python3 score_v3.py config.json
# ① 섹터 모드 현행(2026-11-10 Notion에서 GitHub로 이전, 코드 변경 없음). 종목 모드는 score_v3_2.py를 쓴다.
# 섹터 모드(①): {"mode":"sector","asof","bench_csv"(date,type(D|M),SPY,ETF...),
#   "sectors":[{"name","etf","rev"(-1|0|1 이익 추정치 방향),"demand"(1~5 수요·수주),
#               "leaders"(0~3 리더 밀도),"macro"(-1|0|1),"commodity_peak"(bool),"val_rich"(bool)}]}
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

def px(allp, col, d):
    s = allp[col].dropna(); s = s[s.index <= d]
    return (s.iloc[-1], s.index[-1]) if len(s) else (np.nan, None)

def sector(cfg):
    asof = pd.Timestamp(cfg["asof"])
    daily, allp = load_bench(cfg["bench_csv"], asof)
    rows = []
    for s in cfg["sectors"]:
        e = s["etf"]
        c_now, d_now = px(allp, e, asof)
        spy_now, _ = px(allp, "SPY", d_now)
        def rs(d0, d1):
            a0, _ = px(allp, e, d0); a1, _ = px(allp, e, d1)
            s0, _ = px(allp, "SPY", d0); s1, _ = px(allp, "SPY", d1)
            return (a1/a0 - s1/s0)*100
        rs61 = rs(d_now - pd.Timedelta(days=182), d_now - pd.Timedelta(days=30))   # 6개월, 최근 1개월 제외
        rs3 = rs(d_now - pd.Timedelta(days=91), d_now)                              # 3개월
        T = tech(daily[e])
        G = s["demand"] + 2*s["rev"] + s["leaders"] + s.get("macro", 0)
        rows.append(dict(name=s["name"], etf=e, last=str(d_now.date()), rs61=round(rs61,1), rs3=round(rs3,1),
                         rev=s["rev"], demand=s["demand"], leaders=s["leaders"], macro=s.get("macro",0), G=G,
                         cp=bool(s.get("commodity_peak")), val_rich=bool(s.get("val_rich")), **{k: T[k] for k in ["timing","rsi","d20","overheat"]}))
    df = pd.DataFrame(rows)
    df["mom"] = ((df.rs61.rank(pct=True) + df.rs3.rank(pct=True))/2*100).round(0)
    def cls(r):
        if r.overheat or (r.val_rich and r.mom >= 70): return "과열"
        if r.cp: return "없음"
        trend_ok = r.timing == "전환 확인" or (r.timing == "중립" and r.d20 >= -3)
        if r.mom >= 50 and r.rev >= 0 and r.demand >= 3 and r.G >= 6 and trend_ok: return "추세"
        if r.G >= 8 and r.rev == 1 and r.demand >= 4: return "성장 조정"
        return "없음"
    df["bucket"] = df.apply(cls, axis=1)
    def verdict(r):
        if r.rev == -1 and r.demand <= 2 and r.mom < 30: return "제외"
        if r.rev == -1 or (r.demand <= 2 and r.mom < 50): return "비중축소"
        if r.bucket == "추세": return "비중확대"
        if r.bucket == "성장 조정" and r.timing == "전환 확인": return "비중확대"
        return "중립"
    df["verdict"] = df.apply(verdict, axis=1)
    def note(r):
        if r.bucket == "성장 조정" and r.verdict != "비중확대": return "전환 대기(② 조건부 후보 풀)"
        if r.cp: return "원자재 고점(단기 이벤트용만)"
        return ""
    df["note"] = df.apply(note, axis=1)
    order = {"비중확대": 0, "중립": 1, "비중축소": 2, "제외": 3}
    df = df.assign(o=df.verdict.map(order), g2=-df.G).sort_values(["o", "g2", "mom"], ascending=[True, True, False]).drop(columns=["o", "g2"])
    pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
    print(df.to_string(index=False))
    df.to_csv(cfg.get("out_csv", "sector_v3_out.csv"), index=False)

if __name__ == "__main__":
    c = json.load(open(sys.argv[1]))
    if c.get("mode") != "sector":
        raise SystemExit("종목 모드는 score_v3_2.py를 쓰세요")
    sector(c)
