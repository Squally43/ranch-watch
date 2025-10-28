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
RANCH_COOKIE_HEADER = os.getenv("RANCH_COOKIE_HEADER", "").strip()
RANCH_AUTH_TOKEN    = os.getenv("RANCH_AUTH_TOKEN", "").strip()


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

async def collect_from_embedded_json(page, kind_hint: str) -> Dict[str, dict]:
    """
    Scrape <script type="application/json"> blocks and window.__NEXT_DATA__/__NUXT__/__APOLLO_STATE__.
    """
    found: Dict[str, dict] = {}

    # 1) window globals
    try:
        blob = await page.evaluate("() => JSON.stringify(window.__NEXT_DATA__ || window.__NUXT__ || window.__APOLLO_STATE__ || null)")
        if blob and blob != "null":
            data = json.loads(blob)
            if DEBUG:
                print("[debug] found window JSON global with top-level keys:", list(data.keys())[:12] if isinstance(data, dict) else type(data).__name__)
            # reuse the walker from above
            def iter_dict_lists(x):
                if isinstance(x, list) and x and isinstance(x[0], dict):
                    yield x
                if isinstance(x, dict):
                    for v in x.values():
                        yield from iter_dict_lists(v)
            def coerce_item(obj):
                slug = str(obj.get("slug") or obj.get("id") or obj.get("uid") or "").strip()
                title = str(obj.get("title") or obj.get("name") or "").strip()
                url = obj.get("url") or ""
                if url and not url.startswith("http"):
                    url = urljoin(ROOT, url)
                if not slug:
                    base = title or url or str(obj.get("id") or "")
                    slug = re.sub(r"[^a-z0-9\-]+", "-", base.lower()).strip("-")[:80]
                return {
                    "slug": slug,
                    "url": url or urljoin(ROOT, ("/businesses/" if "business" in kind_hint else "/properties/") + slug),
                    "title": title or slug.replace("-", " ").title(),
                    "price": None, "current_bid": None, "status": None,
                    "last_seen": utc_now_iso(), "absence": 0,
                }
            for arr in iter_dict_lists(data):
                for obj in arr:
                    try:
                        item = coerce_item(obj)
                        if item["slug"]:
                            found[item["slug"]] = item
                    except Exception:
                        pass
    except Exception:
        pass

    # 2) <script type="application/json"> blocks
    try:
        handles = await page.locator('script[type="application/json"]').all()
        for h in handles:
            try:
                txt = await h.inner_text()
                data = json.loads(txt)
            except Exception:
                continue
            # same mining
            def iter_dict_lists(x):
                if isinstance(x, list) and x and isinstance(x[0], dict):
                    yield x
                if isinstance(x, dict):
                    for v in x.values():
                        yield from iter_dict_lists(v)
            for arr in iter_dict_lists(data):
                for obj in arr:
                    try:
                        slug = str(obj.get("slug") or obj.get("id") or "").strip()
                        title = str(obj.get("title") or obj.get("name") or "").strip()
                        if not slug and not title:
                            continue
                        if not slug:
                            slug = re.sub(r"[^a-z0-9\-]+", "-", title.lower()).strip("-")[:80]
                        found[slug] = {
                            "slug": slug,
                            "url": urljoin(ROOT, ("/businesses/" if "business" in kind_hint else "/properties/") + slug),
                            "title": title or slug.replace("-", " ").title(),
                            "price": None, "current_bid": None, "status": None,
                            "last_seen": utc_now_iso(), "absence": 0,
                        }
                    except Exception:
                        pass
    except Exception:
        pass

    if DEBUG:
        print(f"[debug] embedded-json matched items: {len(found)}  (keys: {list(found)[:10]}...)")
    return found

