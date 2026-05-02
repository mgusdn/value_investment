import os
import time
import csv
from datetime import datetime
from dotenv import load_dotenv
import OpenDartReader

load_dotenv()
dart = OpenDartReader(os.getenv("DART_API_KEY"))

# ============================================================
# 설정
# ============================================================

STOCKS = [
    ("삼성전자", "005930"),
    ("SK하이닉스", "000660"),
]

# 수집할 보고서 종류
REPORT_TYPES = [
    ("11014", "3분기",  "quarterly"),
    ("11012", "2분기",  "quarterly"),
    ("11013", "1분기",  "quarterly"),
    ("11011", "연간",   "annual"),
]

YEARS = [2025, 2024, 2023]

# ============================================================
# 유틸
# ============================================================

def parse_amount(value):
    try:
        return int(str(value).replace(",", "").strip())
    except:
        return None


def get_value(fs, account_nm, sj_nm=None, col="thstrm_amount"):
    """account_nm 정확히 일치 + 선택적으로 sj_nm 필터"""
    mask = fs["account_nm"] == account_nm
    if sj_nm:
        mask &= fs["sj_nm"] == sj_nm
    row = fs[mask]
    if row.empty:
        return None
    return parse_amount(row[col].iloc[0])


def get_net_income(fs, period_type):
    """보고서 종류별로 다른 순이익 항목명 처리"""
    if period_type == "annual":
        keywords = ["당기순이익", "분기순이익", "반기순이익"]
        sj = "손익계산서"
    elif period_type == "quarterly":
        keywords = ["분기순이익", "반기순이익", "당기순이익"]
        sj = "포괄손익계산서"
    else:
        keywords = ["당기순이익"]
        sj = "손익계산서"

    for kw in keywords:
        val = get_value(fs, kw, sj_nm=sj)
        if val and val != 0:
            return val
    return None


# ============================================================
# 수집
# ============================================================

def collect(name, code, year, reprt_code, period_label, period_type):
    print(f"  수집 중: {name} {year}년 {period_label}...")

    try:
        fs = dart.finstate_all(name, year, reprt_code=reprt_code)
    except Exception as e:
        print(f"    [실패] {e}")
        return None

    if fs is None or fs.empty:
        print(f"    [스킵] 데이터 없음")
        return None

    # 전기 데이터 (YoY 계산용)
    prev_revenue  = get_value(fs, "매출액",  "손익계산서", col="frmtrm_amount")
    prev_op_income = get_value(fs, "영업이익", "손익계산서", col="frmtrm_amount")

    # 당기 데이터
    revenue     = get_value(fs, "매출액",   "손익계산서")
    gross       = get_value(fs, "매출총이익","손익계산서")
    op_income   = get_value(fs, "영업이익",  "손익계산서")
    net_income  = get_net_income(fs, period_type)
    assets      = get_value(fs, "자산총계",  "재무상태표")
    liabilities = get_value(fs, "부채총계",  "재무상태표")
    equity      = get_value(fs, "자본총계",  "재무상태표")
    retained    = get_value(fs, "이익잉여금","재무상태표")
    cfo         = get_value(fs, "영업활동현금흐름", "현금흐름표")
    cfi         = get_value(fs, "투자활동현금흐름", "현금흐름표")
    cff         = get_value(fs, "재무활동현금흐름", "현금흐름표")
    capex_raw   = get_value(fs, "유형자산의 취득",  "현금흐름표")
    capex       = abs(capex_raw) if capex_raw else None

    # 파생 지표 계산
    def safe_div(a, b):
        try:
            return round(a / b, 4) if a and b and b != 0 else None
        except:
            return None

    def safe_pct(a, b):
        try:
            return round(a / b * 100, 2) if a and b and b != 0 else None
        except:
            return None

    fcf            = (cfo - capex) if cfo and capex else None
    roe            = safe_pct(net_income, equity)
    roa            = safe_pct(net_income, assets)
    debt_ratio     = safe_pct(liabilities, equity)
    op_margin      = safe_pct(op_income, revenue)
    net_margin     = safe_pct(net_income, revenue)
    yoy_revenue    = safe_pct(revenue - prev_revenue, prev_revenue) if revenue and prev_revenue else None
    yoy_op_income  = safe_pct(op_income - prev_op_income, prev_op_income) if op_income and prev_op_income else None

    return {
        # 기본 정보
        "회사명":       name,
        "종목코드":     code,
        "연도":         year,
        "분기":         period_label,
        "기간유형":     period_type,
        "기준일":       f"{year}년 {period_label}",

        # 재무상태표 (단위: 원)
        "자산총계":     assets,
        "부채총계":     liabilities,
        "자본총계":     equity,
        "이익잉여금":   retained,

        # 손익계산서 (단위: 원)
        "매출액":       revenue,
        "매출총이익":   gross,
        "영업이익":     op_income,
        "당기순이익":   net_income,

        # 현금흐름 (단위: 원)
        "영업활동현금흐름": cfo,
        "투자활동현금흐름": cfi,
        "재무활동현금흐름": cff,
        "CAPEX":           capex,
        "FCF":             fcf,

        # 수익성 지표 (단위: %)
        "ROE":          roe,
        "ROA":          roa,
        "영업이익률":   op_margin,
        "순이익률":     net_margin,
        "부채비율":     debt_ratio,

        # 성장성 지표 (단위: %)
        "YoY_매출성장률":    yoy_revenue,
        "YoY_영업이익성장률": yoy_op_income,
    }


# ============================================================
# 실행
# ============================================================

def main():
    os.makedirs("data", exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = f"data/dart_{timestamp}.csv"

    all_rows = []

    for name, code in STOCKS:
        print(f"\n{'='*40}")
        print(f"[ {name} ({code}) ]")
        print(f"{'='*40}")

        for year in YEARS:
            for reprt_code, period_label, period_type in REPORT_TYPES:
                row = collect(name, code, year, reprt_code, period_label, period_type)
                if row:
                    all_rows.append(row)
                time.sleep(0.5)  # API 과호출 방지

    if not all_rows:
        print("\n수집된 데이터가 없습니다.")
        return

    # CSV 저장
    fieldnames = list(all_rows[0].keys())
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\n✅ 저장 완료: {output_path}")
    print(f"   총 {len(all_rows)}개 기간 수집")


if __name__ == "__main__":
    main()