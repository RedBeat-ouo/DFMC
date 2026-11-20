import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from bs4 import BeautifulSoup
import requests

try:
    from curl_cffi import requests as cf_requests
except ImportError:
    cf_requests = None

# ===================== 基礎設定 =====================
BASE_AMMO_URL = "https://deltaforcetools.gg/tw/auction-house/ammo"
TAX_RATE = 0.13  # 三角洲行動拍賣行約 13% 手續費（含上架費與成交稅）
NET_MULTIPLIER = 1.0 - TAX_RATE  # 0.87
MAX_HISTORY_POINTS = 360  # 每 2 小時 1 筆 × 30 天 = 360 筆
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def get_tw_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def parse_number(text: str) -> float:
    cleaned = re.sub(r"[^0-9.\-]", "", (text or "").replace(",", ""))
    if not cleaned or cleaned in ("-", ".", "-."):
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_level(text: str) -> int:
    m = re.search(r"(\d+)", text or "")
    return int(m.group(1)) if m else 0


def http_get_html(url: str) -> tuple[int, str]:
    # 優先使用 curl_cffi 模擬 Chrome TLS 指紋，降低被網站阻擋的機率
    if cf_requests is not None:
        try:
            res = cf_requests.get(url, headers=HEADERS, impersonate="chrome", timeout=25)
            return res.status_code, res.text
        except Exception as e:
            print(f"[curl_cffi 降級改用 requests] {e}", flush=True)

    res = requests.get(url, headers=HEADERS, timeout=25)
    return res.status_code, res.text


# ===================== 1. 爬取國際服彈藥即時行情 =====================
def scrape_deltaforce_ammo(max_pages: int = 12) -> list[dict]:
    items_by_name = {}
    prev_page_sig = None

    for page in range(max_pages):
        url = f"{BASE_AMMO_URL}?page={page}"
        print(f"📡 正在抓取彈藥行情第 {page} 頁：{url}", flush=True)

        status, html = http_get_html(url)
        if status != 200:
            print(f"⚠️ 第 {page} 頁回傳 HTTP {status}，停止翻頁。", flush=True)
            if page == 0:
                print(f"頁面預覽：{html[:300]}", flush=True)
            break

        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("table tbody tr")
        if not rows:
            rows = soup.select("table tr")

        page_names = []
        parsed_count = 0

        for tr in rows:
            tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(tds) < 6:
                continue

            name = tds[0].strip()
            price = parse_number(tds[1])
            change_1d = parse_number(tds[2])
            change_7d = parse_number(tds[3])
            change_30d = parse_number(tds[4])
            level = parse_level(tds[5])
            item_type = tds[6].strip().upper() if len(tds) > 6 else "AMMO"

            if not name or price <= 0:
                continue
            if item_type and "AMMO" not in item_type and "彈" not in item_type:
                continue

            page_names.append(name)
            parsed_count += 1
            items_by_name[name] = {
                "name": name,
                "price": round(price, 2),
                "change_1d": round(change_1d, 2),
                "change_7d": round(change_7d, 2),
                "change_30d": round(change_30d, 2),
                "level": level,
            }

        print(f"   ➔ 本頁解析到 {parsed_count} 款彈藥", flush=True)

        if parsed_count == 0:
            break

        sig = tuple(page_names)
        if sig == prev_page_sig:
            print("   ➔ 與上一頁內容完全相同，翻頁結束。", flush=True)
            break
        prev_page_sig = sig
        time.sleep(1.0)

    results = list(items_by_name.values())
    print(f"✅ 共採集完成 {len(results)} 款彈藥行情。\n", flush=True)
    return results


# ===================== 2. Cloudflare KV 讀寫工具 =====================
def get_kv_config():
    account_id = os.environ.get("CF_ACCOUNT_ID")
    namespace_id = os.environ.get("CF_KV_NAMESPACE_ID")
    api_token = os.environ.get("CF_API_TOKEN")
    if not all([account_id, namespace_id, api_token]):
        return None
    return account_id, namespace_id, api_token


