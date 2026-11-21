import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ===================== 基礎設定（純 Garena 實價 + 1W/1M 漏斗分析） =====================
DASHBOARD_API_URL = "https://app.852941.xyz/api/dashboard"
PRICE_API_URL = "https://app.852941.xyz/api/price"
TAX_RATE = 0.135  # 實測含上架費與成交稅約 13.5%
NET_MULTIPLIER = 1.0 - TAX_RATE  # 0.865
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
MAX_DEEP_DIVE_BULLETS = 10  # 漏斗上限：最多只深挖前 10 款候選彈，保護站方伺服器

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://app.852941.xyz/",
}

WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def get_tw_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def safe_float(val, default: float = 0.0) -> float:
    try:
        if val is None:
            return default
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val, default: int = 0) -> int:
    try:
        if val is None:
            return default
        return int(float(val))
    except (ValueError, TypeError):
        return default


# ===================== 1. 拉取 Garena 實價大盤 JSON =====================
def fetch_garena_dashboard() -> list[dict]:
    print(f"📡 正在讀取 Garena 實價接口：{DASHBOARD_API_URL}", flush=True)
    res = requests.get(DASHBOARD_API_URL, headers=HEADERS, timeout=(10, 25))
    if res.status_code != 200:
        raise RuntimeError(f"API 回傳 HTTP {res.status_code}：{res.text[:200]}")

    payload = res.json()
    raw_data = payload.get("data")
    if not isinstance(raw_data, dict):
        raise RuntimeError("API 回傳格式異常：找不到 data 欄位")

    enriched = []
    seen = set()

    for category, bullet_list in raw_data.items():
        if not isinstance(bullet_list, list):
            continue
        for item in bullet_list:
            if not isinstance(item, dict):
                continue

            name = str(item.get("bullet_name") or "").strip()
            price = safe_float(item.get("current_price"))
            if not name or price <= 0 or name in seen:
                continue
            seen.add(name)

            level = safe_int(item.get("tier"))
            stack_size = safe_int(item.get("stack_size"), 60)

            target_sell = (
                safe_float(item.get("p85_72h"))
                or safe_float(item.get("p85_7d"))
                or safe_float(item.get("p85_24h"))
                or safe_float(item.get("max_72h"))
                or price
            )

            breakeven_sell = math.ceil(price / NET_MULTIPLIER)
            est_net_profit = round(target_sell * NET_MULTIPLIER - price, 1)
            est_net_roi_pct = round((est_net_profit / price) * 100, 1) if price > 0 else 0.0
            profit_per_slot = round(est_net_profit * stack_size, 0)

            ref_prices = [
                v for v in (
                    safe_float(item.get("price_12h")),
                    safe_float(item.get("price_24h")),
                    safe_float(item.get("price_3d")),
                    safe_float(item.get("price_7d")),
                    safe_float(item.get("p70_72h")),
                )
                if v > 0
            ]
            est_avg = round(sum(ref_prices) / len(ref_prices), 1) if ref_prices else price
            vs_avg_pct = round(((price - est_avg) / est_avg) * 100, 1) if est_avg > 0 else 0.0

            pos_24h = safe_float(item.get("position_24h"), 50.0)
            pos_72h = safe_float(item.get("position_72h"), pos_24h)
            pos_7d = safe_float(item.get("position_7d"), pos_72h)

            enriched.append({
                "name": name,
                "category": category,
                "level": level,
                "stack_size": stack_size,
                "price": round(price, 1),
                "breakeven_sell": breakeven_sell,
                "target_sell": round(target_sell, 1),
                "est_avg": est_avg,
                "vs_avg_pct": vs_avg_pct,
                "est_net_profit": est_net_profit,
                "est_net_roi_pct": est_net_roi_pct,
                "profit_per_slot": profit_per_slot,
                "position_24h": round(pos_24h, 1),
                "position_72h": round(pos_72h, 1),
                "position_7d": round(pos_7d, 1),
                "change_12h": safe_float(item.get("change_12h")),
                "change_24h": safe_float(item.get("change_24h")),
                "change_7d": safe_float(item.get("change_7d")),
                "bounces_72h": safe_int(item.get("bounces_72h")),
                "turnover": str(item.get("turnover_24h_label") or "未知"),
                "safety_gate": str(item.get("safety_gate") or "NORMAL"),
                "safety_warning": str(item.get("safety_warning") or ""),
                "restock_in_hours": safe_float(item.get("restock_in_hours"), -99.0),
                "trade_score": safe_float(item.get("trade_score")),
                # 深挖欄位預設值
                "buy_window": "",
                "sell_window": "",
                "trend_30d_pct": 0.0,
                "trend_tag": "橫盤",
            })

    print(f"✅ 成功讀取 {len(enriched)} 款 Garena 彈藥即時行情。\n", flush=True)
    return enriched


