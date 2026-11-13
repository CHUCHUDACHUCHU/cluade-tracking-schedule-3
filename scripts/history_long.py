"""history_long.py — 장기(2004~) 일봉 1회 수집 → store/ohlcv_long.parquet
요인 검증(walk-forward)을 2008·2020·2022 같은 하락장까지 포함해 돌리기 위한 연구용 저장소.
매일 수집(ohlcv.parquet)과 별개. 수동 실행: Actions → history-long → Run workflow.
주의: 지금 목록(universe.csv + core.txt) 기준이라 상장폐지 종목이 빠진다(생존 편향) — 결과 해석 시 감안.
사용: python scripts/history_long.py --out store/ohlcv_long.parquet --start 2004-01-01
"""
import argparse, sys, time, json
from pathlib import Path
import pandas as pd
sys.path.insert(0, str(Path(__file__).parent))
import collect  # download(), COLS 재사용

ap = argparse.ArgumentParser()
ap.add_argument("--out", default="store/ohlcv_long.parquet")
ap.add_argument("--start", default="2004-01-01")
ap.add_argument("--chunk", type=int, default=80)
ap.add_argument("--budget-min", type=float, default=150)
A = ap.parse_args()
t0 = time.time()
root = Path(__file__).resolve().parents[1]
uni = pd.read_csv(root / "tickers" / "universe.csv")["ticker"].dropna().astype(str).tolist()
core = [l.split("#")[0].strip() for l in open(root / "tickers" / "core.txt", encoding="utf-8")]
core = [c for c in core if c and not c.startswith("^") and "=" not in c]
tick = sorted(set(uni) | set(core))
out = Path(A.out); out.parent.mkdir(parents=True, exist_ok=True)
have = set()
frames = []
if out.exists():  # 이어 받기
    old = pd.read_parquet(out); frames.append(old); have = set(old["ticker"].unique())
todo = [t for t in tick if t not in have]
print(f"대상 {len(tick)} / 이미 {len(have)} / 받을 {len(todo)}", flush=True)
for i in range(0, len(todo), A.chunk):
    if (time.time() - t0) / 60 > A.budget_min:
        print("시간 예산 초과 — 저장 후 중단(다시 실행하면 이어 받음)"); break
    part = todo[i:i + A.chunk]
    d = collect.download(part, start=A.start, chunk=A.chunk)
    if len(d):
        for c in ("open", "high", "low", "close", "adj_close"):
            d[c] = d[c].astype("float32")
        d["volume"] = d["volume"].fillna(0).astype("int64")
        frames.append(d)
    print(f"{i + len(part)}/{len(todo)} 받음 {d['ticker'].nunique() if len(d) else 0}", flush=True)
    if (i // A.chunk) % 10 == 9:  # 중간 저장
        pd.concat(frames, ignore_index=True).to_parquet(out, index=False, compression="zstd")
df = pd.concat(frames, ignore_index=True).drop_duplicates(["date", "ticker"], keep="last")
df.to_parquet(out, index=False, compression="zstd")
stat = dict(tickers=int(df.ticker.nunique()), rows=len(df), first=str(df.date.min()), last=str(df.date.max()),
            missing=len(set(tick) - set(df.ticker.unique())), minutes=round((time.time() - t0) / 60, 1))
print(json.dumps(stat, ensure_ascii=False))
Path("data").mkdir(exist_ok=True); json.dump(stat, open("data/history_long_log.json", "w"), ensure_ascii=False, indent=1)
