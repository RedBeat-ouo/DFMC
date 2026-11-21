import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

# ===================== 基礎設定（純 Garena 實價源） =====================
DASHBOARD_API_URL = "https://app.852941.xyz/api/dashboard"
TAX_RATE = 0.135  # 實測含上架費與成交稅約 13.5%（保守高於 13%）
NET_MULTIPLIER = 1.0 - TAX_RATE  # 0.865
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

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

            # 採用 72 小時 P85 作為實戰保守賣價（若缺值依序降級）
            target_sell = (
                safe_float(item.get("p85_72h"))
                or safe_float(item.get("p85_7d"))
                or safe_float(item.get("p85_24h"))
                or safe_float(item.get("max_72h"))
                or price
            )

            breakeven_sell = math.ceil(price / NET_MULTIPLIER)

            # 嚴格依 13.5% 稅後計算每發淨利與 ROI（單格利潤一併算好供 AI 參考）
            est_net_profit = round(target_sell * NET_MULTIPLIER - price, 1)
            est_net_roi_pct = round((est_net_profit / price) * 100, 1) if price > 0 else 0.0
            profit_per_slot = round(est_net_profit * stack_size, 0)

            # 估算近 7 日常態均價（結合 12h/24h/3d/7d 與 72h P70）
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
            })

    print(f"✅ 成功讀取 {len(enriched)} 款 Garena 彈藥即時行情。\n", flush=True)
    return enriched


# ===================== 2. Cloudflare KV 寫入工具 =====================
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


# ===================== 3. 量化篩選 + Gemini 操盤報告 =====================
def build_fallback_report(flip_top: list, self_use_top: list, sell_top: list, now_tw: datetime) -> dict:
    return {
        "updated_at": int(now_tw.timestamp()),
        "updated_str": now_tw.strftime("%Y-%m-%d %H:%M"),
        "source": "Garena 實價量化模型（備援）",
        "market_summary": (
            f"台灣時間 {WEEKDAY_ZH[now_tw.weekday()]} {now_tw.strftime('%H:%M')}。"
            "已依 Garena 實價與 72 小時 P85 賣價扣除 13.5% 手續費完成篩選。"
        ),
        "flip_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "buy_price": x["price"],
                "target_sell": x["target_sell"],
                "roi_pct": x["est_net_roi_pct"],
                "reason": f"72h 水位 {x['position_72h']}%、回彈 {x['bounces_72h']} 次，扣稅後每格約賺 {int(x['profit_per_slot'])}。",
            }
            for x in flip_top[:4]
        ],
        "self_use_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "buy_price": x["price"],
                "discount_pct": round(abs(min(x["vs_avg_pct"], 0.0)), 1),
                "reason": f"72h 水位僅 {x['position_72h']}%，低於均價 {abs(min(x['vs_avg_pct'], 0.0))}%，自用免稅省很大。",
            }
            for x in self_use_top[:4]
        ],
        "sell_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "sell_price": x["price"],
                "reason": f"72h 水位高達 {x['position_72h']}%（高於均價 +{max(x['vs_avg_pct'], 0.0)}%），適合逢高出清。",
            }
            for x in sell_top[:4]
        ],
        "risk_note": "已計入約 13.5% 上架與成交稅；若標示波動偏高或即將系統補貨，請分批掛單避免壓價。",
    }


def generate_ai_report(enriched: list[dict], now_tw: datetime) -> dict:
    # 聚焦實戰主流 Lv.3 ~ Lv.6 子彈
    tradable = [x for x in enriched if x["level"] >= 3 and x["price"] >= 100]
    if not tradable:
        tradable = enriched

    # 1. 抄底倒賣候選：嚴格要求扣 13.5% 稅後淨利 > 0 且位於 72h 中低位
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
        key=lambda x: (
            1 if x["safety_gate"] == "PASS" else 0,
            x["est_net_roi_pct"],
            x["profit_per_slot"],
        ),
        reverse=True,
    )
    flip_candidates = flip_candidates[:8]

    # 2. 自用囤貨候選：優先挑 Lv.4~6 或明顯跌破均價／處於 72h 低檔區（免賣出稅）
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
    self_use_candidates = self_use_candidates[:8]

    # 3. 逢高出清候選：位於 72h/24h 高檔區（>= 80%）且高於均價
    sell_candidates = [
        x for x in tradable
        if x["position_72h"] >= 80.0 or (x["position_24h"] >= 88.0 and x["vs_avg_pct"] >= 5.0)
    ]
    sell_candidates.sort(key=lambda x: (x["position_72h"], x["vs_avg_pct"]), reverse=True)
    sell_candidates = sell_candidates[:8]

    fallback = build_fallback_report(flip_candidates, self_use_candidates, sell_candidates, now_tw)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("⚠️ 未偵測到 GEMINI_API_KEY，改用純數學策略報告。", flush=True)
        return fallback

    prompt = (
        f"你是《三角洲行動 (Delta Force)》Garena 服拍賣行彈藥操盤分析師。"
        f"目前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M')}（{WEEKDAY_ZH[now_tw.weekday()]}）。\n"
        f"以下皆為 Garena 實價大盤計算出的候選數據（目標賣價為 72 小時 P85 價，已扣除 13.5% 手續費，含回彈次數 bounces_72h、流通度 turnover、安全風控 safety_gate 與系統補貨倒數 restock_in_hours）。\n"
        f"請嚴格根據以下數據產出精煉的繁體中文 JSON 報告，絕不捏造數據外的子彈或價格；每個標的的 reason 控制在 40 字以內、直講關鍵重點：\n\n"
        f"【候選 1：抄底倒賣（扣 13.5% 稅仍有正利潤）】\n{json.dumps(flip_candidates, ensure_ascii=False)}\n\n"
        f"【候選 2：自用抄底囤貨（低於均價或處於 72h 低點、免賣出稅）】\n{json.dumps(self_use_candidates, ensure_ascii=False)}\n\n"
        f"【候選 3：逢高出清警戒（72h 高位）】\n{json.dumps(sell_candidates, ensure_ascii=False)}\n\n"
        f"規則：\n"
        f"1. 各分類挑選最值得操作的 3～4 款（若候選為空則回傳 []）。\n"
        f"2. 倒賣優先挑 safety_gate=PASS、回彈次數高且單格利潤佳的標的；若為 CAUTION 或 restock_in_hours 在 0~2 小時內，請在 reason 簡短提醒。\n"
        f"3. discount_pct 請填寫正數百分比（取 vs_avg_pct 的絕對折價幅，若未低於均價則填 0）。"
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
                        "reason": {"type": "STRING"},
                    },
                    "required": ["name", "level", "buy_price", "target_sell", "roi_pct", "reason"],
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
                        "reason": {"type": "STRING"},
                    },
                    "required": ["name", "level", "buy_price", "discount_pct", "reason"],
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
                        "reason": {"type": "STRING"},
                    },
                    "required": ["name", "level", "sell_price", "reason"],
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
        report["source"] = f"Garena 實價 + Gemini ({GEMINI_MODEL})"
        print("🤖 Gemini Garena 操盤報告產出成功！", flush=True)
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