async def get_page_items(page, base_path: str) -> Dict[str, dict]:
    # Bias for URL matching in network capture
    kind_hint = "business" if "business" in base_path else "propert"

    # First, try network JSON capture (works for most SPAs)
    via_net = await collect_items_via_network(page, kind_hint)
    if via_net:
        return via_net

    via_embed = await collect_from_embedded_json(page, kind_hint)
    if via_embed:
        return via_embed

    # Fallback: anchor scraping (kept as-is, trimmed)
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)
    anchors = await page.locator('a[href]').all()

    found: Dict[str, dict] = {}
    for a in anchors:
        href = await a.get_attribute("href")
        if not href:
            continue
        abs_url = urljoin(ROOT, href)
        if ("/business" in abs_url or "/propert" in abs_url):
            path = abs_url.split("://", 1)[-1].split("/", 1)[-1]
            parts = [p for p in path.split("/") if p and not p.startswith("#")]
            if len(parts) < 2:
                continue
            slug = parts[-1].split("?")[0]
            title = (await a.inner_text()).strip() or slug.replace("-", " ").title()
            t = await a.evaluate("""
                el => {
                  let c = el.closest('article, .card, .item, li, .container, .block, .tile, .listing, .panel, .box')
                       || el.parentElement;
                  return (c && c.innerText) ? c.innerText : el.innerText || '';
                }
            """) or ""
            price = None; bid = None; status = None
            m = PRICE_RE.search(t);   price = int(m.group(1).replace(",","")) if m else None
            m = BID_RE.search(t);     bid   = int(m.group(1).replace(",","")) if m else None
            m = STATUS_RE.search(t);  status = m.group(1).title() if m else None
            found[slug] = {
                "slug": slug, "url": abs_url, "title": title,
                "price": price, "current_bid": bid, "status": status,
                "last_seen": utc_now_iso(), "absence": 0
            }
    if DEBUG:
        print(f"[debug] anchor matched items: {len(found)}  (keys: {list(found)[:10]}...)")
    return found

