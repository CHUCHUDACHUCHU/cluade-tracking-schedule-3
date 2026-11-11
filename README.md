# us-closes (v2)

미국 주식 약 3,100종목(S&P 500 + S&P 600 + 러셀 2000·미드캡, iShares가 막히면 나스닥 스크리너의 미국 기업 시총 3억~500억 달러로 대체) + 핵심 목록의 일봉을 매일 수집해 3년 이상 시계열로 쌓고, 펀더멘털·실적 추정치를 순환 수집한다.
GitHub Actions가 yfinance(Yahoo Finance)로 받는다.

## 일정 (`.github/workflows/closes.yml`)
- 화~토 06:41 KST: 전 종목 최근 1개월 받아 누적 → 요약 파일 커밋
- 일 06:41 KST: 위 + 종목 목록 갱신 + 주간 백업
- 실행은 외부 스케줄러(cron-job.org)가 06:41 KST에 `workflow_dispatch`(`scheduled=true`)로 호출. GitHub 예약(같은 시각)은 예비이며, 직전 미국 거래일 종가를 이미 받았으면 바로 끝남
- 수동: Actions → daily-closes → Run workflow (`refresh_universe` 체크 시 목록 다시 받기)

## 저장 위치
| 위치 | 파일 | 내용 |
|---|---|---|
| Release `data` | `ohlcv.parquet` | **전체 시계열**(long: date,ticker,open,high,low,close,adj_close,volume). 첫 실행 3년 백필, 이후 매일 누적·삭제 없음. git 이력에 쌓이지 않음 |
| Release `data` | `ohlcv_backup_YYYY-MM-DD.parquet` | 일요일 백업, 최근 4개 유지 |
| Release `data` | `fundamentals.parquet` | 종목당 최신 펀더멘털 1행(시총·P/E·분기 매출 성장·가속·이익률 추이·연도별 P/E·실적일·서프라이즈·EPS/매출 추정치·추정치 변화·상향/하향 수·목표가 변경) |
| Release `data` | `fund_history.parquet` | 받을 때마다 1행 누적(추정치 변화 추적), 일요일 백업 4개 |
| Release `data` | `financials.parquet` | 분기(최근 8)·연간(최근 4~5) 매출·영업이익·순이익·EPS·FCF |
| git `data/` | `screen.csv` | 전 종목 1행 요약 지표 + 섹터·섹터 ETF·섹터 대비 상대강도(②의 1차 거르기용) |
| git `data/` | `latest.json`, `core_prices.csv` | 핵심 목록(core.txt) 최신 지표·최근 400거래일(①②③ 호환) |
| git `data/` | `universe_latest.csv` | screen.csv 중 핵심 목록 밖 종목(현행 ② 지침 호환) |
| git `data/` | `fund_log.json` | 펀더멘털 수집 통계(받은 수·커버리지·중단 사유) |
| git `data/` | `collect_log.json` | 이번 실행 통계(받은 수·누락·백필·분할 재수집·검증 결과) |
| git `tickers/` | `core.txt` | 핵심 목록(직접 관리: SPY·섹터 ETF·KRW=X·대형주 후보) |
| git `tickers/` | `universe.csv` | 자동 목록(ticker,name,sector,sources) |

## 받는 법(리포터·분석)
```bash
curl -fsSL -o ohlcv.parquet https://github.com/CHUCHUDACHUCHU/cluade-tracking-schedule-3/releases/download/data/ohlcv.parquet
pip install pyarrow   # pandas.read_parquet용
```
```python
import pandas as pd
df = pd.read_parquet("ohlcv.parquet", filters=[("ticker", "in", ["ANET", "STRL"])])  # 필요한 종목만
```

## 펀더멘털 수집 방식(`scripts/fundamentals.py`, 매일 30분 예산)
- A(매일): core.txt + 기술적 사전 순위 상위 150 + 실적 발표 전후(지난 5일·다음 3일) 종목, 최대 220개
- B(순환): 나머지를 오래전에 받은 순으로 — 하루 약 150~250개, 약 3주에 한 바퀴
- 재무제표·실적 일정은 6일마다 또는 실적 전후에만 전체 갱신, 그 사이엔 가벼운 갱신(info·추정치 변화·목표가)
- 레이트 리밋 4회 연속이면 그날 중단(받은 값은 저장), 다음 날 이어서
- screen.csv에 핵심 칸(mcap_b, fwd_pe, pe_hist_median, rev_growth_q, rev_growth_prev_q, rev_accel_pp, op_margin_chg_pp, eps_growth_next_y_pct, eps_1y_chg_30d_pct, rev_1y_up30/down30, pt_raise_90d/pt_cut_90d, next_earnings, last_surprise_pct, target_upside_pct 등)과 pre_rank를 덧붙임

## 안전장치
- 목록: S&P 600(위키피디아), 러셀 2000(iShares IWM 보유 종목), 러셀 미드캡(iShares IWR). 일부 출처 실패 시 직전 목록을 보존, 새 목록이 직전의 80% 미만이면 직전 목록 사용.
- 다운로드: 100개씩, 덩어리마다 3회 재시도, 끝에 빠진 종목만 20개씩 재시도. 못 받은 날은 다음 날 1개월 창으로 자동 보충.
- 일봉 지연: 핵심 목록은 15분봉으로 그날 일봉 보정(장 마감 봉까지 있을 때만, `intraday_filled` 표시).
- 분할: 겹치는 날 종가가 2% 넘게 바뀐 종목은 기존 시작일부터 다시 받음.
- 업로드 전 검증: 행 수 1% 넘게 감소, 종목 수 2% 넘게 감소, 최신 날짜 후퇴, 중복, 0 이하 종가 → 업로드하지 않음(코드 2).
- 저장소가 있는데 내려받기 실패 → 중단(빈 저장소로 덮어쓰기 방지).
- 절반 이상 못 받음(Yahoo 차단 의심) → 받은 만큼 저장하고 실패로 표시(코드 1).

## 참고
- `close` = 분할 반영·배당 미반영 종가(진입·손절 비교용), `adj_close` = 배당까지 반영(수익률용).
- `KRW=X` = 원/달러 환율.
- 대형주(ETN·RTX 등)는 세 목록에 없으므로 `core.txt`에 둔다.

## 리포터용 계산 스크립트 (v5, 2026-11-10)
| 파일 | 쓰는 곳 | 하는 일 |
|---|---|---|
| `scripts/common.py` | ①② | 21개 섹터·테마 ETF 매핑(GICS + Yahoo 세부 업종), 추정치 신호·이익률 추이·사전 품질 점수 |
| `scripts/sector_stage.py` | ① | 섹터별 추정치 방향·리더 밀도를 데이터로 계산 + score_v3 섹터 모드 실행 |
| `scripts/stock_stage.py` | ② | 2,700종목 → 약 40개(A) → 채점 입력값 자동 생성·score_v3_2 채점(B), 웹 확인 대상(C) 표시 |
| `scripts/score_v3.py` | ① | 채점 v3 섹터 모드(Notion에서 이전, 코드 변경 없음) |
| `scripts/score_v3_2.py` | ② | 채점 v3.2 종목 모드(v3.1 + '기타' 바구니) |
리포터는 매 실행 raw.githubusercontent.com에서 이 파일들을 받아 실행한다. 규칙을 바꿀 때는 이 파일과 Notion 시스템 페이지 v5 절을 함께 고친다.
