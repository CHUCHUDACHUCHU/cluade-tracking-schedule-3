# risk_gauge.py — 시장 위험 온도계 (v5.3, 2026-11-13 사용자 승인). 사용: python3 risk_gauge.py data/core_prices.csv data/screen.csv
# 근거: 2023-11~2026-11 시험(시스템 페이지 v5.3 절). 주가 하락을 '예측'하지 못한다 — 단기 위험 수준과 공포 후 기회를 구분할 뿐.
import sys, json, pandas as pd, numpy as np
cp = pd.read_csv(sys.argv[1]); sc = pd.read_csv(sys.argv[2])
px = cp.pivot(index="date", columns="ticker", values="adj_close").sort_index()
px = px[px["SPY"].notna()].ffill()
def pctile(s):  # 최근 약 400거래일 중 오늘 값의 백분위(0~100)
    s = s.dropna(); return float((s <= s.iloc[-1]).mean() * 100) if len(s) > 60 else None
ser = {
    "breadth_rsp_spy_20d": (px["RSP"] / px["SPY"]).pct_change(20),
    "credit_hyg_ief_20d": (px["HYG"] / px["IEF"]).pct_change(20),
    "vol_vixy_5d": px["VIXY"].pct_change(5),
    "dollar_uup_20d": px["UUP"].pct_change(20),
    "copper_gold_20d": (px["CPER"] / px["GLD"]).pct_change(20),
    "oil_uso_20d": px["USO"].pct_change(20),
}
val = {k: round(float(v.dropna().iloc[-1]) * 100, 2) for k, v in ser.items()}
pct = {k: (None if pctile(v) is None else round(pctile(v))) for k, v in ser.items()}
st = sc[(sc.get("sources") != "core") & (sc["avg_dollar_vol_20d"] >= 2e7) & (sc["close"] >= 5) & sc["vs_sma200_pct"].notna()]
b200 = round(float((st["vs_sma200_pct"] > 0).mean() * 100), 1)
spy = px["SPY"]; spy_d20 = round(float(spy.iloc[-1] / spy.tail(20).mean() * 100 - 100), 2)
P = lambda k, d=50: d if pct[k] is None else pct[k]
flags = []
if b200 < 40: state = "약세장"; flags.append("200일선 위 종목 %.0f%% < 40%%" % b200)
elif P("credit_hyg_ief_20d") <= 20: state = "공포(기회 대기)"; flags.append("신용(HYG/IEF) 20일 변화 하위 %.0f%%" % pct["credit_hyg_ief_20d"])
elif P("breadth_rsp_spy_20d") >= 80 or b200 > 70:
    state = "과열 주의"
    if P("breadth_rsp_spy_20d", 0) >= 80: flags.append("시장 폭 급확대(RSP/SPY 20일 상위 %.0f%%)" % (100 - pct["breadth_rsp_spy_20d"]))
    if b200 > 70: flags.append("200일선 위 종목 %.0f%% > 70%%" % b200)
else: state = "보통"
if P("vol_vixy_5d") <= 10: flags.append("변동성 급감(VIXY 5일 하위 %.0f%%) — 안도 랠리 끝물 가능" % pct["vol_vixy_5d"])
rule = {"약세장": "② 매수 후보 → 관찰(엄격화) · ③ 신규 매수 없음",
        "공포(기회 대기)": "③ 계획 금액 그대로 · ② 품질 60↑ 종목 중 20일선 회복 종목을 우선 안내(공포 뒤 3개월 성과 가장 좋았음)",
        "과열 주의": "③ 신규 계획 금액 ×0.5(1차 진입만) · 추격 금지",
        "보통": "변경 없음"}[state]
out = dict(asof=str(px.index[-1]), state=state, rule=rule, flags=flags, b200_pct=b200, spy_vs_sma20_pct=spy_d20,
           values_pct=val, percentiles=pct)
json.dump(out, open("risk_gauge.json", "w"), ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False))
