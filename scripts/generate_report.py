from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo


# ============================================================
# Taiwan ETF Chip Report - Official TWSE ETF Master
# ------------------------------------------------------------
# 正式資料策略：
#   主來源：TWSE 證券編碼系統 ISIN / 分類查詢
#          這是比 TWSE 網站前端商品頁更穩定的官方 master source。
#
#   來源 1：
#     https://isin.twse.com.tw/isin/class_main.jsp
#     以 market=1、issuetype=ETF 篩出上市 ETF。
#
#   來源 2 fallback：
#     https://isin.twse.com.tw/isin/C_public.jsp?strMode=2
#     解析「本國上市證券國際證券辨識號碼一覽表」中的 ETF 區段。
#
#   輔助來源：
#     TWSE ETF 商品資訊 / e添富頁面只作 enrichment，不作唯一主來源，
#     因為這些頁面在 GitHub Actions runner 中可能只回傳殼頁或前端渲染結果。
#
# 嚴格原則：
#   - 不使用 sample ETF。
#   - 不產生假的 ETF rows。
#   - 抓不到可信 TWSE ETF master 就讓 workflow 失敗。
#
# 輸出：
#   data/etf_master_latest.json
#   raw/etf_master/YYYY-MM-DD/twse_etf_master.json
#   data/latest_report.json
#   history/YYYY-MM-DD.json
# ============================================================


ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"
HISTORY_DIR = ROOT / "history"
RAW_MASTER_DIR = ROOT / "raw" / "etf_master"
RAW_HOLDINGS_DIR = ROOT / "raw" / "holdings"
RAW_DEBUG_DIR = ROOT / "raw" / "debug"

for folder in [DATA_DIR, HISTORY_DIR, RAW_MASTER_DIR, RAW_HOLDINGS_DIR, RAW_DEBUG_DIR]:
    folder.mkdir(parents=True, exist_ok=True)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

TOP_N_STOCKS = int(os.getenv("TOP_N_STOCKS", "10"))
MIN_TWSE_ETF_MASTER_ROWS = int(os.getenv("MIN_TWSE_ETF_MASTER_ROWS", "50"))
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_SLEEP_SECONDS = float(os.getenv("HTTP_SLEEP_SECONDS", "0.35"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("twse-etf-master")


TWSE_ISIN_CLASS_URL = "https://isin.twse.com.tw/isin/class_main.jsp"
TWSE_ISIN_PUBLIC_LIST_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"

# Optional enrichment sources. The report will not rely on them as the primary source.
TWSE_ETF_PRODUCT_LIST_URLS = [
    "https://www.twse.com.tw/zh/products/securities/etf/products/list.html",
    "https://www.twse.com.tw/en/products/securities/etf/products/list.html",
    "https://www.twse.com.tw/zh/ETFortune/products",
    "https://www.twse.com.tw/en/ETFortune-institute/products",
]

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# ============================================================
# Utility
# ============================================================
def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_code(value: Any) -> str:
    return clean_text(value).upper()


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = clean_text(value).replace(",", "").replace("%", "")
        if text in {"", "-", "--", "N/A", "nan", "None"}:
            return default
        return float(text)
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        text = clean_text(value).replace(",", "")
        if text in {"", "-", "--", "N/A", "nan", "None"}:
            return default
        return int(float(text))
    except Exception:
        return default


def is_etf_code(value: Any) -> bool:
    code = normalize_code(value)
    # Taiwan ETF examples: 0050, 00631L, 00632R, 00980A, 00981D
    return bool(re.fullmatch(r"\d{4,6}[A-Z]?", code))


def is_isin(value: Any) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2}[A-Z0-9]{10}", clean_text(value)))


def direction_from_value(value: float) -> str:
    if value > 0:
        return "buy"
    if value < 0:
        return "sell"
    return "neutral"


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        delete=False,
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def http_get(url: str, params: Optional[Dict[str, Any]] = None) -> str:
    logger.info("GET %s params=%s", url, params)
    response = requests.get(
        url,
        params=params,
        headers=REQUEST_HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    # TWSE ISIN pages are usually Big5 / CP950; apparent_encoding handles this.
    response.encoding = response.apparent_encoding or "utf-8"
    time.sleep(HTTP_SLEEP_SECONDS)
    return response.text


def debug_save(report_date: str, name: str, content: str) -> None:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:80]
    path = RAW_DEBUG_DIR / report_date / f"{safe}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", errors="ignore")


# ============================================================
# Official TWSE ETF master
# ============================================================
def fetch_twse_etf_master(report_date: str) -> List[Dict[str, Any]]:
    """
    Fetch true TWSE listed ETF master.

    Primary path:
      class_main.jsp classification search with market=1 and ETF type.

    Fallback path:
      C_public.jsp?strMode=2; parse ETF section.

    Enrichment:
      Try TWSE ETF product/e添富 pages only to fill issuer/benchmark/AUM
      when available. They are not allowed to create the master universe by
      themselves.
    """
    errors: List[str] = []

    try:
        master = fetch_from_isin_class_main(report_date)
        logger.info("ISIN class_main parsed %s ETF rows.", len(master))
    except Exception as exc:
        errors.append(f"class_main: {exc}")
        logger.warning("class_main failed: %s", exc)
        master = []

    if len(master) < MIN_TWSE_ETF_MASTER_ROWS or not contains_0050(master):
        try:
            fallback = fetch_from_isin_public_list(report_date)
            logger.info("ISIN public list parsed %s ETF rows.", len(fallback))
            if len(fallback) > len(master):
                master = fallback
        except Exception as exc:
            errors.append(f"C_public: {exc}")
            logger.warning("C_public failed: %s", exc)

    master = dedupe_master_rows(master)

    if len(master) < MIN_TWSE_ETF_MASTER_ROWS or not contains_0050(master):
        raise RuntimeError(
            "TWSE ETF master validation failed. "
            f"rows={len(master)}, has_0050={contains_0050(master)}, "
            f"min_required={MIN_TWSE_ETF_MASTER_ROWS}, errors={errors}. "
            f"Debug files saved under raw/debug/{report_date}/."
        )

    # Optional enrichment. If this fails, master remains valid because ISIN source passed.
    try:
        enrichments = fetch_twse_product_enrichment(report_date)
        master = merge_enrichment(master, enrichments)
    except Exception as exc:
        logger.warning("TWSE product enrichment failed, keep ISIN master only: %s", exc)

    return sorted(dedupe_master_rows(master), key=lambda x: x["etf_code"])