# ===================== 2. 漏斗深挖：拉取 1W/1M 並清洗 OCR 毛刺 =====================
def fetch_clean_series(session: requests.Session, bullet_name: str, mode: str) -> list[tuple[datetime, float]]:
    try:
        res = session.get(
            PRICE_API_URL,
            params={"bullet_name": bullet_name, "mode": mode},
            headers=HEADERS,
            timeout=(8, 20),
        )
        if res.status_code != 200:
            return []
        raw_list = res.json().get("data") or []
    except Exception as e:
        print(f"  ⚠️ 拉取 [{bullet_name}] ({mode}) 失敗：{e}", flush=True)
        return []

    parsed = []
    for row in raw_list:
        if not isinstance(row, dict):
            continue
        p = safe_float(row.get("price"))
        ts_str = str(row.get("created_at") or "").strip()
        if p <= 0 or not ts_str:
            continue
        try:
            dt = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
            parsed.append((dt, p))
        except ValueError:
            continue

    if len(parsed) < 5:
        return parsed

    # 依中位數過濾 OCR 異常爆衝毛刺（如 33327）
    med = statistics.median([p for _, p in parsed])
    low_bound = med * 0.30
    high_bound = med * 2.80
    cleaned = [(dt, p) for dt, p in parsed if low_bound <= p <= high_bound]
    cleaned.sort(key=lambda x: x[0])
    return cleaned


def compute_1w_timing(points: list[tuple[datetime, float]]) -> tuple[str, str, float]:
    if len(points) < 24:
        return "", "", 0.0

    hour_buckets: dict[int, list[float]] = {}
    for dt, p in points:
        hour_buckets.setdefault(dt.hour, []).append(p)

    hour_avgs = {h: sum(vals) / len(vals) for h, vals in hour_buckets.items() if vals}
    if len(hour_avgs) < 6:
        return "", "", 0.0

    # 以連續 2 小時滑動視窗找出「最低買進區間」與「最高出貨區間」
    window_avgs = {}
    for h in range(24):
        h_next = (h + 1) % 24
        vals = []
        if h in hour_avgs:
            vals.append(hour_avgs[h])
        if h_next in hour_avgs:
            vals.append(hour_avgs[h_next])
        if vals:
            window_avgs[h] = sum(vals) / len(vals)

    best_buy_h = min(window_avgs, key=window_avgs.get)
    best_sell_h = max(window_avgs, key=window_avgs.get)

    buy_win = f"{best_buy_h:02d}-{(best_buy_h + 2) % 24:02d}"
    sell_win = f"{best_sell_h:02d}-{(best_sell_h + 2) % 24:02d}"
    clean_avg = round(sum(p for _, p in points) / len(points), 1)
    return buy_win, sell_win, clean_avg


def compute_1m_trend(points: list[tuple[datetime, float]]) -> tuple[float, str]:
    if len(points) < 20:
        return 0.0, "橫盤"

    prices = [p for _, p in points]
    n = len(prices)
    early_med = statistics.median(prices[: max(5, int(n * 0.55))])
    recent_med = statistics.median(prices[max(0, int(n * 0.75)) :])

    if early_med <= 0:
        return 0.0, "橫盤"

    trend_pct = round(((recent_med - early_med) / early_med) * 100, 1)
    if trend_pct <= -10.0:
        label = "陰跌"
    elif trend_pct >= 10.0:
        label = "上漲"
    else:
        label = "橫盤"
    return trend_pct, label