def kv_get_json(key: str, default):
    cfg = get_kv_config()
    if not cfg:
        return default
    account_id, namespace_id, api_token = cfg
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"
    try:
        res = requests.get(url, headers={"Authorization": f"Bearer {api_token}"}, timeout=20)
        if res.status_code == 200:
            return res.json()
    except Exception as e:
        print(f"⚠️ 讀取 KV [{key}] 異常：{e}", flush=True)
    return default


def kv_put_json(key: str, data) -> bool:
    cfg = get_kv_config()
    if not cfg:
        print(f"⚠️ 缺少 Cloudflare KV 憑證，跳過寫入 [{key}]。", flush=True)
        return False
    account_id, namespace_id, api_token = cfg
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


# ===================== 3. 更新歷史紀錄 & 計算 13% 稅後指標 =====================
def update_history_and_compute_metrics(items: list[dict], history: dict, now_tw: datetime) -> tuple[dict, list[dict]]:
    now_unix = int(now_tw.timestamp())
    weekday = now_tw.weekday()  # 0=週一 ... 6=週日
    hour = now_tw.hour

    if not isinstance(history, dict):
        history = {}

    enriched = []

    for it in items:
        name = it["name"]
        price = float(it["price"])
        c1 = float(it["change_1d"])
        c7 = float(it["change_7d"])
        c30 = float(it["change_30d"])
        level = int(it["level"])

        # 追加本次快照至歷史 K 線（格式：[unix_ts, price, weekday, hour]）
        pts = history.get(name, [])
        if not isinstance(pts, list):
            pts = []
        # 避免 30 分鐘內重複手動觸發造成過度密集樣本
        if not pts or (now_unix - pts[-1][0]) >= 1800:
            pts.append([now_unix, round(price, 1), weekday, hour])
            if len(pts) > MAX_HISTORY_POINTS:
                pts = pts[-MAX_HISTORY_POINTS:]
        history[name] = pts

        # 由站方 1D / 7D / 30D 漲跌反推歷史基準價（上線第一天即可算趨勢）
        ref_prices = [price]
        for delta in (c1, c7, c30):
            base_p = price - delta
            if base_p > 0:
                ref_prices.append(base_p)

        hist_prices = [p[1] for p in pts if isinstance(p, list) and len(p) >= 2 and p[1] > 0]
        all_prices = ref_prices + hist_prices

        est_low = min(all_prices)
        est_high = max(all_prices)
        est_avg = sum(all_prices) / len(all_prices)

        # 歷史分位數（0% = 最低點, 100% = 最高點）
        if est_high > est_low:
            percentile = round((price - est_low) / (est_high - est_low) * 100, 1)
        else:
            percentile = 50.0

        # 13% 手續費損益兩平賣價
        breakeven_sell = math.ceil(price / NET_MULTIPLIER)

        # 保守目標賣價：若歷史資料夠多取第 85 百分位，否則取參考高點的 95%
        if len(hist_prices) >= 12:
            sorted_hp = sorted(hist_prices)
            p85_idx = min(len(sorted_hp) - 1, int(len(sorted_hp) * 0.85))
            target_sell = max(sorted_hp[p85_idx], round(est_high * 0.92, 1))
        else:
            target_sell = round(est_high * 0.95, 1)

        # 扣完 13% 手續費後的預期每發淨利與淨報酬率
        net_after_tax = target_sell * NET_MULTIPLIER
        est_net_profit = round(net_after_tax - price, 1)
        est_net_roi_pct = round((est_net_profit / price) * 100, 1) if price > 0 else 0.0

        # 相對均價折價率（負值代表比均價便宜，適合自用抄底）
        vs_avg_pct = round(((price - est_avg) / est_avg) * 100, 1) if est_avg > 0 else 0.0

        # 若累積超過 24 筆樣本，統計歷史最常出現低價與高價的台灣時段
        best_buy_hour = None
        best_sell_hour = None
        if len(pts) >= 24:
            hour_buckets = {}
            for p in pts:
                h = p[3]
                hour_buckets.setdefault(h, []).append(p[1])
            hour_avgs = {h: sum(v) / len(v) for h, v in hour_buckets.items() if len(v) >= 2}
            if hour_avgs:
                best_buy_hour = min(hour_avgs, key=hour_avgs.get)
                best_sell_hour = max(hour_avgs, key=hour_avgs.get)

        enriched.append({
            "name": name,
            "level": level,
            "price": round(price, 1),
            "change_1d": round(c1, 1),
            "change_7d": round(c7, 1),
            "change_30d": round(c30, 1),
            "breakeven_sell": breakeven_sell,
            "target_sell": round(target_sell, 1),
            "est_low": round(est_low, 1),
            "est_high": round(est_high, 1),
            "est_avg": round(est_avg, 1),
            "percentile": percentile,
            "vs_avg_pct": vs_avg_pct,
            "est_net_profit": est_net_profit,
            "est_net_roi_pct": est_net_roi_pct,
            "samples": len(pts),
            "best_buy_hour": best_buy_hour,
            "best_sell_hour": best_sell_hour,
        })

    return history, enriched


