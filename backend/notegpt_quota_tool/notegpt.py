from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .config import AppConfig
from .cancellation import BrowserStopGuard, interruptible_sleep
from .proxy_manager import ProxyManager


LOGIN_TRIGGER_SELECTOR = (
    "#__nuxt > div.min-h-screen.w-full.bg-white.dark\\:bg-muted-900."
    "\\[--tairo-sidebar-subsidebar-width\\:0rem\\] > div:nth-child(1) > "
    "div.fixed.z-50.top-0.start-0.h-full.w-auto.shrink-0.flex.transition-transform."
    "duration-300.ease-in-out.-translate-x-full.xl\\:-translate-x-0 > div > "
    "div.flex.flex-col.shrink-0.mt-auto.flex.flex-col.gap-1.pb-1 > "
    "div.flex.h-12.w-full.items-center.justify-center.duration-300.cursor-pointer."
    "rounded-lg.outline-none > span"
)

# New "Sign In" trigger button after NoteGPT's 2026 UI refresh. The sidebar
# wrapper switched ``0rem`` -> ``0px`` (and added an icon-width var), and the
# clickable element changed from a <span> to a <button> under ``my-3 > div``.
# Kept as a separate constant so the legacy LOGIN_TRIGGER_SELECTOR above still
# works on older builds of the site.
LOGIN_TRIGGER_SELECTOR_V2 = (
    "#__nuxt > div.min-h-screen.w-full.bg-white.dark\\:bg-muted-900."
    "\\[--tairo-sidebar-subsidebar-width\\:0px\\].\\[--tairo-sidebar-icon-width\\:230px\\] > "
    "div:nth-child(1) > "
    "div.fixed.z-50.top-0.start-0.h-full.w-auto.shrink-0.flex.transition-transform."
    "duration-300.ease-in-out.-translate-x-full.xl\\:-translate-x-0 > div > "
    "div.shrink-0.my-3 > div > button"
)

EMAIL_LOGIN_TOGGLE_SELECTOR = (
    "body > div.bg-default.divide-y.divide-default.flex.flex-col.focus\\:outline-none."
    "data-\\[state\\=open\\]\\:animate-\\[scale-in_200ms_ease-out\\]."
    "data-\\[state\\=closed\\]\\:animate-\\[scale-out_200ms_ease-in\\]."
    "max-w-lg.shadow-lg.ring.ring-default.fixed.top-1\\/2.left-1\\/2."
    "-translate-x-1\\/2.-translate-y-1\\/2.max-h-\\[calc\\(100dvh-2rem\\)\\]."
    "sm\\:max-h-\\[calc\\(100dvh-4rem\\)\\].overflow-hidden.w-\\[500px\\]."
    "max-md\\:w-\\[90vw\\].rounded-3xl.z-50 > div > div > button"
)

LOGIN_BUTTON_SELECTOR = "#v-2 > button"
NOTE_GPT_HOME = "https://notegpt.io/"
USER_INFO_URL = "https://notegpt.io/api/v2/user/info"
QUOTA_ENDPOINTS = [
    "https://notegpt.io/api/v2/user/quota-usage",
    "https://notegpt.io/api/v2/plan-quota",
]
LOGIN_DISMISS_TEXTS = (
    "close",
    "dismiss",
    "skip",
    "not now",
    "no thanks",
    "later",
    "\u0111\u00f3ng",
    "b\u1ecf qua",
    "sau",
    "got it",
)
LOGIN_PROMO_TEXTS = (
    "thanks for choosing",
    "exclusive gift",
    "upgrade now",
    "lowest price",
    "mid-year sale",
    "save 50%",
    "sale",
    "promotion",
    "special offer",
    "offer",
    "gift",
)
LOGIN_GOOGLE_IFRAME_HINTS = (
    "accounts.google.com",
    "gsi",
    "google",
)