def enrich_candidates_with_history(candidates: list[dict]):
    unique_map = {}
    for item in candidates:
        if item["name"] not in unique_map and len(unique_map) < MAX_DEEP_DIVE_BULLETS:
            unique_map[item["name"]] = item

    print(f"🔎 啟動漏斗深挖：針對 Top {len(unique_map)} 候選彈拉取 1W / 1M 歷史曲線...", flush=True)
    session = requests.Session()

    for idx, (name, item) in enumerate(unique_map.items(), 1):
        pts_1w = fetch_clean_series(session, name, "1w")
        time.sleep(0.35)
        pts_1m = fetch_clean_series(session, name, "1m")
        time.sleep(0.35)

        buy_win, sell_win, clean_1w_avg = compute_1w_timing(pts_1w)
        trend_pct, trend_tag = compute_1m_trend(pts_1m)

        item["buy_window"] = buy_win
        item["sell_window"] = sell_win
        item["trend_30d_pct"] = trend_pct
        item["trend_tag"] = trend_tag
        if clean_1w_avg > 0:
            item["est_avg"] = clean_1w_avg
            item["vs_avg_pct"] = round(((item["price"] - clean_1w_avg) / clean_1w_avg) * 100, 1)

        print(
            f"  [{idx:02d}/{len(unique_map):02d}] {name} ➔ "
            f"買窗 {buy_win or '-'} / 賣窗 {sell_win or '-'} ｜ 月線 {trend_tag} ({trend_pct:+.1f}%)",
            flush=True,
        )
    print("", flush=True)


# ===================== 3. Cloudflare KV 寫入工具 =====================
def kv_put_json(key: str, data) -> bool:
    account_id = os.environ.get("CF_ACCOUNT_ID")
    namespace_id = os.environ.get("CF_KV_NAMESPACE_ID")
    api_token = os.environ.get("CF_API_TOKEN")
    if not all([account_id, namespace_id, api_token]):
        print(f"⚠️ 缺少 Cloudflare KV 憑證，跳過寫入 [{key}]。", flush=True)
        return False

    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json",
    }
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    res = requests.put(url, headers=headers, data=payload, timeout=30)
    if res.status_code == 200:
        print(f"✅ 已寫入 KV [{key}]（大小約 {len(payload) / 1024:.1f} KB）", flush=True)
        return True
    print(f"❌ 寫入 KV [{key}] 失敗 ({res.status_code})：{res.text[:200]}", flush=True)
    return False


# ===================== 4. 量化篩選 + Gemini 操盤報告 =====================
def build_fallback_report(flip_top: list, self_use_top: list, sell_top: list, now_tw: datetime) -> dict:
    return {
        "updated_at": int(now_tw.timestamp()),
        "updated_str": now_tw.strftime("%Y-%m-%d %H:%M"),
        "source": "Garena 1W/1M 量化模型（備援）",
        "market_summary": (
            f"台灣時間 {WEEKDAY_ZH[now_tw.weekday()]} {now_tw.strftime('%H:%M')}。"
            "已結合 Garena 實價、1週時段規律與 1月趨勢（扣除 13.5% 手續費）完成篩選。"
        ),
        "flip_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "buy_price": x["price"],
                "target_sell": x["target_sell"],
                "roi_pct": x["est_net_roi_pct"],
                "buy_window": x.get("buy_window", ""),
                "sell_window": x.get("sell_window", ""),
                "trend_tag": x.get("trend_tag", "橫盤"),
                "reason": f"月線{x.get('trend_tag', '橫盤')}，72h 回彈 {x['bounces_72h']} 次，單格扣稅淨利約 {int(x['profit_per_slot'])}。",
            }
            for x in flip_top[:4]
        ],
        "self_use_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "buy_price": x["price"],
                "discount_pct": round(abs(min(x["vs_avg_pct"], 0.0)), 1),
                "buy_window": x.get("buy_window", ""),
                "trend_tag": x.get("trend_tag", "橫盤"),
                "reason": f"低於週均價 {abs(min(x['vs_avg_pct'], 0.0))}%，自用免 13.5% 賣出稅。",
            }
            for x in self_use_top[:4]
        ],
        "sell_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "sell_price": x["price"],
                "sell_window": x.get("sell_window", ""),
                "trend_tag": x.get("trend_tag", "橫盤"),
                "reason": f"72h 水位 {x['position_72h']}% 處高檔，建議趁高價時段分批出貨。",
            }
            for x in sell_top[:4]
        ],
        "risk_note": "已濾除歷史異常爆衝值並計入 13.5% 稅率；標記「陰跌」者慎防接飛刀，請分批掛單。",
    }


