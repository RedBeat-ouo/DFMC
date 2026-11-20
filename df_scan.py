import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from playwright.sync_api import sync_playwright
import requests

# ===================== 基礎設定 =====================
BASE_AMMO_URL = "https://deltaforcetools.gg/tw/auction-house/ammo"
TAX_RATE = 0.13  # 三角洲行動拍賣行約 13% 手續費（含上架費與成交稅）
NET_MULTIPLIER = 1.0 - TAX_RATE  # 0.87
MAX_HISTORY_POINTS = 360  # 每 2 小時 1 筆 × 30 天 = 360 筆
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

WEEKDAY_ZH = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


def get_tw_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=8)))


def parse_number(text: str) -> float:
    s = str(text or "").strip().replace(",", "")
    negative = ("▼" in s or "↓" in s or s.startswith("-"))
    cleaned = re.sub(r"[^0-9.]", "", s)
    if not cleaned:
        return 0.0
    try:
        val = float(cleaned)
        return -val if negative else val
    except ValueError:
        return 0.0


def parse_change_cell(text: str) -> tuple[float, bool]:
    s = str(text or "").strip()
    is_pct = "%" in s
    val = parse_number(s)
    return val, is_pct


def parse_level_from_text(text: str) -> int:
    s = str(text or "").strip()
    m = re.search(r"(?:lv\.?|t|等級)\s*([1-6])\b", s, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m2 = re.search(r"([1-6])\s*(?:級|階)", s)
    if m2:
        return int(m2.group(1))
    if re.fullmatch(r"[1-6]", s):
        return int(s)
    return 0


def estimate_base_price(current_price: float, delta_val: float, is_pct: bool) -> float:
    if current_price <= 0 or delta_val == 0:
        return current_price
    if is_pct:
        factor = 1.0 + (delta_val / 100.0)
        if factor > 0.15:
            return current_price / factor
        return current_price
    base = current_price - delta_val
    return base if base > 0 else current_price


# ===================== 1A. 從攔截到的 JSON API 智慧提取彈藥 =====================
NAME_KEYS = ("name_tw", "zh_tw", "zh_name", "nameTw", "item_name", "itemName", "name", "title")
PRICE_KEYS = ("price", "current_price", "currentPrice", "avg_price", "avgPrice", "latest_price", "latestPrice", "market_price", "marketPrice", "value")
LEVEL_KEYS = ("level", "grade", "tier", "rarity", "quality", "lv")


def pick_first_key(obj: dict, keys: tuple):
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    return None


def extract_items_from_json_blob(blob, depth: int = 0) -> list[dict]:
    if depth > 5 or blob is None:
        return []

    best = []
    if isinstance(blob, list):
        parsed = []
        for elem in blob:
            if not isinstance(elem, dict):
                continue
            raw_name = pick_first_key(elem, NAME_KEYS)
            raw_price = pick_first_key(elem, PRICE_KEYS)
            if isinstance(raw_name, dict):
                raw_name = raw_name.get("zh-TW") or raw_name.get("tw") or raw_name.get("zh") or raw_name.get("en")
            if not isinstance(raw_name, str) or len(raw_name.strip()) < 2:
                continue
            price_val = parse_number(str(raw_price)) if raw_price is not None else 0.0
            if price_val <= 0:
                continue

            raw_level = pick_first_key(elem, LEVEL_KEYS)
            level_val = parse_level_from_text(str(raw_level)) if raw_level is not None else 0

            c1 = parse_number(str(elem.get("change_1d") or elem.get("change1d") or elem.get("day_change") or elem.get("diff_1d") or 0))
            c7 = parse_number(str(elem.get("change_7d") or elem.get("change7d") or elem.get("week_change") or elem.get("diff_7d") or 0))
            c30 = parse_number(str(elem.get("change_30d") or elem.get("change30d") or elem.get("month_change") or elem.get("diff_30d") or 0))

            parsed.append({
                "name": raw_name.strip(),
                "price": round(price_val, 2),
                "change_1d": round(c1, 2),
                "change_7d": round(c7, 2),
                "change_30d": round(c30, 2),
                "change_is_pct": False,
                "level": level_val,
            })
        if len(parsed) >= 5:
            return parsed

        for elem in blob[:20]:
            sub = extract_items_from_json_blob(elem, depth + 1)
            if len(sub) > len(best):
                best = sub
        return best

    if isinstance(blob, dict):
        for v in blob.values():
            sub = extract_items_from_json_blob(v, depth + 1)
            if len(sub) > len(best):
                best = sub
    return best


# ===================== 1B. 從瀏覽器渲染後的 DOM 表格智慧提取彈藥 =====================
def extract_rows_from_page(page) -> list[list[str]]:
    return page.evaluate(
        """() => {
            const out = [];
            let rows = Array.from(document.querySelectorAll('table tbody tr'));
            if (rows.length === 0) rows = Array.from(document.querySelectorAll('table tr'));
            if (rows.length === 0) rows = Array.from(document.querySelectorAll('[role="row"]'));
            for (const r of rows) {
                const cells = Array.from(r.querySelectorAll('td, [role="cell"], [role="gridcell"]'))
                    .map(el => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim());
                if (cells.length > 0) out.push(cells);
            }
            return out;
        }"""
    )


def parse_dom_rows(raw_rows: list[list[str]]) -> list[dict]:
    items = []
    for raw_cells in raw_rows:
        # 過濾掉純圖片圖示造成的空白格
        cells = [c for c in raw_cells if c]
        if len(cells) < 2:
            continue

        # 跳過表頭列
        row_joined = " ".join(cells).lower()
        if any(h in row_joined for h in ("名稱", "item name", "價格", "24h", "7d")) and not re.search(r"\d{2,}", row_joined):
            continue

        # 找出物品名稱：第一個包含中文或英文字母且非純標籤的欄位
        name = ""
        name_idx = -1
        for idx, c in enumerate(cells):
            if re.search(r"[\u4e00-\u9fffA-Za-z]", c) and not re.fullmatch(r"(?:lv\.?\s*\d+|t\d+|ammo|彈藥|[+\-]?\d+(?:\.\d+)?%)", c, re.IGNORECASE):
                name = c
                name_idx = idx
                break

        if not name or name_idx < 0:
            continue

        rest = cells[name_idx + 1:]
        price = 0.0
        price_idx = -1
        level = parse_level_from_text(name)

        # 尋找價格（不含 % 且不是純等級標籤的第一個正數）
        for idx, c in enumerate(rest):
            if "%" in c:
                continue
            lv_check = parse_level_from_text(c)
            if lv_check > 0 and re.search(r"(?:lv|級|階|t)", c, re.IGNORECASE):
                level = lv_check
                continue
            val = parse_number(c)
            if val > 0:
                price = val
                price_idx = idx
                break

        if price <= 0:
            continue

        # 解析剩餘欄位中的漲跌與等級
        changes = []
        change_is_pct = False
        for idx, c in enumerate(rest):
            if idx == price_idx:
                continue
            lv = parse_level_from_text(c)
            if lv > 0 and (re.search(r"(?:lv|級|階|t)", c, re.IGNORECASE) or (re.fullmatch(r"[1-6]", c) and len(changes) >= 1)):
                if level == 0:
                    level = lv
                continue
            if re.search(r"\d", c):
                val, is_pct = parse_change_cell(c)
                if is_pct:
                    change_is_pct = True
                changes.append(val)

        c1 = changes[0] if len(changes) > 0 else 0.0
        c7 = changes[1] if len(changes) > 1 else 0.0
        c30 = changes[2] if len(changes) > 2 else 0.0

        items.append({
            "name": name,
            "price": round(price, 2),
            "change_1d": round(c1, 2),
            "change_7d": round(c7, 2),
            "change_30d": round(c30, 2),
            "change_is_pct": change_is_pct,
            "level": level,
        })

    return items


# ===================== 1. 主爬蟲入口（Playwright 真實瀏覽器） =====================
def scrape_deltaforce_ammo(max_pages: int = 12) -> list[dict]:
    items_by_name = {}
    captured_jsons = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            locale="zh-TW",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        def handle_response(response):
            try:
                ct = (response.headers.get("content-type") or "").lower()
                if response.status == 200 and "json" in ct:
                    data = response.json()
                    captured_jsons.append((response.url, data))
            except Exception:
                pass

        page.on("response", handle_response)

        print(f"🌐 正在啟動無頭瀏覽器開啟：{BASE_AMMO_URL}", flush=True)
        try:
            page.goto(BASE_AMMO_URL, wait_until="networkidle", timeout=45000)
        except Exception as e:
            print(f"⚠️ 等待 networkidle 超時（繼續檢查已渲染內容）：{e}", flush=True)

        # 額外等待前端表格或資料掛載完成
        try:
            page.wait_for_selector("table tbody tr, [role='row']", timeout=12000)
        except Exception:
            pass
        page.wait_for_timeout(2500)

        # 優先檢查攔截到的背景 JSON API（若直接拿到全量資料最準確）
        for api_url, blob in captured_jsons:
            extracted = extract_items_from_json_blob(blob)
            if len(extracted) >= 10:
                print(f"🎯 從背景 JSON API 成功攔截 {len(extracted)} 款彈藥：{api_url}", flush=True)
                for it in extracted:
                    items_by_name[it["name"]] = it

        # 若背景 API 未直接拿到全量，從渲染後的真實 DOM 逐頁抓取
        if len(items_by_name) < 10:
            prev_sig = None
            for page_idx in range(1, max_pages + 1):
                raw_rows = extract_rows_from_page(page)
                parsed = parse_dom_rows(raw_rows)
                print(f"📡 DOM 第 {page_idx} 頁：偵測到 {len(raw_rows)} 列 ➔ 解析出 {len(parsed)} 款彈藥", flush=True)

                if not parsed:
                    break

                sig = tuple(x["name"] for x in parsed)
                if sig == prev_sig:
                    print("   ➔ 與上一頁相同，停止翻頁。", flush=True)
                    break
                prev_sig = sig

                for it in parsed:
                    items_by_name[it["name"]] = it

                # 嘗試點擊「下一頁」按鈕
                next_clicked = False
                next_selectors = [
                    "button:has-text('下一頁')",
                    "button:has-text('Next')",
                    "a:has-text('下一頁')",
                    "a:has-text('Next')",
                    "button[aria-label*='next' i]",
                    "a[aria-label*='next' i]",
                    "button:has-text('›')",
                    "button:has-text('>')",
                ]
                for sel in next_selectors:
                    loc = page.locator(sel).first
                    try:
                        if loc.count() > 0 and loc.is_visible() and loc.is_enabled():
                            loc.click(timeout=5000)
                            page.wait_for_timeout(2000)
                            next_clicked = True
                            break
                    except Exception:
                        continue

                if not next_clicked:
                    break

        # 若仍為 0 筆，印出詳細頁面診斷情報方便一次定位
        if len(items_by_name) == 0:
            print("\n🔍 ===== 診斷情報（供排查網站結構）=====", flush=True)
            print(f"最終網址：{page.url}", flush=True)
            print(f"頁面標題：{page.title()}", flush=True)
            print(f"攔截到 JSON 請求數：{len(captured_jsons)}", flush=True)
            for u, _ in captured_jsons[:10]:
                print(f"  - JSON URL: {u}", flush=True)
            sample_rows = extract_rows_from_page(page)
            print(f"原始表格列數：{len(sample_rows)}", flush=True)
            for idx, r in enumerate(sample_rows[:5]):
                print(f"  - Row {idx}: {r}", flush=True)
            body_preview = page.evaluate("() => (document.body.innerText || '').slice(0, 700)")
            print(f"頁面可見文字前段：\n{body_preview}", flush=True)
            print("=========================================\n", flush=True)

        browser.close()

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
        c1 = float(it.get("change_1d", 0.0))
        c7 = float(it.get("change_7d", 0.0))
        c30 = float(it.get("change_30d", 0.0))
        is_pct = bool(it.get("change_is_pct", False))
        level = int(it.get("level", 0))

        # 追加本次快照至歷史 K 線（格式：[unix_ts, price, weekday, hour]）
        pts = history.get(name, [])
        if not isinstance(pts, list):
            pts = []
        if not pts or (now_unix - pts[-1][0]) >= 1800:
            pts.append([now_unix, round(price, 1), weekday, hour])
            if len(pts) > MAX_HISTORY_POINTS:
                pts = pts[-MAX_HISTORY_POINTS:]
        history[name] = pts

        # 由站方 1D / 7D / 30D 漲跌反推歷史基準價（支援金額與百分比）
        ref_prices = [price]
        for delta in (c1, c7, c30):
            base_p = estimate_base_price(price, delta, is_pct)
            if base_p > 0:
                ref_prices.append(base_p)

        hist_prices = [p[1] for p in pts if isinstance(p, list) and len(p) >= 2 and p[1] > 0]
        all_prices = ref_prices + hist_prices

        est_low = min(all_prices)
        est_high = max(all_prices)
        est_avg = sum(all_prices) / len(all_prices)

        if est_high > est_low:
            percentile = round((price - est_low) / (est_high - est_low) * 100, 1)
        else:
            percentile = 50.0

        # 13% 手續費損益兩平賣價
        breakeven_sell = math.ceil(price / NET_MULTIPLIER)

        # 保守目標賣價
        if len(hist_prices) >= 12:
            sorted_hp = sorted(hist_prices)
            p85_idx = min(len(sorted_hp) - 1, int(len(sorted_hp) * 0.85))
            target_sell = max(sorted_hp[p85_idx], round(est_high * 0.92, 1))
        else:
            target_sell = round(est_high * 0.95, 1)

        net_after_tax = target_sell * NET_MULTIPLIER
        est_net_profit = round(net_after_tax - price, 1)
        est_net_roi_pct = round((est_net_profit / price) * 100, 1) if price > 0 else 0.0
        vs_avg_pct = round(((price - est_avg) / est_avg) * 100, 1) if est_avg > 0 else 0.0

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
    tradable = [x for x in enriched if x["level"] >= 3 and x["price"] >= 100]
    if not tradable:
        tradable = [x for x in enriched if x["price"] >= 100]

    flip_candidates = sorted(
        [x for x in tradable if x["est_net_roi_pct"] >= 6.0 and x["percentile"] <= 55.0],
        key=lambda x: (x["est_net_roi_pct"], x["level"]),
        reverse=True,
    )[:8]

    self_use_candidates = sorted(
        [x for x in tradable if x["vs_avg_pct"] <= -7.0],
        key=lambda x: x["vs_avg_pct"],
    )[:8]

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
