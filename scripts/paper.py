"""paper.py — ⑨ 모의계좌(가상 10만 달러) 하루 처리. sysv6.step을 그대로 쓴다(백테스트와 같은 코드).
사용: python3 paper.py --state paper_state.json --ohlcv store/ohlcv.parquet --fin store/financials.parquet --screen data/screen.csv
  상태 파일이 없으면 오늘 기준으로 새로 시작(오늘 주문만 만들고 체결은 다음 거래일 시가).
  여러 날 빠졌으면 빠진 거래일을 순서대로 모두 처리한다.
출력: paper_state.json(덮어씀), paper_report.json, 표준출력 요약
"""
import argparse, json, os, sys
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sysv6 as V
ap = argparse.ArgumentParser()
ap.add_argument("--state", default="paper_state.json"); ap.add_argument("--ohlcv", default="store/ohlcv.parquet")
ap.add_argument("--fin", default="store/financials.parquet"); ap.add_argument("--screen", default="data/screen.csv")
ap.add_argument("--cash", type=float, default=100000); ap.add_argument("--out", default="paper_report.json")
A = ap.parse_args()
X = V.features(pd.read_parquet(A.ohlcv), pd.read_parquet(A.fin), pd.read_csv(A.screen))
dates = X["dates"]; last = str(dates[-1].date())
if os.path.exists(A.state) and os.path.getsize(A.state) > 2:
    S = json.load(open(A.state))
else:
    S = V.new_state(A.cash, start=last); S["last_date"] = None
todo = [i for i, d in enumerate(dates) if S.get("last_date") is None and i == len(dates) - 1 or (S.get("last_date") and str(d.date()) > S["last_date"])]
days = []
for i in todo:
    r = V.step(S, X, i); S["last_date"] = r["date"]; days.append(r)
# 누적 통계(상태를 작게 유지: 전체 곡선 대신 시작·고점·최저 낙폭·최근 거래만)
eq = S.pop("equity", []) 
st = S.setdefault("stats", dict(first=None, min_dd=0.0, n=0, wins=0, sum_ret=0.0, sum_ex=0.0, sum_ex2=0.0, days=0))
for e in eq:
    if st["first"] is None: st["first"] = e
    st["days"] += 1
st["min_dd"] = min(st["min_dd"], min([r["status"]["dd"] for r in days] or [0]))
new_tr = S.get("trades", [])
for t in new_tr:
    ex = t["ret"] - t.get("spy", 0); st["n"] += 1; st["wins"] += t["ret"] > 0; st["sum_ret"] += t["ret"]; st["sum_ex"] += ex; st["sum_ex2"] += ex * ex
S.pop("recent_trades", None); S["trades"] = []; S["equity"] = []
S["last_eq"] = eq[-1] if eq else S.get("last_eq")
def _r(o):
    if isinstance(o, float): return round(o, 4)
    if isinstance(o, dict): return {k: _r(v) for k, v in o.items()}
    if isinstance(o, list): return [_r(v) for v in o]
    return o
S = _r(S)
json.dump(S, open(A.state, "w"), ensure_ascii=False, separators=(",", ":"), default=float)
f = st["first"]; L = S["last_eq"]
ret = L[1] / f[1] - 1 if f and L else 0.0; spr = L[2] / f[2] - 1 if f and L else 0.0
n = st["n"]; avg_ex = st["sum_ex"] / n if n else None
sd_ex = np.sqrt(max(st["sum_ex2"] / n - avg_ex ** 2, 0)) if n > 1 else None
t_ex = (avg_ex / (sd_ex / np.sqrt(n))) if n > 1 and sd_ex else None
crit = [dict(k="운영 기간 ≥ 126거래일(약 6개월)", ok=st["days"] >= 126, v=st["days"]),
        dict(k="종료 거래 ≥ 60건", ok=n >= 60, v=n),
        dict(k="계좌 수익 > SPY 수익(시작 이후)", ok=ret > spr, v="%+.1f%% vs %+.1f%%" % (ret * 100, spr * 100)),
        dict(k="거래당 SPY 대비 초과수익 > 0, t ≥ 1.5", ok=bool(avg_ex is not None and avg_ex > 0 and (t_ex or 0) >= 1.5),
             v=None if avg_ex is None else "%+.2f%% (t %.1f)" % (avg_ex * 100, t_ex or 0)),
        dict(k="최대 낙폭 > −20%", ok=st["min_dd"] > -20, v="%.1f%%" % st["min_dd"])]
pos = []
for t, p in S["pos"].items():
    pos.append(dict(t=t, sh=round(p["sh"], 4), e=round(p["e"], 2), last=round(p["last"], 2), pnl=round((p["last"] / p["e"] - 1) * 100, 1),
                    stop=round(p["stop"], 2), t3=round(p["t3"], 2), d0=p["d0"], val=round(p["sh"] * p["last"], 2)))