def goto_with_retry(
    page,
    url: str,
    *,
    timeout_ms: int,
    wait_until: str = "domcontentloaded",
    attempts: int = 3,
    backoff_sec: float = 2.0,
    log: Optional[Any] = None,
    label: str = "",
    should_stop: Optional[Any] = None,
) -> None:
    """Navigate ``page`` to ``url``, retrying ONLY this navigation on transient
    network/proxy errors (e.g. ``net::ERR_TIMED_OUT``).

    Scoped deliberately to a single ``page.goto`` so parallel worker models and
    the surrounding job/quota/chunk logic stay exactly as before. The browser
    window is kept open between attempts instead of being torn down, so a flaky
    first hit no longer makes Chrome vanish mid-run. After all attempts fail the
    last exception is re-raised, so callers keep their existing error handling
    (mark failed, close browser, etc.).

    ``log`` (optional) receives a short retry message. ``should_stop`` (optional)
    lets a long run bail out of the retry loop immediately when the user stops.
    """
    attempts = max(1, int(attempts))
    prefix = f"{label} " if label else ""
    log_fn = log if callable(log) else (lambda *_: None)
    stop_fn = should_stop if callable(should_stop) else (lambda: False)
    last_exc: Optional[Exception] = None
    for attempt_no in range(1, attempts + 1):
        if stop_fn():
            return
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if attempt_no > 1:
                log_fn(f"[RETRY] {prefix}page loaded on retry {attempt_no}/{attempts}")
            return
        except Exception as exc:
            last_exc = exc
            if attempt_no >= attempts or stop_fn():
                break
            log_fn(
                f"[RETRY] {prefix}open page failed (try {attempt_no}/{attempts}): "
                f"{str(exc)[:160]} — retrying in {backoff_sec:.0f}s"
            )
            interruptible_sleep(max(0.5, backoff_sec), stop_fn)
    if last_exc is not None:
        raise last_exc


@dataclass
class QuotaResult:
    success: bool
    endpoint: str = ""
    basic_left: Optional[int] = None
    premium_left: Optional[int] = None
    premium_sub_left: Optional[int] = None
    reset_at: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def quota_ok(self, threshold: int) -> bool:
        values = [v for v in (self.basic_left, self.premium_left, self.premium_sub_left) if v is not None]
        return bool(values) and max(values) > threshold

    def has_values(self) -> bool:
        return any(v is not None for v in (self.basic_left, self.premium_left, self.premium_sub_left))

    def to_text(self, threshold: int) -> str:
        checked = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not self.success:
            return f"ERROR: {self.error or 'quota api failed'} ({checked})"
        parts = []
        if self.basic_left is not None:
            parts.append(f"basic={self.basic_left}")
        if self.premium_left is not None:
            parts.append(f"premium={self.premium_left}")
        if self.premium_sub_left is not None:
            parts.append(f"premium_sub={self.premium_sub_left}")
        if self.reset_at:
            parts.append(f"reset={self.reset_at}")
        status = "OK" if self.quota_ok(threshold) else "LOW"
        if not parts:
            parts.append("quota=unknown")
        return f"{' | '.join(parts)} | {status} | {checked}"


@dataclass
class AccountRunResult:
    email: str
    success: bool
    status: str
    verify: str
    quota: QuotaResult
    cookies: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""


