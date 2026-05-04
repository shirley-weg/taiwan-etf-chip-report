from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from zoneinfo import ZoneInfo


# ============================================================
# Taiwan ETF Chip Report Generator
# ------------------------------------------------------------
# Purpose:
#   1. Build a daily all-market Taiwan ETF chip-flow report.
#   2. Output data/latest_report.json for GitHub Pages frontend.
#   3. Output history/YYYY-MM-DD.json for historical archive.
#   4. Preserve participating ETF details for each stock signal.
#
# Production design:
#   - GitHub Actions runs this script after Taiwan market close.
#   - Official source adapters should replace SAMPLE_MODE functions.
#   - The frontend should never fetch official sites directly.
# ============================================================


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
HISTORY_DIR = ROOT / "history"
DATA_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)

TAIPEI_TZ = ZoneInfo("Asia/Taipei")

# Keep USE_SAMPLE_DATA=1 until real source adapters are implemented.
# In GitHub Actions, change this to 0 after TWSE/TPEx/PCF adapters are ready.
USE_SAMPLE_DATA = os.getenv("USE_SAMPLE_DATA", "1").strip() == "1"
TOP_N_STOCKS = int(os.getenv("TOP_N_STOCKS", "10"))
MIN_COVERAGE_RATIO = float(os.getenv("MIN_COVERAGE_RATIO", "0.85"))
ALLOW_PARTIAL_REPORT = os.getenv("ALLOW_PARTIAL_REPORT", "0").strip() == "1"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("etf-report")

Direction = Literal["buy", "sell", "neutral"]


@dataclass(frozen=True)
class ETFMaster:
    etf_code: str
    etf_name: str
    issuer: str = ""
    market: Literal["TWSE", "TPEx", "UNKNOWN"] = "UNKNOWN"
    aum_yi: float = 0.0
    close: float = 0.0
    turnover_pct: float = 0.0
    top10_weight_pct: float = 0.0


@dataclass(frozen=True)
class ETFStockDiff:
    etf_code: str
    etf_name: str
    stock_code: str
    stock_name: str
    delta_lot: float
    delta_value_yi: float
    weight_delta_pct: float = 0.0


def now_taipei() -> datetime:
    return datetime.now(TAIPEI_TZ)


def round_money(value: float, digits: int = 2) -> float:
    return round(float(value), digits)


def round_lot(value: float, digits: int = 1) -> float:
    return round(float(value), digits)