def generate_ai_report(enriched: list[dict], now_tw: datetime) -> dict:
    tradable = [x for x in enriched if x["level"] >= 3 and x["price"] >= 100]
    if not tradable:
        tradable = enriched

    # 第一關初篩
    flip_candidates = [
        x for x in tradable
        if x["est_net_roi_pct"] >= 2.5
        and x["est_net_profit"] >= 10
        and x["position_72h"] <= 45.0
    ]
    if len(flip_candidates) < 3:
        flip_candidates = [
            x for x in tradable
            if x["est_net_roi_pct"] > 0.5 and x["position_72h"] <= 55.0
        ]
    flip_candidates.sort(
        key=lambda x: (1 if x["safety_gate"] == "PASS" else 0, x["est_net_roi_pct"], x["profit_per_slot"]),
        reverse=True,
    )
    flip_candidates = flip_candidates[:5]

    self_use_candidates = [
        x for x in tradable
        if x["level"] >= 4 and (x["vs_avg_pct"] <= -5.0 or x["position_72h"] <= 20.0)
    ]
    if len(self_use_candidates) < 4:
        self_use_candidates = [
            x for x in tradable
            if x["vs_avg_pct"] <= -5.0 or x["position_72h"] <= 20.0
        ]
    self_use_candidates.sort(key=lambda x: (x["vs_avg_pct"], x["position_72h"]))
    self_use_candidates = self_use_candidates[:4]

    sell_candidates = [
        x for x in tradable
        if x["position_72h"] >= 80.0 or (x["position_24h"] >= 88.0 and x["vs_avg_pct"] >= 5.0)
    ]
    sell_candidates.sort(key=lambda x: (x["position_72h"], x["vs_avg_pct"]), reverse=True)
    sell_candidates = sell_candidates[:4]

    # 第二關：只針對初篩出的核心候選彈拉取 1W/1M 計算黃金買賣時段與月線方向
    enrich_candidates_with_history(flip_candidates + self_use_candidates + sell_candidates)

    # 倒賣排序二次保護：把 30 天「陰跌」標的往後排，優先推橫盤/上漲
    flip_candidates.sort(
        key=lambda x: (
            0 if x.get("trend_tag") == "陰跌" else 1,
            1 if x["safety_gate"] == "PASS" else 0,
            x["est_net_roi_pct"],
        ),
        reverse=True,
    )

    fallback = build_fallback_report(flip_candidates, self_use_candidates, sell_candidates, now_tw)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("⚠️ 未偵測到 GEMINI_API_KEY，改用純數學策略報告。", flush=True)
        return fallback

    prompt = (
        f"你是《三角洲行動 (Delta Force)》Garena 服拍賣行彈藥操盤分析師。"
        f"目前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M')}（{WEEKDAY_ZH[now_tw.weekday()]}）。\n"
        f"以下皆為 Garena 實價 + 1W 黃金買賣時段 (buy_window/sell_window) + 1M 趨勢 (trend_tag/trend_30d_pct) 計算出的精準數據（已扣除 13.5% 稅率並清洗異常值）。\n"
        f"請嚴格根據以下數據產出精煉的繁體中文 JSON 報告，絕不捏造數據外的子彈、價格或時段；每個 reason 控制在 35 字以內：\n\n"
        f"【候選 1：抄底倒賣】\n{json.dumps(flip_candidates, ensure_ascii=False)}\n\n"
        f"【候選 2：自用抄底囤貨】\n{json.dumps(self_use_candidates, ensure_ascii=False)}\n\n"
        f"【候選 3：逢高出清警戒】\n{json.dumps(sell_candidates, ensure_ascii=False)}\n\n"
        f"規則：\n"
        f"1. 各分類挑選最值得操作的 3 款（若候選不足則全列，空則 []）。\n"
        f"2. buy_window、sell_window、trend_tag 請直接沿用輸入數據中的值（無則填空字串）。\n"
        f"3. 倒賣優先避開 trend_tag='陰跌' 的標的；若不得不列或 safety_gate='CAUTION'，請在 reason 簡短提醒。\n"
        f"4. discount_pct 填寫正數折價百分比（若 vs_avg_pct >= 0 則填 0）。"
    )

    schema = {
        "type": "OBJECT",
        "properties": {
            "market_summary": {"type": "STRING"},
            "flip_picks": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "level": {"type": "INTEGER"},
                        "buy_price": {"type": "NUMBER"},
                        "target_sell": {"type": "NUMBER"},
                        "roi_pct": {"type": "NUMBER"},
                        "buy_window": {"type": "STRING"},
                        "sell_window": {"type": "STRING"},
                        "trend_tag": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": [
                        "name", "level", "buy_price", "target_sell", "roi_pct",
                        "buy_window", "sell_window", "trend_tag", "reason",
                    ],
                },
            },
            "self_use_picks": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "level": {"type": "INTEGER"},
                        "buy_price": {"type": "NUMBER"},
                        "discount_pct": {"type": "NUMBER"},
                        "buy_window": {"type": "STRING"},
                        "trend_tag": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": [
                        "name", "level", "buy_price", "discount_pct",
                        "buy_window", "trend_tag", "reason",
                    ],
                },
            },
            "sell_picks": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "name": {"type": "STRING"},
                        "level": {"type": "INTEGER"},
                        "sell_price": {"type": "NUMBER"},
                        "sell_window": {"type": "STRING"},
                        "trend_tag": {"type": "STRING"},
                        "reason": {"type": "STRING"},
                    },
                    "required": [
                        "name", "level", "sell_price", "sell_window", "trend_tag", "reason",
                    ],
                },
            },
            "risk_note": {"type": "STRING"},
        },
        "required": ["market_summary", "flip_picks", "self_use_picks", "sell_picks", "risk_note"],
    }

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "thinkingConfig": {"thinkingLevel": "LOW"},
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }

    try:
        res = requests.post(
            api_url,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json=payload,
            timeout=(15, 120),
        )
        if res.status_code != 200:
            print(f"⚠️ Gemini API 回傳 HTTP {res.status_code}：{res.text[:250]}，改用數學備援報告。", flush=True)
            return fallback

        data = res.json()
        parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        raw_text = "".join(
            p.get("text", "") for p in parts if not p.get("thought") and isinstance(p.get("text"), str)
        ).strip()

        report = json.loads(raw_text)
        report["updated_at"] = int(now_tw.timestamp())
        report["updated_str"] = now_tw.strftime("%Y-%m-%d %H:%M")
        report["source"] = f"Garena 實價 + 1W/1M 時段模型 ({GEMINI_MODEL})"
        print("🤖 Gemini Garena 時段操盤報告產出成功！", flush=True)
        return report

    except Exception as e:
        print(f"⚠️ Gemini 分析異常 ({e})，改用數學備援報告。", flush=True)
        return fallback