async def collect_items_via_network(page, kind_hint: str) -> Dict[str, dict]:
    collected = []

    def looks_relevant(url: str, ctype: str) -> bool:
        if "application/json" not in (ctype or "").lower():
            return False
        u = url.lower()
        # very permissive: anything JSON while we're on this page
        return True

    async def on_response(resp):
        try:
            ctype = (resp.headers or {}).get("content-type", "")
            url = resp.url
            if looks_relevant(url, ctype):
                try:
                    data = await resp.json()
                    collected.append((url, ctype, data))
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", on_response)

    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)
    await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    await page.wait_for_timeout(800)
    await page.evaluate("() => window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)

    if DEBUG:
        print(f"[debug] network JSON blobs captured: {len(collected)}")
        for url, ctype, data in collected:
            # print top-level keys only (safe)
            top = list(data.keys())[:12] if isinstance(data, dict) else (f"len={len(data)} list" if isinstance(data, list) else type(data).__name__)
            print(f"[debug]  - {url}  ({ctype})  top={top}")

    def coerce_item(obj):
        slug = str(obj.get("slug") or obj.get("id") or obj.get("uid") or obj.get("handle") or "").strip()
        title = str(obj.get("title") or obj.get("name") or obj.get("label") or "").strip()
        price = obj.get("price") or obj.get("amount") or obj.get("askingPrice") or obj.get("cost")
        bid = obj.get("current_bid") or obj.get("currentBid") or obj.get("highestBid") or obj.get("bid")
        status = obj.get("status") or obj.get("state")
        url = (obj.get("url") or obj.get("permalink") or obj.get("href") or "")
        if url and not url.startswith("http"):
            url = urljoin(ROOT, url)
        if not slug:
            base = title or url or str(obj.get("id") or obj.get("uid") or "")
            slug = re.sub(r"[^a-z0-9\-]+", "-", base.lower()).strip("-")[:80]
        if not url:
            base_path = "/businesses" if "business" in kind_hint else "/properties"
            url = urljoin(ROOT, f"{base_path}/{slug}")
        def to_int(v):
            if v is None: return None
            s = str(v).replace(",", "").strip()
            return int(s) if s.isdigit() else None
        return {
            "slug": slug,
            "url": url,
            "title": title or slug.replace("-", " ").title(),
            "price": to_int(price),
            "current_bid": to_int(bid),
            "status": str(status).title() if status else None,
            "last_seen": utc_now_iso(),
            "absence": 0,
        }

    # Walk JSON: hunt for arrays of dicts anywhere
    def iter_dict_lists(x):
        if isinstance(x, list) and x and isinstance(x[0], dict):
            yield x
        if isinstance(x, dict):
            for v in x.values():
                yield from iter_dict_lists(v)

    found: Dict[str, dict] = {}
    for url, _, data in collected:
        for arr in iter_dict_lists(data):
            for obj in arr:
                try:
                    item = coerce_item(obj)
                    if item["slug"]:
                        found[item["slug"]] = item
                except Exception:
                    continue

    if DEBUG:
        print(f"[debug] network matched items: {len(found)}  (keys: {list(found)[:10]}...)")
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
# ------------- Main -------------
async def main():
    async with async_playwright() as p:
        # launch browser
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )

        # create a human-ish context
        UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        context = await browser.new_context(
            user_agent=UA,
            locale="en-GB",
            timezone_id="Europe/London",
        )

        # stealth tweaks (helps with bot checks)
        tmp = await context.new_page()
        await tmp.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', { get: () => ['en-GB','en'] });
        Object.defineProperty(navigator, 'plugins',   { get: () => [1,2,3,4,5] });
        """)
        await tmp.close()

        # inject your real request headers (from GitHub Secrets)
        extra_headers = {}
        if RANCH_COOKIE_HEADER:
            extra_headers["cookie"] = RANCH_COOKIE_HEADER
        if RANCH_AUTH_TOKEN:
            extra_headers["authorization"] = f"Bearer {RANCH_AUTH_TOKEN}"
        if extra_headers:
            await context.set_extra_http_headers(extra_headers)

        # also set cookie jar entries (helps some SPAs)
        if RANCH_COOKIE_HEADER:
            cookie_list = []
            for part in RANCH_COOKIE_HEADER.split(";"):
                if "=" in part:
                    name, value = part.strip().split("=", 1)
                    cookie_list.append({
                        "name": name,
                        "value": value,
                        "domain": "ranchroleplay.com",
                        "path": "/",
                        "secure": True,
                        "httpOnly": False,
                    })
            if cookie_list:
                await context.add_cookies(cookie_list)

        try:
            if DISCORD_TEST:
                post_discord(content="✅ **Ranch Watch test** — webhook and embeds look good.")

            # scrape using the CONTEXT (it has your headers/cookies)
            new_biz,  bidups_biz,  off_biz,  biz_now  = await scrape(context, BUSINESSES_URL, "businesses", "/businesses")
            new_prop, bidups_prop, off_prop, prop_now = await scrape(context, PROPERTIES_URL,  "properties", "/properties")

            # optional full snapshot
            if POST_ALL:
                post_discord_batched("Businesses — Full Snapshot", embeds_for_dump("businesses", biz_now))
                post_discord_batched("Properties — Full Snapshot", embeds_for_dump("properties", prop_now))

            # new items
            if new_biz:
                post_discord_batched("New Businesses Detected", embeds_for_new("businesses", new_biz))
            if new_prop:
                post_discord_batched("New Properties Detected", embeds_for_new("properties", new_prop))

            # bid increases
            if bidups_biz:
                post_discord_batched("Business Bid Increases", embeds_for_bidups("businesses", bidups_biz))
            if bidups_prop:
                post_discord_batched("Property Bid Increases", embeds_for_bidups("properties", bidups_prop))

            # possibly off market
            if off_biz:
                post_discord_batched("Businesses Possibly Off Market", embeds_for_offmarket("businesses", off_biz))
            if off_prop:
                post_discord_batched("Properties Possibly Off Market", embeds_for_offmarket("properties", off_prop))

            # console summary
            print("New businesses:", [i["slug"] for i in new_biz])
            print("New properties:", [i["slug"] for i in new_prop])

        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())





if __name__ == "__main__":
    asyncio.run(main())
