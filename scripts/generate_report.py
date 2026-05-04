import json
import random
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
HISTORY_DIR = ROOT / "history"

DATA_DIR.mkdir(exist_ok=True)
HISTORY_DIR.mkdir(exist_ok=True)


def now_taipei():
    return datetime.now(ZoneInfo("Asia/Taipei"))


def fetch_etf_master():
    """
    MVP 先用示範資料。
    正式版之後會改成抓：
    - TWSE ETF e添富
    - TPEx ETF 資料
    - SITCA ETF 專區
    """
    return [
        {
            "etf_code": "0050",
            "etf_name": "元大台灣50",
            "aum_yi": 4050.3,
            "buy_value_yi": 11.1,
            "sell_value_yi": 9.9,
            "net_flow_yi": 1.2,
            "turnover_pct": 0.4,
            "top10_weight_pct": 72.8,
        },
        {
            "etf_code": "00878",
            "etf_name": "國泰永續高股息",
            "aum_yi": 3210.8,
            "buy_value_yi": 8.4,
            "sell_value_yi": 6.2,
            "net_flow_yi": 2.2,
            "turnover_pct": 0.8,
            "top10_weight_pct": 46.1,
        },
        {
            "etf_code": "00919",
            "etf_name": "群益台灣精選高息",
            "aum_yi": 2789.0,
            "buy_value_yi": 7.9,
            "sell_value_yi": 11.8,
            "net_flow_yi": -3.9,
            "turnover_pct": 1.1,
            "top10_weight_pct": 42.7,
        },
    ]


def fetch_holdings_diff():
    """
    MVP 先用示範資料。
    正式版之後會改成：
    今日 ETF 持股 - 昨日 ETF 持股 = 每日持股變動。
    """
    sample = [
        ("2330", "台積電", "buy", 18250, 18.7, 9, 0.84),
        ("2317", "鴻海", "buy", 15210, 10.3, 7, 0.51),
        ("2454", "聯發科", "buy", 3210, 6.9, 5, 0.38),
        ("2881", "富邦金", "buy", 8850, 5.4, 6, 0.24),
        ("2308", "台達電", "buy", 4170, 4.7, 4, 0.19),
        ("2603", "長榮", "sell", -11600, -7.6, 5, -0.35),
        ("2382", "廣達", "sell", -6090, -6.8, 4, -0.31),
        ("2891", "中信金", "buy", 17020, 5.8, 5, 0.22),
        ("3037", "欣興", "sell", -4300, -3.1, 3, -0.18),
        ("3711", "日月光投控", "buy", 5380, 3.0, 3, 0.13),
    ]

    rows = []
    for code, name, direction, lot, value_yi, etf_count, weight_delta in sample:
        rows.append(
            {
                "stock_code": code,
                "stock_name": name,
                "direction": direction,
                "delta_lot": lot,
                "delta_value_yi": value_yi,
                "etf_count": etf_count,
                "weight_delta_pct": weight_delta,
                "signal": make_stock_signal(name, direction, etf_count),
            }
        )
    return rows


def make_stock_signal(name, direction, etf_count):
    if direction == "buy" and etf_count >= 5:
        return f"{name} 被 {etf_count} 檔 ETF 同步加碼，屬於共識買盤。"
    if direction == "sell" and etf_count >= 4:
        return f"{name} 遭 {etf_count} 檔 ETF 同步減碼，短線籌碼偏弱。"
    return f"{name} 今日 ETF 籌碼變化需持續觀察。"


def build_stock_radar(top_stock_changes):
    rows = []
    for item in top_stock_changes[:6]:
        is_buy = item["delta_value_yi"] > 0
        rows.append(
            {
                "stock_code": item["stock_code"],
                "stock_name": item["stock_name"],
                "buy_etfs": item["etf_count"] if is_buy else random.randint(0, 1),
                "sell_etfs": item["etf_count"] if not is_buy else random.randint(0, 1),
                "net_value_yi": item["delta_value_yi"],
                "streak_days": random.randint(1, 5),
                "event_type": "共識加碼" if is_buy else "共識減碼",
                "note": item["signal"],
            }
        )
    return rows


def build_events(top_stock_changes):
    events = []
    buys = [x for x in top_stock_changes if x["delta_value_yi"] > 0]
    sells = [x for x in top_stock_changes if x["delta_value_yi"] < 0]

    if buys:
        top = buys[0]
        events.append(
            {
                "time": "盤後",
                "title": f"{top['stock_name']} 成為今日 ETF 最大共識買盤",
                "desc": f"估算加碼 {top['delta_value_yi']} 億元，參與 ETF 家數 {top['etf_count']} 檔。",
            }
        )

    if sells:
        top = sorted(sells, key=lambda x: x["delta_value_yi"])[0]
        events.append(
            {
                "time": "盤後",
                "title": f"{top['stock_name']} 出現 ETF 共識減碼",
                "desc": f"估算減碼 {abs(top['delta_value_yi'])} 億元，需觀察是否連續。",
            }
        )

    events.append(
        {
            "time": "盤後",
            "title": "AI 日報已更新",
            "desc": "今日報告由持股差分、ETF 家數、估算市值與集中度指標產生。",
        }
    )
    return events


