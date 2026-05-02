"""
가치투자 재무 데이터 수집기
사용법: python test.py
회사명, 티커, 별칭 등 무엇이든 입력 → GPT가 종목 판별 → 분기별 재무 데이터 CSV 저장
"""

import os
import sys
import json
from datetime import datetime, timedelta

import pandas as pd
from dotenv import load_dotenv

load_dotenv()

OUTPUT_DIR = "data"
N_PERIODS  = 8  # 최근 몇 분기

COL_ORDER = [
    "이름", "코드", "시장", "기간",
    "종가", "시가총액",
    "매출액", "영업이익", "당기순이익",
    "자산총계", "부채총계", "자본총계",
    "EPS", "BPS", "PER", "PBR", "ROE", "부채비율", "영업이익률",
]

# ─────────────────────────────────────────────────────
# GPT로 종목 판별
# ─────────────────────────────────────────────────────

_RESOLVE_PROMPT = """\
You are a stock market expert. Given any input (company name in any language, \
ticker, brand name, nickname, etc.), identify the stock and return JSON only.

Rules:
- market: "KR" for Korean-listed stocks, "US" for US-listed stocks
- name: official company name in English
- For KR stocks → also return:
    dart_name: exact name used in Korea's DART system (Korean)
    code: 6-digit KRX stock code (string, e.g. "005930")
- For US stocks → also return:
    ticker: exchange ticker symbol (e.g. "AAPL")

Examples:
  "애플"       → {"market":"US","name":"Apple Inc","ticker":"AAPL"}
  "삼성"       → {"market":"KR","name":"Samsung Electronics","dart_name":"삼성전자","code":"005930"}
  "엔비디아"   → {"market":"US","name":"NVIDIA Corporation","ticker":"NVDA"}
  "하이닉스"   → {"market":"KR","name":"SK Hynix","dart_name":"SK하이닉스","code":"000660"}
  "Tesla"      → {"market":"US","name":"Tesla Inc","ticker":"TSLA"}
  "MSFT"       → {"market":"US","name":"Microsoft Corporation","ticker":"MSFT"}

Return ONLY valid JSON with no extra text.\
"""


def resolve_company(query: str) -> dict:
    """GPT-4o-mini로 회사명/티커/별칭 → 종목 정보(market, code/ticker 등) 변환"""
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise EnvironmentError(".env에 OPENAI_API_KEY가 설정되지 않았습니다.")

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": _RESOLVE_PROMPT},
            {"role": "user",   "content": query},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )

    result = json.loads(resp.choices[0].message.content)

    # 필수 키 검증
    if result.get("market") == "KR" and not result.get("code"):
        raise ValueError(f"GPT가 '{query}'의 종목코드를 찾지 못했습니다: {result}")
    if result.get("market") == "US" and not result.get("ticker"):
        raise ValueError(f"GPT가 '{query}'의 티커를 찾지 못했습니다: {result}")

    return result


# ─────────────────────────────────────────────────────
# 한국 주식 (DART + FinanceDataReader)
# ─────────────────────────────────────────────────────

# 분기보고서 코드
_KR_REPORTS = [
    ("11014", "3분기"),
    ("11012", "2분기"),
    ("11013", "1분기"),
]


