"""Quick EC2 diagnostic: launch Chrome, open VFS, report what the page shows.

Tells us FAST (no 120s wait, no retry loop) whether Cloudflare is letting the
login form render on this server's IP, or blocking it (spinner / 403).

Run under the virtual display:
    xvfb-run -a .venv/bin/python diag_ec2.py
"""
import time
from playwright.sync_api import sync_playwright
from src.utils.chrome_launcher import ChromeProcess
from src.utils.config_reader import initialize_config, get_config_value

initialize_config()
URL = get_config_value("vfs-url", "AE-MT")

with ChromeProcess(port=9222, url=URL) as chrome:
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(chrome.cdp_url)
        ctx = browser.contexts[0]
        page = next((pg for pg in ctx.pages if "vfsglobal" in (pg.url or "")), None)
        if not page:
            page = ctx.new_page(); page.goto(URL)

        # Let it settle, then probe a few times over ~30s.
        for i in range(6):
            time.sleep(5)
            url = page.url
            title = page.title()
            # Is the login form present?
            has_form = page.locator(
                "input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']"
            ).count()
            # Is a loading spinner showing?
            has_spinner = page.locator(
                "ngx-ui-loader .ngx-overlay.loading-foreground"
            ).count()
            # Any raw error JSON (e.g. 403201) in the body?
            body_start = (page.evaluate("() => document.body ? document.body.innerText.slice(0,120) : ''") or "").replace("\n", " ")
            print(f"[{i*5+5:>2}s] url={url[-40:]} | title={title!r} | "
                  f"form={has_form} spinner={has_spinner} | body[:120]={body_start!r}")

        page.screenshot(path="screenshots/diag_ec2.png", full_page=True)
        print("Saved screenshots/diag_ec2.png")
