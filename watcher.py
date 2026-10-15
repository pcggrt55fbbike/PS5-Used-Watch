import json
import os
import re
import time
import hashlib
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "seen.json"
RESULTS_PATH = ROOT / "watch-results.json"
MIN_PRICE = int(CONFIG.get("min_price", 20000))
MAX_PRICE = int(CONFIG.get("max_price", 29999))
TIMEOUT = int(CONFIG.get("request_timeout_seconds", 20))
MAX_RESULTS = int(CONFIG.get("max_results_per_source", 30))

# Search pages only; keep polling low-frequency and respect each site's terms/robots rules.
SOURCES = [
    ("Mercari", "https://jp.mercari.com/search?keyword={q}"),
    ("Yahoo Auctions", "https://auctions.yahoo.co.jp/search/search?p={q}"),
    ("Yahoo Flea Market", "https://paypayfleamarket.yahoo.co.jp/search/{q}"),
    ("Rakuma", "https://fril.jp/s?query={q}"),
    ("Hard Off", "https://netmall.hardoff.co.jp/search/?q={q}"),
    ("Jimoty", "https://jmty.jp/all/sale?keyword={q}"),
]
JUNK = re.compile(r"ジャンク|故障|不動|起動しない|起動不可|HDMI.{0,5}(不良|故障)|部品取り|修理前提|動作未確認|未確認", re.I)
SOLD = re.compile(r"売り切れ|sold\s*out|\bsold\b|取引完了|落札済み|オークション終了|販売終了|購入できません|出品を取り消し|削除されました|商品が見つかりません", re.I)
BUY_MARKERS = {
    "Yahoo Flea Market": re.compile(r"購入手続きへ|購入する|価格の相談", re.I),
    "Mercari": re.compile(r"購入手続きへ|購入する", re.I),
    "Yahoo Auctions": re.compile(r"入札する|今すぐ落札|即決価格", re.I),
    "Rakuma": re.compile(r"購入に進む|購入申請|購入手続き", re.I),
    "Hard Off": re.compile(r"カートに入れる|在庫あり", re.I),
    "Jimoty": re.compile(r"問い合わせ|投稿者にメール", re.I),
}
GOOD = re.compile(r"動作確認済|動作確認|完動品|正常動作|問題なく動作|動作品", re.I)
PRICE_RE = re.compile(r"(?<!\d)(\d{1,3}(?:,\d{3})+|\d{4,6})\s*円?")
PS5_RE = re.compile(r"\bPS\s*5\b|PlayStation\s*5|プレイステーション\s*5|CFI-?1[012]\d{2}", re.I)
MODEL_RE = re.compile(r"CFI[-ー ]?(1000|1100|1200)[AB]?", re.I)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; PS5UsedPriceWatch/1.0; +https://github.com/)"
})

def price_from(text):
    match = PRICE_RE.search(text.replace("￥", "").replace("¥", ""))
    if not match:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None

def normalize_url(url):
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url
    # Drop tracking query parameters where possible; retain item-identifying paths.
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

def parse_results(source_name, page_url, html, query):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()
    for a in soup.select("a[href]"):
        title = " ".join(a.get_text(" ", strip=True).split())
        href = urljoin(page_url, a.get("href", ""))
        if not title or len(title) < 5 or not href.startswith("http"):
            continue
        # First validate the listing title itself. Do not accept a PS4/PS Portal/
        # controller listing merely because a surrounding card or page mentions PS5.
        title_norm = " ".join(title.split())
        # Exclude PS5-compatible accessories and the PlayStation Portal; they are not consoles.
        if re.search(r"(リモートプレイヤー|PlayStation\\s*Portal|PS\\s*Portal|980\\s*PRO|M\\.?2\\s*SSD|SSD|DualSense|ゲームパッド|ヘッドセット|コントローラー単体|コントローラーのみ|本体カバー|本体ケース|縦置きスタンド)", title_norm, re.I):
            continue
        console_title = bool(
            re.search(r"(PS\s*5|PlayStation\s*5|プレイステーション\s*5)", title_norm, re.I)
            and re.search(r"(本体|本体のみ|本体セット|本体\s*セット)", title_norm)
        ) or bool(re.search(r"\bCFI[-ー ]?(?:1000|1100|1200)[AB]?\b", title_norm, re.I))
        if not console_title:
            continue
        combined = title_norm
        parent = a
        for _ in range(2):
            if parent.parent:
                parent = parent.parent
                combined += " " + " ".join(parent.get_text(" ", strip=True).split())[:350]
        combined = " ".join(combined.split())
        # Search cards can retain sold listings; reject them before considering price.
        if SOLD.search(combined):
            continue
        # Use the listing title price first; card text is fallback only after title validation.
        price = price_from(title_norm) or price_from(combined)
        if price is None or not (MIN_PRICE <= price <= MAX_PRICE):
            continue
        url = normalize_url(href)
        if url in seen:
            continue
        seen.add(url)
        junk = bool(JUNK.search(combined))
        score = 0
        if GOOD.search(combined): score += 3
        if re.search(r"コントローラー|DualSense", combined, re.I): score += 1
        if re.search(r"CFI[-ー ]?1000[AB]?", combined, re.I): score += 1
        if re.search(r"デジタル", combined, re.I): score += 1
        results.append({
            "source": source_name,
            "title": title[:240],
            "price_yen": price,
            "url": href,
            "normalized_url": url,
            "query": query,
            "likely_junk_or_unverified": junk,
            "operation_claim_in_text": bool(GOOD.search(combined)),
            "score": score,
            "snippet": combined[:500]
        })
    results.sort(key=lambda x: (x["likely_junk_or_unverified"], -x["score"], x["price_yen"]))
    return results[:MAX_RESULTS]