def safe_filename(email: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", email.strip()).strip("._") or "account"


def cookies_to_json(cookies: List[Dict[str, Any]]) -> str:
    return json.dumps(cookies, ensure_ascii=False, separators=(",", ":"))


def load_cookies_from_text(text: str) -> List[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return [c for c in data["cookies"] if isinstance(c, dict)]
    return []


def load_cookies_from_file(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return [c for c in data["cookies"] if isinstance(c, dict)]
    return []


def clean_cookies(cookies: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    allowed = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    cleaned_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
    for cookie in cookies:
        item = {k: v for k, v in cookie.items() if k in allowed and v is not None}
        if not item.get("name") or "value" not in item:
            continue
        same_site = item.get("sameSite")
        if same_site not in (None, "Strict", "Lax", "None"):
            item.pop("sameSite", None)
        if "path" not in item:
            item["path"] = "/"
        if "domain" not in item:
            item["domain"] = ".notegpt.io"
        key = (str(item.get("domain", "")), str(item.get("path", "")), str(item.get("name", "")))
        cleaned_by_key[key] = item
    return list(cleaned_by_key.values())


def find_first_value(data: Any, names: set[str]) -> Any:
    if isinstance(data, dict):
        lowered = {str(k).lower(): v for k, v in data.items()}
        for name in names:
            if name in lowered:
                return lowered[name]
        for value in data.values():
            found = find_first_value(value, names)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first_value(item, names)
            if found not in (None, ""):
                return found
    return None


def to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except Exception:
        return None


class NoteGPTAutomation:
    def __init__(self, config: AppConfig, log=None, proxy_manager: Optional[ProxyManager] = None,
                 should_stop: Optional[Callable[[], bool]] = None):
        self.config = config
        self.log = log or print
        self.proxy_manager = proxy_manager or ProxyManager(config, log=self.log)
        # Optional cooperative stop check. When the user hits Stop, callers can
        # supply this so an account that is still queued (or between steps)
        # bails out immediately instead of opening a browser and running to
        # completion. Defaults to "never stop" so existing callers are
        # unaffected.
        self.should_stop = should_stop or (proxy_manager.should_stop if proxy_manager is not None else None) or (lambda: False)

    def run_account(self, account: Dict[str, Any]) -> AccountRunResult:
        email = (account.get("email") or "").strip()
        password = (account.get("password") or "").strip()
        if not email:
            return self._failed(email, "missing email")

        # Bail out before doing any work if a stop was requested while this
        # account was still waiting in the executor queue. This is what makes
        # Stop actually stop Run Selected / Run All Text: queued accounts never
        # launch a browser.
        if self.should_stop():
            return self._stopped(email)

        self.log(f"[{email}] Bat dau")
        cookie_file = self.config.cookie_path / f"{safe_filename(email)}.json"
        cookie_candidates = []
        loaded_cookie_count = 0
        if self.config.reuse_cookies:
            cookie_candidates.extend(load_cookies_from_text(account.get("cookies", "")))
            cookie_candidates.extend(load_cookies_from_file(cookie_file))

        with sync_playwright() as p, BrowserStopGuard(p, self.should_stop):
            self.proxy_manager.wait_if_rotating(f"run {email}")
            launch_kwargs = {
                "headless": self.config.headless,
                "slow_mo": self.config.slow_mo_ms,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            proxy = self.proxy_manager.playwright_proxy("run")
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                accept_downloads=False,
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            try:
                if self.should_stop():
                    return self._stopped(email)
                if cookie_candidates:
                    cookies = clean_cookies(cookie_candidates)
                    if cookies:
                        context.add_cookies(cookies)
                        loaded_cookie_count = len(cookies)
                        self.log(f"[{email}] Nap {len(cookies)} cookies")

                goto_with_retry(
                    page,
                    NOTE_GPT_HOME,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[{email}]",
                    should_stop=self.should_stop,
                )
                if self.should_stop():
                    return self._stopped(email)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                quota = QuotaResult(success=False, error="cookie not checked")
                if loaded_cookie_count:
                    quota = self._fetch_quota(context)
                    if quota.success:
                        self.log(f"[{email}] Cookie hop le qua quota API, bo qua login email/password")
                    else:
                        self.log(f"[{email}] Cookie chua dung duoc quota API: {quota.error}")

                if not quota.success:
                    if not self._is_logged_in(context):
                        if not password:
                            return self._failed(email, "missing password and cookie is invalid")
                        self._login(page, context, email, password)
                    else:
                        self.log(f"[{email}] Cookie dang login")
                    quota = self._fetch_quota(context)
                cookies_out = self._get_all_cookies(context, page)
                if self.config.save_cookies and cookies_out:
                    cookie_file.write_text(json.dumps(cookies_out, ensure_ascii=False, indent=2), encoding="utf-8")
                    self.log(f"[{email}] Luu cookie: {cookie_file}")

                status = quota.to_text(self.config.quota_threshold)
                verify = "OK" if quota.success else "X"
                if quota.success:
                    self.log(f"[{email}] Quota: {status}")
                    return AccountRunResult(
                        email=email,
                        success=True,
                        status=status,
                        verify=verify,
                        quota=quota,
                        cookies=cookies_out,
                    )
                return AccountRunResult(
                    email=email,
                    success=False,
                    status=status,
                    verify=verify,
                    quota=quota,
                    cookies=cookies_out,
                    error=quota.error,
                )
            except Exception as exc:
                if self.should_stop():
                    return self._stopped(email)
                self.log(f"[{email}] Loi: {exc}")
                self.proxy_manager.report_error(f"RUN:{email}", exc)
                return self._failed(email, str(exc))
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    def _stopped(self, email: str) -> AccountRunResult:
        """Result used when the user stopped before/while this account ran.

        Marked as not-successful with a clear ``stopped`` status so the UI
        and any file/sheet writers can tell it apart from a real failure.
        """
        quota = QuotaResult(success=False, error="stopped by user")
        return AccountRunResult(
            email=email,
            success=False,
            status="stopped",
            verify="",
            quota=quota,
            cookies=[],
            error="stopped by user",
        )

    def _failed(self, email: str, error: str) -> AccountRunResult:
        quota = QuotaResult(success=False, error=error)
        return AccountRunResult(
            email=email,
            success=False,
            status=quota.to_text(self.config.quota_threshold),
            verify="X",
            quota=quota,
            error=error,
        )

    def _is_logged_in(self, context) -> bool:
        try:
            resp = context.request.get(USER_INFO_URL, timeout=20000)
            if resp.status != 200:
                return False
            data = resp.json() or {}
            return data.get("code") == 100000 or data.get("success") is True
        except Exception:
            return False

    def _dismiss_login_interstitials(self, page) -> bool:
        """Best-effort cleanup for login-time blockers.

        The current site sometimes inserts a promo modal a few seconds after
        load and, on some sessions, a Google One Tap iframe. We remove only the
        obvious blockers here and never accept the Google sign-in flow.
        """
        try:
            return bool(
                page.evaluate(
                    """
                    ({dismissTexts, promoTexts, googleHints}) => {
                        const norm = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const visible = (el) => {
                            if (!el) {
                                return false;
                            }
                            const style = getComputedStyle(el);
                            return style.display !== 'none' &&
                                style.visibility !== 'hidden' &&
                                (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
                        };
                        const textOf = (el) => norm(el.innerText || el.textContent || el.getAttribute('aria-label') || '');
                        const isDismiss = (text) => dismissTexts.some((wanted) => text === wanted || text.includes(wanted));
                        const isPromo = (text) => promoTexts.some((wanted) => text.includes(wanted));
                        const isGoogleFrame = (el) => {
                            const src = norm(el.src);
                            const title = norm(el.title);
                            const aria = norm(el.getAttribute('aria-label'));
                            return googleHints.some((hint) => src.includes(hint) || title.includes(hint) || aria.includes(hint));
                        };

                        let changed = false;

                        for (const iframe of Array.from(document.querySelectorAll('iframe'))) {
                            if (isGoogleFrame(iframe)) {
                                iframe.remove();
                                changed = true;
                            }
                        }

                        for (const el of Array.from(document.querySelectorAll('button,a,[role="button"]')).filter(visible)) {
                            const text = textOf(el);
                            if (text && isDismiss(text)) {
                                try {
                                    el.click();
                                    changed = true;
                                } catch (err) {
                                    // ignore
                                }
                            }
                        }

                        for (const root of Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], body > div.fixed, body > div.bg-default.fixed')).filter(visible)) {
                            const text = textOf(root);
                            if (!text) {
                                continue;
                            }
                            if (isPromo(text)) {
                                root.remove();
                                changed = true;
                                continue;
                            }
                            const closeButton = Array.from(root.querySelectorAll('button,a,[role="button"]')).find((el) => {
                                const buttonText = textOf(el);
                                return buttonText && isDismiss(buttonText);
                            });
                            if (closeButton) {
                                try {
                                    closeButton.click();
                                    changed = true;
                                } catch (err) {
                                    // ignore
                                }
                            }
                        }

                        return changed;
                    }
                    """,
                    {
                        "dismissTexts": list(LOGIN_DISMISS_TEXTS),
                        "promoTexts": list(LOGIN_PROMO_TEXTS),
                        "googleHints": list(LOGIN_GOOGLE_IFRAME_HINTS),
                    },
                )
            )
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False

    def _click_login_trigger(self, page) -> bool:
        selectors = [
            LOGIN_TRIGGER_SELECTOR_V2,
            LOGIN_TRIGGER_SELECTOR,
            "button:has-text('Sign In')",
            "button:has-text('Sign in')",
            "a:has-text('Sign In')",
            "a:has-text('Sign in')",
            "[role='button']:has-text('Sign In')",
            "[role='button']:has-text('Sign in')",
            "button:has-text('Log in')",
            "button:has-text('Login')",
            "a:has-text('Log in')",
            "a:has-text('Login')",
            "[role='button']:has-text('Log in')",
            "[role='button']:has-text('Login')",
            "text=Sign In",
            "text=Sign in",
            "text=Log in",
            "text=Login",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        loc.click(timeout=3500)
                    except Exception:
                        loc.click(timeout=3500, force=True)
                    return True
            except Exception:
                continue

        try:
            clicked = page.evaluate(
                """
                () => {
                    const wanted = ['sign in', 'sign-in', 'log in', 'login'];
                    const visible = (el) => {
                        if (!el) {
                            return false;
                        }
                        const style = getComputedStyle(el);
                        return style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
                    };
                    const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],span'))
                        .filter(visible);
                    for (const el of nodes) {
                        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (text.length <= 30 && wanted.some(w => text === w || text.includes(w))) {
                            el.click();
                            return text;
                        }
                    }
                    return '';
                }
                """
            )
            return bool(clicked)
        except Exception:
            return False

    def _login(self, page, context, email: str, password: str) -> None:
        self.log(f"[{email}] Login bang email/password")
        self._open_login_popup(page)
        self._fill_login_form(page, email, password)
        self._submit_login(page)
        deadline = time.time() + max(30, self.config.timeout_ms / 1000)
        last_error = ""
        while time.time() < deadline:
            if self._is_logged_in(context) or self._fetch_quota(context).success:
                self.log(f"[{email}] Login OK")
                return
            last_error = self._read_visible_error(page) or last_error
            page.wait_for_timeout(1000)
        raise RuntimeError(last_error or "login timeout")

    def _open_login_popup(self, page) -> None:
        if self._ensure_email_login_form(page):
            return
        deadline = time.time() + 10
        last_status_log = 0.0
        while time.time() < deadline:
            self._dismiss_login_interstitials(page)
            if self._ensure_email_login_form(page):
                return
            if self._click_login_trigger(page):
                page.wait_for_timeout(700)
                if self._ensure_email_login_form(page):
                    return
            now = time.time()
            if now - last_status_log >= 2.5:
                last_status_log = now
                self.log("[LOGIN] Waiting for login popup to settle...")
            page.wait_for_timeout(250)

        if self._ensure_email_login_form(page):
            return
        raise RuntimeError("khong mo duoc popup login")

    def _ensure_email_login_form(self, page) -> bool:
        if self._login_form_visible(page):
            return True
        self._dismiss_login_interstitials(page)
        if not self._login_popup_visible(page):
            return False

        self.log("[LOGIN] Popup da mo, click Sign in with Email...")
        email_mode_selectors = [
            EMAIL_LOGIN_TOGGLE_SELECTOR,
            "button:has-text('Sign in with Email')",
            "button:has-text('Sign In with Email')",
            "button:has-text('Log in with Email')",
            "button:has-text('Login with Email')",
            "button:has-text('Log In with Email')",
            "[role='button']:has-text('Sign in with Email')",
            "[role='button']:has-text('Log in with Email')",
            "[role='button']:has-text('Login with Email')",
            "text=Sign in with Email",
            "text=Log in with Email",
            "text=Login with Email",
        ]
        for selector in email_mode_selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        loc.click(timeout=5000)
                    except Exception:
                        loc.click(timeout=5000, force=True)
                    page.wait_for_timeout(1000)
                    if self._login_form_visible(page):
                        self.log(f"[LOGIN] Da mo form email bang selector: {selector}")
                        return True
            except Exception:
                continue

        try:
            clicked = page.evaluate(
                """
                () => {
                    const wanted = ['sign in with email', 'log in with email', 'login with email'];
                    const nodes = Array.from(document.querySelectorAll(
                        'button,a,[role="button"],div,span'
                    )).filter(el => {
                        if (!el) {
                            return false;
                        }
                        const style = getComputedStyle(el);
                        return style.display !== 'none' &&
                            style.visibility !== 'hidden' &&
                            (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
                    });
                    for (const el of nodes) {
                        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (wanted.some(w => text.includes(w))) {
                            el.click();
                            return text;
                        }
                    }
                    return '';
                }
                """
            )
            if clicked:
                page.wait_for_timeout(1000)
                if self._login_form_visible(page):
                    self.log(f"[LOGIN] Da mo form email bang text: {clicked}")
                    return True
        except Exception:
            pass
        return self._login_form_visible(page)

    def _login_popup_visible(self, page) -> bool:
        selectors = [
            "body > div.bg-default.fixed.top-1\\/2.left-1\\/2",
            "div[role='dialog']",
            ".fixed.top-1\\/2.left-1\\/2",
            "text=Continue with Google",
            "text=Sign in with Email",
            "text=Log in with Email",
            "text=Login with Email",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
        return False

    def _login_form_visible(self, page) -> bool:
        try:
            return page.locator(
                "input[type='email'], input[name='email'], input[placeholder*='email' i], "
                "input[autocomplete='email'], div[role='dialog'] input[type='text'], "
                "body > div.fixed input[type='text']"
            ).first.count() > 0
        except Exception:
            return False

    def _fill_login_form(self, page, email: str, password: str) -> None:
        email_selectors = [
            "input[type='email']",
            "input[name='email']",
            "input[placeholder*='email' i]",
            "input[autocomplete='email']",
            "div[role='dialog'] input[type='text']",
            "body > div.fixed input[type='text']",
        ]
        password_selectors = [
            "input[type='password']",
            "input[name='password']",
            "input[placeholder*='password' i]",
            "input[autocomplete='current-password']",
        ]
        self._fill_first(page, email_selectors, email, "email")
        self._fill_first(page, password_selectors, password, "password")

    def _fill_first(self, page, selectors: List[str], value: str, label: str) -> None:
        last_error = ""
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=8000)
                try:
                    loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    loc.fill(value, timeout=8000)
                    return
                except Exception:
                    loc.evaluate(
                        """
                        (el, nextValue) => {
                            const proto = el.tagName === 'TEXTAREA'
                                ? HTMLTextAreaElement.prototype
                                : HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                            if (setter) {
                                setter.call(el, nextValue);
                            } else {
                                el.value = nextValue;
                            }
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        value,
                    )
                    return
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(f"khong dien duoc {label}: {last_error}")

    def _submit_login(self, page) -> None:
        selectors = [
            LOGIN_BUTTON_SELECTOR,
            "div[role='dialog'] button:has-text('Sign In')",
            "div[role='dialog'] button:has-text('Sign in')",
            "div[role='dialog'] button:has-text('Log In')",
            "div[role='dialog'] button:has-text('Login')",
            "button:has-text('Sign In')",
            "button:has-text('Sign in')",
            "button:has-text('Log In')",
            "button:has-text('Login')",
            "button:has-text('Log in')",
            "form button[type='submit']",
            "button[type='submit']",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=1500)
                    except Exception:
                        pass
                    try:
                        loc.click(timeout=8000)
                    except Exception:
                        loc.click(timeout=8000, force=True)
                    return
            except Exception:
                continue
        raise RuntimeError("khong click duoc nut Sign In")

    def _read_visible_error(self, page) -> str:
        try:
            text = page.locator("[role='alert'], .text-danger, .text-red-500, .text-error").first.inner_text(timeout=1000)
            return " ".join(text.split())
        except Exception:
            return ""

    def _fetch_quota(self, context) -> QuotaResult:
        last_error = ""
        for endpoint in QUOTA_ENDPOINTS:
            try:
                resp = context.request.get(
                    endpoint,
                    headers={
                        "Accept": "application/json, text/plain, */*",
                        "Referer": NOTE_GPT_HOME,
                    },
                    timeout=30000,
                )
                if resp.status != 200:
                    last_error = f"{endpoint} HTTP {resp.status}"
                    continue
                data = resp.json() or {}
                if data.get("code") not in (None, 100000) and data.get("success") is not True:
                    last_error = f"{endpoint}: {data.get('message') or data.get('error') or data.get('code')}"
                    continue
                quota = self._parse_quota(data, endpoint)
                if quota.has_values():
                    return quota
                last_error = f"{endpoint}: empty quota data"
            except Exception as exc:
                last_error = f"{endpoint}: {exc}"
        return QuotaResult(success=False, error=last_error or "all quota endpoints failed")

    def _parse_quota(self, data: Dict[str, Any], endpoint: str) -> QuotaResult:
        root = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)) else data
        basic = to_int(
            find_first_value(
                root,
                {
                    "basic_quota_left",
                    "basic_quotas_left",
                    "basicquotasleft",
                    "free_quota_left",
                    "freequotaleft",
                    "basic_quota",
                    "basicquota",
                    "basic_left",
                    "basicleft",
                    "quota_left",
                    "quotaleft",
                },
            )
        )
        premium = to_int(
            find_first_value(
                root,
                {
                    "monthly_premium_credits_left",
                    "premium_credits_left",
                    "premiumcreditsleft",
                    "premium_quota_left",
                    "premiumquotaleft",
                    "premium_left",
                    "premiumleft",
                },
            )
        )
        premium_sub = to_int(
            find_first_value(
                root,
                {
                    "premium_sub_quota_left",
                    "premiumsubquotaleft",
                    "premium_sub_credits_left",
                    "monthly_premium_sub_credits_left",
                    "premium_pack_quota_left",
                    "premiumpackquotaleft",
                },
            )
        )
        reset = find_first_value(
            root,
            {
                "balance_reset",
                "balance_reset_at",
                "reset_at",
                "reset_time",
                "quota_reset_time",
                "resettime",
            },
        )
        return QuotaResult(
            success=True,
            endpoint=endpoint,
            basic_left=basic,
            premium_left=premium,
            premium_sub_left=premium_sub,
            reset_at=str(reset or ""),
            raw=data,
        )

    def _get_all_cookies(self, context, page) -> List[Dict[str, Any]]:
        cookies = context.cookies()
        try:
            cdp = context.new_cdp_session(page)
            all_cookies = cdp.send("Network.getAllCookies").get("cookies", [])
            if len(all_cookies) > len(cookies):
                cookies = all_cookies
        except (PlaywrightError, PlaywrightTimeoutError):
            pass
        return [c for c in cookies if isinstance(c, dict)]