def fetch_from_isin_class_main(report_date: str) -> List[Dict[str, Any]]:
    """
    Official source:
      https://isin.twse.com.tw/isin/class_main.jsp

    We intentionally query pages and filter 有價證券別=ETF, 市場別=上市.
    """
    rows: List[Dict[str, Any]] = []
    seen_codes: set[str] = set()
    empty_pages = 0

    for page in range(1, 30):
        params = {
            "Page": page,
            "chklike": "Y",
            "market": "1",       # 1 = 上市
            "issuetype": "",     # empty then filter 有價證券別 = ETF
            "industry_code": "",
            "isincode": "",
            "owncode": "",
            "stockname": "",
        }

        html = http_get(TWSE_ISIN_CLASS_URL, params=params)
        debug_save(report_date, f"isin_class_main_page_{page}", html)

        page_rows = parse_isin_class_main_html(html)
        page_rows = [row for row in page_rows if row["etf_code"] not in seen_codes]

        for row in page_rows:
            seen_codes.add(row["etf_code"])

        rows.extend(page_rows)
        logger.info("class_main page=%s parsed_new_rows=%s total=%s", page, len(page_rows), len(rows))

        if not page_rows:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0

        # Stop early when page returned no table or repeated rows.
        if len(rows) >= MIN_TWSE_ETF_MASTER_ROWS and page >= 3:
            # There are usually enough rows by this point, but continue one more
            # page in case pagination is short.
            pass

    return dedupe_master_rows(rows)


def parse_isin_class_main_html(html: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    # Preferred parser: pandas tables.
    try:
        tables = pd.read_html(StringIO(html), displayed_only=False)
        for df in tables:
            rows.extend(parse_isin_class_dataframe(df))
    except Exception:
        pass

    # Fallback parser: tr/td.
    if not rows:
        soup = BeautifulSoup(html, "html.parser")
        for tr in soup.find_all("tr"):
            cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
            if len(cells) < 7:
                continue
            parsed = parse_isin_class_cells(cells)
            if parsed:
                rows.append(parsed)

    return dedupe_master_rows(rows)


def parse_isin_class_dataframe(df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            clean_text(" ".join(str(x) for x in col if str(x) != "nan"))
            for col in df.columns
        ]
    else:
        df.columns = [clean_text(c) for c in df.columns]

    # Some ISIN tables use first row as header.
    if len(df) > 0:
        first_row = [clean_text(x) for x in df.iloc[0].tolist()]
        if any("有價證券代號" in x for x in first_row) or any("ISIN" in x for x in first_row):
            df.columns = first_row
            df = df.iloc[1:].copy()

    for _, row in df.iterrows():
        cells = [clean_text(x) for x in row.tolist()]
        parsed = parse_isin_class_cells(cells)
        if parsed:
            rows.append(parsed)

    return rows


def parse_isin_class_cells(cells: List[str]) -> Optional[Dict[str, Any]]:
    """
    Expected cells for class_main:
      0: row number
      1: ISIN
      2: 有價證券代號
      3: 有價證券名稱
      4: 市場別
      5: 有價證券別
      6: 產業別
      7: 公開發行/上市(櫃)/發行日
      8: CFICode
      9: 備註
    """
    cells = [clean_text(x) for x in cells if clean_text(x)]
    if len(cells) < 7:
        return None

    # Find code and ISIN flexibly.
    code = ""
    isin = ""
    for cell in cells:
        if not code and is_etf_code(cell):
            code = normalize_code(cell)
        if not isin and is_isin(cell):
            isin = clean_text(cell)

    if not code:
        return None

    # Find likely positions.
    code_idx = next((i for i, x in enumerate(cells) if normalize_code(x) == code), -1)
    isin_idx = next((i for i, x in enumerate(cells) if clean_text(x) == isin), -1)

    name = cells[code_idx + 1] if 0 <= code_idx + 1 < len(cells) else ""
    market = ""
    security_type = ""
    listing_date = ""
    cfi_code = ""
    remark = ""

    # If class_main exact order is present.
    if code_idx >= 2 and len(cells) >= code_idx + 6:
        # ... ISIN, code, name, market, type, industry, listing_date, CFI, remark
        maybe_market = cells[code_idx + 2] if code_idx + 2 < len(cells) else ""
        maybe_type = cells[code_idx + 3] if code_idx + 3 < len(cells) else ""
        if "上市" in maybe_market or "上櫃" in maybe_market:
            market = maybe_market
            security_type = maybe_type
            listing_date = cells[code_idx + 5] if code_idx + 5 < len(cells) else ""
            cfi_code = cells[code_idx + 6] if code_idx + 6 < len(cells) else ""
            remark = cells[code_idx + 7] if code_idx + 7 < len(cells) else ""

    # Only TWSE listed ETF.
    joined = " ".join(cells)
    if "ETF" not in joined:
        return None
    if market and "上市" not in market:
        return None
    if security_type and "ETF" not in security_type:
        return None

    if not name or name in {"ETF", "上市"}:
        return None

    return make_master_row(
        etf_code=code,
        etf_name=name,
        listing_date=normalize_date(listing_date),
        issuer=infer_issuer_from_name(name),
        benchmark="",
        market="TWSE",
        source="TWSE ISIN class_main",
        source_url=TWSE_ISIN_CLASS_URL,
        isin=isin,
        security_type=security_type or "ETF",
        cfi_code=cfi_code,
        remark=remark,
    )


def fetch_from_isin_public_list(report_date: str) -> List[Dict[str, Any]]:
    """
    Official fallback:
      https://isin.twse.com.tw/isin/C_public.jsp?strMode=2

    The table is grouped by security type. We parse rows after the ETF header.
    """
    html = http_get(TWSE_ISIN_PUBLIC_LIST_URL)
    debug_save(report_date, "isin_C_public_strMode_2", html)

    rows: List[Dict[str, Any]] = []
    soup = BeautifulSoup(html, "html.parser")
    current_section = ""

    for tr in soup.find_all("tr"):
        cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]
        cells = [x for x in cells if x]

        if not cells:
            continue

        if len(cells) == 1 and not is_etf_code(cells[0]):
            current_section = cells[0]
            continue

        # C_public expected:
        # 有價證券代號及名稱, ISIN Code, 上市日, 市場別, 產業別, CFICode, 備註
        if len(cells) < 3:
            continue

        first = cells[0]
        code, name = split_code_name(first)

        if not code or not name:
            continue

        joined = " ".join(cells)
        if current_section != "ETF" and "ETF" not in joined:
            continue

        isin = next((x for x in cells if is_isin(x)), "")
        listing_date = cells[2] if len(cells) > 2 else ""
        cfi_code = cells[5] if len(cells) > 5 else ""
        remark = cells[6] if len(cells) > 6 else ""

        rows.append(
            make_master_row(
                etf_code=code,
                etf_name=name,
                listing_date=normalize_date(listing_date),
                issuer=infer_issuer_from_name(name),
                benchmark="",
                market="TWSE",
                source="TWSE ISIN C_public",
                source_url=TWSE_ISIN_PUBLIC_LIST_URL,
                isin=isin,
                security_type="ETF",
                cfi_code=cfi_code,
                remark=remark,
            )
        )

    return dedupe_master_rows(rows)


