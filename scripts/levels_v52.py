# levels_v52.py — ② 진입가·손절가·목표가 재계산 (v5.2, 2026-11-12) + v5.3 추격 방지·위험 온도계(2026-11-13), 사용자 승인 + v5.4 목표가 보정(2026-11-13) + v5.6 3개월 손익비 관문 삭제 + v5.7 손절 폭 확대(2026-11-13)
# 사용: stock_stage.py 실행 직후 같은 폴더에서
#   python3 levels_v52.py --stage stock_stage.json --cfg stock_cfg.json --screen data/screen.csv --score scripts/score_v3_2.py --gauge risk_gauge.json
# 규칙
#   진입가 = max(종가, 내일 20일선 위 필요 종가 = 최근 19거래일 종가 평균). 매수 후보가 되는 날엔 = 종가.
#   v5.7 손절가(③이 실제로 파는 가격) = 진입가 × (1 − 5 × ATR(screen.csv atr20_pct), 15~35%). 비중 상한 = 원금 2% ÷ 이 손절 폭(최대 25%).
#     근거: 2.5×ATR(6~15%) 손절은 2년 시험 연 −1.2% vs 5×ATR +8.5%, 20년 시험 +3.2% vs +4.6%(최대 낙폭 비슷) — 촘촘한 손절이 흔들림에 팔림.
#   평가용 위험 폭 = 2.5 × ATR(6~15%) — 손익비·순위값 계산에만 씀(채점 기준을 바꾸지 않으려고 분리). stop_eval로 기록.
#   자기 P/E 계열 = stock_stage의 multiple_source(연도별 후행 × 선행/후행 비율, 80배 초과 제외). m5=중앙값, m2=최근 2개 평균, mh=최근 2개 최고.
#   현재 P/E = 진입가 ÷ NTM EPS. 기본 배수 = max(m5, min(현재 P/E, m2)) (60배 상한).
#   12개월 목표 = NTM EPS × (1 + 0.5 × 내년 EPS 성장률(−30%~+30%)) × 기본 배수.
#   강세 = NTM × (1 + 성장률(0~30%)) × mh(75배 상한) / 약세 = NTM × 0.9 × min(현재 P/E, m5). 확률 25/50/25.
#   3개월 목표 = min(12개월 목표, 진입가 × (1 + 0.5 × 하루 변동성 × √63)) — ③의 전량 매도 기준. 3개월 손익비 rr3는 참고값(v5.6부터 관문 아님).
#   v5.3: 바구니 섹터 ETF 20일 수익률이 SPY보다 3%p 넘게 높으면 매수 후보 → 관찰(추격 방지). 온도계 '약세장'이면 매수 후보 → 관찰.
#   v5.4: 자기 P/E 이력이 없으면 동종 배수 대신 현재 P/E 유지(동종보다 싸면 동종). 매출×EV/매출 방식 종목은 애널리스트 평균 목표가(종가 0.4~3배일 때), 없으면 목표가 산정 부적합.
#   12개월 목표가 종가의 0.4배 미만·3배 초과면 목표가 산정 부적합.
import json, re, sys, argparse, subprocess, shutil
import numpy as np, pandas as pd
ap = argparse.ArgumentParser()
ap.add_argument("--stage", default="stock_stage.json"); ap.add_argument("--cfg", default="stock_cfg.json")
ap.add_argument("--screen", default="data/screen.csv"); ap.add_argument("--score", default="scripts/score_v3_2.py")
ap.add_argument("--gauge", default="risk_gauge.json")
A = ap.parse_args()
st = json.load(open(A.stage)); cfg = json.load(open(A.cfg))
shutil.copy(A.stage, A.stage.replace(".json", "_v5.json"))
sc = pd.read_csv(A.screen).set_index("ticker")
D = {r["t"]: r for r in st["items"]}
try:
    G = json.load(open(A.gauge))
except Exception:
    G = {}
