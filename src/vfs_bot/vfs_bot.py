import logging
import os
from abc import ABC
from datetime import datetime

from playwright.sync_api import sync_playwright
from playwright_stealth import stealth_sync

from src.utils.config_reader import get_config_value
from src.utils.route_schema import get_route_schema

SCREENSHOT_DIR = "screenshots"

# When False, the per-step `_take_screenshot` calls are no-ops; only the single
# final screenshot (taken at the end of run()) is written. Flip to True for
# step-by-step debugging.
SCREENSHOTS_ENABLED = False

USERNAME_SELECTOR = (
    "input[formcontrolname='username'], #mat-input-0, input[placeholder*='email']"
)
PASSWORD_SELECTOR = (
    "input[formcontrolname='password'], #mat-input-1, input[type='password']"
)


class LoginError(Exception):
    """Exception raised when login fails."""


class VfsBot(ABC):
    """
    Slot-check bot for the VFS Malta portal.

    This is the trimmed, production slice of the original schema-driven VFS bot:
    it only reaches Step 1 (Appointment Details), reads the 'Earliest available
    slot' banner for each configured combination, and reports the results via
    Telegram. The booking/OTP/payment machinery is intentionally absent — this
    project never books anything.
    """

    def __init__(self):
        self.source_country_code = None
        self.destination_country_code = None
        self.schema = {}

    def run(self) -> bool:
        """
        Connects to / launches a browser, navigates to the VFS login URL, logs
        in, starts a new booking and runs the slot-check flow.

        Returns:
            bool: Always False (no appointment is booked, by design).
        """
        logging.info(
            f"Starting VFS Slot Checker for "
            f"{self.source_country_code.upper()}-{self.destination_country_code.upper()}"
        )

        # Load the per-route flow schema (the combinations to check).
        self.schema = get_route_schema(
            self.source_country_code, self.destination_country_code
        )

        browser_type = get_config_value("browser", "type", "chromium")
        headless_mode = get_config_value("browser", "headless", "True")
        url_key = self.source_country_code + "-" + self.destination_country_code
        vfs_url = get_config_value("vfs-url", url_key)
        if not vfs_url:
            logging.error(
                f"No VFS URL configured for '{url_key}'. Add it to config/vfs_urls.ini"
            )
            return False

        email_id = get_config_value("vfs-credential", "email")
        password = get_config_value("vfs-credential", "password")

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)

        with sync_playwright() as p:
            cdp_url = get_config_value("browser", "cdp_url")
            if cdp_url:
                # Attach to an existing Chrome launched with --remote-debugging-port
                # (useful locally to watch / get past Cloudflare on a real profile).
                logging.info(f"Connecting to Chrome via CDP: {cdp_url}")
                browser = p.chromium.connect_over_cdp(cdp_url)
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
                page = context.new_page()
            else:
                # Launch our own browser (the prod path — headless on a server).
                is_headless = headless_mode in ("True", "true")
                launch_args = {}
                if browser_type == "chromium":
                    launch_args["args"] = [
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                    ]
                browser = getattr(p, browser_type).launch(
                    headless=is_headless, **launch_args
                )
                context = browser.new_context(
                    viewport={"width": 1280, "height": 720},
                )
                page = context.new_page()
                stealth_sync(page)

            logging.info(f"Navigating to {vfs_url}")
            page.goto(vfs_url, timeout=60000, wait_until="domcontentloaded")

            self.pre_login_steps(page)

            try:
                self.login(page, email_id, password)
                logging.info("Slot check complete.")
            except Exception as e:
                logging.error(f"Slot-check error details: {e}")

            # Single final screenshot capturing wherever the flow ended up.
            self._take_final_screenshot(page, "final")

            # When attached via CDP we leave the page open; an own-launched browser
            # is closed by the sync_playwright context manager on exit.
            logging.info("Run finished.")
            return False

    # ------------------------------------------------------------------ #
    # Flow steps                                                          #
    # ------------------------------------------------------------------ #

    def pre_login_steps(self, page) -> None:
        """Dismiss the cookie consent banner if VFS presents one."""
        policies_reject_button = page.get_by_role("button", name="Reject All")
        try:
            policies_reject_button.click(timeout=5000)
            logging.debug("Rejected all cookie policies")
        except Exception:
            logging.debug("No cookie policy button found, skipping")

    def login(self, page, email_id: str, password: str) -> None:
        """
        Fills the login form, signs in, and — once on the dashboard — clicks
        Start New Booking and runs the slot check.
        """
        # Wait for login form to be ready (VFS can take a while behind Cloudflare).
        page.wait_for_selector(USERNAME_SELECTOR, timeout=120000)
        logging.info("Login form loaded")

        email_input = page.locator(USERNAME_SELECTOR).first
        password_input = page.locator(PASSWORD_SELECTOR).first

        email_input.click()
        page.wait_for_timeout(800)
        email_input.press_sequentially(email_id, delay=200)
        page.wait_for_timeout(1200)

        password_input.click()
        page.wait_for_timeout(800)
        password_input.press_sequentially(password, delay=200)
        page.wait_for_timeout(1500)

        page.get_by_role("button", name="Sign In").click()
        logging.info("Clicked Sign In")
        # A Cloudflare captcha dialog often appears right after Sign In and blocks
        # the redirect to the dashboard, so watch for it during this wait.
        VfsBot._wait_with_captcha_check(page, 6000)

        try:
            page.wait_for_url("**/dashboard", timeout=60000)
            logging.info(f"Reached dashboard: {page.url}")
            page.wait_for_timeout(2000)
            self._start_new_booking(page)
            self._check_slots(page)
        except Exception as e:
            logging.warning(f"Did not reach /dashboard: {e}")

    @staticmethod
    def _start_new_booking(page) -> None:
        """Clicks the 'Start New Booking' button on the VFS dashboard."""
        try:
            page.wait_for_timeout(2000)
            # VFS renders two copies of this button (responsive: one for mobile,
            # one for desktop) — one is CSS-hidden at any given viewport. Target
            # the <button> (not its inner <span>) and keep only the visible copy,
            # otherwise the click lands on the hidden element and times out.
            booking_button = (
                page.locator("button:has-text('Start New Booking')")
                .filter(visible=True)
                .first
            )
            booking_button.scroll_into_view_if_needed(timeout=10000)
            booking_button.click(timeout=15000)
            logging.info("Clicked Start New Booking")
            page.wait_for_timeout(3000)
            VfsBot._take_screenshot(page, "06_start_new_booking")
            logging.info(f"Start New Booking opened. URL: {page.url}")
        except Exception as e:
            logging.warning(f"Start New Booking failed: {e}")
            VfsBot._take_screenshot(page, "ERROR_start_new_booking")

    # ------------------------------------------------------------------ #
    # Slot check                                                         #
    # ------------------------------------------------------------------ #

    def _check_slots(self, page) -> None:
        """
        Slot-check flow (no booking): on the Appointment Details step, run through
        each configured combination (centre / category / sub-category), read the
        earliest-slot banner for each, then send all results in one Telegram
        message. Combinations come from the route schema's `slot_check.combinations`.
        """
        combos = self.schema.get("slot_check", {}).get("combinations", [])
        if not combos:
            logging.warning("Slot-check mode but no combinations configured.")
            return

        try:
            page.wait_for_url("**/application-detail", timeout=30000)
        except Exception:
            logging.warning("Did not reach the Appointment Details page; cannot check slots.")
            return

        VfsBot._wait_for_loader(page)
        page.wait_for_timeout(1000)

        results = []
        prev = {}  # the centre/category/sub-category selected for the previous combo
        for combo in combos:
            label = combo.get("label") or " / ".join(
                filter(None, [combo.get("centre"), combo.get("category"), combo.get("sub_category")])
            )
            logging.info(f"Checking slot for: {label}")

            # The dropdowns cascade (centre -> category -> sub-category), so they
            # are selected in order. Skip any level whose value is unchanged from
            # the previous combination — but once a parent level changes it resets
            # its children, so every level below a change must be re-selected too.
            selections = [
                ("centerCode", "centre", combo.get("centre")),
                ("selectedSubvisaCategory", "category", combo.get("category")),
                ("visaCategoryCode", "sub_category", combo.get("sub_category")),
            ]
            ok = True
            cascade_changed = False
            for control, key, value in selections:
                if not value:
                    continue
                # Re-select if this level's value differs OR a parent changed
                # (which reset this dependent dropdown).
                if not cascade_changed and prev.get(key) == value:
                    logging.info(f"  (unchanged) {key} = '{value}' — skipping re-select")
                    continue
                cascade_changed = True
                if not VfsBot._select_mat_dropdown(page, control, value):
                    ok = False
                    break

            prev = {
                "centre": combo.get("centre"),
                "category": combo.get("category"),
                "sub_category": combo.get("sub_category"),
            }

            if not ok:
                message = "Could not select this combination (option not found)."
            else:
                message = VfsBot._read_slot_message(page) or "No slot message shown (no availability?)."

            logging.info(f"  -> {message}")
            results.append((label, message))
            VfsBot._take_screenshot(page, f"slot_{len(results)}")
            page.wait_for_timeout(1000)

        VfsBot._send_slot_report(results)

    @staticmethod
    def _read_slot_message(page, timeout: int = 12000) -> str:
        """
        Returns the 'Earliest available slot ...' banner text on the Appointment
        Details step, or "" if none is shown (e.g. no availability for the chosen
        combination).
        """
        try:
            VfsBot._wait_for_loader(page)
            slot = page.get_by_text("Earliest available slot", exact=False).first
            slot.wait_for(timeout=timeout)
            return slot.inner_text().strip()
        except Exception:
            return ""

    @staticmethod
    def _send_slot_report(results: list) -> None:
        """Formats the collected (label, message) slot results and sends them to Telegram."""
        from src.utils import telegram

        lines = ["VFS Malta — Earliest appointment slots", ""]
        for label, message in results:
            lines.append(f"• {label}")
            lines.append(f"  {message}")
            lines.append("")
        report = "\n".join(lines).strip()

        logging.info("Slot report:\n" + report)
        if telegram.is_configured():
            telegram.send_message(report)
        else:
            logging.warning(
                "Telegram not configured — slot report logged only. "
                "Set [telegram] bot_token and chat_id in config.ini to receive it."
            )

    # ------------------------------------------------------------------ #
    # Shared UI helpers (loader / captcha / wait dialogs / dropdowns)    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _wait_for_loader(page, timeout: int = 30000) -> None:
        """
        Waits for VFS's ngx-ui-loader overlay to clear. This full-screen spinner
        intercepts pointer events, so clicking while it is up times out. Returns
        immediately if no loader is present.

        Also clears any Cloudflare 'Verify Captcha' dialog and the VFS 'please
        wait before continuing' reminder first — either can pop up at any step
        and blocks the form until dismissed.
        """
        VfsBot._dismiss_captcha(page)
        VfsBot._dismiss_wait_dialog(page)
        try:
            page.locator("ngx-ui-loader .ngx-overlay.loading-foreground").wait_for(
                state="hidden", timeout=timeout
            )
        except Exception:
            pass  # loader absent or already cleared

    @staticmethod
    def _dismiss_wait_dialog(page) -> None:
        """
        Dismisses VFS's intermittent reminder dialogs that block a step — e.g.
        'Please wait for some time before saving and continuing'. These are
        mat-dialogs whose only action is a 'Continue' (or 'OK') button; clicking
        it lets the flow proceed. Silent no-op when none is present.
        """
        try:
            dialog = page.locator("mat-dialog-container, .mat-mdc-dialog-container")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
            text = (dialog.first.inner_text() or "").lower()
        except Exception:
            return

        # Only handle the informational 'wait/reminder' dialogs here — leave the
        # Cloudflare captcha dialog to its dedicated handler.
        if "captcha" in text:
            return
        if not any(k in text for k in ("wait", "reminder", "received", "please")):
            return

        for label in ("Continue", "OK", "Ok", "Close"):
            try:
                btn = dialog.first.get_by_role("button", name=label).first
                if btn.count() > 0 and btn.is_visible():
                    btn.click(timeout=5000)
                    logging.info(f"Dismissed VFS reminder dialog via '{label}'.")
                    page.wait_for_timeout(1500)
                    return
            except Exception:
                continue

    @staticmethod
    def _dismiss_captcha(page) -> None:
        """
        Dismisses the Cloudflare 'Verify Captcha' dialog (`app-cloudflare-dialog`)
        if it is showing. The Turnstile widget auto-solves, so we just need to
        click its 'Submit' button. Returns immediately (and silently) when no
        dialog is present.
        """
        try:
            dialog = page.locator("app-cloudflare-dialog")
            if dialog.count() == 0 or not dialog.first.is_visible():
                return
        except Exception:
            return

        VfsBot._do_dismiss_captcha(page)

    @staticmethod
    def _wait_with_captcha_check(page, total_ms: int, step_ms: int = 3000) -> None:
        """
        Sleeps for `total_ms`, checking for (and dismissing) the Cloudflare captcha
        dialog every `step_ms`. Use for long idle waits where the dialog could pop
        up while the bot is otherwise doing nothing.
        """
        elapsed = 0
        while elapsed < total_ms:
            page.wait_for_timeout(min(step_ms, total_ms - elapsed))
            elapsed += step_ms
            VfsBot._dismiss_captcha(page)

    @staticmethod
    def _do_dismiss_captcha(page) -> None:
        """
        Clears a confirmed-visible Cloudflare captcha dialog.

        The Turnstile widget shows 'Verifying...' for a few seconds and only
        populates its hidden `cf-turnstile-response` token once solved — clicking
        Submit before then does nothing. So we wait for that token to appear,
        then click Submit, and retry the whole cycle a few times if the dialog
        is still up (it can require more than one round).
        """
        dialog = page.locator("app-cloudflare-dialog")
        logging.info("Cloudflare 'Verify Captcha' dialog detected — handling it.")

        for attempt in range(1, 4):  # up to 3 Submit cycles
            VfsBot._wait_for_turnstile_token(page)
            try:
                submit = dialog.get_by_role("button", name="Submit").first
                submit.click(timeout=10000)
                logging.info(f"Clicked captcha 'Submit' (attempt {attempt})")
            except Exception as e:
                logging.warning(f"Could not click captcha 'Submit': {e}")
                VfsBot._take_screenshot(page, "ERROR_captcha")
                return

            # Did the dialog go away?
            try:
                dialog.first.wait_for(state="hidden", timeout=12000)
                logging.info("Captcha dialog cleared.")
                VfsBot._take_screenshot(page, "captcha_handled")
                return
            except Exception:
                # Still visible (e.g. token wasn't ready yet) — loop and retry.
                if not VfsBot._captcha_visible(page):
                    return  # raced away on its own
                logging.info(
                    f"Captcha still visible after Submit (attempt {attempt}); retrying..."
                )

        logging.warning(
            "Captcha dialog still visible after retries — it may need a manual solve."
        )
        VfsBot._take_screenshot(page, "ERROR_captcha_persist")

    @staticmethod
    def _captcha_visible(page) -> bool:
        """True if the Cloudflare captcha dialog is currently showing."""
        try:
            dialog = page.locator("app-cloudflare-dialog")
            return dialog.count() > 0 and dialog.first.is_visible()
        except Exception:
            return False

    @staticmethod
    def _wait_for_turnstile_token(page, timeout_ms: int = 15000) -> None:
        """
        Waits until the Turnstile hidden input (`cf-turnstile-response`) holds a
        non-empty token, meaning the challenge auto-solved. Falls back to a short
        fixed wait if the input can't be read (it lives in a closed shadow root on
        some pages, so its value isn't always queryable).
        """
        try:
            page.wait_for_function(
                """() => {
                    const el = document.querySelector('input[name="cf-turnstile-response"]');
                    return el && el.value && el.value.length > 0;
                }""",
                timeout=timeout_ms,
            )
            logging.debug("Turnstile token populated.")
        except Exception:
            # Token not observable (closed shadow DOM) — give it a moment anyway.
            page.wait_for_timeout(3000)

    @staticmethod
    def _select_mat_dropdown(page, control_name: str, value: str) -> bool:
        """
        Selects an option in an Angular Material dropdown (`mat-select`).

        Opens the dropdown identified by its `formcontrolname`, then clicks the
        option whose visible text contains `value` (case-insensitive substring).

        Returns:
            bool: True if the option was selected, False otherwise.
        """
        try:
            VfsBot._wait_for_loader(page)  # centre/category lists load behind a spinner
            trigger = page.locator(f"mat-select[formcontrolname='{control_name}']").first
            trigger.scroll_into_view_if_needed(timeout=10000)
            trigger.click(timeout=10000)
            page.wait_for_timeout(1000)  # wait for the options overlay to render
            page.get_by_role("option", name=value, exact=False).first.click(
                timeout=10000
            )
            logging.info(f"Selected '{value}' (dropdown: '{control_name}')")
            page.wait_for_timeout(1000)
            VfsBot._wait_for_loader(page)  # let the dependent dropdown reload
            return True
        except Exception as e:
            logging.warning(f"Could not select '{value}' for '{control_name}': {e}")
            VfsBot._take_screenshot(page, "ERROR_dropdown")
            return False

    # ------------------------------------------------------------------ #
    # Screenshots                                                        #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _take_screenshot(page, name: str):
        """Per-step screenshot — a no-op unless SCREENSHOTS_ENABLED is True."""
        if not SCREENSHOTS_ENABLED:
            return
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _take_final_screenshot(page, name: str = "final"):
        """Always writes one screenshot (used at the end of the run)."""
        VfsBot._write_screenshot(page, name)

    @staticmethod
    def _write_screenshot(page, name: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(SCREENSHOT_DIR, f"{timestamp}_{name}.png")
        try:
            page.screenshot(path=path, full_page=True)
            logging.info(f"Screenshot saved: {path}")
        except Exception as e:
            logging.warning(f"Failed to take screenshot '{name}': {e}")
