# news_rules.py — ② 정책·뉴스 레이더 적용 (v5.5, 2026-11-13 사용자 승인). levels_v52.py 바로 뒤에 실행.
# 사용: python3 news_rules.py --stage stock_stage.json --sectors sectors.json --events events.json --screen data/screen.csv --asof YYYY-MM-DD
# 규칙(검증 전이라 보수적으로만):
#   역풍: 종목의 바구니 ETF 또는 GICS 섹터 ETF 중 하나라도 ① '역풍'(만료 전)이면 data.news.mult = 0.5(권장 비중 절반). 상태는 바꾸지 않음.
#   순풍: data.news.tag = '순풍'만 기록(점수 불변, 같은 조건 후보끼리 순서 정할 때만 앞으로).
#   A등급 일정 전날: 기준일 다음 미국 거래일에 A등급 일정이 있으면 매수 후보 → 관찰,
#     gates에 "실적·일정 대기(이벤트 MM/DD) — 다음 날 자동 해소" 추가(기술 관문으로 분류됨).
import json, argparse
import pandas as pd
from pandas.tseries.holiday import AbstractHolidayCalendar, Holiday, nearest_workday, USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay, USLaborDay, USThanksgivingDay
class NYSE(AbstractHolidayCalendar):  # 미국 증시 휴장일(재향군인의 날·콜럼버스 데이는 개장)
    rules = [Holiday("NewYear", month=1, day=1, observance=nearest_workday), USMartinLutherKingJr, USPresidentsDay, GoodFriday, USMemorialDay,
             Holiday("Juneteenth", month=6, day=19, observance=nearest_workday), Holiday("July4", month=7, day=4, observance=nearest_workday),
             USLaborDay, USThanksgivingDay, Holiday("Christmas", month=12, day=25, observance=nearest_workday)]
ap = argparse.ArgumentParser()
ap.add_argument("--stage", default="stock_stage.json"); ap.add_argument("--sectors", default="sectors.json")
ap.add_argument("--events", default="events.json"); ap.add_argument("--screen", default="data/screen.csv")
ap.add_argument("--asof", required=True)
A = ap.parse_args()
st = json.load(open(A.stage)); secs = json.load(open(A.sectors))
try:
    ev = json.load(open(A.events))
except Exception:
    ev = []
sc = pd.read_csv(A.screen).set_index("ticker")
asof = pd.Timestamp(A.asof)
hol = NYSE().holidays(asof, asof + pd.Timedelta(days=15))
nxt = asof + pd.tseries.offsets.CustomBusinessDay(1, holidays=hol)
N = {}
for s in secs:
    tag = s.get("news") or "중립"; exp = s.get("news_exp")
    if tag != "중립" and exp and pd.Timestamp(exp) < asof: tag = "중립"  # 만료
    N[s["etf"]] = dict(tag=tag, note=(s.get("news_note") or "")[:120])
evA = [e for e in ev if (e.get("grade") == "A") and e.get("date") and pd.Timestamp(e["date"]).normalize() == nxt.normalize()]
moved, head, tail = [], [], []
for r in st["items"]:
    d = r["data"]; t = r["t"]
    etfs = [d.get("basket_etf")] + ([str(sc.loc[t, "sector_etf"])] if t in sc.index and pd.notna(sc.loc[t, "sector_etf"]) else [])
    tags = [(e, N[e]) for e in dict.fromkeys(etfs) if e in N]
    hw = [(e, n) for e, n in tags if n["tag"] == "역풍"]; tw = [(e, n) for e, n in tags if n["tag"] == "순풍"]
    if hw:
        d["news"] = dict(tag="역풍", etf=hw[0][0], note=hw[0][1]["note"], mult=0.5); head.append(t)
    elif tw:
        d["news"] = dict(tag="순풍", etf=tw[0][0], note=tw[0][1]["note"], mult=1.0); tail.append(t)
    else:
        d["news"] = dict(tag="중립", etf=None, note="", mult=1.0)
    if evA:
        e = evA[0]; txt = "실적·일정 대기(%s %s) — 다음 날 자동 해소" % (e.get("name", "A등급 일정"), pd.Timestamp(e["date"]).strftime("%m/%d"))
        d["event_wait"] = txt
        if r["status"] == "매수 후보":
            r["gates"] = ("" if r["gates"] == "통과" else r["gates"] + "; ") + txt; r["status"] = "관찰"; moved.append(t)
st["news_rules"] = dict(asof=A.asof, next_trading_day=str(nxt.date()), eventA=[e.get("name") for e in evA],
                        headwind=head, tailwind=tail, moved_to_watch=moved,
                        sectors={k: v["tag"] for k, v in N.items() if v["tag"] != "중립"})
st["stage_a"]["status_counts"] = pd.Series([r["status"] for r in st["items"]]).value_counts().to_dict()
json.dump(st, open(A.stage, "w"), ensure_ascii=False, indent=1, default=str)
print("뉴스 레이더:", json.dumps(st["news_rules"], ensure_ascii=False))