pos.sort(key=lambda x: -x["val"])
rep = dict(asof=last, processed=[r["date"] for r in days], value=L[1] if L else A.cash, cash=round(S["cash"], 2),
           ret_pct=round(ret * 100, 2), spy_pct=round(spr * 100, 2), min_dd=st["min_dd"], n_pos=len(S["pos"]),
           fills=[f_ for r in days for f_ in r["fills"]], orders=S["pending"], status=days[-1]["status"] if days else None,
           closed=dict(n=n, win_pct=round(st["wins"] / n * 100) if n else None, avg_ret_pct=round(st["sum_ret"] / n * 100, 2) if n else None,
                       avg_ex_pct=None if avg_ex is None else round(avg_ex * 100, 2), t=None if t_ex is None else round(t_ex, 2)),
           golive=dict(ready=all(c["ok"] for c in crit), checks=crit), positions=pos, trades_today=new_tr)
json.dump(rep, open(A.out, "w"), ensure_ascii=False, indent=1, default=float)
print(json.dumps({k: rep[k] for k in ("asof", "processed", "value", "cash", "ret_pct", "spy_pct", "min_dd", "n_pos", "status", "closed")}, ensure_ascii=False, default=float))
print("새 거래일 없음" if not days else "처리 %d일" % len(days), "| 체결", len(rep["fills"]), "| 내일 시가 주문", len(rep["orders"]), "| 실전 전환", "충족" if rep["golive"]["ready"] else "미충족")
for o in rep["orders"]: print(" ", o["side"], o["t"], o.get("amt", ""), o.get("why", ""), o.get("est_stop", ""), o.get("est_t3", ""))
# Notion용 산출(③이 그대로 옮겨 적음)
import hashlib
sha = hashlib.sha256(open(A.state, "rb").read()).hexdigest()[:16]
fx_ = lambda L: "; ".join(L) if L else "없음"
props = {"기록": "⑨ " + last, "date:기준일:start": last, "date:기준일:is_datetime": 0, "평가액": rep["value"], "현금": rep["cash"],
         "누적 수익(%)": rep["ret_pct"], "SPY 누적(%)": rep["spy_pct"], "최대 낙폭(%)": rep["min_dd"], "보유 수": rep["n_pos"],
         "체결": fx_(["%s %s %.4g주 @%.2f%s" % ("매수" if f_["side"] == "buy" else "매도", f_["t"], f_["sh"], f_["px"], "" if f_["side"] == "buy" else "(" + f_["why"] + ")") for f_ in rep["fills"]])[:1900],
         "다음 시가 주문": fx_(["%s %s%s" % ("매수" if o["side"] == "buy" else "매도", o["t"], " $%.0f(손절≈%.2f·목표≈%.2f)" % (o["amt"], o["est_stop"], o["est_t3"]) if o["side"] == "buy" else "(" + o["why"] + ")") for o in rep["orders"]])[:1900],
         "종료 거래": fx_(["%s %+.1f%%(SPY %+.1f%%, %s)" % (t["t"], t["ret"] * 100, t.get("spy", 0) * 100, t["why"]) for t in rep["trades_today"]])[:1900],
         "누적 종료 거래": rep["closed"]["n"], "거래당 초과수익(%)": rep["closed"]["avg_ex_pct"],
         "상태": "새 거래일 없음" if not days else "낙폭 %.1f%% · 200일선 위 %.0f%% · 시장 폭 백분위 %s · 비중 ×%.1f · %s" % (rep["status"]["dd"], rep["status"]["b200"], rep["status"]["breadth_pct"], rep["status"]["mult"], rep["status"]["gate"] or "신규 가능"),
         "실전 전환": "충족" if rep["golive"]["ready"] else "미충족", "상태 sha": sha}
md = ["| 종목 | 수량 | 진입 | 현재 | 손익 | 손절 | 3개월 목표 | 진입일 |", "|---|---|---|---|---|---|---|---|"]
md += ["| %s | %.4g | %.2f | %.2f | %+.1f%% | %.2f | %.2f | %s |" % (p["t"], p["sh"], p["e"], p["last"], p["pnl"], p["stop"], p["t3"], p["d0"]) for p in pos]
md += ["", "실전 전환 기준: " + " · ".join(("✅ " if c["ok"] else "⬜ ") + c["k"] + "(" + str(c["v"]) + ")" for c in crit)]
json.dump(dict(props=props, positions_md="\n".join(md), sha=sha, state_json=open(A.state).read()), open("paper_notion.json", "w"), ensure_ascii=False, indent=1)
print("상태 sha", sha)
