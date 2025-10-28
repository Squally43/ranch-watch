import asyncio, json, os, re, sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urljoin
from typing import Dict, List, Tuple

from playwright.async_api import async_playwright
import urllib.request, urllib.error

# ---------------- Config ----------------
ROOT = "https://ranchroleplay.com"
BUSINESSES_URL = f"{ROOT}/businesses"
PROPERTIES_URL  = f"{ROOT}/properties"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"


STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True)

# ENV controls (set via GitHub Actions inputs or local env)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
DISCORD_TEST    = (os.getenv("DISCORD_TEST", "false").lower() == "true")
POST_ALL        = (os.getenv("POST_ALL", "false").lower() == "true")

# How many absent runs before we say "OFF MARKET"
ABSENCE_THRESHOLD = 1  # alert as soon as it disappears once; raise to 2 if you want to be safer

# ------------- Helpers: state I/O -------------
def _state_path(name: str) -> Path:
    return STATE_DIR / f"{name}.json"

def load_map(name: str) -> Dict[str, dict]:
    p = _state_path(name)
    if p.exists():
        return json.loads(p.read_text())
    return {}

def save_map(name: str, data: Dict[str, dict]):
    _state_path(name).write_text(json.dumps(data, indent=2, ensure_ascii=False))

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ------------- Discord (embeds) -------------
def _chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def build_embed(title: str, url: str, fields: List[Tuple[str, str]], color: int = 0x2b6cb0):
    # Max 25 fields per embed; we’ll keep it small (<=6)
    embed = {
        "title": title[:256],
        "url": url,
        "color": color,
        "timestamp": utc_now_iso(),
        "fields": [{"name": k[:256], "value": (v or "-")[:1024], "inline": True} for k, v in fields][:10]
    }
    return embed

def post_discord(content: str = "", embeds: List[dict] = None):
    """
    Send to Discord using curl (same path as the sanity step) to avoid urllib quirks.
    """
    if not DISCORD_WEBHOOK:
        print("[warn] No DISCORD_WEBHOOK set; printing only.")
        print(content)
        if embeds:
            print(json.dumps(embeds, indent=2))
        return

    url = DISCORD_WEBHOOK
    if "?wait=" not in url:
        url += "?wait=true"

    payload = {"content": content}
    if embeds:
        payload["embeds"] = embeds

    import subprocess, shlex
    data = json.dumps(payload)

    cmd = f'curl -sS -X POST -H "Content-Type: application/json" -d {shlex.quote(data)} "{url}"'
    print("[debug] posting via curl…")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)

    if res.returncode != 0:
        print("[err] curl failed:", res.stderr.strip())
        raise RuntimeError(f"Discord post failed (curl exit {res.returncode})")

    # When wait=true Discord returns JSON; if empty, it's fine too.
    if res.stdout:
        print("[ok] Discord responded with body:", res.stdout[:2000])
    else:
        print("[ok] Discord responded (no body).")


def post_discord_batched(title_prefix: str, items: List[dict], batch_size=10):
    # Discord allows up to 10 embeds per message
    for i, batch in enumerate(_chunk(items, batch_size), start=1):
        content = f"**{title_prefix}** (batch {i})" if len(items) > batch_size else f"**{title_prefix}**"
        post_discord(content=content, embeds=batch)

# ------------- Scraping -------------
PRICE_RE  = re.compile(r"\$\s*([0-9][\d,]*)", re.I)
BID_RE    = re.compile(r"(?:current\s*)?bid[:\s]*\$\s*([0-9][\d,]*)", re.I)
STATUS_RE = re.compile(r"(for sale|on the market|sold|pending|closed|reserved|taken|leased|rented)", re.I)