def decide_direction(value: float, tolerance: float = 1e-9) -> Direction:
    if value > tolerance:
        return "buy"
    if value < -tolerance:
        return "sell"
    return "neutral"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically to avoid partially-written report files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False, suffix=".tmp") as tmp:
        json.dump(payload, tmp, ensure_ascii=False, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


# ============================================================
# Official data adapters
# ============================================================
def fetch_all_twse_tpex_etfs() -> List[ETFMaster]:
    """
    Return the complete Taiwan ETF universe.

    Production target:
      - TWSE listed ETFs
      - TPEx listed ETFs
      - ETF name, issuer, market, AUM, close, concentration if available

    Current implementation:
      - USE_SAMPLE_DATA=1 returns deterministic sample data.
      - USE_SAMPLE_DATA=0 raises NotImplementedError until official adapters are implemented.
    """
    if USE_SAMPLE_DATA:
        return build_sample_etf_master()

    raise NotImplementedError(
        "Official TWSE/TPEx ETF master adapters are not implemented yet. "
        "Implement fetch_all_twse_tpex_etfs() with TWSE + TPEx sources, "
        "or keep USE_SAMPLE_DATA=1 for UI testing."
    )


def fetch_all_etf_stock_diffs(etf_master: List[ETFMaster]) -> List[ETFStockDiff]:
    """
    Return ETF-level stock holding diffs.

    Each row means ETF X changed its holding of stock Y today.

    Production target:
      today ETF holdings - previous ETF holdings = per-ETF stock diff

    Required official input:
      - Today's PCF / daily holdings for each ETF
      - Yesterday's PCF / daily holdings for each ETF
      - Stock close price or official valuation price
      - Weight percentage where available
    """
    if USE_SAMPLE_DATA:
        return build_sample_etf_stock_diffs(etf_master)

    raise NotImplementedError(
        "Official ETF holdings/PCF adapters are not implemented yet. "
        "Implement fetch_all_etf_stock_diffs() to fetch issuer PCF/daily holdings "
        "and compute today-vs-yesterday diffs."
    )


# ============================================================
# Deterministic sample data for UI and pipeline validation
# ============================================================
def build_sample_etf_master() -> List[ETFMaster]:
    return [
        ETFMaster("0050", "元大台灣50", "元大投信", "TWSE", 4050.3, 185.2, 0.4, 72.8),
        ETFMaster("006208", "富邦台50", "富邦投信", "TWSE", 1850.2, 112.5, 0.5, 71.4),
        ETFMaster("0056", "元大高股息", "元大投信", "TWSE", 2890.4, 37.8, 0.7, 39.8),
        ETFMaster("00878", "國泰永續高股息", "國泰投信", "TWSE", 3210.8, 23.1, 0.8, 46.1),
        ETFMaster("00919", "群益台灣精選高息", "群益投信", "TWSE", 2789.0, 24.3, 1.1, 42.7),
        ETFMaster("00929", "復華台灣科技優息", "復華投信", "TWSE", 1640.6, 19.8, 1.5, 52.2),
        ETFMaster("00922", "國泰台灣領袖50", "國泰投信", "TWSE", 820.3, 22.4, 1.0, 66.4),
        ETFMaster("00923", "群益台ESG低碳50", "群益投信", "TWSE", 510.5, 21.9, 0.9, 61.7),
        ETFMaster("00713", "元大台灣高息低波", "元大投信", "TWSE", 1410.9, 57.2, 0.8, 41.3),
        ETFMaster("00915", "凱基優選高股息30", "凱基投信", "TWSE", 760.1, 26.8, 0.7, 44.0),
        ETFMaster("00900", "富邦特選高股息30", "富邦投信", "TWSE", 390.7, 14.2, 0.6, 38.2),
        ETFMaster("00918", "大華優利高填息30", "大華銀投信", "TWSE", 640.2, 22.1, 0.8, 40.4),
        ETFMaster("00927", "群益半導體收益", "群益投信", "TWSE", 450.6, 18.6, 1.2, 58.8),
        ETFMaster("00881", "國泰台灣5G+", "國泰投信", "TWSE", 720.4, 17.4, 1.0, 57.1),
        ETFMaster("00892", "富邦台灣半導體", "富邦投信", "TWSE", 610.2, 13.8, 1.1, 60.9),
    ]


def build_sample_etf_stock_diffs(etf_master: List[ETFMaster]) -> List[ETFStockDiff]:
    raw = [
        ("0050", "2330", "台積電", 6200, 6.40, 0.21),
        ("006208", "2330", "台積電", 1900, 2.00, 0.08),
        ("00878", "2330", "台積電", 2600, 2.70, 0.09),
        ("00919", "2330", "台積電", 2100, 2.20, 0.07),
        ("00713", "2330", "台積電", 1650, 1.70, 0.06),
        ("00929", "2330", "台積電", 1300, 1.30, 0.05),
        ("00915", "2330", "台積電", 1100, 1.10, 0.04),
        ("00922", "2330", "台積電", 820, 0.80, 0.03),
        ("00923", "2330", "台積電", 580, 0.50, 0.02),
        ("0050", "2317", "鴻海", 5200, 3.50, 0.18),
        ("006208", "2317", "鴻海", 3100, 2.10, 0.11),
        ("00929", "2317", "鴻海", 2400, 1.60, 0.08),
        ("00922", "2317", "鴻海", 1900, 1.30, 0.06),
        ("00878", "2317", "鴻海", 1300, 0.90, 0.04),
        ("00919", "2317", "鴻海", 900, 0.60, 0.03),
        ("00713", "2317", "鴻海", 410, 0.30, 0.01),
        ("0050", "2454", "聯發科", 1200, 2.60, 0.14),
        ("006208", "2454", "聯發科", 880, 1.90, 0.10),
        ("00922", "2454", "聯發科", 520, 1.10, 0.06),
        ("00929", "2454", "聯發科", 410, 0.90, 0.05),
        ("00923", "2454", "聯發科", 200, 0.40, 0.03),
        ("00878", "2881", "富邦金", 2800, 1.70, 0.08),
        ("00919", "2881", "富邦金", 2400, 1.50, 0.07),
        ("00713", "2881", "富邦金", 1500, 0.90, 0.04),
        ("00915", "2881", "富邦金", 1100, 0.70, 0.03),
        ("0050", "2881", "富邦金", 650, 0.40, 0.02),
        ("006208", "2881", "富邦金", 400, 0.20, 0.01),
        ("00878", "2891", "中信金", 5200, 1.80, 0.07),
        ("00919", "2891", "中信金", 4300, 1.50, 0.06),
        ("0056", "2891", "中信金", 3500, 1.20, 0.05),
        ("00713", "2891", "中信金", 2400, 0.80, 0.03),
        ("00915", "2891", "中信金", 1620, 0.50, 0.01),
        ("0050", "2308", "台達電", 1800, 2.00, 0.08),
        ("006208", "2308", "台達電", 1100, 1.20, 0.05),
        ("00929", "2308", "台達電", 760, 0.90, 0.04),
        ("00922", "2308", "台達電", 510, 0.60, 0.02),
        ("00919", "2603", "長榮", -4200, -2.70, -0.13),
        ("00878", "2603", "長榮", -3000, -2.00, -0.09),
        ("00713", "2603", "長榮", -2100, -1.40, -0.06),
        ("00915", "2603", "長榮", -1500, -1.00, -0.05),
        ("0056", "2603", "長榮", -800, -0.50, -0.02),
        ("00929", "2382", "廣達", -2300, -2.60, -0.12),
        ("00922", "2382", "廣達", -1700, -1.90, -0.09),
        ("0050", "2382", "廣達", -1200, -1.30, -0.06),
        ("006208", "2382", "廣達", -890, -1.00, -0.04),
        ("00929", "3037", "欣興", -1900, -1.40, -0.08),
        ("00922", "3037", "欣興", -1500, -1.10, -0.06),
        ("00923", "3037", "欣興", -900, -0.60, -0.04),
        ("0050", "3711", "日月光投控", 2600, 1.40, 0.06),
        ("006208", "3711", "日月光投控", 1800, 1.00, 0.04),
        ("00929", "3711", "日月光投控", 980, 0.60, 0.03),
    ]

    etf_name_by_code = {x.etf_code: x.etf_name for x in etf_master}
    return [
        ETFStockDiff(
            etf_code=etf_code,
            etf_name=etf_name_by_code.get(etf_code, ""),
            stock_code=stock_code,
            stock_name=stock_name,
            delta_lot=float(delta_lot),
            delta_value_yi=float(delta_value_yi),
            weight_delta_pct=float(weight_delta_pct),
        )
        for etf_code, stock_code, stock_name, delta_lot, delta_value_yi, weight_delta_pct in raw
    ]


# ============================================================
# Data quality and readiness
# ============================================================
def calculate_coverage(etf_master: List[ETFMaster], etf_stock_diffs: List[ETFStockDiff]) -> Dict[str, Any]:
    total_etfs = len({x.etf_code for x in etf_master})
    covered_etfs = len({x.etf_code for x in etf_stock_diffs})
    coverage_ratio = covered_etfs / total_etfs if total_etfs else 0.0
    return {
        "total_etfs": total_etfs,
        "covered_etfs": covered_etfs,
        "coverage_ratio": round(coverage_ratio, 4),
        "min_required_coverage_ratio": MIN_COVERAGE_RATIO,
        "is_ready": coverage_ratio >= MIN_COVERAGE_RATIO,
        "allow_partial_report": ALLOW_PARTIAL_REPORT,
    }


def should_generate_report(quality: Dict[str, Any]) -> bool:
    if quality["is_ready"]:
        return True
    if ALLOW_PARTIAL_REPORT:
        logger.warning("Coverage below threshold, but ALLOW_PARTIAL_REPORT=1. Continue.")
        return True
    logger.warning(
        "Data not ready. Coverage %.2f%% < %.2f%%. Skip report generation.",
        quality["coverage_ratio"] * 100,
        MIN_COVERAGE_RATIO * 100,
    )
    return False


# ============================================================
# Business logic
# ============================================================
def make_stock_signal(stock_name: str, direction: Direction, etf_count: int, value_yi: float) -> str:
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


def aggregate_stock_changes(etf_stock_diffs: List[ETFStockDiff], top_n: int = TOP_N_STOCKS) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[ETFStockDiff]] = defaultdict(list)
    for row in etf_stock_diffs:
        if abs(row.delta_value_yi) < 1e-9 and abs(row.delta_lot) < 1e-9:
            continue
        grouped[row.stock_code].append(row)

    output: List[Dict[str, Any]] = []
    for stock_code, rows in grouped.items():
        stock_name = rows[0].stock_name
        total_lot = sum(x.delta_lot for x in rows)
        total_value_yi = sum(x.delta_value_yi for x in rows)
        total_weight_delta = sum(x.weight_delta_pct for x in rows)
        direction = decide_direction(total_value_yi)

        participants = []
        for x in sorted(rows, key=lambda r: abs(r.delta_value_yi), reverse=True):
            participant_direction = decide_direction(x.delta_value_yi)
            if participant_direction == "neutral":
                continue
            participants.append(
                {
                    "etf_code": x.etf_code,
                    "etf_name": x.etf_name,
                    "direction": participant_direction,
                    "delta_lot": round_lot(x.delta_lot),
                    "delta_value_yi": round_money(x.delta_value_yi),
                    "weight_delta_pct": round_money(x.weight_delta_pct, 3),
                }
            )

        etf_count = len(participants)
        output.append(
            {
                "stock_code": stock_code,
                "stock_name": stock_name,
                "direction": direction,
                "delta_lot": round_lot(total_lot),
                "delta_value_yi": round_money(total_value_yi),
                "etf_count": etf_count,
                "weight_delta_pct": round_money(total_weight_delta, 3),
                "signal": make_stock_signal(stock_name, direction, etf_count, total_value_yi),
                "participating_etfs": participants,
            }
        )

    output.sort(key=lambda x: abs(safe_float(x["delta_value_yi"])), reverse=True)
    return output[:top_n]


