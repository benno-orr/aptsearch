#!/usr/bin/env python3
"""
One-time auth setup for all apartment-search sources.

    python3 auth.py            # walk through everything
    python3 auth.py fb         # just Facebook
    python3 auth.py zillow     # just Zillow

Run this in a real terminal (it opens browser windows and prompts you).
Safe to re-run anytime — already-authed sources are detected and skipped.

What each source needs:
  Facebook       one-time login (session persists in .fb_profile)
  Zillow         one-time bot-check clearance (persists in .zillow_profile)
  Craigslist     nothing — verified reachable
  Apartments.com nothing — verified not currently rate-limited
"""

import asyncio
import sys

import track

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"

def ok(m):   print(f"  {GREEN}[ok]{RESET} {m}")
def bad(m):  print(f"  {RED}[!!]{RESET} {m}")
def warn(m): print(f"  {YELLOW}[--]{RESET} {m}")


async def auth_facebook(p):
    print("\n-- Facebook -------------------------------------------------")
    ctx = await p.chromium.launch_persistent_context(
        track.FB_PROFILE_DIR, headless=False,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        for _ in range(3):
            logged_out = ("login" in page.url.lower()) or bool(await page.query_selector('input[name="email"]'))
            if not logged_out:
                ok("Facebook: logged in — session saved in .fb_profile")
                return True
            print("  -> Log into Facebook in the browser window (incl. any 2FA),")
            input("     then press Enter here to verify... ")
            await page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)
        bad("Facebook: still not logged in after 3 tries — re-run `python3 auth.py fb` later")
        return False
    except Exception as e:
        bad(f"Facebook: {e}")
        return False
    finally:
        await ctx.close()


async def auth_zillow(p):
    print("\n-- Zillow ---------------------------------------------------")
    ctx = await p.chromium.launch_persistent_context(
        track.ZILLOW_PROFILE_DIR, headless=False,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    try:
        url = track.ZILLOW_URLS[0][1]
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        for _ in range(3):
            title = (await page.title()).lower()
            if "denied" not in title and "captcha" not in title:
                ok("Zillow: clear — clearance saved in .zillow_profile")
                return True
            print("  -> Zillow bot check. In the browser window, press & hold the button")
            input("     until it clears, then press Enter here to verify... ")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
        bad("Zillow: still blocked — wait 30-60 min and re-run `python3 auth.py zillow`")
        return False
    except Exception as e:
        bad(f"Zillow: {e}")
        return False
    finally:
        await ctx.close()


async def check_craigslist(p):
    print("\n-- Craigslist (no auth needed; verifying access) ------------")
    b = await p.chromium.launch(headless=True)
    try:
        ctx = await b.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        page = await ctx.new_page()
        await page.goto(track.SEARCH_URL_ALL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(2000)
        n = len(await page.query_selector_all(".cl-search-result"))
        if n:
            ok(f"Craigslist: reachable ({n} results on page)")
            return True
        bad("Craigslist: page loaded but no results parsed — may be blocked or markup changed")
        return False
    except Exception as e:
        bad(f"Craigslist: {e}")
        return False
    finally:
        await b.close()


async def check_apartments(p):
    print("\n-- Apartments.com (no auth needed; verifying access) --------")
    b = await p.chromium.launch(headless=False)
    try:
        ctx = await b.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()
        await page.goto(track.APTS_URLS[0][1], wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(4000)
        title = (await page.title()).lower()
        if "access denied" in title:
            warn("Apartments.com: currently Akamai rate-limited. Nothing to auth — "
                 "it recovers on its own; just try again in an hour.")
            return False
        n = len(await page.query_selector_all("article.placard"))
        ok(f"Apartments.com: reachable ({n} cards on page)")
        return True
    except Exception as e:
        bad(f"Apartments.com: {e}")
        return False
    finally:
        await b.close()


async def main():
    if not sys.stdin.isatty():
        print("Run this in a real terminal — it needs you to interact with browser windows.")
        sys.exit(1)

    only = sys.argv[1].lower() if len(sys.argv) > 1 else None
    from playwright.async_api import async_playwright

    results = {}
    async with async_playwright() as p:
        if only in (None, "fb", "facebook"):
            results["Facebook"] = await auth_facebook(p)
        if only in (None, "zillow"):
            results["Zillow"] = await auth_zillow(p)
        if only is None:
            results["Craigslist"] = await check_craigslist(p)
            results["Apartments.com"] = await check_apartments(p)

    print("\n=============================================================")
    print("  SUMMARY")
    for name, good in results.items():
        print(f"    {'[ok]' if good else '[!!]'} {name}")
    if all(results.values()):
        print("\n  All set! Run `python3 server.py` and use the refresh buttons,")
        print("  or `python3 track.py daily` for a full scrape.")
    else:
        print("\n  Some sources need another pass — see messages above.")
    print("=============================================================")


if __name__ == "__main__":
    asyncio.run(main())
