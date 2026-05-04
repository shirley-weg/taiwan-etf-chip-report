from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo


# ============================================================
# Taiwan ETF Chip Report - Snapshot MVP
# ------------------------------------------------------------
# What this version does:
#   1. Creates raw/holdings/YYYY-MM-DD/{etf_code}.json snapshots.
#   2. Uses sample holdings to generate yesterday + today snapshots.
#   3. Loads yesterday and today snapshots.
#   4. Calculates per-ETF stock holding diffs.
#   5. Aggregates all ETF diffs into stock-level top changes.
#   6. Preserves participating_etfs details for frontend modal.
#   7. Outputs:
#        - data/latest_report.json
#        - history/YYYY-MM-DD.json
#
# How to use:
#   Put this file at:
#       scripts/generate_report.py
#
#   Then run:
#       python scripts/generate_report.py
#
# Later production upgrade:
#   Replace generate_sample_holdings_snapshots() with real crawlers:
#       - fetch_all_twse_tpex_etfs()
#       - fetch_today_all_etf_holdings()
# ============================================================


# ----------------------------
# Runtime configuration
# ----------------------------
ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = ROOT / "data"
HISTORY_DIR = ROOT / "history"
RAW_HOLDINGS_DIR = ROOT / "raw" / "holdings"

DATA_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_DIR.mkdir(parents=True, exist_ok=True)
RAW_HOLDINGS_DIR.mkdir(parents=True, exist_ok=True)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# SAMPLE_MODE=true means this script creates yesterday/today sample holdings.
# After real crawlers are completed, set USE_SAMPLE_HOLDINGS=0 in GitHub Actions.
USE_SAMPLE_HOLDINGS = os.getenv("USE_SAMPLE_HOLDINGS", "1").strip() == "1"

TOP_N_STOCKS = int(os.getenv("TOP_N_STOCKS", "10"))
MIN_COVERAGE_RATIO = float(os.getenv("MIN_COVERAGE_RATIO", "0.85"))
ALLOW_PARTIAL_REPORT = os.getenv("ALLOW_PARTIAL_REPORT", "1").strip() == "1"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("etf-snapshot-report")


# ============================================================
# Utility functions
# ============================================================
def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def previous_business_day(d: date) -> date:
    """
    Simple previous business day logic.
    For production, replace with TWSE trading calendar to handle holidays.
    """
    prev = d - timedelta(days=1)
    while prev.weekday() >= 5:  # 5=Saturday, 6=Sunday
        prev -= timedelta(days=1)
    return prev