def build_etf_rankings(etf_master: List[ETFMaster], etf_stock_diffs: List[ETFStockDiff]) -> List[Dict[str, Any]]:
    diffs_by_etf: Dict[str, List[ETFStockDiff]] = defaultdict(list)
    for row in etf_stock_diffs:
        diffs_by_etf[row.etf_code].append(row)

    rankings: List[Dict[str, Any]] = []
    for etf in etf_master:
        rows = diffs_by_etf.get(etf.etf_code, [])
        buy_value_yi = sum(max(0.0, x.delta_value_yi) for x in rows)
        sell_value_yi = abs(sum(min(0.0, x.delta_value_yi) for x in rows))
        net_flow_yi = buy_value_yi - sell_value_yi
        turnover_pct = etf.turnover_pct
        if etf.aum_yi > 0:
            turnover_pct = max(turnover_pct, (buy_value_yi + sell_value_yi) / etf.aum_yi * 100)
        rankings.append(
            {
                "etf_code": etf.etf_code,
                "etf_name": etf.etf_name,
                "aum_yi": round_money(etf.aum_yi, 1),
                "buy_value_yi": round_money(buy_value_yi, 2),
                "sell_value_yi": round_money(sell_value_yi, 2),
                "net_flow_yi": round_money(net_flow_yi, 2),
                "turnover_pct": round_money(turnover_pct, 2),
                "top10_weight_pct": round_money(etf.top10_weight_pct, 1),
            }
        )
    rankings.sort(key=lambda x: abs(safe_float(x["buy_value_yi"]) + safe_float(x["sell_value_yi"])), reverse=True)
    return rankings


