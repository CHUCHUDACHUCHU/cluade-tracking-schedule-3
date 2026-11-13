import json, sys, pandas as pd
sys.path.insert(0, "scripts"); from common import themes_of
# 사용: python3 interp.py stock_stage.json data/screen.csv sectors.json prev.json [manual.json]
st = json.load(open(sys.argv[1])); sc = pd.read_csv(sys.argv[2]).set_index("ticker")
sectors = json.load(open(sys.argv[3])); prev = json.load(open(sys.argv[4]))
manual = json.load(open(sys.argv[5])) if len(sys.argv) > 5 else {}   # {티커: "관찰 사유"} 수동 엄격화
TECH = ("20일선 아래", "20일선 하락", "섹터 대비", "과열", "실적")  # 기술 관문(가격만 오면 풀림)
def split(g):
    parts = [p.strip() for p in (g or "").split(";") if p.strip() and p.strip() != "통과"]
    t = [p for p in parts if p.startswith(TECH)]; v = [p for p in parts if p not in t]
    return t, v
rows = []
for it in st["items"]:
    t = it["t"]; D = it["data"]; q = it["quality"]
    s = sc.loc[t] if t in sc.index else None
    close = D["close"]; ma = D["sma20"]; atr = float(s["atr20_pct"]) if s is not None else None
    tg, vg = split(it["gates"])
    if D.get("valuation_flag"): vg.append("목표가 산정 부적합")
    if t in manual: vg.append("수동 엄격화: " + manual[t])
    if close < ma: trig, kind = ma, "20일선 회복"
    elif any(p.startswith("과열") for p in tg): trig, kind = ma * 1.08, "과열 해소(20일선+8% 이내)"
    else: trig, kind = close, "가격 조건 충족(20일선 상승·RS 대기)"
    dist = (trig / close - 1) * 100
    p = prev.get(t, {})
    rows.append(dict(t=t, etf=D["basket_etf"], basket=it["basket"], q=q, ready=it["ready_left"],
        status=it["status"] if t not in manual else "관찰", E=it["E"], rr=it["rr"], bear=it["bear"],
        close=close, ma20=round(ma, 2), trig=round(trig, 2), kind=kind, dist=round(dist, 1),
        atr=atr, atr_days=None if not atr else round(abs(dist) / atr, 1),
        tech=tg, value=vg, base=it["inputs"]["scen"]["base"][0],
        rev=it["inputs"]["rev_growth"], est30=D["eps_1y_chg_30d_pct"], ne=D["next_earnings"], le=D.get("last_earnings"),
        above20=close > ma, members={D["basket_etf"], *(themes_of(D.get("industry")) or set())} | ({str(s["sector_etf"])} if s is not None else set()), dq=None if p.get("quality") is None else round(q - p["quality"], 1),
        dready=None if p.get("ready") is None else it["ready_left"] - p["ready"], prev_status=p.get("status"),
        news=(D.get("news") or {}).get("tag", "중립"), news_etf=(D.get("news") or {}).get("etf"), news_mult=(D.get("news") or {}).get("mult", 1.0)))
cand = [r for r in rows if r["q"] >= 60 and r["status"] != "매수 후보"]
# ② DB 기록 대상과 같은 규칙: 매수 후보 전부 + 품질 상위, 합계 25
sel = sorted(rows, key=lambda r: (r["status"] != "매수 후보", -r["q"]))[:25]; selt = {r["t"] for r in sel}
# 가장 먼저 후보가 될 종목: 가치 관문 통과(기술 조건만 남음) 우선 → 남은 기술 조건 → 트리거 거리
first = sorted([r for r in cand if r["t"] not in manual], key=lambda r: (len(r["value"]) > 0, r["ready"], r["news"] != "순풍", abs(r["dist"])))[:3]  # v5.5 순풍은 같은 조건일 때만 앞으로
etf = {k: v for k, v in json.load(open("data/latest.json"))["tickers"].items()}
sec = []
for s_ in sectors:
    v, b = s_["verdict"], s_["bucket"]
    if not (v == "비중확대" or (v == "중립" and b == "성장 조정")): continue
    m = [r for r in rows if s_["etf"] in r["members"] and r["q"] >= 60]
    e = etf.get(s_["etf"], {}); ed20 = e.get("vs_sma20_pct")
    sec.append(dict(etf=s_["etf"], verdict=v, q60=len(m), above20=[r["t"] for r in m if r["above20"]],
        etf_d20=ed20, lead_first=(ed20 is not None and ed20 < 0) and any(r["above20"] for r in m)))
qmap = {r["t"]: r["q"] for r in rows}
new = [r["t"] for r in sel if r["prev_status"] in (None, "제외")]
gone = [(t, qmap.get(t, "미채점")) for t, p in prev.items() if p.get("status") in ("관찰", "매수 후보") and t not in selt]
big = [(r["t"], r["dq"]) for r in rows if r["dq"] is not None and abs(r["dq"]) >= 10]
for r in rows: r["members"] = sorted(r["members"])
out = dict(sel=[r["t"] for r in sel], first=first, cand=sorted(cand, key=lambda r: (-r["q"], r["ready"])), sectors=sec,
           new=new, gone=gone, big_q=big, buys=[r for r in rows if r["status"] == "매수 후보"])
json.dump(out, open("interp.json", "w"), ensure_ascii=False, indent=1, default=str)
for r in first: print("FIRST", r["t"], r["etf"], r["q"], r["ready"], r["kind"], r["trig"], r["dist"], r["atr_days"], r["tech"], r["value"], "뉴스", r["news"])
print("NEWS", st.get("news_rules"))
for s_ in sec: print("SEC", s_)
print("NEW", new, "GONE", gone, "BIGQ", big)