async def get_page_items(page, base_path: str) -> Dict[str, dict]:
    """
    Robust extractor:
    - Waits for network idle + small delays
    - Scrolls to bottom to trigger lazy loads
    - Collects ALL anchors and normalizes to absolute URLs
    - Accepts any path containing '/business' or '/propert'
    - Uses the final path segment as the slug
    """
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)

    # try to trigger any lazy content
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(800)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    anchors = await page.locator('a[href]').all()
    hrefs = []
    for a in anchors:
        h = await a.get_attribute("href")
        if h:
            # to absolute
            hrefs.append(urljoin(ROOT, h))

    if DEBUG:
        print(f"[debug] total anchors found: {len(hrefs)}")
        susp = [h for h in hrefs if ("/business" in h or "/propert" in h)]
        print("[debug] sample candidates:", susp[:25])

    found: Dict[str, dict] = {}
    for a in anchors:
        href = await a.get_attribute("href")
        if not href:
            continue
        abs_url = urljoin(ROOT, href)

        # accept both businesses/properties/singular variants/other prefixes
        if ("/business" in abs_url or "/propert" in abs_url):
            # get last non-empty path segment as slug
            path = abs_url.split("://", 1)[-1].split("/", 1)[-1]  # strip scheme/host
            parts = [p for p in path.split("/") if p and not p.startswith("#")]
            if len(parts) < 2:  # need at least "businesses" + "slug"
                continue
            slug = parts[-1]
            if not slug or "." in slug or "?" in slug:  # skip files/query-only
                slug = slug.split("?")[0]

            # title from link text
            title = (await a.inner_text()).strip() or slug.replace("-", " ").title()

            # inspect nearest card/container for details
            container_text = await a.evaluate("""
                el => {
                  let c = el.closest('article, .card, .item, li, .container, .block, .tile, .listing, .panel, .box')
                       || el.parentElement;
                  return (c && c.innerText) ? c.innerText : el.innerText || '';
                }
            """) or ""
            # parse details
            price = None; bid = None; status = None
            m = PRICE_RE.search(container_text);   price = int(m.group(1).replace(",","")) if m else None
            m = BID_RE.search(container_text);     bid   = int(m.group(1).replace(",","")) if m else None
            m = STATUS_RE.search(container_text);  status = m.group(1).title() if m else None

            found[slug] = {
                "slug": slug,
                "url": abs_url,
                "title": title,
                "price": price,
                "current_bid": bid,
                "status": status,
                "last_seen": utc_now_iso(),
                "absence": 0,
            }
    if DEBUG:
        print(f"[debug] matched items: {len(found)}  (keys: {list(found)[:10]}...)")
    return found


async def scrape(browser, url: str, state_name: str, base_path: str):
    page = await browser.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    items = await get_page_items(page, base_path)
    await page.close()

    prev = load_map(state_name)  # map: slug -> data
    now  = items

    new_items, bid_ups, disappeared = [], [], []

    # Compare current vs previous
    for slug, info in now.items():
        if slug not in prev:
            new_items.append(info)
        else:
            # bid increase?
            before = prev[slug]
            b_prev = before.get("current_bid")
            b_now  = info.get("current_bid")
            if b_now is not None and b_prev is not None and b_now > b_prev:
                bid_ups.append((before, info))

            # carry absence forward (we saw it this run)
            info["absence"] = 0

    # Detect disappeared (potentially off market)
    for slug, old in prev.items():
        if slug not in now:
            # mark absence
            old_abs = int(old.get("absence", 0)) + 1
            old["absence"] = old_abs
            if old_abs >= ABSENCE_THRESHOLD:
                disappeared.append(old)

    # Merge & persist state
    merged = {**prev, **now}
    # ensure we keep increased absence for missing ones
    for d in disappeared:
        merged[d["slug"]] = d
    save_map(state_name, merged)

    return new_items, bid_ups, disappeared, now