def build_stock_radar(top_stock_changes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in top_stock_changes:
        participants = item.get("participating_etfs", [])
        buy_etfs = sum(1 for x in participants if x.get("direction") == "buy")
        sell_etfs = sum(1 for x in participants if x.get("direction") == "sell")
        direction = item.get("direction", "neutral")
        if direction == "buy" and buy_etfs >= 5:
            event_type = "高共識加碼"
        elif direction == "buy":
            event_type = "共識加碼"
        elif direction == "sell" and sell_etfs >= 4:
            event_type = "高共識減碼"
        elif direction == "sell":
            event_type = "共識減碼"
        else:
            event_type = "觀察"
        rows.append(
            {
                "stock_code": item["stock_code"],
                "stock_name": item["stock_name"],
                "buy_etfs": buy_etfs,
                "sell_etfs": sell_etfs,
                "net_value_yi": item["delta_value_yi"],
                "streak_days": 1,
                "event_type": event_type,
                "note": item.get("signal", ""),
            }
        )
    return rows[:10]


def build_kpis(top_stock_changes: List[Dict[str, Any]], all_stock_changes: List[Dict[str, Any]]) -> Dict[str, Any]:
    net = sum(safe_float(x["delta_value_yi"]) for x in all_stock_changes)
    consensus_buy = sum(1 for x in all_stock_changes if safe_float(x["delta_value_yi"]) > 0 and int(x.get("etf_count", 0)) >= 2)
    consensus_sell = sum(1 for x in all_stock_changes if safe_float(x["delta_value_yi"]) < 0 and int(x.get("etf_count", 0)) >= 2)
    total_abs = sum(abs(safe_float(x["delta_value_yi"])) for x in all_stock_changes) or 1.0
    top3_abs = sum(abs(safe_float(x["delta_value_yi"])) for x in top_stock_changes[:3])
    concentration_score = round(top3_abs / total_abs * 100)
    return {
        "net_change_value_yi": round_money(net, 1),
        "consensus_buy_count": consensus_buy,
        "consensus_sell_count": consensus_sell,
        "concentration_score": concentration_score,
    }


def build_events(top_stock_changes: List[Dict[str, Any]], quality: Dict[str, Any]) -> List[Dict[str, str]]:
    events: List[Dict[str, str]] = []
    buys = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) > 0]
    sells = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) < 0]
    if buys:
        top = buys[0]
        events.append({"time": "盤後", "title": f"{top['stock_name']} 成為今日 ETF 最大共識買盤", "desc": f"估算加碼 {top['delta_value_yi']} 億元，參與 ETF {top['etf_count']} 檔。"})
    if sells:
        top = sorted(sells, key=lambda x: safe_float(x["delta_value_yi"]))[0]
        events.append({"time": "盤後", "title": f"{top['stock_name']} 出現 ETF 共識減碼", "desc": f"估算減碼 {abs(safe_float(top['delta_value_yi'])):.2f} 億元，參與 ETF {top['etf_count']} 檔。"})
    events.append({"time": "資料檢查", "title": "全市場 ETF 資料覆蓋率", "desc": f"已覆蓋 {quality['covered_etfs']}/{quality['total_etfs']} 檔 ETF，覆蓋率 {quality['coverage_ratio']:.1%}。"})
    return events