# ===================== 4. 篩選候選標的並交給 Gemini 產出操盤建議 =====================
def build_fallback_report(flip_top: list, self_use_top: list, sell_top: list, now_tw: datetime) -> dict:
    return {
        "updated_at": int(now_tw.timestamp()),
        "updated_str": now_tw.strftime("%Y-%m-%d %H:%M"),
        "source": "數學模型備援（未啟用 AI）",
        "market_summary": (
            f"目前時間為台灣 {WEEKDAY_ZH[now_tw.weekday()]} {now_tw.strftime('%H:%M')}。"
            "系統已依 13% 手續費門檻（賣價需比買價高至少 15% 才打平）篩選出以下重點彈藥。"
        ),
        "flip_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "buy_price": x["price"],
                "target_sell": x["target_sell"],
                "roi_pct": x["est_net_roi_pct"],
                "reason": f"現價落在區間 {x['percentile']}% 分位，反彈至目標價扣 13% 稅後預估淨利 {x['est_net_roi_pct']}%。",
            }
            for x in flip_top[:4]
        ],
        "self_use_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "buy_price": x["price"],
                "discount_pct": abs(x["vs_avg_pct"]),
                "reason": f"低於均價 {abs(x['vs_avg_pct'])}%，自用囤貨可完全避開 13% 賣出稅。",
            }
            for x in self_use_top[:4]
        ],
        "sell_picks": [
            {
                "name": x["name"],
                "level": x["level"],
                "sell_price": x["price"],
                "reason": f"現價處於 {x['percentile']}% 高位（高於均價 +{x['vs_avg_pct']}%），適合逢高出清。",
            }
            for x in sell_top[:4]
        ],
        "risk_note": "本行情採集自國際服拍賣行大盤；Garena 服單價可能存在折溢價，掛單前請核對局內市價並避開過高溢價累進稅。",
    }