def build_kpis(top_stock_changes):
    net = sum(x["delta_value_yi"] for x in top_stock_changes)
    consensus_buy = sum(
        1 for x in top_stock_changes
        if x["delta_value_yi"] > 0 and x["etf_count"] >= 2
    )
    consensus_sell = sum(
        1 for x in top_stock_changes
        if x["delta_value_yi"] < 0 and x["etf_count"] >= 2
    )

    total_abs = sum(abs(x["delta_value_yi"]) for x in top_stock_changes) or 1
    top3_abs = sum(abs(x["delta_value_yi"]) for x in top_stock_changes[:3])
    concentration_score = round(top3_abs / total_abs * 100)

    return {
        "net_change_value_yi": round(net, 1),
        "consensus_buy_count": consensus_buy,
        "consensus_sell_count": consensus_sell,
        "concentration_score": concentration_score,
    }


def build_ai_report(top_stock_changes, kpis):
    buys = [x for x in top_stock_changes if x["delta_value_yi"] > 0]
    sells = [x for x in top_stock_changes if x["delta_value_yi"] < 0]

    top_buy = buys[0] if buys else None
    top_sell = sorted(sells, key=lambda x: x["delta_value_yi"])[0] if sells else None

    if kpis["net_change_value_yi"] > 0:
        bias = "偏多"
    elif kpis["net_change_value_yi"] < 0:
        bias = "偏空"
    else:
        bias = "中性"

    headline = f"今日 ETF 籌碼 {bias}，前十大變動淨額 {kpis['net_change_value_yi']} 億元。"

    summary_parts = [
        f"今日跨 ETF 持股變動顯示，整體籌碼方向為{bias}。",
        f"前十大變動集中度為 {kpis['concentration_score']}，代表資金是否集中在少數權值股需要特別觀察。",
    ]

    if top_buy:
        summary_parts.append(
            f"最大買盤為 {top_buy['stock_code']} {top_buy['stock_name']}，估算加碼 {top_buy['delta_value_yi']} 億元。"
        )

    if top_sell:
        summary_parts.append(
            f"最大賣壓為 {top_sell['stock_code']} {top_sell['stock_name']}，估算減碼 {abs(top_sell['delta_value_yi'])} 億元。"
        )

    watchlist = []
    for x in top_stock_changes[:5]:
        direction = "加碼" if x["delta_value_yi"] > 0 else "減碼"
        watchlist.append(
            f"{x['stock_code']} {x['stock_name']}：ETF {direction} {abs(x['delta_value_yi'])} 億，參與 ETF {x['etf_count']} 檔"
        )

    return {
        "headline": headline,
        "summary": "".join(summary_parts),
        "watchlist": watchlist,
        "risk": "若買盤集中在少數大型權值股，隔日需觀察是否延續，避免把一次性調倉誤判為趨勢。",
    }


def build_data_sources():
    return [
        {
            "name": "TWSE ETF e添富",
            "type": "ETF master / AUM / ranking",
            "update_freq": "每日或盤中依官方更新",
            "status": "ready",
            "fields": ["etf_code", "etf_name", "aum", "close", "issuer", "beneficiaries"],
        },
        {
            "name": "TWSE / TPEx ETF 交易資料",
            "type": "成交價量 / 折溢價輔助",
            "update_freq": "交易日",
            "status": "ready",
            "fields": ["close", "volume", "turnover", "premium_discount"],
        },
        {
            "name": "SITCA ETF 專區",
            "type": "產業統計 / 明細資料入口",
            "update_freq": "月 / 季 / 不定期",
            "status": "ready",
            "fields": ["fund_type", "issuer", "monthly_top5", "quarter_holdings"],
        },
        {
            "name": "MOPS 公開資訊觀測站",
            "type": "公開說明書 / 財報 / 基金資訊",
            "update_freq": "依公告",
            "status": "manual",
            "fields": ["prospectus", "financial_report", "fund_nav", "holdings"],
        },
        {
            "name": "集保 ETF 觀測站",
            "type": "預估淨值 / 折溢價 / 成分證券資訊",
            "update_freq": "盤中 / 每日",
            "status": "watch",
            "fields": ["iNAV", "premium_discount", "constituents"],
        },
        {
            "name": "各投信 PCF / 每日持股揭露",
            "type": "每日持股核心資料",
            "update_freq": "每日盤前或盤後",
            "status": "watch",
            "fields": ["stock_code", "shares", "weight", "cash_component", "creation_unit"],
        },
    ]


def main():
    now = now_taipei()
    report_date = now.strftime("%Y-%m-%d")
    updated_at = now.strftime("%Y-%m-%d %H:%M:%S")

    etf_rankings = fetch_etf_master()
    top_stock_changes = fetch_holdings_diff()
    kpis = build_kpis(top_stock_changes)
    stock_radar = build_stock_radar(top_stock_changes)
    events = build_events(top_stock_changes)
    ai_report = build_ai_report(top_stock_changes, kpis)

    if kpis["net_change_value_yi"] > 0:
        market_bias = "偏多"
    elif kpis["net_change_value_yi"] < 0:
        market_bias = "偏空"
    else:
        market_bias = "中性"

    report = {
        "meta": {
            "report_date": report_date,
            "updated_at": updated_at,
            "tracked_etfs": len(etf_rankings),
            "universe": "Taiwan ETF",
            "market_bias": market_bias,
        },
        "kpis": kpis,
        "top_stock_changes": top_stock_changes,
        "stock_radar": stock_radar,
        "etf_rankings": etf_rankings,
        "events": events,
        "data_sources": build_data_sources(),
        "ai_report": ai_report,
    }

    latest_path = DATA_DIR / "latest_report.json"
    history_path = HISTORY_DIR / f"{report_date}.json"

    latest_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    history_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    print(f"Generated {latest_path}")
    print(f"Generated {history_path}")


if __name__ == "__main__":
    main()
