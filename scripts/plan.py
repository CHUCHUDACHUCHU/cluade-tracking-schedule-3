import json, sys, re, argparse, pandas as pd
# 사용: python3 plan.py --rows rows2.json --screen data/screen.csv --latest data/latest.json --total 총평가원 --principal 원금원 [--first "TER,CRGY,ZYME"] [--mult 0.5]
ap = argparse.ArgumentParser()
for a in ("--rows", "--screen", "--latest"): ap.add_argument(a, required=True)
ap.add_argument("--total", type=float, required=True); ap.add_argument("--principal", type=float, required=True)
ap.add_argument("--first", default=""); ap.add_argument("--out", default="plan.json")
ap.add_argument("--mult", type=float, default=1.0)  # v5.3 위험 온도계: 과열 주의 0.5, 약세장 0, 그 외 1
A = ap.parse_args()
num = lambda x: None if x in (None, "") else float(re.sub(r"[^0-9.\-]", "", str(x)) or "nan")
clean = lambda s: (s or "").replace("\\", "")
def t3_of(r):  # ② 시나리오 근거 맨 앞 "[v5.2] … 3개월 목표 $X …"
    m = re.search(r"3개월 목표 \$?([0-9][0-9,]*\.?[0-9]*)", clean(r.get("시나리오 근거")))
    return float(m.group(1).replace(",", "")) if m else None
rows = json.load(open(A.rows)); rows = rows["results"] if isinstance(rows, dict) else rows; sc = pd.read_csv(A.screen).set_index("ticker")
fx = json.load(open(A.latest))["tickers"]["KRW=X"]["close"]
TECH = ("20일선 아래", "20일선 하락", "섹터 대비", "과열", "실적")
CAP = {"추세": 0.25, "성장 조정": 0.15, "성장 조정 대기": 0.15, "기타": 0.075}
out = []
for r in rows:
    if r.get("상태") != "관찰" or r.get("채점 버전") != "v3": continue
    q = num(r.get("품질 점수"))
    if q is None or q < 60: continue
    if "수동 엄격화" in clean(r.get("진입 조건")) + clean(r.get("제외·변경 사유")): continue
    t = r["티커"]; g = clean(r.get("관문 결과")).replace("[v3.2]", "").split("|")[0]
    parts = [p.strip() for p in g.split(";") if p.strip() and not p.strip().startswith("통과")]
    tech = [p for p in parts if p.startswith(TECH)]; value = [p for p in parts if p not in tech]
    if "목표가 산정 부적합" in clean(r.get("진입 조건")) and "목표가 산정 부적합" not in value: value.append("목표가 산정 부적합")
    if t not in sc.index: out.append(dict(t=t, err="screen.csv 없음")); continue
    s = sc.loc[t]; close, ma, atr = float(s.close), float(s.sma20), float(s.atr20_pct)
    if close < ma: trig, kind = ma, "20일선 회복"
    elif any(p.startswith("과열") for p in tech): trig, kind = ma * 1.08, "과열 해소(20일선+8% 아래로 식어야 함)"
    else: trig, kind = close, "가격 조건 충족(20일선 상승 전환·섹터 대비 강세 대기)"
    dist = (trig / close - 1) * 100
    entry, stop, base = num(r.get("진입 가격대")), num(r.get("손절가")), num(r.get("목표가"))
    b = r.get("바구니") or "기타"
    d = dict(t=t, basket=b, q=q, ready=int(num(r.get("남은 기술 조건")) or 0), close=close, date=str(s.date), ma20=round(ma, 2),
             trig=round(trig, 2), kind=kind, dist=round(dist, 1), atr=atr, atr_days=round(abs(dist) / atr, 1) if atr else None,
             tech=tech, value=value, entry=entry, stop=stop, base=base, t3=t3_of(r), ne=str(s.next_earnings))
    if not value and entry and stop and entry > stop:
        w = (entry - stop) / entry
        plan = min(CAP.get(b, 0.075) * A.total, A.principal * 0.02 / w) * A.mult
        first = plan / 3 if b != "추세" else plan
        d.update(stop_w=round(w * 100, 1), plan_krw=round(plan), plan_usd=round(plan / fx, 2), first_krw=round(first),
                 first_usd=round(first / fx, 2), first_qty=round(first / fx / entry, 4),
                 loss_first=round(first * w), loss_plan=round(plan * w), split=b != "추세", mult=A.mult)
    out.append(d)
order = [x.strip() for x in A.first.split(",") if x.strip()]
key = lambda d: (order.index(d["t"]) if d["t"] in order else 99, len(d.get("value", [])) > 0, d.get("ready", 9), abs(d.get("dist", 99)))
out.sort(key=key)
wait = [d for d in out if not d.get("value") and "err" not in d][:3]
watch = [d for d in out if d.get("value")][:5]
json.dump(dict(fx=fx, mult=A.mult, wait=wait, watch=watch, errors=[d for d in out if "err" in d]), open(A.out, "w"), ensure_ascii=False, indent=1)
for d in wait: print("WAIT", d)
for d in watch: print("WATCH", d["t"], d["basket"], d["q"], d["kind"], d["trig"], d["dist"], d["atr_days"], d["value"], "base", d["base"], "t3", d["t3"], "close", d["close"])