# ------------- Reporting -------------
def embeds_for_new(kind: str, items: List[dict]) -> List[dict]:
    embeds = []
    for it in items:
        fields = [
            ("Status", it.get("status") or "On the Market"),
            ("Price", f"${it['price']:,}" if it.get("price") else "—"),
            ("Current Bid", f"${it['current_bid']:,}" if it.get("current_bid") else "—"),
        ]
        embeds.append(build_embed(f"New {kind[:-1].title()}: {it['title']}", it["url"], fields, color=0x22c55e))
    return embeds

def embeds_for_bidups(kind: str, pairs: List[Tuple[dict, dict]]) -> List[dict]:
    embeds = []
    for before, after in pairs:
        fields = [
            ("Previous Bid", f"${before['current_bid']:,}" if before.get("current_bid") else "—"),
            ("New Bid", f"${after['current_bid']:,}" if after.get("current_bid") else "—"),
            ("Price", f"${after['price']:,}" if after.get("price") else "—"),
        ]
        embeds.append(build_embed(f"{kind[:-1].title()} Bid Increased: {after['title']}", after["url"], fields, color=0xf59e0b))
    return embeds

def embeds_for_offmarket(kind: str, items: List[dict]) -> List[dict]:
    embeds = []
    for it in items:
        fields = [
            ("Last Known Status", it.get("status") or "Unknown"),
            ("Last Seen (UTC)", it.get("last_seen") or "—"),
            ("Last Price", f"${it['price']:,}" if it.get("price") else "—"),
            ("Last Bid", f"${it['current_bid']:,}" if it.get("current_bid") else "—"),
        ]
        embeds.append(build_embed(f"{kind[:-1].title()} Possibly Off Market: {it['title']}", it.get("url") or "", fields, color=0xef4444))
    return embeds

def embeds_for_dump(kind: str, items_map: Dict[str, dict]) -> List[dict]:
    embeds = []
    for slug, it in sorted(items_map.items(), key=lambda kv: kv[1].get("title","").lower()):
        fields = [
            ("Status", it.get("status") or "On the Market"),
            ("Price", f"${it['price']:,}" if it.get("price") else "—"),
            ("Current Bid", f"${it['current_bid']:,}" if it.get("current_bid") else "—"),
            ("Slug", slug),
        ]
        embeds.append(build_embed(f"{kind[:-1].title()}: {it['title']}", it.get("url") or "", fields, color=0x3b82f6))
    return embeds

# ------------- Main -------------
async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            # Optional: test message
            if DISCORD_TEST:
                post_discord(content="✅ **Ranch Watch test** — webhook and embeds look good.")

            # Scrape both pages
            new_biz, bidups_biz, off_biz, biz_now = await scrape(browser, BUSINESSES_URL, "businesses", "/businesses")
            new_prop, bidups_prop, off_prop, prop_now = await scrape(browser, PROPERTIES_URL,  "properties", "/properties")

            # Post ALL (baseline snapshot)
            if POST_ALL:
                post_discord_batched("Businesses — Full Snapshot", embeds_for_dump("businesses", biz_now))
                post_discord_batched("Properties — Full Snapshot", embeds_for_dump("properties", prop_now))

            # New items
            if new_biz:
                post_discord_batched("New Businesses Detected", embeds_for_new("businesses", new_biz))
            if new_prop:
                post_discord_batched("New Properties Detected", embeds_for_new("properties", new_prop))

            # Bid increases
            if bidups_biz:
                post_discord_batched("Business Bid Increases", embeds_for_bidups("businesses", bidups_biz))
            if bidups_prop:
                post_discord_batched("Property Bid Increases", embeds_for_bidups("properties", bidups_prop))

            # Off market
            if off_biz:
                post_discord_batched("Businesses Possibly Off Market", embeds_for_offmarket("businesses", off_biz))
            if off_prop:
                post_discord_batched("Properties Possibly Off Market", embeds_for_offmarket("properties", off_prop))

            # Console summary
            print("New businesses:", [i["slug"] for i in new_biz])
            print("New properties:", [i["slug"] for i in new_prop])

        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