def build_ai_report(top_stock_changes: List[Dict[str, Any]], kpis: Dict[str, Any], quality: Dict[str, Any]) -> Dict[str, Any]:
    buys = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) > 0]
    sells = [x for x in top_stock_changes if safe_float(x["delta_value_yi"]) < 0]
    top_buy = buys[0] if buys else None
    top_sell = sorted(sells, key=lambda x: safe_float(x["delta_value_yi"]))[0] if sells else None
    net_value = safe_float(kpis["net_change_value_yi"])
    bias = "偏多" if net_value > 0 else "偏空" if net_value < 0 else "中性"
    headline = f"今日全市場 ETF 籌碼{bias}，前十大變動淨額 {net_value:.1f} 億元。"
    summary_parts = [
        f"今日以 TWSE + TPEx 全部 ETF 為分析範圍，資料覆蓋率為 {quality['coverage_ratio']:.1%}。",
        f"跨 ETF 持股差分顯示，整體籌碼方向為{bias}，共識加碼股票 {kpis['consensus_buy_count']} 檔，共識減碼股票 {kpis['consensus_sell_count']} 檔。",
        f"前三大變動占全體變動絕對值的 {kpis['concentration_score']}%，可用來判斷今日資金是否過度集中。",
    ]
    if top_buy:
        summary_parts.append(f"最大買盤為 {top_buy['stock_code']} {top_buy['stock_name']}，估算加碼 {top_buy['delta_value_yi']} 億元，參與 ETF {top_buy['etf_count']} 檔。")
    if top_sell:
        summary_parts.append(f"最大賣壓為 {top_sell['stock_code']} {top_sell['stock_name']}，估算減碼 {abs(safe_float(top_sell['delta_value_yi'])):.2f} 億元，參與 ETF {top_sell['etf_count']} 檔。")
    watchlist = [
        f"{x['stock_code']} {x['stock_name']}：ETF {'加碼' if safe_float(x['delta_value_yi']) > 0 else '減碼'} {abs(safe_float(x['delta_value_yi'])):.2f} 億，參與 ETF {x['etf_count']} 檔"
        for x in top_stock_changes[:5]
    ]
    risk = "若資料覆蓋率未達 100%，應避免將單次報告視為完整市場結論；若買盤集中在少數權值股，也需觀察隔日是否延續。"
    return {"headline": headline, "summary": "".join(summary_parts), "watchlist": watchlist, "risk": risk}