def _parse_amount(value) -> int | None:
    try:
        return int(str(value).replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _extract_kr_fs(fs) -> dict:
    """DART finstate_all DataFrame → 주요 재무 항목 dict"""
    data = {}

    for item in ["자산총계", "부채총계", "자본총계", "매출액"]:
        row = fs[fs["account_nm"] == item]
        if not row.empty:
            data[item] = _parse_amount(row["thstrm_amount"].iloc[0])

    # 영업이익: 분기보고서는 "영업이익(손실)" 등으로 변형될 수 있음
    row = fs[fs["account_nm"].str.contains("영업이익", na=False)]
    if not row.empty:
        data["영업이익"] = _parse_amount(row["thstrm_amount"].iloc[0])

    # 순이익: 분기→분기순이익, 반기→반기순이익, 연간→당기순이익
    row = fs[fs["account_nm"].str.contains("당기순이익|분기순이익|반기순이익", na=False)]
    if not row.empty:
        data["당기순이익"] = _parse_amount(row["thstrm_amount"].iloc[0])

    return data


def _get_shares_from_eps(fs, net_income: int) -> int | None:
    """재무제표의 기본주당이익(EPS)으로 발행주식수 역산"""
    if not net_income:
        return None
    row = fs[fs["account_nm"].str.contains("기본주당이익|기본주당순이익", na=False)]
    if row.empty:
        return None
    eps = _parse_amount(row["thstrm_amount"].iloc[0])
    if eps and eps != 0:
        return abs(net_income // eps)
    return None


def collect_kr(dart_name: str, code: str) -> pd.DataFrame:
    """
    dart_name : DART API 검색에 사용할 공식 한국어 회사명
    code      : 6자리 KRX 종목코드
    """
    import OpenDartReader
    import FinanceDataReader as fdr

    dart = OpenDartReader(os.getenv("DART_API_KEY"))
    name = dart_name
    print(f"  종목코드: {code}")

    # 현재 주가
    now   = datetime.now()
    end   = now.strftime("%Y-%m-%d")
    start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    try:
        df_p  = fdr.DataReader(code, start, end)
        price = float(df_p["Close"].iloc[-1]) if not df_p.empty else None
    except Exception:
        price = None

    rows  = []
    years = [now.year - i for i in range(1, 5)]

    for year in years:
        for reprt_code, quarter in _KR_REPORTS:
            if len(rows) >= N_PERIODS:
                break
            try:
                fs = dart.finstate_all(name, year, reprt_code=reprt_code)
                if fs is None or fs.empty:
                    continue

                fin = _extract_kr_fs(fs)
                if not fin:
                    continue

                net_income = fin.get("당기순이익") or 0
                equity     = fin.get("자본총계") or 0
                debt       = fin.get("부채총계") or 0
                revenue    = fin.get("매출액") or 0
                operating  = fin.get("영업이익") or 0
                shares     = _get_shares_from_eps(fs, net_income)

                row = {
                    "이름":     name,
                    "코드":     code,
                    "시장":     "KR",
                    "기간":     f"{year}년 {quarter}",
                    "종가":     price,
                    "시가총액": price * shares if (price and shares) else None,
                    **fin,
                    "ROE":      net_income / equity * 100 if equity else None,
                    "부채비율": debt / equity * 100 if equity else None,
                    "영업이익률": operating / revenue * 100 if revenue else None,
                }

                if shares and price:
                    eps = net_income / shares
                    bps = equity / shares
                    row["EPS"] = eps
                    row["BPS"] = bps
                    row["PER"] = price / eps if eps > 0 else None
                    row["PBR"] = price / bps if bps > 0 else None

                rows.append(row)
                print(f"    ✅ {year}년 {quarter}")

            except Exception as e:
                print(f"    ⚠️  {year}년 {quarter}: {e}")
                continue

        if len(rows) >= N_PERIODS:
            break

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────
# 미국 주식 (yfinance)
# ─────────────────────────────────────────────────────

def _quarter_label(dt) -> tuple[int, str]:
    m = dt.month
    if m <= 3:
        return dt.year - 1, "4분기"
    elif m <= 6:
        return dt.year, "1분기"
    elif m <= 9:
        return dt.year, "2분기"
    else:
        return dt.year, "3분기"


def collect_us(ticker: str) -> pd.DataFrame:
    import yfinance as yf

    ticker = ticker.upper()
    stock  = yf.Ticker(ticker)
    info   = stock.info
    fin    = stock.quarterly_financials
    bs     = stock.quarterly_balance_sheet

    if fin is None or fin.empty:
        raise ValueError(f"yfinance에서 '{ticker}' 데이터를 찾을 수 없습니다.")

    price  = info.get("currentPrice")
    shares = info.get("sharesOutstanding")

    rows = []
    for col in fin.columns[:N_PERIODS]:
        year, quarter = _quarter_label(col)

        row = {
            "이름":     ticker,
            "코드":     ticker,
            "시장":     "US",
            "기간":     f"{year}년 {quarter}",
            "종가":     price,
            "시가총액": info.get("marketCap"),
        }

        for label, key in [
            ("매출액",    "Total Revenue"),
            ("영업이익",  "Operating Income"),
            ("당기순이익","Net Income"),
        ]:
            try:
                row[label] = float(fin.loc[key, col])
            except KeyError:
                row[label] = None

        if bs is not None and not bs.empty and col in bs.columns:
            for label, key in [
                ("자산총계", "Total Assets"),
                ("부채총계", "Total Liabilities Net Minority Interest"),
            ]:
                try:
                    row[label] = float(bs.loc[key, col])
                except KeyError:
                    row[label] = None
            if row.get("자산총계") and row.get("부채총계"):
                row["자본총계"] = row["자산총계"] - row["부채총계"]

        equity    = row.get("자본총계") or 0
        debt      = row.get("부채총계") or 0
        net_income = row.get("당기순이익")
        revenue   = row.get("매출액")
        operating = row.get("영업이익")

        row["ROE"]       = net_income / equity * 100 if (net_income and equity) else None
        row["부채비율"]   = debt / equity * 100 if equity else None
        row["영업이익률"] = operating / revenue * 100 if (operating and revenue) else None

        if shares:
            row["EPS"] = net_income / shares if net_income is not None else None
            row["BPS"] = equity / shares if equity else None

        pbr = info.get("priceToBook")
        row["PER"] = info.get("trailingPE")
        row["PBR"] = pbr if pbr else (
            price / row["BPS"] if (row.get("BPS") and row["BPS"] > 0 and price) else None
        )

        rows.append(row)
        print(f"    ✅ {year}년 {quarter}")

    return pd.DataFrame(rows)


# ─────────────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────────────

def main():
    query = input("종목 입력 (예: 애플 / 삼성 / NVDA / 하이닉스): ").strip()
    if not query:
        return

    # ── GPT로 종목 판별 ──────────────────────────
    print("\n🤖 종목 판별 중...")
    try:
        info = resolve_company(query)
    except (EnvironmentError, ValueError) as e:
        print(f"오류: {e}")
        sys.exit(1)

    market = info["market"]
    name   = info["name"]
    print(f"  → {name}  ({market})")

    # ── 데이터 수집 ──────────────────────────────
    try:
        if market == "KR":
            print(f"\n🇰🇷 한국 주식 수집: {info['dart_name']} ({info['code']})")
            df = collect_kr(info["dart_name"], info["code"])
        else:
            print(f"\n🇺🇸 미국 주식 수집: {info['ticker']}")
            df = collect_us(info["ticker"])
    except ValueError as e:
        print(f"\n오류: {e}")
        sys.exit(1)

    if df.empty:
        print("수집된 데이터가 없습니다.")
        return

    # ── 컬럼 정렬 & CSV 저장 ─────────────────────
    cols = [c for c in COL_ORDER if c in df.columns]
    df   = df[cols]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_name = info.get("dart_name") or info.get("ticker", query)
    path = os.path.join(OUTPUT_DIR, f"{safe_name}_분기.csv")
    df.to_csv(path, index=False, encoding="utf-8-sig")

    print(f"\n💾 저장 완료: {path}  ({len(df)}개 분기)")


if __name__ == "__main__":
    main()