def to_date_str(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def direction_from_value(value: float) -> str:
    if value > 0:
        return "buy"
    if value < 0:
        return "sell"
    return "neutral"


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """
    Avoid partially-written JSON files.
    """
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


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# ============================================================
# ETF master sample
# ============================================================
def fetch_all_twse_tpex_etfs() -> List[Dict[str, Any]]:
    """
    MVP:
      Returns sample ETF universe.

    Production:
      Replace this with TWSE + TPEx ETF master crawlers.

    Required fields:
      etf_code, etf_name, issuer, market, aum_yi
    """
    return [
        {
            "etf_code": "0050",
            "etf_name": "元大台灣50",
            "issuer": "元大投信",
            "market": "TWSE",
            "aum_yi": 4050.3,
            "close": 185.2,
            "top10_weight_pct": 72.8,
        },
        {
            "etf_code": "006208",
            "etf_name": "富邦台50",
            "issuer": "富邦投信",
            "market": "TWSE",
            "aum_yi": 1850.2,
            "close": 112.5,
            "top10_weight_pct": 71.4,
        },
        {
            "etf_code": "00878",
            "etf_name": "國泰永續高股息",
            "issuer": "國泰投信",
            "market": "TWSE",
            "aum_yi": 3210.8,
            "close": 23.1,
            "top10_weight_pct": 46.1,
        },
        {
            "etf_code": "00919",
            "etf_name": "群益台灣精選高息",
            "issuer": "群益投信",
            "market": "TWSE",
            "aum_yi": 2789.0,
            "close": 24.3,
            "top10_weight_pct": 42.7,
        },
        {
            "etf_code": "00929",
            "etf_name": "復華台灣科技優息",
            "issuer": "復華投信",
            "market": "TWSE",
            "aum_yi": 1640.6,
            "close": 19.8,
            "top10_weight_pct": 52.2,
        },
    ]


# ============================================================
# Raw holdings snapshot layer
# ============================================================
def holdings_folder(trade_date: str) -> Path:
    return RAW_HOLDINGS_DIR / trade_date


def holdings_path(trade_date: str, etf_code: str) -> Path:
    return holdings_folder(trade_date) / f"{etf_code}.json"


def save_etf_holdings_snapshot(trade_date: str, etf_code: str, payload: Dict[str, Any]) -> None:
    """
    Save one ETF holdings snapshot:
        raw/holdings/YYYY-MM-DD/{etf_code}.json
    """
    path = holdings_path(trade_date, etf_code)
    atomic_write_json(path, payload)


def load_holdings_snapshot(trade_date: str) -> Dict[str, Dict[str, Any]]:
    """
    Load all ETF holdings snapshots for one date.

    Return:
      {
        "0050": { snapshot payload },
        "00878": { snapshot payload },
        ...
      }
    """
    folder = holdings_folder(trade_date)
    if not folder.exists():
        logger.warning("Holdings folder does not exist: %s", folder)
        return {}

    output: Dict[str, Dict[str, Any]] = {}
    for path in sorted(folder.glob("*.json")):
        try:
            payload = read_json(path)
            etf_code = str(payload.get("etf_code", path.stem))
            output[etf_code] = payload
        except Exception as exc:
            logger.warning("Failed to read %s: %s", path, exc)

    return output


def get_previous_available_holding_date(report_date: str, max_lookback_days: int = 10) -> Optional[str]:
    """
    Find latest prior raw/holdings/YYYY-MM-DD folder with JSON files.
    This is better than simply using yesterday because of weekends/holidays.
    """
    current = datetime.strptime(report_date, "%Y-%m-%d").date()

    for i in range(1, max_lookback_days + 1):
        candidate = to_date_str(current - timedelta(days=i))
        folder = holdings_folder(candidate)
        if folder.exists() and any(folder.glob("*.json")):
            return candidate

    return None


# ============================================================
# Sample holdings generator
# ============================================================
def make_holding(
    stock_code: str,
    stock_name: str,
    shares: int,
    close: float,
    weight_pct: float,
) -> Dict[str, Any]:
    return {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "shares": int(shares),
        "weight_pct": round(float(weight_pct), 4),
        "close": round(float(close), 2),
        "market_value": round(int(shares) * float(close), 2),
    }


def build_sample_holdings_for_date(
    trade_date: str,
    etf: Dict[str, Any],
    day_type: str,
) -> Dict[str, Any]:
    """
    Create sample ETF holdings.
    day_type:
      - "yesterday"
      - "today"

    The difference between yesterday and today is intentional.
    It allows calculate_etf_stock_diffs() to produce real diffs.
    """
    etf_code = etf["etf_code"]

    # Base holdings by ETF.
    base: Dict[str, List[Dict[str, Any]]] = {
        "0050": [
            make_holding("2330", "台積電", 900_000, 950, 45.2),
            make_holding("2317", "鴻海", 500_000, 170, 4.8),
            make_holding("2454", "聯發科", 120_000, 1180, 5.6),
            make_holding("2382", "廣達", 180_000, 260, 2.4),
            make_holding("2881", "富邦金", 300_000, 78, 1.3),
        ],
        "006208": [
            make_holding("2330", "台積電", 420_000, 950, 46.1),
            make_holding("2317", "鴻海", 260_000, 170, 4.6),
            make_holding("2454", "聯發科", 80_000, 1180, 5.2),
            make_holding("2382", "廣達", 90_000, 260, 2.0),
            make_holding("2881", "富邦金", 160_000, 78, 1.2),
        ],
        "00878": [
            make_holding("2330", "台積電", 260_000, 950, 8.1),
            make_holding("2891", "中信金", 600_000, 38, 4.3),
            make_holding("2881", "富邦金", 480_000, 78, 5.2),
            make_holding("2603", "長榮", 350_000, 170, 4.8),
            make_holding("2317", "鴻海", 220_000, 170, 3.1),
        ],
        "00919": [
            make_holding("2330", "台積電", 200_000, 950, 7.2),
            make_holding("2891", "中信金", 520_000, 38, 4.0),
            make_holding("2881", "富邦金", 430_000, 78, 5.0),
            make_holding("2603", "長榮", 420_000, 170, 5.2),
            make_holding("2317", "鴻海", 150_000, 170, 2.4),
        ],
        "00929": [
            make_holding("2330", "台積電", 160_000, 950, 12.5),
            make_holding("2317", "鴻海", 240_000, 170, 5.1),
            make_holding("2454", "聯發科", 55_000, 1180, 4.8),
            make_holding("2382", "廣達", 260_000, 260, 5.6),
            make_holding("3037", "欣興", 180_000, 185, 3.2),
        ],
    }

    rows = [dict(x) for x in base.get(etf_code, [])]

    if day_type == "today":
        # Apply deterministic changes by ETF.
        # Positive = ETF adds holdings; negative = ETF cuts holdings.
        adjustments: Dict[str, Dict[str, int]] = {
            "0050": {
                "2330": 6_200_000,
                "2317": 5_200_000,
                "2454": 1_200_000,
                "2382": -1_200_000,
                "2881": 650_000,
                "3711": 2_600_000,  # new position
            },
            "006208": {
                "2330": 1_900_000,
                "2317": 3_100_000,
                "2454": 880_000,
                "2382": -890_000,
                "2881": 400_000,
                "3711": 1_800_000,
            },
            "00878": {
                "2330": 2_600_000,
                "2317": 1_300_000,
                "2891": 5_200_000,
                "2881": 2_800_000,
                "2603": -3_000_000,
            },
            "00919": {
                "2330": 2_100_000,
                "2317": 900_000,
                "2891": 4_300_000,
                "2881": 2_400_000,
                "2603": -4_200_000,
            },
            "00929": {
                "2330": 1_300_000,
                "2317": 2_400_000,
                "2454": 410_000,
                "2382": -2_300_000,
                "3037": -1_900_000,
                "3711": 980_000,
            },
        }

        prices = {
            "2330": ("台積電", 950),
            "2317": ("鴻海", 170),
            "2454": ("聯發科", 1180),
            "2382": ("廣達", 260),
            "2881": ("富邦金", 78),
            "2891": ("中信金", 38),
            "2603": ("長榮", 170),
            "3037": ("欣興", 185),
            "3711": ("日月光投控", 155),
        }

        row_map = {row["stock_code"]: row for row in rows}
        for stock_code, delta_shares in adjustments.get(etf_code, {}).items():
            stock_name, close = prices[stock_code]

            if stock_code not in row_map:
                row_map[stock_code] = make_holding(stock_code, stock_name, 0, close, 0.0)

            row = row_map[stock_code]
            row["shares"] = max(0, safe_int(row["shares"]) + delta_shares)
            row["close"] = close
            row["market_value"] = round(row["shares"] * close, 2)

            # Simple sample weight movement.
            row["weight_pct"] = round(safe_float(row.get("weight_pct")) + delta_shares / 1_000_000 * 0.01, 4)

        rows = list(row_map.values())

    return {
        "trade_date": trade_date,
        "etf_code": etf["etf_code"],
        "etf_name": etf["etf_name"],
        "issuer": etf.get("issuer", ""),
        "market": etf.get("market", ""),
        "source": "sample_holdings_snapshot",
        "holdings": sorted(rows, key=lambda x: x["stock_code"]),
    }


def generate_sample_holdings_snapshots(
    etf_master: List[Dict[str, Any]],
    report_date: str,
    previous_date: str,
) -> None:
    """
    Generate both yesterday and today snapshots.
    This lets you test the full formal pipeline before connecting crawlers.
    """
    logger.info("Generating sample holdings snapshots: %s and %s", previous_date, report_date)

    for etf in etf_master:
        yesterday_payload = build_sample_holdings_for_date(previous_date, etf, "yesterday")
        today_payload = build_sample_holdings_for_date(report_date, etf, "today")

        save_etf_holdings_snapshot(previous_date, etf["etf_code"], yesterday_payload)
        save_etf_holdings_snapshot(report_date, etf["etf_code"], today_payload)


# ============================================================
# Production placeholders
# ============================================================
def fetch_today_all_etf_holdings(etf_master: List[Dict[str, Any]], report_date: str) -> Dict[str, Dict[str, Any]]:
    """
    Production version should:
      1. Loop through all ETFs from TWSE + TPEx master.
      2. Dispatch by issuer:
           元大 -> fetch_yuanta_pcf()
           富邦 -> fetch_fubon_pcf()
           國泰 -> fetch_cathay_pcf()
           群益 -> fetch_capital_pcf()
           ...
      3. Save each result into raw/holdings/YYYY-MM-DD/{etf_code}.json
      4. Return loaded snapshots.

    MVP version:
      - If USE_SAMPLE_HOLDINGS=1, snapshots are already created by
        generate_sample_holdings_snapshots().
      - Then simply load raw/holdings/{report_date}.
    """
    if not USE_SAMPLE_HOLDINGS:
        raise NotImplementedError(
            "Real PCF crawlers are not implemented yet. "
            "Keep USE_SAMPLE_HOLDINGS=1 until issuer crawlers are ready."
        )

    return load_holdings_snapshot(report_date)


# ============================================================
# Diff calculation
# ============================================================
def normalize_holding_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "stock_code": str(row.get("stock_code", "")).strip(),
        "stock_name": str(row.get("stock_name", "")).strip(),
        "shares": safe_float(row.get("shares", 0)),
        "weight_pct": safe_float(row.get("weight_pct", 0)),
        "close": safe_float(row.get("close", 0)),
        "market_value": safe_float(row.get("market_value", 0)),
    }