def main():
    now_tw = get_tw_now()
    print(f"🚀 啟動 Garena 三角洲彈藥分析（台灣時間 {now_tw.strftime('%Y-%m-%d %H:%M:%S')}）\n", flush=True)

    try:
        enriched = fetch_garena_dashboard()
    except Exception as e:
        print(f"❌ 讀取 Garena 行情失敗：{e}", flush=True)
        sys.exit(1)

    if not enriched:
        print("❌ 未取得任何彈藥資料，中止寫入以保護既有 KV。", flush=True)
        sys.exit(1)

    enriched.sort(key=lambda x: (x["level"], x["price"]), reverse=True)
    report = generate_ai_report(enriched, now_tw)

    latest_payload = {
        "updated_at": int(now_tw.timestamp()),
        "updated_str": now_tw.strftime("%Y-%m-%d %H:%M"),
        "count": len(enriched),
        "items": enriched,
    }

    kv_put_json("df_ammo_latest", latest_payload)
    kv_put_json("df_ammo_report", report)

    print("\n📊 Garena 操盤快報預覽：", flush=True)
    print(report.get("market_summary", ""), flush=True)
    print(f"  - 抄底倒賣推薦：{len(report.get('flip_picks', []))} 款", flush=True)
    print(f"  - 自用囤貨推薦：{len(report.get('self_use_picks', []))} 款", flush=True)
    print(f"  - 逢高出清警戒：{len(report.get('sell_picks', []))} 款", flush=True)
    print("🎉 任務全數完成！", flush=True)


if __name__ == "__main__":
    main()