def generate_ai_report(enriched: list[dict], now_tw: datetime) -> dict:
    # 聚焦實戰流通度最高的 Lv.3 ~ Lv.6 子彈（且單價 >= 100 避免低階彈噪音）
    tradable = [x for x in enriched if x["level"] >= 3 and x["price"] >= 100]
    if not tradable:
        tradable = [x for x in enriched if x["price"] >= 100]

    # 1. 抄底倒賣候選：預期反彈至目標高點扣 13% 稅後，淨報酬 >= 6% 且目前位於中低位（<= 55% 分位）
    flip_candidates = sorted(
        [x for x in tradable if x["est_net_roi_pct"] >= 6.0 and x["percentile"] <= 55.0],
        key=lambda x: (x["est_net_roi_pct"], x["level"]),
        reverse=True,
    )[:8]

    # 2. 自用囤貨候選：低於均價至少 7%（直接省錢免賣出稅）
    self_use_candidates = sorted(
        [x for x in tradable if x["vs_avg_pct"] <= -7.0],
        key=lambda x: x["vs_avg_pct"],
    )[:8]

    # 3. 逢高出清警戒：高於均價 >= 12% 或位於歷史高位（>= 80% 分位）且 7D 明顯上漲
    sell_candidates = sorted(
        [x for x in tradable if x["vs_avg_pct"] >= 12.0 or (x["percentile"] >= 80.0 and x["change_7d"] > 0)],
        key=lambda x: (x["vs_avg_pct"], x["percentile"]),
        reverse=True,
    )[:8]

    fallback = build_fallback_report(flip_candidates, self_use_candidates, sell_candidates, now_tw)
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        print("⚠️ 未偵測到 GEMINI_API_KEY，改用純數學策略報告。", flush=True)
        return fallback

    prompt = (
        f"你是《三角洲行動 (Delta Force)》資深拍賣行子彈操盤分析師。"
        f"目前台灣時間：{now_tw.strftime('%Y-%m-%d %H:%M')}（{WEEKDAY_ZH[now_tw.weekday()]}）。\n"
        f"請嚴格根據以下 Python 預先算好的候選子彈數據（皆已計入原生交易行 13% 手續費，需漲幅超過 15% 才打平），"
        f"產出一份繁體中文 JSON 操盤報告，只講具體數字與操作建議，絕不捏造沒在數據裡的子彈或價格：\n\n"
        f"【候選 1：抄底倒賣（扣 13% 稅仍有預期淨利）】\n{json.dumps(flip_candidates, ensure_ascii=False)}\n\n"
        f"【候選 2：自用抄底囤貨（低於均價、免 13% 賣出稅）】\n{json.dumps(self_use_candidates, ensure_ascii=False)}\n\n"
        f"【候選 3：逢高出清警戒（相對高位）】\n{json.dumps(sell_candidates, ensure_ascii=False)}\n\n"
        f"請從各分類挑選最值得操作的 3～4 款（若該分類候選為空則回傳空陣列 []），並結合目前是平日還是週末、凌晨還是晚間，給出簡短精準的買賣時機建議。"
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
        report["source"] = f"Gemini ({GEMINI_MODEL}) + 13% 稅後量化模型"
        print("🤖 Gemini 子彈操盤報告產出成功！", flush=True)
        return report

    except Exception as e:
        print(f"⚠️ Gemini 分析異常 ({e})，改用數學備援報告。", flush=True)
        return fallback


def main():
    now_tw = get_tw_now()
    print(f"🚀 啟動三角洲行動彈藥行情與 AI 操盤分析（台灣時間 {now_tw.strftime('%Y-%m-%d %H:%M:%S')}）\n", flush=True)

    items = scrape_deltaforce_ammo()
    if not items:
        print("❌ 未抓到任何彈藥資料，中止寫入以保護既有 KV 資料。", flush=True)
        sys.exit(1)

    history = kv_get_json("df_ammo_history", {})
    updated_history, enriched = update_history_and_compute_metrics(items, history, now_tw)

    # 依等級與價格排序，方便 Discord 顯示 4/5/6 級熱門彈藥榜
    enriched.sort(key=lambda x: (x["level"], x["price"]), reverse=True)

    report = generate_ai_report(enriched, now_tw)

    latest_payload = {
        "updated_at": int(now_tw.timestamp()),
        "updated_str": now_tw.strftime("%Y-%m-%d %H:%M"),
        "count": len(enriched),
        "items": enriched,
    }

    kv_put_json("df_ammo_latest", latest_payload)
    kv_put_json("df_ammo_history", updated_history)
    kv_put_json("df_ammo_report", report)

    print("\n📊 操盤快報預覽：", flush=True)
    print(report.get("market_summary", ""), flush=True)
    print(f"  - 抄底倒賣推薦：{len(report.get('flip_picks', []))} 款", flush=True)
    print(f"  - 自用囤貨推薦：{len(report.get('self_use_picks', []))} 款", flush=True)
    print(f"  - 逢高出清警戒：{len(report.get('sell_picks', []))} 款", flush=True)
    print("🎉 任務全數完成！", flush=True)


if __name__ == "__main__":
    main()
