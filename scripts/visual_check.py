"""Visual validation of the JARVIS web shell with Playwright.

Captures screenshots at multiple viewports, navigates all 8 views,
sends one test message, collects console errors, and checks for
overlapping elements. Run: .venv/bin/python scripts/visual_check.py
"""

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

CHROME = os.path.expanduser("~/.cache/ms-playwright/chromium-1243/chrome-linux/chrome")

BASE = "http://127.0.0.1:8123"
OUT = Path("/tmp/jarvis-visual")  # noqa: S108 - scratch output for screenshots
OUT.mkdir(parents=True, exist_ok=True)

VIEWS = ["home", "sessions", "tasks", "memory", "activity", "audit", "nexus", "system"]
VIEWPORTS = [(1920, 1080), (1600, 900), (1440, 900), (1280, 720), (1024, 768)]


async def main() -> int:
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        console: list[str] = []
        page.on("console", lambda m: console.append(f"{m.type}: {m.text[:200]}"))
        page.on("pageerror", lambda e: console.append(f"pageerror: {e!s}"[:200]))
        bad4xx: list[str] = []

        def _track_4xx(r):  # noqa: ANN001, ANN202
            if r.status >= 400:
                bad4xx.append(f"{r.status} {r.url}")

        page.on("response", _track_4xx)

        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_timeout(2500)  # boot overlay dismissal

        # 1. Screenshot each view at 1920x1080
        for view in VIEWS:
            btn = page.locator(f'[data-view="{view}"], button[data-nav="{view}"]')
            if await btn.count() == 0:
                errors.append(f"nav button missing for view '{view}'")
                continue
            await btn.first.click()
            await page.wait_for_timeout(600)
            visible = await page.evaluate(
                """() => {
                    const views = [...document.querySelectorAll('.view')];
                    return views.filter(v => {
                        const cs = getComputedStyle(v);
                        return cs.display !== 'none' && v.offsetParent !== null;
                    }).map(v => v.id || v.dataset.view || v.className);
                }"""
            )
            if len(visible) != 1:
                errors.append(f"view '{view}': {len(visible)} views visible: {visible}")
            await page.screenshot(path=str(OUT / f"view-{view}-1920.png"))

        # 2. Composer test on Home
        await page.locator('[data-view="home"], button[data-nav="home"]').first.click()
        await page.wait_for_timeout(400)
        comp = page.locator("textarea#input")
        if await comp.count() > 0:
            await comp.first.fill("Como está o NEXUS?")
            await comp.first.press("Enter")
            await page.wait_for_timeout(6000)
            await page.screenshot(path=str(OUT / "composer-reply-1920.png"))
        else:
            errors.append("composer input not found")

        # 3. Other viewports: Home screenshot + overlap scan
        for w, h in VIEWPORTS[1:]:
            await page.set_viewport_size({"width": w, "height": h})
            await page.wait_for_timeout(800)
            await page.screenshot(path=str(OUT / f"home-{w}x{h}.png"))
            overlap = await page.evaluate(
                """() => {
                    const els = [...document.querySelectorAll('body *')].filter(e => {
                        const r = e.getBoundingClientRect();
                        return r.width > 4 && r.height > 4 && r.bottom > 0 && r.right > 0
                            && getComputedStyle(e).visibility !== 'hidden'
                            && e.offsetParent !== null;
                    });
                    const bad = [];
                    for (let i = 0; i < Math.min(els.length, 400); i++) {
                        const a = els[i].getBoundingClientRect();
                        for (let j = i + 1; j < Math.min(els.length, 400); j++) {
                            const b = els[j].getBoundingClientRect();
                            const ox = Math.min(a.right, b.right) - Math.max(a.left, b.left);
                            const oy = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
                            if (ox > 40 && oy > 40) {
                                // ignore ancestor/descendant containment
                                if (!els[i].contains(els[j]) && !els[j].contains(els[i])) {
                                    const nm = (e) => (e.tagName + '.' + e.className).slice(0, 50);
                                    bad.push(nm(els[i]) + ' X ' + nm(els[j]));
                                    if (bad.length > 8) return bad;
                                }
                            }
                        }
                    }
                    return bad;
                }"""
            )
            if overlap:
                errors.append(f"overlap@{w}x{h}: {overlap[:5]}")
            hscroll = await page.evaluate(
                "() => document.documentElement.scrollWidth > document.documentElement.clientWidth"
            )
            if hscroll:
                errors.append(f"horizontal scrollbar present at {w}x{h}")

        print("CONSOLE:", *console, sep="\n  ")
        print("HTTP4XX:", *(bad4xx or ["none"]), sep="\n  ")
        print("ERRORS:", *errors, sep="\n  ")
        await browser.close()
    return 1 if errors or any("error" in c.lower() for c in console) else 0


sys.exit(asyncio.run(main()))