def verify_listing(item):
    """Return active only when the item page explicitly looks purchasable."""
    try:
        response = SESSION.get(item["url"], timeout=TIMEOUT, allow_redirects=True)
        item["availability_http_status"] = response.status_code
        if response.status_code != 200:
            item["availability"] = "unverified"
            return False
        soup = BeautifulSoup(response.text, "html.parser")
        page_text = " ".join(soup.get_text(" ", strip=True).split())
        item["page_title"] = (soup.title.get_text(" ", strip=True) if soup.title else "")[:180]
        if SOLD.search(page_text) or SOLD.search(item["page_title"]):
            item["availability"] = "sold_or_ended"
            return False
        marker = BUY_MARKERS.get(item["source"])
        if marker and marker.search(page_text):
            item["availability"] = "active_marker_found"
            return True
        item["availability"] = "unverified"
        return False
    except requests.RequestException as exc:
        item["availability"] = "unverified"
        item["availability_error"] = str(exc)[:160]
        return False

def load_seen():
    try:
        return set(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()

def send_discord(items):
    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if not webhook or not items:
        return
    # Discord message limit is 2000 chars.
    lines = ["**PS5中古ウォッチ：新しい2万円台候補**"]
    for item in items[:12]:
        mark = "⚠️要確認" if item["likely_junk_or_unverified"] else ("✅動作表記あり" if item["operation_claim_in_text"] else "🔎動作要確認")
        lines.append(f"{mark} **¥{item['price_yen']:,}** [{item['source']}: {item['title'][:90]}]({item['url']})")
    payload = {"content": "\n".join(lines)[:1950]}
    response = SESSION.post(webhook, json=payload, timeout=TIMEOUT)
    response.raise_for_status()

def main():
    seen = load_seen()
    all_items = []
    status = []
    for source_name, template in SOURCES:
        source_count = 0
        for query in CONFIG["queries"]:
            search_url = template.format(q=quote(query))
            try:
                response = SESSION.get(search_url, timeout=TIMEOUT)
                status.append({"source": source_name, "query": query, "http_status": response.status_code})
                if response.status_code != 200:
                    continue
                parsed = parse_results(source_name, response.url, response.text, query)
                all_items.extend(parsed)
                source_count += len(parsed)
                time.sleep(1.5)  # polite delay between queries
            except requests.RequestException as exc:
                status.append({"source": source_name, "query": query, "error": str(exc)[:200]})
        print(f"{source_name}: {source_count} matching candidate(s) parsed")
    dedup = {}
    for item in all_items:
        key = item["normalized_url"] or hashlib.sha256(item["title"].encode()).hexdigest()
        if key not in dedup or item["score"] > dedup[key]["score"]:
            dedup[key] = item
    results = sorted(dedup.values(), key=lambda x: (x["likely_junk_or_unverified"], -x["score"], x["price_yen"]))
    # Verify every candidate at its detail page; never notify based on search cards alone.
    active_items = []
    for item in results:
        if verify_listing(item):
            active_items.append(item)
        time.sleep(0.8)
    new_items = [item for item in active_items if item["normalized_url"] not in seen]
    payload = {
        "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "price_range_yen": [MIN_PRICE, MAX_PRICE],
        "candidate_count": len(results),
        "verified_active_count": len(active_items),
        "new_candidate_count": len(new_items),
        "status": status,
        "candidates": results
    }
    RESULTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    send_discord(new_items)
    seen.update(item["normalized_url"] for item in active_items)
    STATE_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Candidates parsed: {len(results)}; verified active: {len(active_items)}; new active: {len(new_items)}")
    availability_counts = {}
    for item in results:
        key = item.get("availability", "not_checked")
        availability_counts[key] = availability_counts.get(key, 0) + 1
    print("Availability checks: " + json.dumps(availability_counts, ensure_ascii=False))
    for item in results[:30]:
        print(f"¥{item['price_yen']:,} | {item['source']} | {item['title']} | {item['url']}")
    if not results:
        print("No candidates parsed. Search-page markup may have changed or access may be blocked.")

if __name__ == "__main__":
    main()
