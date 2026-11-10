"""①·② 단계 스크립트 공통 정의 (2026-11-10)."""
from __future__ import annotations

import pandas as pd

SECTORS = [  # (섹터명, ETF) — ① DB 21행과 같은 순서
    ("기술", "XLK"), ("커뮤니케이션", "XLC"), ("경기소비재", "XLY"), ("필수소비재", "XLP"), ("에너지", "XLE"),
    ("금융", "XLF"), ("헬스케어", "XLV"), ("산업재", "XLI"), ("소재", "XLB"), ("리츠", "XLRE"), ("유틸리티", "XLU"),
    ("반도체", "SMH"), ("소프트웨어", "IGV"), ("전력망", "GRID"), ("인프라 시공", "PAVE"), ("방산·항공우주", "ITA"),
    ("바이오", "XBI"), ("지역은행", "KRE"), ("주택건설", "XHB"), ("원자력·우라늄", "URA"), ("통신서비스", "IYZ"),
]
GICS_ETFS = {"XLK", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLU"}
NON_STOCK = {e for _, e in SECTORS} | {"SPY", "KRW=X"}

# 테마 ETF ← Yahoo 세부 업종(yf_industry). 한 종목이 GICS 섹터 ETF와 테마 ETF 둘 다에 속할 수 있다.
THEME_INDUSTRY = {
    "SMH": {"Semiconductors", "Semiconductor Equipment & Materials"},
    "IGV": {"Software - Application", "Software - Infrastructure"},
    "GRID": {"Electrical Equipment & Parts", "Utilities - Regulated Electric", "Utilities - Renewable"},
    "PAVE": {"Engineering & Construction", "Building Materials", "Infrastructure Operations", "Railroads"},
    "ITA": {"Aerospace & Defense"},
    "XBI": {"Biotechnology"},
    "KRE": {"Banks - Regional"},
    "XHB": {"Residential Construction", "Building Products & Equipment", "Home Improvement Retail",
            "Furnishings, Fixtures & Appliances"},
    "URA": {"Uranium", "Utilities - Independent Power Producers"},
    "IYZ": {"Telecom Services"},
}
# 테마 ETF의 상위 GICS 섹터(테마 소속은 펀더멘털 수집 후에만 알 수 있어, 커버리지는 상위 섹터 기준으로 잰다)
THEME_PARENT = {"SMH": ["XLK"], "IGV": ["XLK"], "GRID": ["XLI", "XLU"], "PAVE": ["XLI", "XLB"], "ITA": ["XLI"],
                "XBI": ["XLV"], "KRE": ["XLF"], "XHB": ["XLY", "XLI"], "URA": ["XLE", "XLU"], "IYZ": ["XLC"]}
# 경기순환 업종(이익이 원자재·제품 가격에 좌우) — 정상화 P/E 관문 대상
CYCLICAL_INDUSTRY = {
    "Oil & Gas E&P", "Oil & Gas Integrated", "Oil & Gas Refining & Marketing", "Oil & Gas Equipment & Services",
    "Oil & Gas Drilling", "Steel", "Copper", "Aluminum", "Other Industrial Metals & Mining", "Gold", "Silver",
    "Coking Coal", "Thermal Coal", "Uranium", "Marine Shipping", "Chemicals", "Specialty Chemicals",
    "Agricultural Inputs", "Lumber & Wood Production", "Paper & Paper Products", "Auto Manufacturers",
}
CYCLICAL_TICKERS = {"MU", "WDC", "STX", "SNDK"}   # 메모리·저장장치(업종명이 반도체라 따로)


def themes_of(industry) -> set[str]:
    s = str(industry or "")
    return {etf for etf, inds in THEME_INDUSTRY.items() if s in inds}


def sector_members(screen: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """ETF별 소속 종목(주식만)."""
    st = screen[~screen["ticker"].isin(NON_STOCK)].copy()
    st["themes"] = st["yf_industry"].map(themes_of) if "yf_industry" in st else [set()] * len(st)
    out = {}
    for _, etf in SECTORS:
        if etf in GICS_ETFS:
            out[etf] = st[st["sector_etf"] == etf]
        else:
            out[etf] = st[st["themes"].map(lambda s: etf in s)]
    return out


def liquid(df: pd.DataFrame) -> pd.DataFrame:
    return df[(df["avg_dollar_vol_20d"].fillna(0) >= 2e7) & (df["close"].fillna(0) >= 5)
              & (df["history_days"].fillna(0) >= 120) & (~df["stale"].fillna(False).astype(bool))]


def est_up_data(r) -> int | None:
    """이익 추정치 신호(-3~3)를 데이터로: 30일 내년 EPS 추정치 변화, 30일 상향−하향 애널리스트 수, 90일 목표가 상향−하향."""
    sig, have = 0, 0
    v = r.get("eps_1y_chg_30d_pct")
    if pd.notna(v):
        have += 1
        sig += 1 if v > 2 else (-1 if v < -2 else 0)
    up, dn = r.get("rev_1y_up30"), r.get("rev_1y_down30")
    if pd.notna(up) or pd.notna(dn):
        have += 1
        d = (0 if pd.isna(up) else up) - (0 if pd.isna(dn) else dn)
        sig += 1 if d >= 2 else (-1 if d <= -2 else 0)
    pu, pc = r.get("pt_raise_90d"), r.get("pt_cut_90d")
    if pd.notna(pu) or pd.notna(pc):
        have += 1
        d = (0 if pd.isna(pu) else pu) - (0 if pd.isna(pc) else pc)
        sig += 1 if d >= 2 else (-1 if d <= -2 else 0)
    return sig if have else None


def margin_trend_data(r) -> int | None:
    v = r.get("op_margin_chg_pp")
    if pd.isna(v):
        return None
    return 1 if v >= 1 else (-1 if v <= -1 else 0)


def quality_pre(r, guide="유지", guide_beat=False) -> tuple[float, str]:
    """score_v3_2의 품질 점수와 같은 공식(가이던스는 기본 '유지')."""
    g = r.get("rev_growth_q")
    g = 0 if pd.isna(g) else g
    q = {"rev": 35 * min(max(g / 60, 0), 1)}
    q["guide"] = 0 if guide == "하향" else (20 if (guide == "상향" and guide_beat) else (15 if guide == "상향" else 8))
    e = est_up_data(r)
    q["est"] = (max(-3, min(3, e or 0)) + 3) / 6 * 20
    qmax, miss = 75, []
    gp = r.get("rev_growth_prev_q")
    if pd.notna(gp):
        q["accel"] = 10 * min(max((g - gp) / 10, 0), 1); qmax += 10
    else:
        miss.append("가속")
    m = margin_trend_data(r)
    if m is not None:
        q["margin"] = {-1: 0, 0: 7.5, 1: 15}[m]; qmax += 15
    else:
        miss.append("수익성")
    return round(sum(q.values()) / qmax * 100, 1), ("환산(미입력: %s)" % "·".join(miss)) if miss else "전 항목"
