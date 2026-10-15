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
        combined = title
        parent = a
        for _ in range(3):
            if parent.parent:
                parent = parent.parent
                combined += " " + " ".join(parent.get_text(" ", strip=True).split())[:500]
        combined = " ".join(combined.split())
        if not PS5_RE.search(combined) and not MODEL_RE.search(combined):
            continue
        price = price_from(combined)
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
    new_items = [item for item in results if item["normalized_url"] not in seen]
    payload = {
        "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "price_range_yen": [MIN_PRICE, MAX_PRICE],
        "candidate_count": len(results),
        "new_candidate_count": len(new_items),
        "status": status,
        "candidates": results
    }
    RESULTS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    send_discord(new_items)
    seen.update(item["normalized_url"] for item in results)
    STATE_PATH.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Candidates: {len(results)}; new: {len(new_items)}")
    for item in results[:30]:
        print(f"¥{item['price_yen']:,} | {item['source']} | {item['title']} | {item['url']}")
    if not results:
        print("No candidates parsed. Search-page markup may have changed or access may be blocked.")

if __name__ == "__main__":
    main()