def build_data_sources() -> List[Dict[str, Any]]:
    status = "ready" if not USE_SAMPLE_DATA else "watch"
    return [
        {"name": "TWSE ETF e添富", "type": "ETF master / AUM / ranking", "update_freq": "每日或盤中依官方更新", "status": status, "fields": ["etf_code", "etf_name", "aum", "close", "issuer", "beneficiaries"]},
        {"name": "TWSE / TPEx ETF 交易資料", "type": "成交價量 / 折溢價輔助", "update_freq": "交易日", "status": status, "fields": ["close", "volume", "turnover", "premium_discount"]},
        {"name": "SITCA ETF 專區", "type": "產業統計 / 明細資料入口", "update_freq": "月 / 季 / 不定期", "status": "manual", "fields": ["fund_type", "issuer", "monthly_top5", "quarter_holdings"]},
        {"name": "MOPS 公開資訊觀測站", "type": "公開說明書 / 財報 / 基金資訊", "update_freq": "依公告", "status": "manual", "fields": ["prospectus", "financial_report", "fund_nav", "holdings"]},
        {"name": "集保 ETF 觀測站", "type": "預估淨值 / 折溢價 / 成分證券資訊", "update_freq": "盤中 / 每日", "status": "watch", "fields": ["iNAV", "premium_discount", "constituents"]},
        {"name": "各投信 PCF / 每日持股揭露", "type": "每日持股核心資料", "update_freq": "每日盤前或盤後", "status": "watch", "fields": ["stock_code", "shares", "weight", "cash_component", "creation_unit"]},
    ]


def build_report() -> Optional[Dict[str, Any]]:
    now = now_taipei()
    report_date = now.strftime("%Y-%m-%d")
    updated_at = now.strftime("%Y-%m-%d %H:%M:%S")
    etf_master = fetch_all_twse_tpex_etfs()
    etf_stock_diffs = fetch_all_etf_stock_diffs(etf_master)
    quality = calculate_coverage(etf_master, etf_stock_diffs)
    logger.info("ETF coverage: %s/%s = %.2f%%", quality["covered_etfs"], quality["total_etfs"], quality["coverage_ratio"] * 100)
    if not should_generate_report(quality):
        return None
    all_stock_changes = aggregate_stock_changes(etf_stock_diffs, top_n=10_000)
    top_stock_changes = all_stock_changes[:TOP_N_STOCKS]
    etf_rankings = build_etf_rankings(etf_master, etf_stock_diffs)
    kpis = build_kpis(top_stock_changes, all_stock_changes)
    stock_radar = build_stock_radar(top_stock_changes)
    events = build_events(top_stock_changes, quality)
    ai_report = build_ai_report(top_stock_changes, kpis, quality)
    net_value = safe_float(kpis["net_change_value_yi"])
    market_bias = "偏多" if net_value > 0 else "偏空" if net_value < 0 else "中性"
    return {
        "meta": {
            "report_date": report_date,
            "updated_at": updated_at,
            "timezone": "Asia/Taipei",
            "tracked_etfs": len(etf_master),
            "covered_etfs": quality["covered_etfs"],
            "coverage_ratio": quality["coverage_ratio"],
            "universe": "TWSE + TPEx 全部 ETF",
            "market_bias": market_bias,
            "sample_mode": USE_SAMPLE_DATA,
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
    logger.info("Start Taiwan ETF report generation. USE_SAMPLE_DATA=%s", USE_SAMPLE_DATA)
    try:
        report = build_report()
        if report is None:
            logger.info("Report generation skipped because data is not ready.")
            return 0
        persist_report(report)
        logger.info("Report generation completed.")
        return 0
    except NotImplementedError as exc:
        logger.error("%s", exc)
        return 2
    except Exception as exc:
        logger.exception("Unexpected report generation failure: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
