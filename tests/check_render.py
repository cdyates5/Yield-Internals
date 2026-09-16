#!/usr/bin/env python3
"""
Headless render check for the built dashboard. Fails (exit 1) on any console error, page error,
missing charts, or a mismatch between the embedded data and latest.json.

  python tests/check_render.py site/index.html
"""
import sys, json, asyncio, pathlib
from playwright.async_api import async_playwright

MIN_CHARTS = 35

async def check(path, viewport, scheme):
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(viewport=viewport, color_scheme=scheme)
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
        page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url}"))
        await page.goto(path.as_uri(), wait_until="load")
        await page.wait_for_function("window.Chart !== undefined", timeout=20000)
        height = await page.evaluate("document.body.scrollHeight")
        for y in range(0, height, 700):                      # charts render lazily on scroll
            await page.evaluate(f"window.scrollTo(0, {y})")
            await page.wait_for_timeout(80)
        await page.wait_for_timeout(400)
        n = await page.evaluate("Object.keys(Chart.instances).length")
        asof = await page.evaluate("document.getElementById('asof').textContent")
        overflow = await page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
        await browser.close()
    return errors, n, asof, overflow

def main():
    html = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "site/index.html").resolve()
    latest = json.loads((html.parent / "latest.json").read_text())
    failed = False
    for name, vp, scheme in [("desktop", {"width": 1360, "height": 900}, "light"),
                             ("mobile", {"width": 390, "height": 844}, "light"),
                             ("dark", {"width": 1360, "height": 900}, "dark")]:
        errors, n, asof, overflow = asyncio.run(check(html, vp, scheme))
        ok = not errors and n >= MIN_CHARTS and not overflow
        print(f"{name:8s} charts={n} errors={len(errors)} overflow={overflow} header='{asof}'")
        for e in errors[:10]: print("   ", e)
        failed |= not ok
    day, mon, yr = None, None, None
    y, m, d = latest["asof"].split("-")
    months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    if f"{int(d)} {months[int(m)-1]} {y}" not in asof:
        print(f"as-of mismatch: latest.json {latest['asof']} vs header '{asof}'"); failed = True
    print("render check", "FAILED" if failed else "passed")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