lv = {}
for it in cfg["items"]:
    t = it["t"]; r = D.get(t)
    if r is None: continue
    d = r["data"]
    p = pd.read_csv(it["prices_csv"])["close"].dropna().values
    if len(p) < 25: continue
    c = float(p[-1]); rets = np.diff(p[-21:]) / p[-21:-1]; sig = float(np.std(rets))
    need = float(p[-19:].mean()); need_up = float(20 * p[-24:-4].mean() - p[-19:].sum())
    entry = max(c, need)
    atr = float(sc.loc[t, "atr20_pct"]) / 100 if t in sc.index and pd.notna(sc.loc[t, "atr20_pct"]) else float(np.mean(np.abs(rets)))
    sd = min(max(2.5 * atr, 0.06), 0.15); stop = entry * (1 - sd)  # 평가용(채점 손익비·순위값)
    sdx = min(max(5.0 * atr, 0.15), 0.35); stop_x = entry * (1 - sdx)  # v5.7 실제 손절(③ 매도 기준)
    scen = it["scen"]; method = d.get("valuation_method", ""); note = "EV/매출 방식 — 목표가 유지"
    ntm = d.get("ntm_eps"); m5 = m2 = mh = cur = mb = None
    if method == "EPS×P/E" and ntm and ntm > 0:
        src = d.get("multiple_source") or ""
        mm = re.search(r"후행 ([^×]+)× 선행/후행 ([0-9.]+)", src)
        hv = []
        if mm:
            ratio = float(mm.group(2))
            for kv in mm.group(1).split(","):
                if ":" in kv:
                    v = float(kv.split(":")[1])
                    if 0 < v <= 80: hv.append(v * ratio)
        cur = entry / ntm; own = len(hv) >= 2
        if own:
            m5, m2, mh = float(np.median(hv)), float(np.mean(hv[-2:])), float(max(hv[-2:]))
        else:  # v5.4: 자기 P/E 이력 없음 → 동종 배수로 끌어내리지 않고 현재 P/E 유지(동종보다 싸면 동종까지)
            peer = float(d.get("base_multiple") or 15)
            m5, m2 = peer, max(peer, cur); mh = max(cur, float(d.get("bull_multiple") or peer * 1.25))
        g = (d.get("eps_growth_next_y_pct") or 0) / 100
        mb = min(max(m5, min(cur, m2)), 60.0)
        base = ntm * (1 + 0.5 * min(max(g, -0.3), 0.3)) * mb
        bull = ntm * (1 + min(max(g, 0), 0.3)) * min(mh, 75.0)
        bear = ntm * 0.9 * min(cur, m5)
        scen = {"bull": [round(bull, 2), 25], "base": [round(base, 2), 50], "bear": [round(bear, 2), 25]}
        note = ("자기 P/E 중앙 %.1f·2년 평균 %.1f·2년 최고 %.1f·현재 %.1f → 기본 %.1f배, 내년 EPS 성장 %+.0f%%" % (m5, m2, mh, cur, mb, g * 100)) if own else \
               ("자기 P/E 이력 없음 → 현재 P/E %.1f 유지(동종 %.1f) → 기본 %.1f배, 내년 EPS 성장 %+.0f%%" % (cur, m5, mb, g * 100))
    if method != "EPS×P/E" or not (ntm and ntm > 0):  # v5.4: 매출 방식 종목 — 동종 EV/매출로 끌어내린 목표 대신 애널리스트 평균 목표가
        tm = d.get("target_mean")
        if tm and 0.4 * c <= float(tm) <= 3 * c:
            tm = float(tm); scen = {"bull": [round(tm * 1.2, 2), 25], "base": [round(tm, 2), 50], "bear": [round(c * 0.7, 2), 25]}
            note = "이익 기준 산정 불가 → 애널리스트 평균 목표가 $%.2f 사용" % tm
        else:
            note = "목표가 산정 불가(이익 없음·애널리스트 목표 없음/비정상)"
    base = scen["base"][0]
    t3 = entry * (1 + min(base / entry - 1, 0.5 * sig * np.sqrt(63))) if base > entry else base
    it["entry"], it["stop"], it["scen"] = round(entry, 2), round(stop, 2), scen
    flag = None
    if note.startswith("목표가 산정 불가"):
        flag = "목표가 산정 부적합 — 이익 없음·애널리스트 목표 없음/비정상(웹 확인 전 판단 보류)"
    elif base < 0.4 * c or base > 3 * c:
        flag = "목표가 산정 부적합(기본 %.0f vs 종가 %.0f) — 웹 확인 전 판단 보류" % (base, c)
    lv[t] = dict(need=round(need, 2), need_up=round(need_up, 2), entry=round(entry, 2), stop=round(stop_x, 2), stop_pct=round(sdx * 100, 1),
                 stop_eval=round(stop, 2), stop_eval_pct=round(sd * 100, 1),
                 atr_pct=round(atr * 100, 2), t3=round(t3, 2), t12=round(base, 2), rr3=round((t3 - entry) / (entry - stop), 2),
                 w_cap_pct=round(min(0.02 / sdx, 0.25) * 100, 1), mult=None if mb is None else round(mb, 1),
                 cur_pe=None if cur is None else round(cur, 1), note=note, flag=flag)