def split_code_name(text: str) -> tuple[str, str]:
    text = clean_text(text)
    match = re.match(r"^(\d{4,6}[A-Z]?)\s+(.+)$", text)
    if not match:
        return "", ""
    code = normalize_code(match.group(1))
    name = clean_text(match.group(2))
    if not is_etf_code(code):
        return "", ""
    return code, name


def normalize_date(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    text = text.replace("/", ".").replace("-", ".")
    return text


def make_master_row(
    etf_code: str,
    etf_name: str,
    listing_date: str,
    issuer: str,
    benchmark: str,
    market: str,
    source: str,
    source_url: str,
    isin: str = "",
    security_type: str = "ETF",
    cfi_code: str = "",
    remark: str = "",
    category: str = "",
    aum_yi: float = 0.0,
    close: float = 0.0,
    beneficiaries: int = 0,
    avg_daily_trading_value_ytd_million: float = 0.0,
    avg_daily_trading_volume_ytd_shares: int = 0,
) -> Dict[str, Any]:
    etf_name = clean_text(etf_name)
    return {
        "etf_code": normalize_code(etf_code),
        "etf_name": etf_name,
        "isin": clean_text(isin),
        "listing_date": clean_text(listing_date),
        "benchmark": clean_text(benchmark),
        "issuer": clean_text(issuer) or infer_issuer_from_name(etf_name),
        "market": market,
        "security_type": security_type,
        "source": source,
        "source_url": source_url,
        "category": clean_text(category),
        "aum_yi": round(float(aum_yi or 0.0), 4),
        "close": round(float(close or 0.0), 4),
        "avg_daily_trading_value_ytd_million": round(float(avg_daily_trading_value_ytd_million or 0.0), 4),
        "avg_daily_trading_volume_ytd_shares": int(avg_daily_trading_volume_ytd_shares or 0),
        "beneficiaries": int(beneficiaries or 0),
        "top10_weight_pct": 0.0,
        "cfi_code": clean_text(cfi_code),
        "remark": clean_text(remark),
    }


def dedupe_master_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    best: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        code = normalize_code(row.get("etf_code"))
        if not is_etf_code(code):
            continue

        row = dict(row)
        row["etf_code"] = code
        row["etf_name"] = clean_text(row.get("etf_name", ""))

        if not row["etf_name"]:
            continue

        if code not in best:
            best[code] = row
        elif row_quality_score(row) > row_quality_score(best[code]):
            merged = {**best[code], **row}
            # Preserve useful old values if new value is blank.
            for key in best[code]:
                if not merged.get(key) and best[code].get(key):
                    merged[key] = best[code][key]
            best[code] = merged
        else:
            for key, value in row.items():
                if not best[code].get(key) and value:
                    best[code][key] = value

    return sorted(best.values(), key=lambda x: x["etf_code"])


def row_quality_score(row: Dict[str, Any]) -> int:
    score = 0
    for key in ["etf_name", "isin", "listing_date", "issuer", "benchmark", "security_type", "cfi_code", "remark"]:
        if row.get(key):
            score += 2
    for key in ["aum_yi", "close", "beneficiaries", "avg_daily_trading_value_ytd_million"]:
        if safe_float(row.get(key)) > 0:
            score += 4
    if "class_main" in row.get("source", ""):
        score += 5
    if "C_public" in row.get("source", ""):
        score += 3
    return score


def contains_0050(rows: List[Dict[str, Any]]) -> bool:
    return any(row.get("etf_code") == "0050" for row in rows)


def infer_issuer_from_name(etf_name: str) -> str:
    mapping = [
        ("元大", "元大投信"),
        ("富邦", "富邦投信"),
        ("國泰", "國泰投信"),
        ("群益", "群益投信"),
        ("復華", "復華投信"),
        ("永豐", "永豐投信"),
        ("凱基", "凱基投信"),
        ("中信", "中國信託投信"),
        ("台新", "台新投信"),
        ("統一", "統一投信"),
        ("兆豐", "兆豐投信"),
        ("野村", "野村投信"),
        ("第一金", "第一金投信"),
        ("大華", "大華銀投信"),
        ("街口", "街口投信"),
        ("玉山", "玉山投信"),
        ("聯博", "聯博投信"),
        ("貝萊德", "貝萊德投信"),
        ("摩根", "摩根投信"),
        ("Yuanta", "元大投信"),
        ("Fubon", "富邦投信"),
        ("Cathay", "國泰投信"),
        ("Capital", "群益投信"),
        ("Fuh Hwa", "復華投信"),
        ("SinoPac", "永豐投信"),
        ("KGI", "凱基投信"),
        ("CTBC", "中國信託投信"),
        ("Taishin", "台新投信"),
        ("Uni-President", "統一投信"),
        ("Mega", "兆豐投信"),
        ("Nomura", "野村投信"),
        ("First", "第一金投信"),
        ("UOB", "大華銀投信"),
        ("JKO", "街口投信"),
        ("E.SUN", "玉山投信"),
        ("BlackRock", "貝萊德投信"),
        ("J.P. Morgan", "摩根投信"),
        ("AllianceBernstein", "聯博投信"),
    ]
    lower = etf_name.lower()
    for key, issuer in mapping:
        if key.lower() in lower:
            return issuer
    return ""


# ============================================================
# Optional enrichment from TWSE ETF product / ETFortune pages
# ============================================================
def fetch_twse_product_enrichment(report_date: str) -> Dict[str, Dict[str, Any]]:
    enrich: Dict[str, Dict[str, Any]] = {}

    for url in TWSE_ETF_PRODUCT_LIST_URLS:
        try:
            html = http_get(url)
            debug_save(report_date, f"enrich_{url_to_name(url)}", html)

            rows = parse_product_or_etfortune_enrichment(html, url)
            for row in rows:
                code = row["etf_code"]
                if code not in enrich or row_quality_score(row) > row_quality_score(enrich[code]):
                    enrich[code] = row

            logger.info("Enrichment %s parsed %s rows.", url, len(rows))
        except Exception as exc:
            logger.warning("Enrichment source failed %s: %s", url, exc)

    return enrich


def url_to_name(url: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", url)[-70:]


def parse_product_or_etfortune_enrichment(html: str, source_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    # Try pandas tables first.
    try:
        tables = pd.read_html(StringIO(html), displayed_only=False)
        for df in tables:
            rows.extend(parse_enrichment_dataframe(df, source_url))
    except Exception:
        pass

    # Token parser for server-rendered ETFortune pages.
    if not rows:
        rows.extend(parse_enrichment_tokens(html, source_url))

    return dedupe_master_rows(rows)


def parse_enrichment_dataframe(df: pd.DataFrame, source_url: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            clean_text(" ".join(str(x) for x in col if str(x) != "nan"))
            for col in df.columns
        ]
    else:
        df.columns = [clean_text(c) for c in df.columns]

    def find_col(names: List[str]) -> Optional[str]:
        for col in df.columns:
            col_l = clean_text(col).lower()
            for name in names:
                if name.lower() in col_l:
                    return col
        return None

    code_col = find_col(["股票代號", "ETF Code", "證券代號"])
    name_col = find_col(["ETF名稱", "ETF Name", "證券簡稱", "名稱"])
    listing_col = find_col(["上市日期", "Listing Date"])
    benchmark_col = find_col(["標的指數", "Benchmark"])
    issuer_col = find_col(["發行人", "Issuer"])
    aum_col = find_col(["資產規模", "AUM"])
    close_col = find_col(["收盤價", "Closing Price"])
    value_col = find_col(["成交值", "Trading Value"])
    volume_col = find_col(["成交量", "Trading Volume"])
    beneficiary_col = find_col(["受益人", "Beneficiary"])

    if not code_col or not name_col:
        return []

    for _, row in df.iterrows():
        code = normalize_code(row.get(code_col))
        name = clean_text(row.get(name_col))
        if not is_etf_code(code) or not name:
            continue

        rows.append(
            make_master_row(
                etf_code=code,
                etf_name=name,
                listing_date=normalize_date(row.get(listing_col, "")) if listing_col else "",
                issuer=clean_text(row.get(issuer_col, "")) if issuer_col else infer_issuer_from_name(name),
                benchmark=clean_text(row.get(benchmark_col, "")) if benchmark_col else "",
                market="TWSE",
                source="TWSE ETF product enrichment",
                source_url=source_url,
                aum_yi=safe_float(row.get(aum_col)) if aum_col else 0.0,
                close=safe_float(row.get(close_col)) if close_col else 0.0,
                beneficiaries=safe_int(row.get(beneficiary_col)) if beneficiary_col else 0,
                avg_daily_trading_value_ytd_million=safe_float(row.get(value_col)) if value_col else 0.0,
                avg_daily_trading_volume_ytd_shares=safe_int(row.get(volume_col)) if volume_col else 0,
            )
        )

    return rows


def parse_enrichment_tokens(html: str, source_url: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    tokens = [clean_text(x) for x in soup.get_text("\n", strip=True).splitlines()]
    tokens = [x for x in tokens if x]

    rows: List[Dict[str, Any]] = []
    for i, token in enumerate(tokens):
        # Token may be "00400A" or "00400A, 主動國泰..."
        match = re.match(r"^(\d{4,6}[A-Z]?)(?:\s*[,，]\s*(.*))?$", token)
        if not match:
            continue

        code = normalize_code(match.group(1))
        if not is_etf_code(code):
            continue

        inline_rest = clean_text(match.group(2) or "")
        name = inline_rest or (tokens[i + 1] if i + 1 < len(tokens) else "")
        if not name or is_etf_code(name) or looks_like_number(name):
            continue

        listing_date = ""
        benchmark = ""
        issuer = infer_issuer_from_name(name)
        aum = 0.0
        close = 0.0
        trading_value = 0.0
        trading_volume = 0
        beneficiaries = 0

        # Scan next 12 tokens for known fields.
        ahead = tokens[i + 1:i + 14]
        date_idx = next((j for j, x in enumerate(ahead) if looks_like_date(x)), None)
        if date_idx is not None:
            listing_date = normalize_date(ahead[date_idx])
            if date_idx + 1 < len(ahead):
                benchmark = ahead[date_idx + 1]
            numeric_after = [x for x in ahead[date_idx + 2:] if looks_like_number(x)]
            if len(numeric_after) >= 1:
                aum = safe_float(numeric_after[0])
            if len(numeric_after) >= 2:
                close = safe_float(numeric_after[1])
            if len(numeric_after) >= 3:
                trading_value = safe_float(numeric_after[2])
            if len(numeric_after) >= 4:
                trading_volume = safe_int(numeric_after[3])
            if len(numeric_after) >= 5:
                beneficiaries = safe_int(numeric_after[4])

            non_numeric_after = [
                x for x in ahead[date_idx + 2:]
                if not looks_like_number(x) and not is_etf_code(x)
            ]
            if non_numeric_after:
                possible_issuer = non_numeric_after[-1]
                if possible_issuer != benchmark:
                    issuer = possible_issuer or issuer

        rows.append(
            make_master_row(
                etf_code=code,
                etf_name=name,
                listing_date=listing_date,
                issuer=issuer,
                benchmark=benchmark,
                market="TWSE",
                source="TWSE product token enrichment",
                source_url=source_url,
                aum_yi=aum,
                close=close,
                beneficiaries=beneficiaries,
                avg_daily_trading_value_ytd_million=trading_value,
                avg_daily_trading_volume_ytd_shares=trading_volume,
            )
        )

    return rows


def merge_enrichment(master: List[Dict[str, Any]], enrich: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    for row in master:
        code = row["etf_code"]
        extra = enrich.get(code)
        if not extra:
            output.append(row)
            continue

        merged = dict(row)
        for key in [
            "etf_name",
            "listing_date",
            "benchmark",
            "issuer",
            "category",
            "aum_yi",
            "close",
            "avg_daily_trading_value_ytd_million",
            "avg_daily_trading_volume_ytd_shares",
            "beneficiaries",
        ]:
            if not merged.get(key) and extra.get(key):
                merged[key] = extra[key]
            elif key in ["aum_yi", "close", "avg_daily_trading_value_ytd_million", "avg_daily_trading_volume_ytd_shares", "beneficiaries"]:
                if safe_float(merged.get(key)) == 0 and safe_float(extra.get(key)) != 0:
                    merged[key] = extra[key]
        output.append(merged)
    return dedupe_master_rows(output)


def looks_like_date(text: Any) -> bool:
    return bool(re.fullmatch(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}", clean_text(text)))


def looks_like_number(text: Any) -> bool:
    return bool(re.fullmatch(r"[-+]?\d[\d,]*(?:\.\d+)?%?", clean_text(text)))


# ============================================================
# Holdings snapshot and diff
# ============================================================
def holdings_folder(trade_date: str) -> Path:
    return RAW_HOLDINGS_DIR / trade_date


def load_holdings_snapshot(trade_date: str) -> Dict[str, Dict[str, Any]]:
    folder = holdings_folder(trade_date)
    if not folder.exists():
        return {}

    output: Dict[str, Dict[str, Any]] = {}
    for path in sorted(folder.glob("*.json")):
        try:
            payload = read_json(path)
            code = normalize_code(payload.get("etf_code", path.stem))
            output[code] = payload
        except Exception as exc:
            logger.warning("Failed to read holdings snapshot %s: %s", path, exc)
    return output


def previous_available_holding_date(report_date: str, max_lookback_days: int = 10) -> Optional[str]:
    current = datetime.strptime(report_date, "%Y-%m-%d").date()
    for i in range(1, max_lookback_days + 1):
        candidate = date_str(current - timedelta(days=i))
        folder = holdings_folder(candidate)
        if folder.exists() and any(folder.glob("*.json")):
            return candidate
    return None


def normalize_holding_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "stock_code": clean_text(row.get("stock_code", "")),
        "stock_name": clean_text(row.get("stock_name", "")),
        "shares": safe_float(row.get("shares", 0)),
        "weight_pct": safe_float(row.get("weight_pct", 0)),
        "close": safe_float(row.get("close", 0)),
        "market_value": safe_float(row.get("market_value", 0)),
    }


def calculate_etf_stock_diffs(
    today_holdings: Dict[str, Dict[str, Any]],
    previous_holdings: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    diffs: List[Dict[str, Any]] = []

    for etf_code, today_payload in sorted(today_holdings.items()):
        previous_payload = previous_holdings.get(etf_code)
        if not previous_payload:
            continue

        etf_name = today_payload.get("etf_name") or previous_payload.get("etf_name") or ""
        today_rows = [normalize_holding_row(x) for x in today_payload.get("holdings", [])]
        previous_rows = [normalize_holding_row(x) for x in previous_payload.get("holdings", [])]

        today_map = {row["stock_code"]: row for row in today_rows if row["stock_code"]}
        previous_map = {row["stock_code"]: row for row in previous_rows if row["stock_code"]}

        for stock_code in sorted(set(today_map) | set(previous_map)):
            today_row = today_map.get(stock_code, {})
            prev_row = previous_map.get(stock_code, {})

            stock_name = today_row.get("stock_name") or prev_row.get("stock_name") or ""
            today_shares = safe_float(today_row.get("shares", 0))
            prev_shares = safe_float(prev_row.get("shares", 0))
            delta_shares = today_shares - prev_shares

            if abs(delta_shares) < 1:
                continue

            price = safe_float(today_row.get("close", 0)) or safe_float(prev_row.get("close", 0))
            today_weight = safe_float(today_row.get("weight_pct", 0))
            prev_weight = safe_float(prev_row.get("weight_pct", 0))

            diffs.append(
                {
                    "etf_code": etf_code,
                    "etf_name": etf_name,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "delta_shares": round(delta_shares, 0),
                    "delta_lot": round(delta_shares / 1000, 1),
                    "delta_value_yi": round(delta_shares * price / 100_000_000, 4),
                    "weight_delta_pct": round(today_weight - prev_weight, 4),
                }
            )

    return diffs


# ============================================================
# Report sections
# ============================================================
def make_stock_signal(stock_name: str, direction: str, etf_count: int, value_yi: float) -> str:
    abs_value = abs(value_yi)
    if direction == "buy" and etf_count >= 5:
        return f"{stock_name} 被 {etf_count} 檔 ETF 同步加碼，估算買盤 {abs_value:.2f} 億元，屬於高共識買盤。"
    if direction == "buy" and etf_count >= 2:
        return f"{stock_name} 被 {etf_count} 檔 ETF 加碼，估算買盤 {abs_value:.2f} 億元，建議觀察是否延續。"
    if direction == "sell" and etf_count >= 4:
        return f"{stock_name} 遭 {etf_count} 檔 ETF 同步減碼，估算賣壓 {abs_value:.2f} 億元，短線籌碼偏弱。"
    if direction == "sell" and etf_count >= 2:
        return f"{stock_name} 遭 {etf_count} 檔 ETF 減碼，估算賣壓 {abs_value:.2f} 億元，需觀察是否擴散。"
    return f"{stock_name} 今日 ETF 籌碼變化有限，暫列觀察。"


def aggregate_stock_changes(diffs: List[Dict[str, Any]], top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in diffs:
        grouped[row["stock_code"]].append(row)

    output: List[Dict[str, Any]] = []
    for stock_code, rows in grouped.items():
        stock_name = rows[0]["stock_name"]
        total_lot = sum(safe_float(x["delta_lot"]) for x in rows)
        total_value_yi = sum(safe_float(x["delta_value_yi"]) for x in rows)
        total_weight_delta = sum(safe_float(x["weight_delta_pct"]) for x in rows)
        direction = direction_from_value(total_value_yi)

        participants = []
        for x in sorted(rows, key=lambda r: abs(safe_float(r["delta_value_yi"])), reverse=True):
            delta_value_yi = safe_float(x["delta_value_yi"])
            participant_direction = direction_from_value(delta_value_yi)
            if participant_direction == "neutral":
                continue
            participants.append(
                {
                    "etf_code": x["etf_code"],
                    "etf_name": x["etf_name"],
                    "direction": participant_direction,
                    "delta_lot": round(safe_float(x["delta_lot"]), 1),
                    "delta_value_yi": round(delta_value_yi, 4),
                    "weight_delta_pct": round(safe_float(x["weight_delta_pct"]), 4),
                }
            )

        output.append(
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "direction": direction,
                "delta_lot": round(total_lot, 1),
                "delta_value_yi": round(total_value_yi, 2),
                "etf_count": len(participants),
                "weight_delta_pct": round(total_weight_delta, 4),
                "signal": make_stock_signal(stock_name, direction, len(participants), total_value_yi),
                "participating_etfs": participants,
            }
        )

    output.sort(key=lambda x: abs(safe_float(x["delta_value_yi"])), reverse=True)
    return output[:top_n] if top_n is not None else output


def build_etf_rankings(master: List[Dict[str, Any]], diffs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    diffs_by_etf: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in diffs:
        diffs_by_etf[row["etf_code"]].append(row)

    rankings: List[Dict[str, Any]] = []
    for etf in master:
        code = etf["etf_code"]
        rows = diffs_by_etf.get(code, [])

        buy_value_yi = sum(max(0.0, safe_float(x["delta_value_yi"])) for x in rows)
        sell_value_yi = abs(sum(min(0.0, safe_float(x["delta_value_yi"])) for x in rows))
        net_flow_yi = buy_value_yi - sell_value_yi
        aum_yi = safe_float(etf.get("aum_yi"))
        turnover_pct = (buy_value_yi + sell_value_yi) / aum_yi * 100 if aum_yi > 0 and rows else 0.0

        rankings.append(
            {
                "etf_code": code,
                "etf_name": etf.get("etf_name", ""),
                "aum_yi": round(aum_yi, 1),
                "buy_value_yi": round(buy_value_yi, 2),
                "sell_value_yi": round(sell_value_yi, 2),
                "net_flow_yi": round(net_flow_yi, 2),
                "turnover_pct": round(turnover_pct, 2),
                "top10_weight_pct": round(safe_float(etf.get("top10_weight_pct")), 1),
            }
        )

    rankings.sort(
        key=lambda x: (
            abs(safe_float(x["buy_value_yi"]) + safe_float(x["sell_value_yi"])),
            safe_float(x["aum_yi"]),
            x["etf_code"],
        ),
        reverse=True,
    )
    return rankings


def build_kpis(all_changes: List[Dict[str, Any]], top_changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    net = sum(safe_float(x["delta_value_yi"]) for x in all_changes)
    consensus_buy = sum(1 for x in all_changes if safe_float(x["delta_value_yi"]) > 0 and safe_int(x.get("etf_count")) >= 2)
    consensus_sell = sum(1 for x in all_changes if safe_float(x["delta_value_yi"]) < 0 and safe_int(x.get("etf_count")) >= 2)

    total_abs = sum(abs(safe_float(x["delta_value_yi"])) for x in all_changes) or 1.0
    top3_abs = sum(abs(safe_float(x["delta_value_yi"])) for x in top_changes[:3])
    concentration_score = round(top3_abs / total_abs * 100) if all_changes else 0

    return {
        "net_change_value_yi": round(net, 1),
        "consensus_buy_count": consensus_buy,
        "consensus_sell_count": consensus_sell,
        "concentration_score": concentration_score,
    }


def build_stock_radar(top_changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    for item in top_changes:
        participants = item.get("participating_etfs", [])
        buy_etfs = sum(1 for x in participants if x.get("direction") == "buy")
        sell_etfs = sum(1 for x in participants if x.get("direction") == "sell")

        if item["direction"] == "buy" and buy_etfs >= 5:
            event_type = "高共識加碼"
        elif item["direction"] == "buy":
            event_type = "共識加碼"
        elif item["direction"] == "sell" and sell_etfs >= 4:
            event_type = "高共識減碼"
        elif item["direction"] == "sell":
            event_type = "共識減碼"
        else:
            event_type = "觀察"

        output.append(
            {
                "stock_code": item["stock_code"],
                "stock_name": item["stock_name"],
                "buy_etfs": buy_etfs,
                "sell_etfs": sell_etfs,
                "net_value_yi": item["delta_value_yi"],
                "streak_days": 1,
                "event_type": event_type,
                "note": item["signal"],
            }
        )
    return output


def holdings_quality(master_count: int, today_holdings_count: int) -> Dict[str, Any]:
    ratio = today_holdings_count / master_count if master_count else 0.0
    return {
        "tracked_etfs": master_count,
        "covered_etfs": today_holdings_count,
        "coverage_ratio": round(ratio, 4),
        "is_ready": ratio >= 0.85,
    }


def build_events(top_changes: List[Dict[str, Any]], quality: Dict[str, Any], master_count: int) -> List[Dict[str, Any]]:
    events = []

    buys = [x for x in top_changes if safe_float(x["delta_value_yi"]) > 0]
    sells = [x for x in top_changes if safe_float(x["delta_value_yi"]) < 0]

    if buys:
        top = buys[0]
        events.append(
            {
                "time": "盤後",
                "title": f"{top['stock_name']} 成為今日 ETF 最大共識買盤",
                "desc": f"估算加碼 {top['delta_value_yi']} 億元，參與 ETF {top['etf_count']} 檔。",
            }
        )

    if sells:
        top = sorted(sells, key=lambda x: safe_float(x["delta_value_yi"]))[0]
        events.append(
            {
                "time": "盤後",
                "title": f"{top['stock_name']} 出現 ETF 共識減碼",
                "desc": f"估算減碼 {abs(safe_float(top['delta_value_yi'])):.2f} 億元，參與 ETF {top['etf_count']} 檔。",
            }
        )

    events.append(
        {
            "time": "資料檢查",
            "title": "TWSE ETF Master 已成功更新",
            "desc": f"本次從 TWSE ISIN 官方資料解析並驗證 {master_count} 檔上市 ETF master。",
        }
    )
    events.append(
        {
            "time": "資料檢查",
            "title": "ETF 持股快照覆蓋率",
            "desc": f"今日已覆蓋 {quality['covered_etfs']}/{quality['tracked_etfs']} 檔 ETF，覆蓋率 {quality['coverage_ratio']:.1%}。",
        }
    )
    return events


def build_ai_report(top_changes: List[Dict[str, Any]], kpis: Dict[str, Any], master_count: int) -> Dict[str, Any]:
    if not top_changes:
        return {
            "headline": f"TWSE ETF master 已成功更新，本次追蹤 {master_count} 檔上市 ETF。",
            "summary": (
                "目前已接上正式 TWSE ISIN ETF master，不再使用 sample ETF 清單。"
                "此階段會提供 ETF 代號、名稱、ISIN、上市日期、CFI Code 等 master data。"
                "每日個股層級 ETF 加減碼需要下一步接上各投信 PCF / 每日持股揭露後才會產生。"
            ),
            "watchlist": [],
            "risk": "目前個股籌碼變化尚未接上正式持股快照，請勿將空白的 top_stock_changes 解讀為沒有 ETF 調倉。",
        }

    net = safe_float(kpis["net_change_value_yi"])
    bias = "偏多" if net > 0 else "偏空" if net < 0 else "中性"

    watchlist = [
        f"{x['stock_code']} {x['stock_name']}：ETF {'加碼' if safe_float(x['delta_value_yi']) > 0 else '減碼'} {abs(safe_float(x['delta_value_yi'])):.2f} 億，參與 ETF {x['etf_count']} 檔"
        for x in top_changes[:5]
    ]

    return {
        "headline": f"今日全市場 ETF 籌碼{bias}，前十大變動淨額 {net:.1f} 億元。",
        "summary": f"本報告以 TWSE ETF master 與 raw/holdings 快照計算，前十大變動集中度為 {kpis['concentration_score']}%。",
        "watchlist": watchlist,
        "risk": "若持股快照覆蓋率未達 100%，請避免將報告視為完整市場結論。",
    }


def build_data_sources() -> List[Dict[str, Any]]:
    return [
        {
            "name": "TWSE ISIN 證券編碼分類查詢",
            "type": "ETF master",
            "update_freq": "依 TWSE 官方資料更新",
            "status": "ready",
            "fields": ["isin", "etf_code", "etf_name", "market", "security_type", "listing_date", "cfi_code"],
        },
        {
            "name": "TWSE ISIN 本國上市證券國際證券辨識號碼一覽表",
            "type": "ETF master fallback",
            "update_freq": "依 TWSE 官方資料更新",
            "status": "ready",
            "fields": ["etf_code", "etf_name", "isin", "listing_date", "market", "cfi_code"],
        },
        {
            "name": "TWSE ETF 商品資訊 / e添富",
            "type": "ETF enrichment",
            "update_freq": "依 TWSE 官方頁面更新",
            "status": "watch",
            "fields": ["issuer", "benchmark", "aum_yi", "close", "beneficiaries"],
        },
        {
            "name": "各投信 PCF / 每日持股揭露",
            "type": "每日持股核心資料",
            "update_freq": "每日盤前或盤後",
            "status": "watch",
            "fields": ["stock_code", "shares", "weight", "cash_component", "creation_unit"],
        },
    ]


# ============================================================
# Report
# ============================================================
def save_master(report_date: str, master: List[Dict[str, Any]]) -> None:
    atomic_write_json(DATA_DIR / "etf_master_latest.json", master)
    atomic_write_json(RAW_MASTER_DIR / report_date / "twse_etf_master.json", master)


def build_report() -> Dict[str, Any]:
    now = now_taipei()
    report_date = date_str(now.date())

    master = fetch_twse_etf_master(report_date)
    save_master(report_date, master)

    today_holdings = load_holdings_snapshot(report_date)
    prev_date = previous_available_holding_date(report_date)
    prev_holdings = load_holdings_snapshot(prev_date) if prev_date else {}

    diffs = calculate_etf_stock_diffs(today_holdings, prev_holdings) if today_holdings and prev_holdings else []
    all_stock_changes = aggregate_stock_changes(diffs, top_n=None)
    top_stock_changes = all_stock_changes[:TOP_N_STOCKS]
    kpis = build_kpis(all_stock_changes, top_stock_changes)
    quality = holdings_quality(len(master), len(today_holdings))

    net = safe_float(kpis["net_change_value_yi"])
    if top_stock_changes:
        market_bias = "偏多" if net > 0 else "偏空" if net < 0 else "中性"
    else:
        market_bias = "Master 已更新"

    return {
        "meta": {
            "report_date": report_date,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Taipei",
            "tracked_etfs": len(master),
            "covered_etfs": quality["covered_etfs"],
            "coverage_ratio": quality["coverage_ratio"],
            "universe": "TWSE listed ETFs",
            "market_bias": market_bias,
            "snapshot_mode": "production_twse_isin_master",
            "previous_snapshot_date": prev_date or "",
            "sample_mode": False,
        },
        "data_quality": quality,
        "kpis": kpis,
        "top_stock_changes": top_stock_changes,
        "stock_radar": build_stock_radar(top_stock_changes),
        "etf_rankings": build_etf_rankings(master, diffs),
        "events": build_events(top_stock_changes, quality, len(master)),
        "data_sources": build_data_sources(),
        "ai_report": build_ai_report(top_stock_changes, kpis, len(master)),
    }


def persist_report(report: Dict[str, Any]) -> None:
    report_date = report["meta"]["report_date"]
    atomic_write_json(DATA_DIR / "latest_report.json", report)
    atomic_write_json(HISTORY_DIR / f"{report_date}.json", report)
    logger.info("Wrote %s", DATA_DIR / "latest_report.json")
    logger.info("Wrote %s", HISTORY_DIR / f"{report_date}.json")


def main() -> int:
    try:
        report = build_report()
        persist_report(report)
        logger.info("Completed successfully.")
        return 0
    except Exception as exc:
        logger.exception("Generation failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
