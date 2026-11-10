# us-closes

미국 주식·ETF 일별 종가 수집기. GitHub Actions가 매일 yfinance로 받아 `data/`에 커밋한다.

## 설치 (한 번만)
1. GitHub에 **공개** 저장소 `us-closes` 생성 → 이 폴더 내용을 그대로 push
2. 저장소 Settings → Actions → General → Workflow permissions: **Read and write** 선택
3. Actions 탭 → `daily-closes` → **Run workflow** (universe 체크) 로 첫 실행 → `data/` 파일 생성 확인

## 일정
- 화~토 06:41 KST: 핵심 목록(`tickers/core.txt`) → `core_prices.csv`, `latest.json`
- 일 06:41 KST: 위 + 넓은 목록 → `universe_latest.csv` (기본 S&P 600 위키 목록, `tickers/universe.txt`가 있으면 그 목록)

## 파일
| 파일 | 내용 |
|---|---|
| `data/latest.json` | `meta`(as_of 기준일, missing_today, stale) + 티커별 최신 지표: close·prev_close·high·low·sma20/50/200·vs_sma20_pct·sma20_rising·rsi14·atr20_pct·ret_5d/21d/63d/126d·ret_6m_ex1m·from_high_pct·avg_dollar_vol_20d·history_days |
| `data/core_prices.csv` | 최근 400거래일 OHLCV (date,ticker,open,high,low,close,adj_close,volume) |
| `data/universe_latest.csv` | 넓은 목록 1행/티커 요약 지표 (일요일 재선정용) |

- `close`는 미조정 종가(손절가·진입가 비교용), 수익률(`ret_*`)은 배당·분할 조정 종가 기준.
- `KRW=X` = 원/달러 환율.
- ② 후보 풀이 바뀌면 `tickers/core.txt`를 고친다(다음 실행부터 반영, 과거 이력은 2년 치 자동 수집).

## 리포터 지침에 넣을 문구 (①②③ 공통)
```
[종가 — GitHub CSV 우선, 2026-11-xx부터]
- 종가·지표는 웹 검색 대신 아래 파일을 bash curl로 받아 python으로 읽는다(출력은 필요한 값만 print).
  · 최신 지표: https://raw.githubusercontent.com/<계정>/us-closes/main/data/latest.json
  · 일별 이력: https://raw.githubusercontent.com/<계정>/us-closes/main/data/core_prices.csv
  · 넓은 목록(일요일 ②): https://raw.githubusercontent.com/<계정>/us-closes/main/data/universe_latest.csv
- meta.as_of가 미국 기준일과 같으면 그 값을 '확정 종가'로 쓴다(잠정 아님, 교차확인 완료로 본다).
- meta.as_of가 기준일보다 이르거나 파일을 못 받으면 직전 방식(웹 확인, 종목당 1회)으로 대체하고 리포트에 "종가 CSV 미갱신"을 쓴다.
- 티커가 missing_today·stale에 있으면 그 종목만 "확인 불가(최신: MM/DD)".
- 환율은 tickers의 "KRW=X" close를 쓴다(출처: Yahoo Finance).
- ⑤·⑥ Notion 가격 DB에는 더 이상 쓰지 않는다(과거 기록 보관용).
```