json.dump(cfg, open(A.cfg, "w"), ensure_ascii=False)
subprocess.run([sys.executable, A.score, A.cfg], check=True, stdout=subprocess.DEVNULL)
res = pd.read_csv(cfg.get("out_csv", "stock_v3_2_out.csv")).set_index("t")
out = []
for t, r in D.items():
    if t not in res.index or t not in lv: out.append(r); continue
    s = res.loc[t]; L = lv[t]
    for k in ("status", "rank", "E", "risk", "rr", "bear", "cert", "gates"):
        v = s[k]; r[k] = None if (isinstance(v, float) and np.isnan(v)) else (v.item() if hasattr(v, "item") else v)
    be = r["data"].get("basket_etf")
    if be in sc.index and "SPY" in sc.index and pd.notna(sc.loc[be, "ret_21d_pct"]) and pd.notna(sc.loc["SPY", "ret_21d_pct"]):
        ex = float(sc.loc[be, "ret_21d_pct"] - sc.loc["SPY", "ret_21d_pct"]); L["sector_ex20"] = round(ex, 1)
        if ex > 3:
            r["gates"] = ("" if r["gates"] == "통과" else r["gates"] + "; ") + "섹터 대비 급등 — 추격 주의(%s 20일 SPY 대비 %+.1f%%p)" % (be, ex)
            if r["status"] == "매수 후보": r["status"] = "관찰"
    if G.get("state") == "약세장":
        r["gates"] = ("" if r["gates"] == "통과" else r["gates"] + "; ") + "위험 온도계 약세장(200일선 위 종목 %.0f%%)" % G.get("b200_pct", 0)
        if r["status"] == "매수 후보": r["status"] = "관찰"
    # v5.6: 3개월 손익비 관문 삭제(위·아래 폭을 같은 변동성으로 계산해 항상 1 근처 → 판별력 없음). rr3는 참고값으로만 남김.
    r["inputs"]["entry"], r["inputs"]["stop"], r["inputs"]["scen"] = L["entry"], L["stop"], next(i for i in cfg["items"] if i["t"] == t)["scen"]
    r["data"]["levels_v52"] = L; r["data"]["valuation_flag"] = L["flag"]
    r["data"]["base_multiple"] = L["mult"] if L["mult"] is not None else r["data"].get("base_multiple")
    out.append(r)
out.sort(key=lambda r: ({"매수 후보": 0, "관찰": 1, "제외": 2}.get(r["status"], 3), -(r["quality"] or 0), r["ready_left"]))
n = 0
for r in out:
    r["needs_web"] = False
    if n >= 10 or r["data"].get("valuation_flag"): continue
    if r["status"] == "매수 후보" or ((r["quality"] or 0) >= 60 and r["ready_left"] <= 1 and (r["rr"] or 0) >= 1.5):
        r["needs_web"] = True; n += 1
st["items"] = out; st["stage_a"]["needs_web"] = n
st["stage_a"]["status_counts"] = pd.Series([r["status"] for r in out]).value_counts().to_dict()
st["levels"] = "v5.7"; st["risk_gauge"] = G.get("state")
json.dump(st, open(A.stage, "w"), ensure_ascii=False, indent=1, default=str)
rows = [dict(t=r["t"], st=r["status"], q=r["quality"], ready=r["ready_left"], close=r["data"]["close"], **{k: r["data"].get("levels_v52", {}).get(k) for k in ("need", "entry", "stop", "stop_pct", "t3", "t12", "rr3", "mult", "cur_pe", "sector_ex20")}, E=r["E"], rr=r["rr"], bear=r["bear"]) for r in out]
pd.set_option("display.width", 250); pd.set_option("display.max_columns", 30)
print("온도계:", G.get("state"), G.get("flags")); print(json.dumps(st["stage_a"], ensure_ascii=False)); print(pd.DataFrame(rows).to_string(index=False))