def calculate_etf_stock_diffs(
    today_holdings: Dict[str, Dict[str, Any]],
    yesterday_holdings: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Calculate ETF-level stock diffs.

    Input:
      today_holdings["0050"]["holdings"] = list of stock rows
      yesterday_holdings["0050"]["holdings"] = list of stock rows

    Output rows:
      {
        etf_code,
        etf_name,
        stock_code,
        stock_name,
        delta_shares,
        delta_lot,
        delta_value_yi,
        weight_delta_pct,
      }
    """
    diffs: List[Dict[str, Any]] = []

    for etf_code, today_payload in sorted(today_holdings.items()):
        yesterday_payload = yesterday_holdings.get(etf_code)
        if not yesterday_payload:
            logger.warning("No yesterday snapshot for ETF %s. Skip diff.", etf_code)
            continue

        etf_name = today_payload.get("etf_name") or yesterday_payload.get("etf_name") or ""
        today_rows = [normalize_holding_row(x) for x in today_payload.get("holdings", [])]
        yesterday_rows = [normalize_holding_row(x) for x in yesterday_payload.get("holdings", [])]

        today_map = {row["stock_code"]: row for row in today_rows if row["stock_code"]}
        yesterday_map = {row["stock_code"]: row for row in yesterday_rows if row["stock_code"]}

        all_stock_codes = sorted(set(today_map) | set(yesterday_map))

        for stock_code in all_stock_codes:
            today_row = today_map.get(stock_code, {})
            yesterday_row = yesterday_map.get(stock_code, {})

            stock_name = (
                today_row.get("stock_name")
                or yesterday_row.get("stock_name")
                or ""
            )

            today_shares = safe_float(today_row.get("shares", 0))
            yesterday_shares = safe_float(yesterday_row.get("shares", 0))
            delta_shares = today_shares - yesterday_shares

            if abs(delta_shares) < 1:
                continue

            price = (
                safe_float(today_row.get("close", 0))
                or safe_float(yesterday_row.get("close", 0))
            )

            today_weight = safe_float(today_row.get("weight_pct", 0))
            yesterday_weight = safe_float(yesterday_row.get("weight_pct", 0))
            weight_delta_pct = today_weight - yesterday_weight

            diffs.append(
                {
                    "etf_code": etf_code,
                    "etf_name": etf_name,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "delta_shares": round(delta_shares, 0),
                    "delta_lot": round(delta_shares / 1000, 1),
                    "delta_value_yi": round(delta_shares * price / 100_000_000, 4),
                    "weight_delta_pct": round(weight_delta_pct, 4),
                }
            )

    return diffs


# ============================================================
# Aggregation and report sections
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


def aggregate_stock_changes(etf_stock_diffs: List[Dict[str, Any]], top_n: Optional[int] = None) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for row in etf_stock_diffs:
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

    if top_n is not None:
        return output[:top_n]
    return output


def build_etf_rankings(
    etf_master: List[Dict[str, Any]],
    etf_stock_diffs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    diffs_by_etf: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in etf_stock_diffs:
        diffs_by_etf[row["etf_code"]].append(row)

    output: List[Dict[str, Any]] = []

    for etf in etf_master:
        etf_code = etf["etf_code"]
        rows = diffs_by_etf.get(etf_code, [])

        buy_value_yi = sum(max(0.0, safe_float(x["delta_value_yi"])) for x in rows)
        sell_value_yi = abs(sum(min(0.0, safe_float(x["delta_value_yi"])) for x in rows))
        net_flow_yi = buy_value_yi - sell_value_yi

        aum_yi = safe_float(etf.get("aum_yi"))
        turnover_pct = (buy_value_yi + sell_value_yi) / aum_yi * 100 if aum_yi > 0 else 0.0

        output.append(
            {
                "etf_code": etf_code,
                "etf_name": etf.get("etf_name", ""),
                "aum_yi": round(aum_yi, 1),
                "buy_value_yi": round(buy_value_yi, 2),
                "sell_value_yi": round(sell_value_yi, 2),
                "net_flow_yi": round(net_flow_yi, 2),
                "turnover_pct": round(turnover_pct, 2),
                "top10_weight_pct": round(safe_float(etf.get("top10_weight_pct")), 1),
            }
        )

    output.sort(
        key=lambda x: abs(safe_float(x["buy_value_yi"]) + safe_float(x["sell_value_yi"])),
        reverse=True,
    )
    return output


def build_kpis(all_stock_changes: List[Dict[str, Any]], top_stock_changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    net = sum(safe_float(x["delta_value_yi"]) for x in all_stock_changes)

    consensus_buy = sum(
        1 for x in all_stock_changes
        if safe_float(x["delta_value_yi"]) > 0 and safe_int(x.get("etf_count")) >= 2
    )
    consensus_sell = sum(
        1 for x in all_stock_changes
        if safe_float(x["delta_value_yi"]) < 0 and safe_int(x.get("etf_count")) >= 2
    )

    total_abs = sum(abs(safe_float(x["delta_value_yi"])) for x in all_stock_changes) or 1.0
    top3_abs = sum(abs(safe_float(x["delta_value_yi"])) for x in top_stock_changes[:3])
    concentration_score = round(top3_abs / total_abs * 100)

    return {
        "net_change_value_yi": round(net, 1),
        "consensus_buy_count": consensus_buy,
        "consensus_sell_count": consensus_sell,
        "concentration_score": concentration_score,
    }


def build_stock_radar(top_stock_changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []

    for item in top_stock_changes:
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


def build_events(top_stock_changes: List[Dict[str, Any]], quality: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    buys = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) > 0]
    sells = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) < 0]

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
            "title": "ETF 持股快照覆蓋率",
            "desc": (
                f"今日已覆蓋 {quality['covered_etfs']}/{quality['tracked_etfs']} 檔 ETF，"
                f"覆蓋率 {quality['coverage_ratio']:.1%}。"
            ),
        }
    )

    return events


def build_ai_report(
    top_stock_changes: List[Dict[str, Any]],
    kpis: Dict[str, Any],
    quality: Dict[str, Any],
) -> Dict[str, Any]:
    net = safe_float(kpis["net_change_value_yi"])
    bias = "偏多" if net > 0 else "偏空" if net < 0 else "中性"

    buys = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) > 0]
    sells = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) < 0]

    headline = f"今日全市場 ETF 籌碼{bias}，前十大變動淨額 {net:.1f} 億元。"

    summary_parts = [
        f"本報告以 TWSE + TPEx 全部 ETF 為分析範圍，今日持股快照覆蓋率為 {quality['coverage_ratio']:.1%}。",
        f"跨 ETF 持股差分顯示，共識加碼股票 {kpis['consensus_buy_count']} 檔，共識減碼股票 {kpis['consensus_sell_count']} 檔。",
        f"前三大變動占整體變動絕對值 {kpis['concentration_score']}%，可用來衡量今日 ETF 資金是否集中於少數權值股。",
    ]

    if buys:
        top = buys[0]
        summary_parts.append(
            f"最大買盤為 {top['stock_code']} {top['stock_name']}，估算加碼 {top['delta_value_yi']} 億元，參與 ETF {top['etf_count']} 檔。"
        )

    if sells:
        top = sorted(sells, key=lambda x: safe_float(x["delta_value_yi"]))[0]
        summary_parts.append(
            f"最大賣壓為 {top['stock_code']} {top['stock_name']}，估算減碼 {abs(safe_float(top['delta_value_yi'])):.2f} 億元，參與 ETF {top['etf_count']} 檔。"
        )

    watchlist = []
    for x in top_stock_changes[:5]:
        direction = "加碼" if safe_float(x["delta_value_yi"]) > 0 else "減碼"
        watchlist.append(
            f"{x['stock_code']} {x['stock_name']}：ETF {direction} {abs(safe_float(x['delta_value_yi'])):.2f} 億，參與 ETF {x['etf_count']} 檔"
        )

    risk = (
        "此版本使用 sample holdings snapshot 驗證資料流程；正式上線後，需確認 TWSE / TPEx / 投信 PCF 資料完整性。"
        "若覆蓋率未達 100%，請避免將報告視為完整市場結論。"
    )

    return {
        "headline": headline,
        "summary": "".join(summary_parts),
        "watchlist": watchlist,
        "risk": risk,
    }


def build_data_sources() -> List[Dict[str, Any]]:
    return [
        {
            "name": "TWSE ETF e添富",
            "type": "ETF master / AUM / ranking",
            "update_freq": "每日或盤中依官方更新",
            "status": "watch",
            "fields": ["etf_code", "etf_name", "aum", "close", "issuer", "beneficiaries"],
        },
        {
            "name": "TWSE / TPEx ETF 交易資料",
            "type": "成交價量 / 折溢價輔助",
            "update_freq": "交易日",
            "status": "watch",
            "fields": ["close", "volume", "turnover", "premium_discount"],
        },
        {
            "name": "各投信 PCF / 每日持股揭露",
            "type": "每日持股核心資料",
            "update_freq": "每日盤前或盤後",
            "status": "watch",
            "fields": ["stock_code", "shares", "weight", "cash_component", "creation_unit"],
        },
        {
            "name": "raw/holdings snapshot",
            "type": "本系統標準化後持股快照",
            "update_freq": "每日",
            "status": "ready",
            "fields": ["trade_date", "etf_code", "stock_code", "shares", "weight_pct", "close"],
        },
    ]


def calculate_snapshot_quality(
    etf_master: List[Dict[str, Any]],
    today_holdings: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    tracked = len(etf_master)
    covered = len(today_holdings)
    coverage = covered / tracked if tracked else 0.0

    return {
        "tracked_etfs": tracked,
        "covered_etfs": covered,
        "coverage_ratio": round(coverage, 4),
        "min_required_coverage_ratio": MIN_COVERAGE_RATIO,
        "is_ready": coverage >= MIN_COVERAGE_RATIO,
        "allow_partial_report": ALLOW_PARTIAL_REPORT,
    }


# ============================================================
# Report generation
# ============================================================
def build_report() -> Optional[Dict[str, Any]]:
    now = now_taipei()
    report_date = to_date_str(now.date())
    default_previous_date = to_date_str(previous_business_day(now.date()))

    logger.info("Report date: %s", report_date)
    logger.info("Default previous business date: %s", default_previous_date)

    etf_master = fetch_all_twse_tpex_etfs()

    if USE_SAMPLE_HOLDINGS:
        generate_sample_holdings_snapshots(
            etf_master=etf_master,
            report_date=report_date,
            previous_date=default_previous_date,
        )

    today_holdings = fetch_today_all_etf_holdings(etf_master, report_date)
    previous_snapshot_date = get_previous_available_holding_date(report_date) or default_previous_date
    yesterday_holdings = load_holdings_snapshot(previous_snapshot_date)

    quality = calculate_snapshot_quality(etf_master, today_holdings)
    logger.info(
        "Snapshot coverage: %s/%s = %.1f%%",
        quality["covered_etfs"],
        quality["tracked_etfs"],
        quality["coverage_ratio"] * 100,
    )

    if not quality["is_ready"] and not ALLOW_PARTIAL_REPORT:
        logger.warning("Snapshot data is not ready. Skip output.")
        return None

    etf_stock_diffs = calculate_etf_stock_diffs(today_holdings, yesterday_holdings)
    logger.info("Calculated ETF-stock diff rows: %s", len(etf_stock_diffs))

    all_stock_changes = aggregate_stock_changes(etf_stock_diffs, top_n=None)
    top_stock_changes = all_stock_changes[:TOP_N_STOCKS]

    etf_rankings = build_etf_rankings(etf_master, etf_stock_diffs)
    kpis = build_kpis(all_stock_changes, top_stock_changes)
    stock_radar = build_stock_radar(top_stock_changes)
    events = build_events(top_stock_changes, quality)
    ai_report = build_ai_report(top_stock_changes, kpis, quality)

    net = safe_float(kpis["net_change_value_yi"])
    market_bias = "偏多" if net > 0 else "偏空" if net < 0 else "中性"

    return {
        "meta": {
            "report_date": report_date,
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "timezone": "Asia/Taipei",
            "tracked_etfs": quality["tracked_etfs"],
            "covered_etfs": quality["covered_etfs"],
            "coverage_ratio": quality["coverage_ratio"],
            "universe": "TWSE + TPEx 全部 ETF",
            "market_bias": market_bias,
            "snapshot_mode": "sample" if USE_SAMPLE_HOLDINGS else "production",
            "previous_snapshot_date": previous_snapshot_date,
        },
        "data_quality": quality,
        "kpis": kpis,
        "top_stock_changes": top_stock_changes,
        "stock_radar": stock_radar,
        "etf_rankings": etf_rankings,
        "events": events,
        "data_sources": build_data_sources(),
        "ai_report": ai_report,
    }


def persist_report(report: Dict[str, Any]) -> None:
    report_date = report["meta"]["report_date"]

    latest_path = DATA_DIR / "latest_report.json"
    history_path = HISTORY_DIR / f"{report_date}.json"

    atomic_write_json(latest_path, report)
    atomic_write_json(history_path, report)

    logger.info("Generated %s", latest_path)
    logger.info("Generated %s", history_path)


def main() -> int:
    try:
        report = build_report()
        if report is None:
            logger.info("No report generated.")
            return 0

        persist_report(report)
        return 0

    except Exception as exc:
        logger.exception("Report generation failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
