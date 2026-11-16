"""학계·공개 데이터 받기(한 번 또는 가끔 수동 실행).

1) Open Source Asset Pricing(Chen & Zimmermann, openassetpricing.com) — 학계 예측 신호 212개
   - 신호 설명서(signal_doc) 전체
   - 애널리스트·실적 관련 신호(I/B/E/S 기반 등)의 포트폴리오 월 수익률(원 논문 방식 + 10분위)
   - 같은 신호들의 종목별 월간 값(permno 기준, 받을 수 있을 때만)
2) Hugging Face 공개 데이터 — Yahoo 추정치 추세·변경 인원 일별 스냅숏(edarchimbaud, MIT 라이선스)

결과: out/openap_doc.parquet, out/openap_port_*.parquet, out/openap_signals.parquet, out/hf_<이름>.parquet, out/open_research_log.json
"""
import json, os, re, sys, traceback, datetime as dt
import pandas as pd

OUT = sys.argv[1] if len(sys.argv) > 1 else "data_out"
os.makedirs(OUT, exist_ok=True)
log = {"started_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "steps": {}}

KEYWORDS = re.compile(r"IBES|I/B/E/S|analyst|forecast|recommend|revision|surprise|earnings announcement|estimate", re.I)
EXTRA = ["AnalystRevision", "REV6", "FEPS", "ChForecastAccrual", "AnalystValue", "ConsRecomm", "DownRecomm", "UpRecomm",
         "EarningsForecastDisp", "EarningsSurprise", "EarningsStreak", "RevenueSurprise", "sfe", "fgr5yrLag", "AnnouncementReturn",
         "EarnSupBig", "ShortInterest", "Recomm_ShortInterest", "MomRev", "Mom12m", "Mom6m", "STreversal", "IndMom"]

def step(name, fn):
    try:
        r = fn(); log["steps"][name] = {"ok": True, **(r or {})}; print("OK", name, r, flush=True)
    except Exception as e:
        log["steps"][name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:500]}
        print("FAIL", name, flush=True); traceback.print_exc()

# ---------- 1) Open Source Asset Pricing ----------
state = {}
def oap_init():
    import openassetpricing as oap
    state["o"] = oap.OpenAP()
    doc = state["o"].dl_signal_doc("pandas")
    doc.to_parquet(os.path.join(OUT, "openap_doc.parquet"), index=False)
    txt = doc.astype(str).agg(" ".join, axis=1)
    acr_col = next(c for c in doc.columns if c.lower() in ("acronym", "signalname", "signal"))
    picked = sorted(set(doc.loc[txt.str.contains(KEYWORDS), acr_col].astype(str)) | (set(EXTRA) & set(doc[acr_col].astype(str))))
    state["signals"] = picked
    return {"n_doc": int(len(doc)), "n_signals": len(picked), "signals": picked}
step("openap_doc", oap_init)

def oap_port(kind):
    def f():
        p = state["o"].dl_port(kind, "pandas", state["signals"])
        p.to_parquet(os.path.join(OUT, f"openap_port_{kind}.parquet"), index=False)
        dcol = next((c for c in p.columns if c.lower() in ("date", "yyyymm")), None)
        return {"rows": int(len(p)), "date_min": str(p[dcol].min()) if dcol else None, "date_max": str(p[dcol].max()) if dcol else None,
                "columns": list(map(str, p.columns))[:12]}
    return f
if "signals" in state:
    names = [str(x) for x in state["o"].name_id_map.filter(state["o"].name_id_map["full_name"].str.contains("Portfolios/"))["download_name"].to_list()]
    log["port_names"] = names
    want = [k for k in ("op", "deciles_ew", "deciles_vw", "quintiles_ew", "quintiles_vw") if k in names] or names[:3]
    for kind in want:
        step(f"openap_port_{kind}", oap_port(kind))
    def oap_sig():
        o = state["o"]
        fn = getattr(o, "dl_signal", None)
        if fn is None:
            return {"skipped": "dl_signal 없음(패키지 버전)"}
        sig = [x for x in state["signals"] if x not in ("Price", "Size", "STreversal")]   # 이 셋은 CRSP(WRDS) 필요
        for _ in range(3):
            try:
                s = fn("pandas", sig); break
            except ValueError as e:
                miss = re.findall(r"'([^']+)'", str(e))
                if not miss: raise
                sig = [x for x in sig if x not in miss]
        s.to_parquet(os.path.join(OUT, "openap_signals.parquet"), index=False)
        return {"rows": int(len(s)), "columns": list(map(str, s.columns))[:15]}
    step("openap_signals", oap_sig)

# ---------- 2) Hugging Face 공개 스냅숏 ----------
HF = ["edarchimbaud/eps-trend-stocks", "edarchimbaud/eps-revisions-stocks", "edarchimbaud/earnings-estimate-stocks",
      "edarchimbaud/eps-trend-sp500", "edarchimbaud/eps-revisions-sp500", "edarchimbaud/earnings-estimate-sp500"]
def hf_get(repo):
    def f():
        from huggingface_hub import snapshot_download
        path = snapshot_download(repo_id=repo, repo_type="dataset", allow_patterns=["*.parquet", "*.csv", "README.md"])
        files = [os.path.join(r, x) for r, _, fs in os.walk(path) for x in fs if x.endswith((".parquet", ".csv"))]
        if not files:
            return {"files": 0}
        df = pd.concat([pd.read_parquet(x) if x.endswith(".parquet") else pd.read_csv(x) for x in files], ignore_index=True)
        name = repo.split("/")[1]
        df.to_parquet(os.path.join(OUT, f"hf_{name}.parquet"), index=False)
        dcol = "date" if "date" in df.columns else None
        return {"files": len(files), "rows": int(len(df)), "tickers": int(df["symbol"].nunique()) if "symbol" in df.columns else None,
                "date_min": str(df[dcol].min()) if dcol else None, "date_max": str(df[dcol].max()) if dcol else None,
                "n_dates": int(df[dcol].nunique()) if dcol else None}
    return f
for repo in HF:
    step("hf_" + repo.split("/")[1], hf_get(repo))

log["finished_utc"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
json.dump(log, open(os.path.join(OUT, "open_research_log.json"), "w"), ensure_ascii=False, indent=1)
print(json.dumps(log, ensure_ascii=False, indent=1)[:4000])
