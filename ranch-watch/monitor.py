import asyncio, json, os, re, sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Tuple
from urllib.parse import urljoin

from playwright.async_api import async_playwright
import urllib.request
import urllib.error

BUSINESSES_URL = "https://ranchroleplay.com/businesses"
PROPERTIES_URL = "https://ranchroleplay.com/properties"
STATE_DIR = Path("state")
STATE_DIR.mkdir(exist_ok=True)
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

def load_seen(name: str) -> set:
    p = STATE_DIR / f"{name}.json"
    if p.exists():
        return set(json.loads(p.read_text()))
    return set()

def save_seen(name: str, items: set):
    (STATE_DIR / f"{name}.json").write_text(json.dumps(sorted(items), indent=2))

async def extract_slugs(page, base_path: str) -> List[Tuple[str, str]]:
    """
    Heuristics:
    - Gather all anchors with href containing the base path (e.g., '/business' or '/properties')
    - Normalize to unique list of (slug, absolute_url)
    - If the app loads via XHR/fetch to an API, this still works because the DOM will contain links/cards.
    """
    await page.wait_for_load_state("networkidle")
    # Give client time to render after data load
    await page.wait_for_timeout(2000)

    anchors = await page.locator('a[href]').all()
    items = []
    for a in anchors:
        href = await a.get_attribute("href")
        txt = (await a.inner_text()).strip() if await a.is_visible() else ""
        if not href:
            continue
        # Match either '/businesses/<slug>' or '/properties/<slug>'
        if re.search(rf"^{base_path}/[^/#?]+$", href):
            slug = href.rstrip("/").split("/")[-1]
            abs_url = href if href.startswith("http") else urljoin("https://ranchroleplay.com", href)
            items.append((slug, abs_url))
        # Some SPAs use '/business/<slug>' singular — catch that too:
        elif base_path == "/businesses" and re.search(r"^/business/[^/#?]+$", href):
            slug = href.rstrip("/").split("/")[-1]
            abs_url = urljoin("https://ranchroleplay.com", href)
            items.append((slug, abs_url))
    # Deduplicate by slug
    unique = {}
    for slug, u in items:
        unique.setdefault(slug, u)
    return [(k, v) for k, v in unique.items()]

async def check_page(browser, url: str, state_name: str, base_path: str) -> List[str]:
    page = await browser.new_page()
    await page.goto(url, wait_until="domcontentloaded")
    slugs = await extract_slugs(page, base_path)
    await page.close()

    current = set([s for s, _ in slugs])
    seen = load_seen(state_name)

    new = sorted(current - seen)
    if new:
        save_seen(state_name, seen.union(current))
        # Build pretty lines with URLs
        lookup = {s: u for s, u in slugs}
        lines = [f"- **{s}** → {lookup.get(s, '')}" for s in new]
        await notify_discord(
            title=f"New {state_name.capitalize()} detected",
            lines=lines
        )
    return new

async def notify_discord(title: str, lines: List[str]):
    if not DISCORD_WEBHOOK:
        print("[warn] No DISCORD_WEBHOOK set; printing instead:")
        print(title)
        print("\n".join(lines))
        return
    content = f"**{title}**\n" + "\n".join(lines) + f"\n\n_UTC: {datetime.now(timezone.utc).isoformat(timespec='seconds')}_"
    data = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(DISCORD_WEBHOOK, data=data, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            print("[ok] Discord notified:", resp.status)
    except urllib.error.HTTPError as e:
        print("[err] Discord HTTPError:", e.read().decode("utf-8"), file=sys.stderr)
    except Exception as e:
        print("[err] Discord error:", e, file=sys.stderr)

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            nb = await check_page(browser, BUSINESSES_URL, "businesses", "/businesses")
            np = await check_page(browser, PROPERTIES_URL, "properties", "/properties")
            print(f"New businesses: {nb}")
            print(f"New properties: {np}")
        finally:
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
