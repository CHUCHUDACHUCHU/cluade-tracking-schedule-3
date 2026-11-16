"""SEC 내부자 매매(Form 4) 수집기.

SEC가 분기마다 공개하는 'Insider Transactions Data Sets'(YYYYqN_form345.zip)를 받아
수집 대상 종목(data/screen.csv의 ticker)의 공개시장 매수(P)·매도(S)만 골라 insider.parquet로 저장한다.

- 이미 받은 분기는 건너뛴다(state: insider_quarters.json). 최근 2개 분기는 정정·늦은 공개 때문에 매번 다시 받는다.
- SEC는 User-Agent에 이름·연락처를 요구한다: 저장소 Secret SEC_USER_AGENT (예: "Hong Gildong hong@example.com").
- SEC 요청 한도(초당 10회)보다 훨씬 느리게 받는다(분기 파일 1개당 1회 + 1초 대기).

사용: python scripts/insider.py --out data_out --start 2019q1
"""
import argparse, io, json, os, sys, time, zipfile, datetime as dt
import pandas as pd
import requests

HOSTS = ("structureddata", "datastandardsinnovation")
URL = "https://www.sec.gov/files/{host}/data/insider-transactions-data-sets/{q}_form345.zip"
KEEP_CODES = {"P", "S"}          # P = 공개시장 매수, S = 공개시장 매도 (옵션 행사·증여·보상은 제외)


def quarters(start: str, today: dt.date):
    y, q = int(start[:4]), int(start[-1])
    while (y, q) <= (today.year, (today.month - 1) // 3 + 1):
        yield f"{y}q{q}"
        q += 1
        if q == 5:
            y, q = y + 1, 1


def fetch_zip(q: str, ua: str):
    for host in HOSTS:
        r = requests.get(URL.format(host=host, q=q), headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=180)
        time.sleep(1.0)
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content
    return None


def read_tsv(zf: zipfile.ZipFile, name: str, cols):
    member = next((n for n in zf.namelist() if n.upper().endswith(name + ".TSV")), None)
    if member is None:
        raise KeyError(f"{name}.tsv 없음: {zf.namelist()}")
    with zf.open(member) as f:
        df = pd.read_csv(f, sep="\t", dtype=str, usecols=lambda c: c in cols, quoting=3, on_bad_lines="skip", encoding="utf-8")
    return df


def parse(content: bytes, tickers: set, q: str) -> pd.DataFrame:
    zf = zipfile.ZipFile(io.BytesIO(content))
    sub = read_tsv(zf, "SUBMISSION", {"ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERTRADINGSYMBOL"})
    own = read_tsv(zf, "REPORTINGOWNER", {"ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"})
    tr = read_tsv(zf, "NONDERIV_TRANS", {"ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES", "TRANS_PRICEPERSHARE",
                                         "TRANS_ACQUIRED_DISP_CD", "SHRS_OWND_FOLWNG_TRANS", "DIRECT_INDIRECT_OWNERSHIP", "EQUITY_SWAP_INVOLVED"})
    sub["ticker"] = sub.ISSUERTRADINGSYMBOL.fillna("").str.upper().str.strip().str.replace(".", "-", regex=False)
    sub = sub[sub.DOCUMENT_TYPE.isin(["4", "4/A"]) & sub.ticker.isin(tickers)]
    tr = tr[tr.TRANS_CODE.isin(KEEP_CODES)]
    # 한 공시에 보고자가 여럿이면(공동 보고) 첫 보고자를 대표로 쓰고 인원수를 남긴다
    own = own.groupby("ACCESSION_NUMBER").agg(owner_cik=("RPTOWNERCIK", "first"), owner_name=("RPTOWNERNAME", "first"),
                                               relationship=("RPTOWNER_RELATIONSHIP", "first"), title=("RPTOWNER_TITLE", "first"),
                                               n_owners=("RPTOWNERCIK", "nunique")).reset_index()
    df = tr.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    if df.empty:
        return pd.DataFrame()
    to_date = lambda s: pd.to_datetime(s, format="%d-%b-%Y", errors="coerce")
    num = lambda s: pd.to_numeric(s, errors="coerce")
    out = pd.DataFrame({
        "ticker": df.ticker, "issuer_cik": df.ISSUERCIK, "accession": df.ACCESSION_NUMBER, "doc_type": df.DOCUMENT_TYPE,
        "filing_date": to_date(df.FILING_DATE), "trans_date": to_date(df.TRANS_DATE), "code": df.TRANS_CODE,
        "acq_disp": df.TRANS_ACQUIRED_DISP_CD, "shares": num(df.TRANS_SHARES), "price": num(df.TRANS_PRICEPERSHARE),
        "shares_after": num(df.SHRS_OWND_FOLWNG_TRANS), "direct": df.DIRECT_INDIRECT_OWNERSHIP, "swap": df.EQUITY_SWAP_INVOLVED,
        "owner_cik": df.owner_cik, "owner_name": df.owner_name, "relationship": df.relationship, "title": df.title,
        "n_owners": df.n_owners, "src_quarter": q})
    out["value"] = out.shares * out.price
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data_out"); ap.add_argument("--start", default="2019q1")
    ap.add_argument("--screen", default="data/screen.csv"); ap.add_argument("--existing", default="")
    ap.add_argument("--state", default=""); ap.add_argument("--refresh-last", type=int, default=2)
    a = ap.parse_args()
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if "@" not in ua:
        sys.exit("SEC_USER_AGENT(이름 + 이메일) Secret이 필요합니다. 예: 'Hong Gildong hong@example.com'")
    os.makedirs(a.out, exist_ok=True)
    tickers = set(pd.read_csv(a.screen, usecols=["ticker"]).ticker.astype(str).str.upper())
    state = json.load(open(a.state)) if a.state and os.path.exists(a.state) else {"done": []}
    old = pd.read_parquet(a.existing) if a.existing and os.path.exists(a.existing) else pd.DataFrame()
    qs = list(quarters(a.start, dt.date.today()))
    redo = set(qs[-a.refresh_last:]) if a.refresh_last else set()
    frames, log = [], {"fetched": [], "missing": [], "rows": {}}
    for q in qs:
        if q in state["done"] and q not in redo:
            continue
        content = fetch_zip(q, ua)
        if content is None:
            log["missing"].append(q); print("없음(아직 미공개일 수 있음):", q, flush=True); continue
        df = parse(content, tickers, q)
        frames.append(df); log["fetched"].append(q); log["rows"][q] = int(len(df))
        if q not in state["done"]:
            state["done"].append(q)
        print(q, len(df), "행", flush=True)
    if frames:
        new = pd.concat(frames, ignore_index=True)
        if not old.empty:
            old = old[~old.src_quarter.isin(set(new.src_quarter))]
        allx = pd.concat([old, new], ignore_index=True)
        allx = allx.drop_duplicates(subset=["accession", "trans_date", "code", "shares", "price", "owner_cik"])
        allx = allx.sort_values(["filing_date", "ticker"]).reset_index(drop=True)
    else:
        allx = old
    allx.to_parquet(os.path.join(a.out, "insider.parquet"), index=False)
    state["done"] = sorted(set(state["done"]))
    json.dump(state, open(os.path.join(a.out, "insider_quarters.json"), "w"), indent=1)
    log.update(total_rows=int(len(allx)), updated_utc=dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"))
    json.dump(log, open(os.path.join(a.out, "insider_log.json"), "w"), ensure_ascii=False, indent=1)
    print("완료:", log)


if __name__ == "__main__":
    main()
