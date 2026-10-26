from __future__ import annotations

import csv
import html as html_lib
import io
import json
import random
import re
import secrets
import string
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import unquote, urlsplit

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.config import AppConfig, PROJECT_ROOT
    from notegpt_quota_tool.cancellation import BrowserStopGuard, interruptible_call, OperationStopped
    from notegpt_quota_tool.notegpt import NoteGPTAutomation, goto_with_retry, safe_filename
    from notegpt_quota_tool.proxy_manager import ProxyManager
    from notegpt_quota_tool.quota_scanner import AccountQuotaScanner, quota_left_from_quota
    from notegpt_quota_tool.unlimitmail import UnlimitMailClient
    from notegpt_quota_tool.tempmail_domains import TempMailDomainPool
else:
    from .config import AppConfig, PROJECT_ROOT
    from .cancellation import BrowserStopGuard, interruptible_call, OperationStopped
    from .notegpt import NoteGPTAutomation, goto_with_retry, safe_filename
    from .proxy_manager import ProxyManager
    from .quota_scanner import AccountQuotaScanner, quota_left_from_quota
    from .unlimitmail import UnlimitMailClient
    from .tempmail_domains import TempMailDomainPool


TEMPMAIL_BASE_URL = "https://tempmail.id.vn/api"
NOTEGPT_REGISTER_URL = "https://notegpt.io/user/register"
NOTEGPT_HOME = "https://notegpt.io/"
SIGNUP_WELCOME_BUTTON_SELECTOR = (
    r"#__nuxt > div:nth-child(1) > div.min-h-screen.flex.items-center.justify-center"
    r".bg-muted-50.dark\:bg-muted-950.p-4 > div > div.flex.flex-col.gap-3.mt-2 > "
    r"button.font-medium.inline-flex.items-center.disabled\:cursor-not-allowed"
    r".aria-disabled\:cursor-not-allowed.disabled\:opacity-75.aria-disabled\:opacity-75"
    r".transition-colors.px-3.py-2.gap-2.active\:bg-inverted\/90.disabled\:bg-inverted"
    r".aria-disabled\:bg-inverted.focus-visible\:outline-2.focus-visible\:outline-offset-2"
    r".focus-visible\:outline-inverted.w-full.justify-center.mt-4.md\:mt-6.mb-12"
    r".md\:mb-18.text-white.text-sm.rounded-lg.h-10.bg-black.hover\:bg-gray-800"
    r".dark\:bg-black.dark\:hover\:bg-gray-800"
)
SIGNUP_SUBMIT_SELECTOR = "#v-4-2 > button"


FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey",
    "Riley", "Jamie", "Avery", "Quinn", "Blake",
]
LAST_NAMES = [
    "Smith", "Johnson", "Brown", "Davis", "Wilson",
    "Moore", "Anderson", "Thomas", "Jackson", "White",
]


@dataclass
class ScreenSize:
    """Logical desktop work area, in pixels.

    Important: the unit must match what Chromium uses for ``--window-position``
    and ``--window-size``. On Windows that is DIPs (logical pixels), so we
    avoid toggling process DPI awareness here and prefer asking Chromium
    itself for the screen size when possible.
    """

    width: int = 1920
    height: int = 1080
    left: int = 0
    top: int = 0


def get_screen_size_win32() -> ScreenSize:
    """Win32 detector that always returns DIPs (logical pixels).

    Chromium's ``--window-position`` and ``--window-size`` are interpreted
    in DIPs on Windows. Because the parent process (CustomTkinter) may or
    may not have opted into per-monitor DPI awareness, the raw values from
    ``SystemParametersInfo`` could be either DIPs or physical pixels. We
    normalize:

      * If the process is DPI-aware → raw values are physical, divide by
        the primary monitor's effective DPI scale.
      * If the process is DPI-unaware → Windows already lies to us with
        DIP-equivalent values, use them as-is.

    This makes window positions match what Chromium expects on every
    machine regardless of monitor scaling (100%, 125%, 150%, 200%, …).
    """
    if sys.platform != "win32":
        return ScreenSize(width=1920, height=1080, left=0, top=0)

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        # SPI_GETWORKAREA = 0x0030 (work area excludes the taskbar)
        ok = user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        if ok:
            raw_w = rect.right - rect.left
            raw_h = rect.bottom - rect.top
            raw_l = rect.left
            raw_t = rect.top
        else:
            raw_w = user32.GetSystemMetrics(0)
            raw_h = user32.GetSystemMetrics(1)
            raw_l = raw_t = 0
        if raw_w <= 0 or raw_h <= 0:
            return ScreenSize(width=1920, height=1080, left=0, top=0)

        is_aware = False
        try:
            is_aware = bool(user32.IsProcessDPIAware())
        except Exception:
            is_aware = False

        if not is_aware:
            return ScreenSize(width=int(raw_w), height=int(raw_h),
                              left=int(raw_l), top=int(raw_t))

        # Process is DPI-aware → convert physical pixels to DIPs.
        scale_x = scale_y = 1.0
        try:
            class POINT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]

            shcore = ctypes.windll.shcore
            # MONITOR_DEFAULTTOPRIMARY = 1
            hmon = user32.MonitorFromPoint(POINT(0, 0), 1)
            dpi_x = ctypes.c_uint()
            dpi_y = ctypes.c_uint()
            # GetDpiForMonitor(hmon, MDT_EFFECTIVE_DPI=0, &dpi_x, &dpi_y)
            res = shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
            if res == 0 and dpi_x.value > 0 and dpi_y.value > 0:
                scale_x = dpi_x.value / 96.0
                scale_y = dpi_y.value / 96.0
        except Exception:
            scale_x = scale_y = 1.0

        # Cap absurd scales to a sane range.
        scale_x = max(1.0, min(4.0, scale_x))
        scale_y = max(1.0, min(4.0, scale_y))
        return ScreenSize(
            width=int(raw_w / scale_x),
            height=int(raw_h / scale_y),
            left=int(raw_l / scale_x),
            top=int(raw_t / scale_y),
        )
    except Exception:
        return ScreenSize(width=1920, height=1080, left=0, top=0)


def probe_screen_via_chromium(
    log: Optional[Callable[[str], None]] = None,
    proxy: Optional[Dict[str, str]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[ScreenSize]:
    """Headed Chromium probe used as a last resort.

    Headless Chromium reports its synthetic viewport size, not the desktop,
    so we deliberately launch headed and minimized to read
    ``window.screen.availWidth`` / ``availHeight``. The window is closed
    immediately so it never reaches the user.
    """
    log_fn = log or (lambda *_: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        log_fn(f"[REG] Screen probe skipped: playwright not available ({exc})")
        return None
    try:
        with sync_playwright() as p, BrowserStopGuard(p, should_stop or (lambda: False)):
            launch_kwargs: Dict[str, Any] = {
                "headless": False,
                "args": [
                    "--start-minimized",
                    "--window-position=-32000,-32000",
                ],
            }
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = p.chromium.launch(**launch_kwargs)
            try:
                context = browser.new_context()
                page = context.new_page()
                data = page.evaluate(
                    """
                    () => ({
                      w:  window.screen.availWidth,
                      h:  window.screen.availHeight,
                      l:  window.screen.availLeft  || 0,
                      t:  window.screen.availTop   || 0,
                      sw: window.screen.width,
                      sh: window.screen.height,
                      dpr: window.devicePixelRatio || 1,
                    })
                    """
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
        if not isinstance(data, dict):
            return None
        width = int(data.get("w") or 0)
        height = int(data.get("h") or 0)
        if width <= 0 or height <= 0:
            return None
        log_fn(
            f"[REG] Chromium screen probe: avail={width}x{height} "
            f"@ ({data.get('l')},{data.get('t')}) | "
            f"full={data.get('sw')}x{data.get('sh')} | dpr={data.get('dpr')}"
        )
        return ScreenSize(
            width=width,
            height=height,
            left=int(data.get("l") or 0),
            top=int(data.get("t") or 0),
        )
    except Exception as exc:
        log_fn(f"[REG] Chromium screen probe failed: {exc}")
        return None


def get_screen_size(log: Optional[Callable[[str], None]] = None, should_stop=None) -> ScreenSize:
    """Best-effort screen size suitable for Chromium window placement.

    Strategy, picked specifically so window positions land where the user
    can see them on any screen and any DPI scaling:

      1. Win32 work area, normalized to DIPs by the monitor scale factor.
         Authoritative on Windows because Chromium uses the same DIP space.
      2. If somehow Win32 reports a value that obviously is not a desktop
         (less than 800x600), retry with a headed Chromium probe.
      3. Hard fallback: 1920x1080.
    """
    win32 = get_screen_size_win32()
    if win32.width >= 800 and win32.height >= 600:
        if log:
            log(
                f"[REG] Win32 DIP screen: {win32.width}x{win32.height} "
                f"@ ({win32.left},{win32.top})"
            )
        return win32
    probed = probe_screen_via_chromium(log=log, should_stop=should_stop)
    if probed:
        return probed
    return ScreenSize(width=1920, height=1080, left=0, top=0)


def compute_grid_layout(
    total: int, screen: Optional[ScreenSize] = None
) -> List[Dict[str, int]]:
    """Tile ``total`` browser windows over the work area.

    Mirrors getWindowLayout from notegpt_extension/background.js. Returns a
    list of dicts with ``left``, ``top``, ``width``, ``height`` keys, indexed
    0..total-1. Uses integer division so the rightmost/bottommost tiles
    extend to the screen edge instead of leaking past it.
    """
    total = max(1, int(total))
    s = screen or get_screen_size()
    sw = max(800, int(s.width))
    sh = max(600, int(s.height))
    sx = int(s.left)
    sy = int(s.top)

    if total == 1:
        cols, rows = 1, 1
    elif total == 2:
        cols, rows = 2, 1
    elif total <= 4:
        cols, rows = 2, 2
    elif total <= 6:
        cols, rows = 3, 2
    elif total <= 9:
        cols, rows = 3, 3
    else:
        cols = 4
        rows = (total + cols - 1) // cols

    cell_w = sw // cols
    cell_h = sh // rows
    # Tiny visual gap between tiles so window borders don't appear merged.
    margin = 2

    layout: List[Dict[str, int]] = []
    for index in range(total):
        col = index % cols
        row = index // cols
        left = sx + col * cell_w
        top = sy + row * cell_h
        # Last column / row gets the remainder so windows don't drift
        # past the screen edge from accumulated rounding.
        if col == cols - 1:
            width = sw - col * cell_w
        else:
            width = cell_w
        if row == rows - 1:
            height = sh - row * cell_h
        else:
            height = cell_h
        # Apply gap, never letting a window fall under the minimum usable size.
        width = max(200, width - margin)
        height = max(200, height - margin)
        # Final clamp so a window absolutely cannot exceed the work area.
        if left + width > sx + sw:
            width = (sx + sw) - left
        if top + height > sy + sh:
            height = (sy + sh) - top
        layout.append(
            {
                "left": left,
                "top": top,
                "width": width,
                "height": height,
            }
        )
    return layout


@dataclass
class AutoRegisterResult:
    """Single account registration result."""

    email: str = ""
    password: str = ""
    name: str = ""
    status: str = ""
    verify: str = ""
    cookies: str = ""
    quota_left: int = 0
    quota_text: str = ""
    quota_status: str = ""
    started_at: str = ""
    finished_at: str = ""
    error: str = ""
    # Internal only: proxy revision used by the rate-limit retry coordinator.
    proxy_generation: int = field(default=0, repr=False, compare=False)

    @property
    def success(self) -> bool:
        # An account is "successful" only when we have a usable email, NoteGPT
        # confirmed the registration via the verify link, AND no error was
        # recorded along the way (e.g. a proxy/quota failure). Anything else
        # (no email, captcha lost, verify timeout, verify failed, quota error)
        # is considered failed for the purposes of `only_save_success`, so a
        # row that carries an error message never lands in the output file.
        if not self.email or self.verify != "verified":
            return False
        if (self.error or "").strip():
            return False
        # When a quota check ran, a non-OK result (X / EMPTY) also counts as a
        # failure so only genuinely usable accounts are written.
        if (self.quota_status or "").strip().upper() not in ("", "OK"):
            return False
        return True


@dataclass
class AutoRegisterSummary:
    total: int
    success: int
    failed: int
    output_path: str
    results: List[AutoRegisterResult] = field(default_factory=list)
    stopped: bool = False
    error: str = ""


class TempMailClient:
    """Minimal client for tempmail.id.vn used by the extension version.

    All endpoints accept a Bearer token. Multiple tokens can be supplied so
    parallel workers don't share the same quota.

    A ``proxies`` dict (same shape as ``requests`` accepts) can be supplied
    so tempmail traffic flows through the same Zing proxy as Playwright.
    """

    def __init__(
        self,
        tokens: List[str],
        log: Optional[Callable[[str], None]] = None,
        timeout: int = 30,
        proxies: Optional[Dict[str, str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.should_stop = should_stop or (lambda: False)
        self._tokens = [t.strip() for t in tokens if t and t.strip()]
        self.log = log or (lambda *_: None)
        self.timeout = timeout
        self.proxies = proxies or None
        self._domain_pool: Optional[TempMailDomainPool] = None

    def configure_domains(
        self, domains: List[str], domains_by_token: List[List[str]], worker_count: int,
    ) -> None:
        if len(domains_by_token) != len(self._tokens):
            raise ValueError("Domain list does not match the current tokens. Refresh the list.")
        pool = TempMailDomainPool(domains, domains_by_token)
        pool.validate_workers(worker_count)
        self._domain_pool = pool

    @property
    def has_tokens(self) -> bool:
        return bool(self._tokens)

    def _token(self, worker_index: int) -> str:
        if not self._tokens:
            raise RuntimeError("TempMail token is empty")
        return self._tokens[worker_index % len(self._tokens)]

    def _headers(self, worker_index: int) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token(worker_index)}",
        }

    def get_domains(self, worker_index: int = 0) -> List[Dict[str, Any]]:
        url = f"{TEMPMAIL_BASE_URL}/domain"
        resp = interruptible_call(requests.get, self.should_stop, 
            url,
            headers=self._headers(worker_index),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or data.get("success") is False:
            raise ValueError("TempMail domain API rejected the request.")
        items = data.get("data", data.get("items"))
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("name") or item.get("domain"), str)
            or not (item.get("name") or item.get("domain")).strip()
            for item in items
        ):
            raise ValueError("TempMail returned an invalid domain list.")
        return items

    def create_email(
        self,
        worker_index: int = 0,
        *,
        domain: str = "",
        prefer_edu: bool = False,
    ) -> Dict[str, Any]:
        url = f"{TEMPMAIL_BASE_URL}/email/create"
        body: Dict[str, Any] = {}
        chosen_domain = domain.strip()
        if not chosen_domain and self._domain_pool is not None:
            chosen_domain = self._domain_pool.next(worker_index)

        if not chosen_domain and prefer_edu:
            try:
                domains = self.get_domains(worker_index)
                edus = [
                    str(d.get("name") or d.get("domain") or "").strip()
                    for d in domains
                    if ".edu" in str(d.get("name") or d.get("domain") or "").lower()
                ]
                if edus:
                    chosen_domain = random.choice(edus)
                    self.log(f"[REG:W{worker_index + 1}] Picked .edu domain: {chosen_domain}")
            except Exception as exc:
                self.log(f"[REG:W{worker_index + 1}] get_domains failed: {exc}")

        if chosen_domain:
            # TempMail ignores domain-only requests and allocates a random address.
            body["user"] = secrets.token_hex(8)
            body["domain"] = chosen_domain

        resp = interruptible_call(requests.post, self.should_stop, 
            url,
            json=body,
            headers={**self._headers(worker_index), "Content-Type": "application/json"},
            timeout=self.timeout,
            proxies=self.proxies,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"create_email HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json() or {}
        if not data.get("success", True) and "data" not in data:
            raise RuntimeError(f"create_email failed: {data.get('message') or data}")
        inner = data.get("data") or data
        email = str(inner.get("email") or inner.get("address") or "").strip()
        mail_id = str(inner.get("id") or inner.get("_id") or inner.get("mail_id") or "").strip()
        if not email or not mail_id:
            raise RuntimeError(f"create_email returned bad payload: {data}")
        returned_domain = email.rpartition("@")[2].lower()
        if chosen_domain and returned_domain != chosen_domain.lower():
            raise RuntimeError(
                f"TempMail domain mismatch: requested {chosen_domain}, "
                f"received {returned_domain or '(missing)'}. Mailbox not used."
            )
        return {"email": email, "id": mail_id, "raw": data}

    def list_messages(self, mail_id: str, worker_index: int = 0) -> List[Dict[str, Any]]:
        url = f"{TEMPMAIL_BASE_URL}/email/{mail_id}"
        resp = interruptible_call(requests.get, self.should_stop, 
            url,
            headers=self._headers(worker_index),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        for key in ("data.items", "data", "items"):
            cur: Any = data
            for part in key.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            if isinstance(cur, list):
                return [m for m in cur if isinstance(m, dict)]
        return []

    def read_message(self, message_id: str, worker_index: int = 0) -> Dict[str, Any]:
        url = f"{TEMPMAIL_BASE_URL}/message/{message_id}"
        resp = interruptible_call(requests.get, self.should_stop, 
            url,
            headers=self._headers(worker_index),
            timeout=self.timeout,
            proxies=self.proxies,
        )
        resp.raise_for_status()
        return resp.json() or {}


def create_mail_client(
    provider: str,
    tokens: List[str],
    *,
    log: Optional[Callable[[str], None]] = None,
    proxies: Optional[Dict[str, str]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> TempMailClient | UnlimitMailClient:
    name = str(provider or "tempmail").strip().lower()
    if name == "unlimitmail":
        return UnlimitMailClient(tokens, log=log, proxies=proxies, should_stop=should_stop)
    if name == "tempmail":
        return TempMailClient(tokens, log=log, proxies=proxies, should_stop=should_stop)
    raise ValueError(f"Unsupported mail provider: {name}")


# Verify link extractor ported from notegpt_extension/background.js
_VERIFY_DIRECT_RE = re.compile(
    r"(https?://(?:www\.)?notegpt\.io/user/confirm-email\?[^\s\"'<>]+)",
    re.IGNORECASE,
)
_VERIFY_AWS_RE = re.compile(
    r"(https?://[^\s\"'<>]*awstrack\.me[^\s\"'<>]*https?(?::|%3A)(?:/|%2F)(?:/|%2F)"
    r"(?:www\.)?notegpt\.io(?:/|%2F)user(?:/|%2F)confirm-email[^\s\"'<>]*)",
    re.IGNORECASE,
)
_VERIFY_TOKEN_RE = re.compile(r"token[=%3D]+([^&\s\"'<>/]+)", re.IGNORECASE)
_VERIFY_TYPE_RE = re.compile(r"type[=%3D]*([^&\s\"'<>/]*)", re.IGNORECASE)
_VERIFY_FALLBACK_RE = re.compile(
    r"(https?://[^\s\"'<>]*confirm-email[^\s\"'<>]*)", re.IGNORECASE
)
_VERIFY_ANY_URL_RE = re.compile(r"(https?://[^\s\"'<>\\)]+)", re.IGNORECASE)
_VERIFY_NOTEGPT_URL_RE = re.compile(r"(https?://[^\s\"'<>\\)]*notegpt\.io[^\s\"'<>\\)]*)", re.IGNORECASE)


def _decode_mail_text(value: str) -> str:
    decoded = value or ""
    for _ in range(3):
        next_value = html_lib.unescape(decoded)
        next_value = (
            next_value.replace("\\/", "/")
            .replace("\\u0026", "&")
            .replace("\\u003d", "=")
            .replace("\\u003D", "=")
            .replace("=3D", "=")
            .replace("=26", "&")
            .replace("&#x2F;", "/")
        )
        next_value = re.sub(r"=\r?\n", "", next_value)
        next_value = re.sub(r"\r?\n", " ", next_value)
        try:
            next_value = unquote(next_value)
        except Exception:
            pass
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _is_verify_candidate(url: str) -> bool:
    lower = (url or "").lower()
    if "notegpt.io" not in lower:
        return False
    if any(bad in lower for bad in ("unsubscribe", "privacy", "terms", "google")):
        return False
    return any(
        marker in lower
        for marker in (
            "confirm-email",
            "verify",
            "verification",
            "activate",
            "activation",
            "token=",
            "code=",
            "email",
        )
    )


def _normalize_verify_url(url: str) -> str:
    cleaned = (url or "").rstrip(".,;\"'>)]} ")
    for _ in range(3):
        decoded = _decode_mail_text(cleaned).rstrip(".,;\"'>)]} ")
        if decoded == cleaned:
            break
        cleaned = decoded
    token_match = _VERIFY_TOKEN_RE.search(cleaned)
    type_match = _VERIFY_TYPE_RE.search(cleaned)
    if "notegpt.io" not in cleaned.lower() and token_match:
        token = unquote(token_match.group(1))
        type_value = unquote(type_match.group(1)) if type_match else ""
        return f"https://notegpt.io/user/confirm-email?token={token}&type={type_value}"
    return cleaned


def extract_verify_link(html: str) -> Optional[str]:
    """Pull the NoteGPT confirm-email URL out of a tempmail HTML body.

    Handles:
      - Direct https://notegpt.io/user/confirm-email?token=... links
      - awstrack.me wrapped links with URL-encoded inner URL
      - Any confirm-email link as a last fallback
    """
    if not html:
        return None
    decoded = _decode_mail_text(html)

    for match in _VERIFY_DIRECT_RE.finditer(decoded):
        link = _normalize_verify_url(match.group(1))
        if len(link) > 40:
            return link

    for match in _VERIFY_AWS_RE.finditer(decoded):
        track_url = _normalize_verify_url(match.group(1))
        token_match = _VERIFY_TOKEN_RE.search(track_url)
        type_match = _VERIFY_TYPE_RE.search(track_url)
        if token_match:
            token = unquote(token_match.group(1))
            type_value = unquote(type_match.group(1)) if type_match else ""
            return f"https://notegpt.io/user/confirm-email?token={token}&type={type_value}"
        return track_url

    for match in _VERIFY_FALLBACK_RE.finditer(decoded):
        link = _normalize_verify_url(match.group(1))
        if len(link) > 40 and "unsubscribe" not in link.lower():
            return link

    # Newer emails may use a different verify path or put the NoteGPT URL
    # inside a tracking link. Decode each URL and accept any NoteGPT URL that
    # looks like an account verification/action URL.
    for match in _VERIFY_ANY_URL_RE.finditer(decoded):
        raw_url = _normalize_verify_url(match.group(1))
        candidates = [raw_url]
        try:
            candidates.append(unquote(raw_url))
        except Exception:
            pass
        for candidate_text in list(candidates):
            for inner in _VERIFY_NOTEGPT_URL_RE.finditer(candidate_text):
                candidates.append(_normalize_verify_url(inner.group(1)))
        for candidate in candidates:
            candidate = _normalize_verify_url(candidate)
            if len(candidate) > 30 and _is_verify_candidate(candidate):
                return candidate
    return None


def random_password(length: int = 12) -> str:
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%"
    pool = upper + lower + digits + special
    seed = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(special),
    ]
    seed += [random.choice(pool) for _ in range(max(0, length - 4))]
    random.shuffle(seed)
    return "".join(seed)


def random_name() -> Dict[str, str]:
    return {
        "first_name": random.choice(FIRST_NAMES),
        "last_name": random.choice(LAST_NAMES),
    }


class _TextOutputWriter:
    """Append-only TSV writer used to persist accounts.

    Format: tab-separated columns matching AutoRegisterResult fields. Header
    is written when the file is empty.
    """

    HEADERS = [
        "email",
        "password",
        "name",
        "status",
        "verify",
        "quota_left",
        "quota_text",
        "quota_status",
        "started_at",
        "finished_at",
        "error",
    ]

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
                writer.writerow(self.HEADERS)

    def append(self, result: AutoRegisterResult) -> None:
        row = [
            result.email,
            result.password,
            result.name,
            result.status,
            result.verify,
            str(result.quota_left or 0),
            result.quota_text,
            result.quota_status,
            result.started_at,
            result.finished_at,
            result.error,
        ]
        # Strip newlines so each account stays one line.
        row = [str(v).replace("\r", " ").replace("\n", " ").strip() for v in row]
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
                writer.writerow(row)


class AutoRegisterRunner:
    """
    Register new NoteGPT accounts using TempMail and Playwright.

    Mirrors the Chrome extension flow: open /user/register, fill form, wait
    for the user to clear any captcha (when not headless), submit, poll the
    tempmail inbox for the verify link, click it, extract cookies, and
    append the result line to a plain text (TSV) output file.
    """

    def __init__(
        self,
        config: AppConfig,
        log: Optional[Callable[[str], None]] = None,
        on_account_done: Optional[Callable[[int, int, AutoRegisterResult], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        captcha_signal: Optional[Callable[[int, str], None]] = None,
        wait_captcha: Optional[Callable[[int], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        proxy_manager: Optional[ProxyManager] = None,
        on_rate_limit=None,
        continue_registration_errors=False,
    ) -> None:
        self.config = config
        self.log = log or print
        self.on_account_done = on_account_done
        self.on_progress = on_progress
        self.captcha_signal = captcha_signal
        self.wait_captcha = wait_captcha
        self._external_should_stop = should_stop or (lambda: False)
        self._stop_event = threading.Event()
        self.should_stop = (
            lambda: self._stop_event.is_set() or self._external_should_stop()
        )
        self.proxy_manager = proxy_manager
        self.continue_registration_errors = continue_registration_errors
        self.on_rate_limit = on_rate_limit
        self._limit_lock = threading.Lock()
        self.rate_limit_stopped = False
        self._layouts: List[Dict[str, int]] = []
        self._active_browsers_lock = threading.RLock()
        self._active_browsers: Dict[int, Dict[str, Any]] = {}
        # Rate-limit retries share one Kiot rotation. This prevents N workers
        # that fail in the same wave from spamming the rotate endpoint.
        self._rate_limit_rotation_condition = threading.Condition(threading.RLock())
        self._rate_limit_rotation_in_progress = False

    def request_stop(self) -> None:
        """Signal cancellation and close every browser owned by this runner."""
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        # Each Playwright session watches this event, including during launch.

    def stop_for_rate_limit(self,result):
        if self.continue_registration_errors:
            return False
        if not self._is_registration_rate_limit(result):return False
        with self._limit_lock:
            if self.rate_limit_stopped:return True
            self.rate_limit_stopped=True
            self.request_stop()
            self.log('[REG] RATE_LIMIT_STOP: cancelling all workers; no retry or IP rotation.')
            if self.on_rate_limit:self.on_rate_limit()
        return True

    @staticmethod
    def _close_tracked_browsers(active: List[Dict[str, Any]]) -> None:
        for item in active:
            transport_pid = int(item.get("transport_pid") or 0)
            killed = False
            if sys.platform == "win32" and transport_pid > 0:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(transport_pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    killed = True
                except Exception:
                    pass
            if not killed:
                browser = item.get("browser")
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def _track_browser(
        self,
        worker_index: int,
        browser: Any,
        *,
        context: Any = None,
        page: Any = None,
    ) -> int:
        token = id(browser)
        transport_pid = 0
        try:
            transport = browser._impl_obj._connection._transport
            transport_pid = int(getattr(getattr(transport, "_proc", None), "pid", 0) or 0)
        except Exception:
            pass
        with self._active_browsers_lock:
            current = self._active_browsers.get(token, {})
            current.update(
                {
                    "worker_index": worker_index,
                    "browser": browser,
                    "context": context,
                    "page": page,
                    "transport_pid": transport_pid,
                }
            )
            self._active_browsers[token] = current
        return token

    def _untrack_browser(self, token: int) -> None:
        with self._active_browsers_lock:
            self._active_browsers.pop(token, None)

    def _wait_interruptibly(self, seconds: float) -> bool:
        """Wait for up to ``seconds``; return False as soon as Stop is set."""
        deadline = time.time() + max(0.0, float(seconds))
        while not self.should_stop():
            remaining = deadline - time.time()
            if remaining <= 0:
                return True
            self._stop_event.wait(min(0.2, remaining))
        return False

    def run(
        self,
        *,
        tokens: List[str],
        output_path: str | Path,
        count: int,
        parallel: int = 1,
        rotate_every: int = 0,
        on_rotation_wait=None,
        prefer_edu: bool = False,
        domain: str = "",
        verify_timeout_sec: int = 120,
        per_account_delay_sec: int = 3,
        wait_for_captcha: bool = True,
        save_cookies: bool = True,
        check_quota_after_register: bool = False,
        only_save_success: bool = False,
        failure_threshold_pct: float = 50.0,
        mail_provider: str = "tempmail",
        retry_count: int = 2,
        domains: Optional[List[str]] = None,
        domains_by_token: Optional[List[List[str]]] = None,
    ) -> AutoRegisterSummary:
        if rotate_every:
            if rotate_every < 1 or parallel < 1 or self.proxy_manager is None:
                raise ValueError('Periodic rotation requires positive worker count and a proxy.')
        if count <= 0:
            return AutoRegisterSummary(0, 0, 0, str(output_path), error="count must be > 0")
        if not tokens:
            return AutoRegisterSummary(
                0, 0, 0, str(output_path), error="no mail API tokens supplied"
            )
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except Exception as exc:
            return AutoRegisterSummary(
                0, 0, 0, str(output_path), error=f"Playwright is not installed: {exc}"
            )

        output_path_obj = Path(output_path)
        if not output_path_obj.is_absolute():
            output_path_obj = PROJECT_ROOT / output_path_obj
        if output_path_obj.suffix.lower() not in (".txt", ".tsv", ".csv"):
            output_path_obj = output_path_obj.with_suffix(".txt")

        writer = _TextOutputWriter(output_path_obj)
        # Pipe TempMail traffic through the same proxy as Playwright when
        # the user enabled it for register; otherwise fall back to direct.
        tempmail_proxies: Optional[Dict[str, str]] = None
        if self.proxy_manager is not None:
            try:
                tempmail_proxies = self.proxy_manager.requests_proxies("register")
            except Exception:
                tempmail_proxies = None
        tempmail = create_mail_client(
            mail_provider, tokens, log=self.log, proxies=tempmail_proxies,
            should_stop=self.should_stop,
        )
        if domains is not None and isinstance(tempmail, TempMailClient):
            try:
                tempmail.configure_domains(domains, domains_by_token or [], max(1, min(parallel, count)))
                self.log(f"[REG] TempMail selected domains: {', '.join(domains)}")
            except ValueError as exc:
                return AutoRegisterSummary(0, 0, 0, str(output_path_obj), error=str(exc))

        results: List[AutoRegisterResult] = []
        results_lock = threading.Lock()
        success_count = {"value": 0}
        failed_count = {"value": 0}
        completed_count = {"value": 0}

        parallel = max(1, min(parallel, count))
        failure_threshold_pct = max(1.0, min(100.0, float(failure_threshold_pct)))
        retry_count = max(0, min(5, int(retry_count)))
        self.log(
            f"[REG] Start auto register: count={count}, parallel={parallel}, "
            f"mail_provider={mail_provider}, "
            f"prefer_edu={prefer_edu}, output={output_path_obj}, "
            f"only_save_success={'on' if only_save_success else 'off'}, "
            f"rotate_failure_threshold={failure_threshold_pct:g}%, "
            f"same_mail_retries={retry_count}"
        )
        # Pre-compute window layout so all browsers tile evenly across the
        # primary monitor — makes manual captcha solving on many parallel
        # tabs much easier. Probing Chromium itself avoids DPI scaling
        # mismatches between Win32 metrics and Chromium window coords.
        screen = get_screen_size(log=self.log, should_stop=self.should_stop)
        layouts = compute_grid_layout(parallel, screen)
        self._layouts = layouts
        self.log(
            f"[REG] Screen work area: {screen.width}x{screen.height} "
            f"@ ({screen.left},{screen.top}); tiles={len(layouts)}"
        )

        def rotate_after_failed_wave(
            wave_results: List[AutoRegisterResult],
            wave_end: int,
        ) -> bool:
            if self.proxy_manager is None:
                return True
            try:
                settings = getattr(self.proxy_manager, "settings", None)
                provider = str(getattr(settings, "provider", "") or "").lower()
                if provider not in ("zing", "kiot"):
                    return True
                if not getattr(self.proxy_manager, "enabled", False):
                    return True
            except Exception:
                return True

            total_seen = len(wave_results)
            failed_seen = sum(1 for result in wave_results if not result.success)
            failed_pct = (failed_seen / total_seen) if total_seen else 0.0
            threshold_ratio = failure_threshold_pct / 100.0
            if total_seen == 0 or failed_pct < threshold_ratio or wave_end >= count:
                return True

            reason = (
                f"AutoReg wave failed {failed_seen}/{total_seen} "
                f"({failed_pct:.0%}) after account {wave_end}"
            )
            self.log(
                f"[REG] Failure rate reached {failure_threshold_pct:g}%; "
                f"blocking next wave until IP "
                f"rotation succeeds: {reason}"
            )
            try:
                if provider == "kiot":
                    ok = self.proxy_manager.rotate_until_success(reason=reason)
                else:
                    ok = self.proxy_manager.rotate(reason=reason)
                    if ok:
                        self.proxy_manager.wait_if_rotating(
                            "autoreg failure-rate rotate"
                        )
                if ok:
                    try:
                        tempmail.proxies = self.proxy_manager.requests_proxies("register")
                    except Exception:
                        pass
                else:
                    if provider == "kiot":
                        self.log(
                            "[REG] Kiot rotate stopped or failed; "
                            "next wave will not start"
                        )
                    else:
                        self.log("[REG] Failure-rate rotate skipped or failed")
                # Preserve Zing's existing single-attempt behavior. Kiot is
                # the strict provider that must rotate before the next wave.
                return bool(ok) if provider == "kiot" else True
            except Exception as exc:
                self.log(f"[REG] Failure-rate rotate error: {exc}")
                return False

        # Never let a worker wave cross a mandatory rotation boundary.
        group_size = rotate_every or count
        waves = (
            (start, min(start + parallel, group_start + group_size, count))
            for group_start in range(0, count, group_size)
            for start in range(group_start, min(group_start + group_size, count), parallel)
        )
        for wave_start, wave_stop in waves:
            if self.should_stop():
                break
            wave_indices = list(range(wave_start, wave_stop))
            wave_results: List[AutoRegisterResult] = []
            wave_results_lock = threading.Lock()
            self.log(
                f"[REG] Wave start: accounts={wave_indices[0] + 1}-"
                f"{wave_indices[-1] + 1}/{count} | workers={len(wave_indices)}"
            )

            def worker(worker_index: int, idx: int) -> None:
                if self.should_stop():
                    return
                attempt = idx + 1
                self.log(f"[REG:W{worker_index + 1}] === Account {attempt}/{count} ===")
                try:
                    result = self._register_one(
                        tempmail=tempmail,
                        worker_index=worker_index,
                        attempt=attempt,
                        total=count,
                        prefer_edu=prefer_edu,
                        domain=domain,
                        verify_timeout_sec=verify_timeout_sec,
                        wait_for_captcha=wait_for_captcha,
                        save_cookies=save_cookies,
                        check_quota_after_register=check_quota_after_register,
                        retry_count=retry_count,
                    )
                except Exception as exc:
                    result = AutoRegisterResult(
                        status='stopped' if self.should_stop() else 'failed_register',
                        error=str(exc), finished_at=datetime.now().isoformat(timespec='seconds'))
                    self.log(f'[REG] Attempt {attempt} failed: {exc}')
                self.stop_for_rate_limit(result)
                with results_lock:
                    results.append(result)
                    completed_count["value"] += 1
                    done_now = completed_count["value"]
                    if result.success:
                        success_count["value"] += 1
                    else:
                        failed_count["value"] += 1
                with wave_results_lock:
                    wave_results.append(result)
                # Persist policy: by default we log every attempt so the user
                # has audit trail even when something fails. With
                # only_save_success=True the text file becomes a clean list
                # of usable accounts (no failed/no-quota rows).
                if (not only_save_success) or result.success:
                    writer.append(result)
                else:
                    self.log(
                        f"[REG:W{worker_index + 1}] Skipped writing failed row: "
                        f"{result.email or '(no email)'} | status={result.status} | "
                        f"verify={result.verify} | error={result.error}"
                    )
                if self.on_account_done:
                    try:
                        self.on_account_done(attempt, count, result)
                    except Exception:
                        pass
                if self.on_progress:
                    try:
                        self.on_progress(done_now, count)
                    except Exception:
                        pass
                if per_account_delay_sec > 0 and not self.should_stop():
                    self._wait_interruptibly(per_account_delay_sec)

            threads: List[threading.Thread] = []
            for worker_index, idx in enumerate(wave_indices):
                t = threading.Thread(
                    target=worker,
                    args=(worker_index, idx),
                    daemon=True,
                )
                t.start()
                threads.append(t)
            for t in threads:
                while t.is_alive() and not self.should_stop():
                    t.join(timeout=0.2)
            if self.should_stop():
                self.request_stop()
                for t in threads:
                    t.join()
                break

            wave_end = wave_indices[-1] + 1
            wave_failed = sum(1 for result in wave_results if not result.success)
            self.log(
                f"[REG] Wave complete: accounts={wave_indices[0] + 1}-"
                f"{wave_end}/{count} | ok={len(wave_results) - wave_failed} | "
                f"failed={wave_failed}"
            )
            if rotate_every:
                if wave_end < count and wave_end % rotate_every == 0:
                    if self.should_stop():
                        break
                    if on_rotation_wait:
                        on_rotation_wait(True)
                    try:
                        self.log(f'[REG] Completed {wave_end} attempts; waiting for scheduled Kiot rotation')
                        if not self.proxy_manager.rotate_until_success(
                            reason=f'Scheduled rotation after {wave_end} attempts'
                        ):
                            if self.should_stop():
                                break
                            raise RuntimeError('Kiot rotation failed; next registration blocked.')
                        if self.should_stop():
                            break
                        tempmail.proxies = self.proxy_manager.requests_proxies('register')
                    finally:
                        if on_rotation_wait:
                            on_rotation_wait(False)
            elif not rotate_after_failed_wave(wave_results, wave_end):
                break

        summary = AutoRegisterSummary(
            total=count,
            success=success_count["value"],
            failed=failed_count["value"],
            output_path=str(output_path_obj),
            results=results,
            stopped=self.should_stop(),
        )
        self.log(
            f"[REG] Done: ok={summary.success}, failed={summary.failed}, "
            f"total={summary.total}, output={output_path_obj}"
        )
        return summary

    # --------------------------------------------------------------- per-account

    def _register_one(
        self,
        *,
        tempmail: TempMailClient,
        worker_index: int,
        attempt: int,
        total: int,
        prefer_edu: bool,
        domain: str,
        verify_timeout_sec: int,
        wait_for_captcha: bool,
        save_cookies: bool,
        check_quota_after_register: bool = False,
        retry_count: int = 2,
    ) -> AutoRegisterResult:
        result = AutoRegisterResult(started_at=datetime.now().isoformat(timespec="seconds"))
        try:
            email_data = tempmail.create_email(
                worker_index, domain=domain, prefer_edu=prefer_edu
            )
        except Exception as exc:
            result.error = f"create_email: {exc}"
            result.status = "stopped" if self.should_stop() else "failed_email"
            result.finished_at = datetime.now().isoformat(timespec="seconds")
            self.log(f"[REG:W{worker_index + 1}] Cannot create temp email: {exc}")
            self._report_proxy_error(worker_index, exc)
            return result

        result.email = email_data["email"]
        mail_id = email_data["id"]
        result.password = random_password()
        names = random_name()
        result.name = f"{names['first_name']} {names['last_name']}"
        self.log(
            f"[REG:W{worker_index + 1}] Email={result.email} | mail_id={mail_id} | "
            f"name={result.name}"
        )

        retry_count = max(0, min(5, int(retry_count)))
        max_attempts = retry_count + 1
        for attempt_index in range(max_attempts):
            if self.should_stop():
                result.status = "stopped"
                break
            if attempt_index > 0:
                self._reset_registration_attempt(result)
            failed_proxy_generation = self._proxy_generation()
            self.log(
                f"[REG:W{worker_index + 1}] Registration attempt "
                f"{attempt_index + 1}/{max_attempts} using the same mailbox"
            )
            try:
                self._run_browser_register(
                    worker_index=worker_index,
                    attempt=attempt,
                    result=result,
                    first_name=names["first_name"],
                    last_name=names["last_name"],
                    wait_for_captcha=wait_for_captcha,
                    save_cookies=save_cookies,
                    tempmail=tempmail,
                    mail_id=mail_id,
                    verify_timeout_sec=verify_timeout_sec,
                    check_quota_after_register=check_quota_after_register,
                )
                failed_proxy_generation = (
                    result.proxy_generation or failed_proxy_generation
                )
            except Exception as exc:
                if self.should_stop():
                    result.status = "stopped"
                    result.error = "Stopped by user"
                    break
                result.error = f"register: {exc}"
                result.status = result.status or "failed_register"
                self.log(f"[REG:W{worker_index + 1}] Register error: {exc}")
                if not self._is_rate_limit_text(exc):
                    self._report_proxy_error(worker_index, exc)

            if self.stop_for_rate_limit(result):break
            if result.success or result.status == "stopped" or self.should_stop():
                break
            if not self._should_retry_registration(result):
                break
            if attempt_index >= retry_count:
                result.error = (
                    f"{result.error or result.status or 'registration failed'}; "
                    f"same-mail retries exhausted ({retry_count})"
                )
                self.log(
                    f"[REG:W{worker_index + 1}] Same-mail retries exhausted "
                    f"({retry_count}); final={result.status or 'failed'}"
                )
                break

            retry_number = attempt_index + 1
            reason = result.error or result.status or "registration failed"
            rate_limited = self._is_registration_rate_limit(result)
            self.log(
                f"[REG:W{worker_index + 1}] Retrying same mailbox "
                f"({retry_number}/{retry_count}); reason={reason[:180]}"
            )
            if rate_limited:
                if not self._rotate_kiot_after_rate_limit(
                    worker_index=worker_index,
                    failed_proxy_generation=failed_proxy_generation,
                    tempmail=tempmail,
                ):
                    result.status = "stopped" if self.should_stop() else result.status
                    break
            else:
                self._report_proxy_error(worker_index, reason)
            if self.proxy_manager is not None:
                try:
                    self.proxy_manager.wait_if_rotating(
                        f"same-mail retry {retry_number}/{retry_count}"
                    )
                    tempmail.proxies = self.proxy_manager.requests_proxies("register")
                except Exception:
                    pass
            if not self._wait_interruptibly((10.0, 30.0)[min(retry_number - 1, 1)]):
                result.status = "stopped"
                break

        result.finished_at = datetime.now().isoformat(timespec="seconds")
        # Feed proxy-like failures (verify timeout, open_verify net errors, …)
        # into the proxy manager so it can rotate early and pause other workers.
        if result.error and not self._is_registration_rate_limit(result):
            self._report_proxy_error(worker_index, result.error)
        return result

    @staticmethod
    def _is_rate_limit_text(value: Any) -> bool:
        text = str(value or "").lower()
        return bool(
            re.search(r"\b(?:429|100020|164010)\b", text)
            or "too many requests" in text
            or "rate limit" in text
        )

    @classmethod
    def _is_registration_rate_limit(cls, result: AutoRegisterResult) -> bool:
        return cls._is_rate_limit_text(
            f"{result.status} {result.error}"
        )

    def _proxy_generation(self) -> int:
        if self.proxy_manager is None:
            return 0
        try:
            return int(getattr(self.proxy_manager, "proxy_generation", 0) or 0)
        except Exception:
            return 0

    def _rotate_kiot_after_rate_limit(
        self,
        *,
        worker_index: int,
        failed_proxy_generation: int,
        tempmail: TempMailClient,
    ) -> bool:
        """Rotate Kiot once for a rate-limit response, then unblock retries.

        Workers that observed the same proxy revision wait for the in-flight
        rotation. A worker that started on an older revision does not issue a
        second request after the first rotation succeeds. Kiot's existing
        cooldown and 30-second failed-request retry policy remain in charge.
        """
        manager = self.proxy_manager
        if manager is None:
            return True
        try:
            provider = str(getattr(manager.settings, "provider", "") or "").lower()
            if provider != "kiot" or not manager.enabled_for("register"):
                return True
        except Exception:
            return True

        condition = self._rate_limit_rotation_condition
        while not self.should_stop():
            with condition:
                if self._proxy_generation() > failed_proxy_generation:
                    return True
                if not self._rate_limit_rotation_in_progress:
                    self._rate_limit_rotation_in_progress = True
                    break
                self.log(
                    f"[REG:W{worker_index + 1}] Rate-limit rotation already in progress; waiting"
                )
                condition.wait(timeout=0.25)

        if self.should_stop():
            return False

        ok = False
        try:
            reason = (
                f"NoteGPT rate limit on worker {worker_index + 1} "
                f"(proxy generation {failed_proxy_generation})"
            )
            self.log(
                f"[REG:W{worker_index + 1}] NoteGPT rate limit detected; "
                f"rotating Kiot before retry"
            )
            ok = bool(
                manager.rotate_until_success(
                    reason=reason,
                    retry_interval_sec=30.0,
                )
            )
            if ok:
                try:
                    tempmail.proxies = manager.requests_proxies("register")
                except Exception:
                    pass
                self.log(
                    f"[REG:W{worker_index + 1}] Kiot rotation confirmed; retry can continue"
                )
            return ok and not self.should_stop()
        finally:
            with condition:
                self._rate_limit_rotation_in_progress = False
                condition.notify_all()

    @staticmethod
    def _reset_registration_attempt(result: AutoRegisterResult) -> None:
        """Clear transient state while retaining the allocated mailbox.

        The email, password, and generated name deliberately stay untouched;
        retrying must never allocate or consume a second mailbox.
        """
        result.status = ""
        result.verify = ""
        result.cookies = ""
        result.quota_left = 0
        result.quota_text = ""
        result.quota_status = ""
        result.error = ""
        result.proxy_generation = 0

    @staticmethod
    def _should_retry_registration(result: AutoRegisterResult) -> bool:
        """Return whether a failed registration can reasonably be retried.

        A verified account is not submitted again: at that point only the
        post-verification sign-in/quota path failed and resubmitting the same
        address could turn a transient issue into a duplicate-registration
        error. Permanent validation/authentication errors are also excluded.
        """
        if not result.email or result.verify == "verified":
            return False
        error_text = f"{result.status} {result.error}".lower()
        permanent_markers = (
            "already exists",
            "already registered",
            "email already",
            "invalid email",
            "invalid password",
            "password too weak",
            "must include",
            "domain not found",
            "unauthorized",
            "forbidden",
            "token is empty",
            "token invalid",
            "banned",
            "blocked",
            "not allowed",
        )
        return not any(marker in error_text for marker in permanent_markers)

    def _report_proxy_error(self, worker_index: int, error: Any) -> None:
        """Forward a failure to the proxy manager's error window (best effort).

        ProxyManager.report_error ignores non-proxy errors, so this is safe to
        call with any failure text. It lets a bad proxy trigger a rotate after
        the configured threshold, which in turn pauses other workers via
        wait_if_rotating before they open new tabs.
        """
        if self.continue_registration_errors or self.should_stop() or self.proxy_manager is None:
            return
        try:
            self.proxy_manager.report_error(f"REG:W{worker_index + 1}", error)
        except Exception:
            pass

    def _check_quota_for_result(self, result: AutoRegisterResult, worker_index: int) -> None:
        """Scan the freshly-registered account's quota using the saved cookies.

        Mirrors the cookie-only path used by `Scan Live Quota` (no second
        browser session, no extra quota.io request than necessary).
        """
        if not result.email:
            return

        # AccountQuotaScanner reads cookies from disk via the email's safe
        # filename, which is exactly what `_run_browser_register` just wrote.
        scanner = AccountQuotaScanner(
            self.config,
            log=self.log,
            should_stop=self.should_stop,
            proxy_manager=self.proxy_manager,
        )
        account_payload = {
            "email": result.email,
            "password": result.password,
            "cookies": "",
        }
        scan = scanner._scan_one(account_payload)
        result.quota_left = int(scan.quota_left or 0)
        result.quota_text = scan.quota_text or ""
        if scan.usable:
            result.quota_status = "OK"
        elif scan.error:
            result.quota_status = "X"
            if not result.error:
                result.error = f"quota: {scan.error}"
        else:
            result.quota_status = "EMPTY"
        self.log(
            f"[REG:W{worker_index + 1}] Quota for {result.email}: "
            f"left={result.quota_left} | {result.quota_text}"
        )

    # ------------------------------------------------------------------ browser

    def _goto_register_with_retry(
        self,
        page,
        worker_index: int,
        *,
        attempts: int = 3,
        backoff_sec: float = 2.0,
    ) -> None:
        """Open the register page, retrying ONLY this navigation on transient
        network/proxy errors (e.g. ``net::ERR_TIMED_OUT``).

        Delegates to the shared :func:`goto_with_retry` helper so the register,
        long-text and batch flows all share one retry policy. Scoped to the
        single ``page.goto`` so the parallel worker model and the rest of the
        register flow stay exactly as before; the last exception is re-raised so
        the caller still marks the account ``failed_register``.
        """
        goto_with_retry(
            page,
            NOTEGPT_REGISTER_URL,
            timeout_ms=self.config.timeout_ms,
            log=self.log,
            label=f"[REG:W{worker_index + 1}]",
            attempts=attempts,
            backoff_sec=backoff_sec,
            should_stop=self.should_stop,
        )

    def _run_browser_register(
        self,
        *,
        worker_index: int,
        attempt: int,
        result: AutoRegisterResult,
        first_name: str,
        last_name: str,
        wait_for_captcha: bool,
        save_cookies: bool,
        tempmail: TempMailClient,
        mail_id: str,
        verify_timeout_sec: int,
        check_quota_after_register: bool,
    ) -> None:
        from playwright.sync_api import sync_playwright

        # New NoteGPT signup flow no longer needs a manual captcha pause.
        # Respect the global headless setting instead of forcing headed mode.
        headless = self.config.headless

        # Pick this worker's tile so all parallel browsers fan out across
        # the screen instead of stacking on top of each other.
        layout: Optional[Dict[str, int]] = None
        if self._layouts:
            layout = self._layouts[worker_index % len(self._layouts)]

        with sync_playwright() as p, BrowserStopGuard(p, self.should_stop):
            # Don't open a browser tab while the proxy is rotating/settling.
            # This mirrors the Run/Scan flows so a bad proxy pauses workers
            # instead of spawning more tabs that immediately fail.
            if self.proxy_manager is not None:
                self.proxy_manager.wait_if_rotating(f"register W{worker_index + 1}")
            if self.should_stop():
                result.status = result.status or "stopped"
                return

            launch_kwargs: Dict[str, Any] = {
                "headless": headless,
                "slow_mo": self.config.slow_mo_ms,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if layout and not headless:
                launch_kwargs["args"].extend(
                    [
                        f"--window-position={int(layout['left'])},{int(layout['top'])}",
                        f"--window-size={int(layout['width'])},{int(layout['height'])}",
                    ]
                )
            if self.proxy_manager is not None:
                proxy = self.proxy_manager.playwright_proxy("register")
                if proxy:
                    launch_kwargs["proxy"] = proxy
                # Capture the revision after provider selection. If another
                # worker rotates while this browser is being launched, the
                # shared coordinator will still avoid a duplicate rotation.
                result.proxy_generation = self._proxy_generation()
            browser = p.chromium.launch(**launch_kwargs)
            browser_token = self._track_browser(worker_index, browser)
            try:
                viewport: Dict[str, int]
                if layout:
                    # Leave room for the Chrome UI chrome (~80px) so the page
                    # content roughly fills the tile.
                    viewport = {
                        "width": max(360, int(layout["width"])),
                        "height": max(360, int(layout["height"]) - 90),
                    }
                else:
                    viewport = {"width": 1280, "height": 820}
                context = browser.new_context(
                    accept_downloads=False,
                    viewport=viewport,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/121.0.0.0 Safari/537.36"
                    ),
                )
                page = context.new_page()
                self._track_browser(
                    worker_index,
                    browser,
                    context=context,
                    page=page,
                )
                self._goto_register_with_retry(page, worker_index)
                self._open_signup_flow(page, worker_index)
                self._fill_register_form(
                    page=page,
                    email=result.email,
                    password=result.password,
                    first_name=first_name,
                    last_name=last_name,
                )
                self._submit_and_wait_register(page, result, worker_index)
                if self.stop_for_rate_limit(result):return
                if self.should_stop():
                    result.status = "stopped"
                    return
                # A rejected/unknown submission must not start an inbox poll.
                if result.status != "needs_verify":
                    result.error = result.error or "signup did not confirm sending a verification email"
                    self.log(
                        f"[REG:W{worker_index + 1}] Signup not accepted; skipping email verification: "
                        f"{result.status} | {result.error}"
                    )
                    return

                # Verify must happen inside the same browser context so the
                # session cookies set by NoteGPT during /confirm-email are
                # actually the ones we save and use for the quota check.
                # Hitting the link with `requests` outside Playwright leaves
                # the in-memory cookies stale and quota responds with
                # "login expired".
                if not self.should_stop():
                    self._verify_in_browser(
                        page=page,
                        tempmail=tempmail,
                        mail_id=mail_id,
                        verify_timeout_sec=verify_timeout_sec,
                        worker_index=worker_index,
                        result=result,
                    )

                if result.verify == "verified" and not self.should_stop():
                    self._signin_after_verify(
                        page=page,
                        context=context,
                        result=result,
                        worker_index=worker_index,
                    )

                # Optional: scan quota using the cookies that just landed in
                # the same Playwright context after sign-in.
                if (
                    check_quota_after_register
                    and result.verify == "verified"
                    and not self.should_stop()
                ):
                    try:
                        self._check_quota_with_context_cookies(
                            context=context,
                            result=result,
                            worker_index=worker_index,
                        )
                    except Exception as exc:
                        self.log(
                            f"[REG:W{worker_index + 1}] Quota check failed: {exc}"
                        )
                        if not result.error:
                            result.error = f"quota_check: {exc}"

                if save_cookies and result.email:
                    cookies = context.cookies()
                    cookie_dir = self.config.cookie_path
                    cookie_dir.mkdir(parents=True, exist_ok=True)
                    cookie_file = cookie_dir / f"{safe_filename(result.email)}.json"
                    cookie_file.write_text(
                        json.dumps(cookies, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    result.cookies = str(cookie_file)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                self._untrack_browser(browser_token)

    def _verify_in_browser(
        self,
        *,
        page,
        tempmail: TempMailClient,
        mail_id: str,
        verify_timeout_sec: int,
        worker_index: int,
        result: AutoRegisterResult,
    ) -> None:
        verify_link = self._wait_verify_link(
            tempmail=tempmail,
            mail_id=mail_id,
            worker_index=worker_index,
            timeout_sec=verify_timeout_sec,
        )
        if not verify_link:
            result.verify = "needs_verify_no_link"
            result.error = result.error or "verify link not found in inbox"
            self.log(f"[REG:W{worker_index + 1}] Verify link not found within timeout")
            return
        try:
            page.goto(
                verify_link,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            # Some flows show a Confirm button; click it if present.
            try:
                page.evaluate(
                    """
                    () => {
                      const btn = [...document.querySelectorAll('button,a')].find(el =>
                        /confirm|verify|activate|continue|proceed|done/i.test(el.textContent || '')
                        && el.offsetParent !== null
                      );
                      if (btn) btn.click();
                    }
                    """
                )
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            self.log(
                f"[REG:W{worker_index + 1}] Verify navigated -> {page.url[:120]}"
            )
            result.verify = "verified"
        except Exception as exc:
            result.verify = "verify_failed"
            result.error = result.error or f"open_verify: {exc}"

    def _check_quota_with_context_cookies(
        self,
        *,
        context,
        result: AutoRegisterResult,
        worker_index: int,
    ) -> None:
        """Quota check using the live Playwright session cookies.

        Bypasses disk round-trip so we get a fresh post-verify session.
        Falls back to disk-cookie path inside AccountQuotaScanner if the
        live cookies somehow yield no usable quota info.
        """
        if not result.email:
            return

        live_cookies = context.cookies()
        scanner = AccountQuotaScanner(
            self.config,
            log=self.log,
            should_stop=self.should_stop,
            proxy_manager=self.proxy_manager,
        )
        # Build the same shape AccountQuotaScanner expects but skip its
        # cookie-from-disk lookup by feeding the cookies directly.
        try:
            quota = scanner._fetch_quota_with_requests(live_cookies)
        except Exception as exc:
            raise RuntimeError(f"fetch_quota: {exc}") from exc
        if quota.success:
            quota_left = quota_left_from_quota(quota)
            result.quota_left = int(quota_left or 0)
            result.quota_text = quota.to_text(self.config.quota_threshold)
            result.quota_status = "OK" if quota_left > 0 else "EMPTY"
        else:
            result.quota_status = "X"
            result.quota_text = quota.to_text(self.config.quota_threshold)
            if not result.error:
                result.error = f"quota: {quota.error}"
        self.log(
            f"[REG:W{worker_index + 1}] Quota for {result.email}: "
            f"left={result.quota_left} | {result.quota_text}"
        )

    def _fill_register_form(
        self,
        *,
        page,
        email: str,
        password: str,
        first_name: str,
        last_name: str,
    ) -> None:
        # Same selectors as injectFillForm in background.js, but evaluated
        # via Playwright instead of chrome.scripting.
        fill_script = """
            ({email, password, firstName, lastName}) => {
              function setVal(el, v) {
                const s = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value")?.set;
                if (s) s.call(el, v); else el.value = v;
                el.dispatchEvent(new Event("focus", {bubbles: true}));
                el.dispatchEvent(new Event("input", {bubbles: true}));
                el.dispatchEvent(new Event("change", {bubbles: true}));
                el.dispatchEvent(new Event("blur", {bubbles: true}));
              }
              function vis(el) { return el && el.offsetParent !== null && !el.disabled && !el.readOnly; }
              const inputs = [...document.querySelectorAll("input")].filter(vis);
              const fn = document.querySelector("input[name='first_name'],input[placeholder*='First'],input[autocomplete='given-name']") || inputs[0];
              if (fn) setVal(fn, firstName);
              const ln = document.querySelector("input[name='last_name'],input[placeholder*='Last'],input[autocomplete='family-name']") || inputs[1];
              if (ln) setVal(ln, lastName);
              const em = document.querySelector("input[type='email'],input[name='email'],input[placeholder*='Email'],input[placeholder*='email']") || inputs.find(i => i.type === "email");
              if (em) setVal(em, email);
              const pwds = [...document.querySelectorAll("input[type='password']")].filter(vis);
              if (pwds[0]) setVal(pwds[0], password);
              if (pwds[1]) setVal(pwds[1], password);
              return {filled: true, totalInputs: inputs.length, url: location.href};
            }
        """
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        try:
            page.evaluate(
                fill_script,
                {
                    "email": email,
                    "password": password,
                    "firstName": first_name,
                    "lastName": last_name,
                },
            )
        except Exception as exc:
            raise RuntimeError(f"fill_form: {exc}") from exc

    def _submit_register_form(self, page) -> None:
        submit_script = """
            () => {
              const btn = document.querySelector("button[type='submit'],input[type='submit']")
                || [...document.querySelectorAll("button")].find(b =>
                    /register|sign.?up|submit|đăng.?ký/i.test(b.textContent) && b.offsetParent);
              if (btn) { btn.click(); return true; }
              return false;
            }
        """
        try:
            ok = page.evaluate(submit_script)
            if not ok:
                raise RuntimeError("submit button not found")
        except Exception as exc:
            raise RuntimeError(f"submit_form: {exc}") from exc

    def _wait_register_outcome(self, page, result: AutoRegisterResult, worker_index: int) -> None:
        deadline = time.time() + 60
        last_url = page.url
        while time.time() < deadline:
            if self.should_stop():
                return
            try:
                cur_url = page.url
            except Exception:
                cur_url = last_url
            if cur_url and cur_url != NOTEGPT_REGISTER_URL:
                if "confirm" in cur_url.lower() or "verify" in cur_url.lower():
                    result.status = "needs_verify"
                else:
                    result.status = "submitted"
                return
            try:
                state = page.evaluate(
                    """
                    () => {
                      const t = (document.body?.innerText || '').toLowerCase();
                      return {
                        activation: t.includes('account activation') || t.includes('check your email')
                          || t.includes('we have sent') || t.includes('sent an email'),
                        urlNow: location.href,
                      };
                    }
                    """
                )
                if state and state.get("activation"):
                    result.status = "needs_verify"
                    return
            except Exception:
                pass
            self._wait_interruptibly(2)
        result.status = "submitted_unknown"

    def _signin_after_verify(
        self,
        *,
        page,
        context,
        result: AutoRegisterResult,
        worker_index: int,
    ) -> None:
        if not result.email or not result.password:
            result.status = "failed_signin"
            result.error = result.error or "missing email/password for post-verify sign in"
            return
        try:
            self.log(f"[REG:W{worker_index + 1}] Sign in after verify...")
            auth = NoteGPTAutomation(
                self.config,
                log=self.log,
                proxy_manager=self.proxy_manager,
            )
            auth._login(page, context, result.email, result.password)
            result.status = "signed_in"
            self.log(f"[REG:W{worker_index + 1}] Sign in after verify OK")
        except Exception as exc:
            result.status = "failed_signin"
            result.error = result.error or f"signin_after_verify: {exc}"
            self.log(f"[REG:W{worker_index + 1}] Sign in after verify failed: {exc}")

    def _open_signup_flow(self, page, worker_index: int) -> None:
        if self._signup_form_visible(page):
            return
        self.log(f"[REG:W{worker_index + 1}] Open Sign Up flow...")
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        email_signup_selectors = [
            "button:has-text('Sign up with Email')",
            "button:has-text('Sign Up with Email')",
            "button:has-text('Sign up with email')",
            "[role='button']:has-text('Sign up with Email')",
            "text=Sign up with Email",
        ]
        welcome_signup_selectors = [
            SIGNUP_WELCOME_BUTTON_SELECTOR,
            "button:text-is('Sign Up')",
            "button:has-text('Sign Up')",
            "[role='button']:has-text('Sign Up')",
            "text=Sign Up",
        ]

        # Route may land directly on the provider-choice modal.
        self._click_first(page, email_signup_selectors, "Sign up with Email", required=False)
        if self._wait_for_signup_form(page, timeout_sec=3):
            return

        self._click_first(page, welcome_signup_selectors, "Sign Up", required=False)
        page.wait_for_timeout(800)
        if self._wait_for_signup_form(page, timeout_sec=2):
            return

        self._click_first(page, email_signup_selectors, "Sign up with Email", required=False)
        if self._wait_for_signup_form(page, timeout_sec=6):
            return

        clicked = self._click_by_text_fallback(
            page,
            ["sign up with email", "sign up"],
            label="Sign Up fallback",
        )
        if clicked:
            if self._wait_for_signup_form(page, timeout_sec=6):
                return
            self._click_first(page, email_signup_selectors, "Sign up with Email", required=False)
            if self._wait_for_signup_form(page, timeout_sec=6):
                return
        raise RuntimeError("cannot open email sign-up form")

    def _wait_for_signup_form(self, page, timeout_sec: int = 8) -> bool:
        deadline = time.time() + max(1, timeout_sec)
        while time.time() < deadline:
            if self.should_stop():
                return False
            if self._signup_form_visible(page):
                return True
            page.wait_for_timeout(300)
        return self._signup_form_visible(page)

    def _signup_form_visible(self, page) -> bool:
        try:
            password_count = page.locator("input[type='password']").count()
            if password_count <= 0:
                return False
            text = ""
            try:
                text = page.locator("body").inner_text(timeout=1000).lower()
            except Exception:
                text = ""
            if "sign in with email" in text and "create your account" not in text:
                return False
            if (
                "create your account" not in text
                and "sign up" not in text
                and "8–16 characters" not in text
                and "8-16 characters" not in text
            ):
                return False
            email_like = page.locator(
                "input[type='email'], input[name='email'], input[placeholder*='email' i], "
                "input[autocomplete='email'], input:not([type='password'])"
            ).first
            return email_like.count() > 0
        except Exception:
            return False

    def _click_first(
        self,
        page,
        selectors: List[str],
        label: str,
        *,
        required: bool = True,
        timeout_ms: int = 5000,
    ) -> bool:
        last_error = ""
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() <= 0:
                    continue
                try:
                    if not loc.is_visible(timeout=1200):
                        continue
                except Exception:
                    pass
                try:
                    loc.scroll_into_view_if_needed(timeout=1500)
                except Exception:
                    pass
                try:
                    loc.click(timeout=timeout_ms)
                except Exception:
                    loc.click(timeout=timeout_ms, force=True)
                self.log(f"[REG] Click {label}: {selector}")
                return True
            except Exception as exc:
                last_error = str(exc)
                continue
        if required:
            raise RuntimeError(f"cannot click {label}: {last_error}")
        return False

    def _click_by_text_fallback(self, page, wanted: List[str], *, label: str) -> bool:
        try:
            clicked = page.evaluate(
                """
                (wanted) => {
                  const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'))
                    .filter(el => {
                      if (!el) return false;
                      const style = getComputedStyle(el);
                      const visible = style.display !== 'none' && style.visibility !== 'hidden'
                        && (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0);
                      return visible && !el.disabled;
                    });
                  for (const el of nodes) {
                    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                    if (wanted.some(w => text === w || text.includes(w))) {
                      el.click();
                      return text;
                    }
                  }
                  return '';
                }
                """,
                wanted,
            )
            if clicked:
                self.log(f"[REG] Click {label}: {clicked}")
                return True
        except Exception:
            pass
        return False

    def _fill_register_form(
        self,
        *,
        page,
        email: str,
        password: str,
        first_name: str,
        last_name: str,
    ) -> None:
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        email_selectors = [
            "input[type='email']",
            "input[name='email']",
            "input[placeholder*='email' i]",
            "input[autocomplete='email']",
            "div[role='dialog'] input:not([type='password'])",
            "form input:not([type='password'])",
            "input:not([type='password'])",
        ]
        password_selectors = [
            "input[type='password']",
            "input[name='password']",
            "input[placeholder*='password' i]",
            "input[autocomplete='new-password']",
            "input[autocomplete='current-password']",
        ]
        self._fill_first(page, email_selectors, email, "email")
        self._fill_first(page, password_selectors, password, "password")
        page.wait_for_timeout(500)

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
                except Exception:
                    loc.evaluate(
                        """
                        (el, nextValue) => {
                          const proto = el.tagName === 'TEXTAREA'
                            ? HTMLTextAreaElement.prototype
                            : HTMLInputElement.prototype;
                          const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                          if (setter) setter.call(el, nextValue); else el.value = nextValue;
                          el.dispatchEvent(new Event('focus', { bubbles: true }));
                          el.dispatchEvent(new Event('input', { bubbles: true }));
                          el.dispatchEvent(new Event('change', { bubbles: true }));
                          el.dispatchEvent(new Event('blur', { bubbles: true }));
                        }
                        """,
                        value,
                    )
                self.log(f"[REG] Filled {label}: {selector}")
                return
            except Exception as exc:
                last_error = str(exc)
                continue
        raise RuntimeError(f"cannot fill {label}: {last_error}")

    def _submit_register_form(self, page) -> None:
        selectors = [
            SIGNUP_SUBMIT_SELECTOR,
            "div[role='dialog'] button:has-text('Sign Up')",
            "form button:has-text('Sign Up')",
            "button:has-text('Sign Up')",
            "button[type='submit']",
            "input[type='submit']",
        ]
        if self._click_first(page, selectors, "Sign Up submit", required=False):
            return
        clicked = self._click_by_text_fallback(page, ["sign up", "submit"], label="Sign Up submit")
        if not clicked:
            raise RuntimeError("submit button not found")

    @staticmethod
    def _safe_signup_error(value: Any, result: AutoRegisterResult) -> str:
        text = str(value or "")
        for secret in (result.email, result.password):
            if secret:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"https?://\S+", "[URL REDACTED]", text)
        text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
        text = re.sub(
            r"(?i)(token|password|authorization|secret|api[_-]?key)[\"']?\s*[:=]\s*[\"']?(?:Bearer\s+)?[^\s,;\"'}]+",
            r"\1=[REDACTED]", text,
        )
        return " ".join(text.split())[:300]

    def _submit_and_wait_register(self, page, result: AutoRegisterResult, worker_index: int) -> None:
        response_details: List[str] = []

        def on_response(response) -> None:
            try:
                url = urlsplit(response.url)
                host = (url.hostname or "").lower()
                if not (host == "notegpt.io" or host.endswith(".notegpt.io")):
                    return
                if response.request.method != "POST" or not re.search(r"/api/.*/auth/.*/register/?$", url.path):
                    return
                details = f"HTTP {response.status}"
                rejected = response.status >= 400
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    rejected = rejected or payload.get("success") is False
                    for field in ("code", "message", "msg"):
                        value = payload.get(field)
                        if isinstance(value, (str, int, float, bool)):
                            details += f" | {field}={self._safe_signup_error(value, result)}"
                response_details.append(details)
                self.log(f"[REG:W{worker_index + 1}] Signup response: {details}")
                if rejected:
                    result.status = "failed_submit"
                    result.error = f"signup rejected: {details}"
            except Exception:
                # Diagnostics must not change a successful submission.
                pass

        page.on("response", on_response)
        try:
            self._submit_register_form(page)
            # The activation screen can render before Playwright dispatches the
            # matching registration response. Give the response listener a
            # short chance to record an API rejection before DOM text wins.
            page.wait_for_timeout(150)
            self._wait_register_outcome(page, result, worker_index)
            if result.status == "failed_submit" and response_details:
                if response_details[-1] not in result.error:
                    result.error += f" | {response_details[-1]}"
        finally:
            page.remove_listener("response", on_response)

    def _wait_register_outcome(self, page, result: AutoRegisterResult, worker_index: int) -> None:
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if self.should_stop():
                result.status = "stopped"
                return
            if result.status == "failed_submit":
                return
            try:
                state = page.evaluate(
                    r"""
                    () => {
                      const t = (document.body?.innerText || '').toLowerCase();
                      const errorPattern = /registration failed|sign[ -]?up failed|already exists|already registered|too weak|invalid email|invalid password|too many requests|rate limit|try again later|not allowed|not supported|not accepted|email.{0,40}(blocked|banned)|failed/i;
                      const visibleErrors = [...document.querySelectorAll('[role="alert"], [aria-live="assertive"]')]
                        .filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
                        .map(el => el.innerText.trim()).filter(Boolean);
                      const formError = visibleErrors.find(text => errorPattern.test(text))
                        || (document.body?.innerText || '').split('\n').map(line => line.trim())
                          .find(line => errorPattern.test(line)) || '';
                      return {
                        activation: t.includes('verify your email') || t.includes('an email has been sent')
                          || t.includes('check your inbox') || t.includes('click "verify"')
                          || t.includes('account activation') || t.includes('check your email')
                          || t.includes('we have sent') || t.includes('sent an email'),
                        formError,
                        urlNow: location.href,
                      };
                    }
                    """
                )
                if result.status == "failed_submit":
                    return
                if state and state.get("activation"):
                    result.status = "needs_verify"
                    return
                if state and state.get("formError") and self._signup_form_visible(page):
                    result.status = "failed_submit"
                    result.error = result.error or (
                        "signup rejected: " + self._safe_signup_error(state["formError"], result)
                    )
                    self.log(f"[REG:W{worker_index + 1}] {result.error}")
                    return
            except Exception:
                pass
            page.wait_for_timeout(1000)
        result.status = "submitted_unknown"

    def _wait_verify_link(
        self,
        *,
        tempmail: TempMailClient,
        mail_id: str,
        worker_index: int,
        timeout_sec: int,
    ) -> Optional[str]:
        deadline = time.time() + max(15, timeout_sec)
        read_attempts: Dict[str, int] = {}
        no_link_logged: set[str] = set()
        while time.time() < deadline:
            if self.should_stop():
                return None
            try:
                msgs = tempmail.list_messages(mail_id, worker_index)
            except Exception as exc:
                self.log(f"[REG:W{worker_index + 1}] list_messages error: {exc}")
                self._wait_interruptibly(5)
                continue
            for msg in msgs:
                msg_id = str(
                    msg.get("id") or msg.get("_id") or msg.get("message_id") or msg.get("mail_id") or ""
                ).strip()
                msg_key = msg_id or str(abs(hash(json.dumps(msg, ensure_ascii=False, sort_keys=True))))
                if read_attempts.get(msg_key, 0) >= 3:
                    continue
                read_attempts[msg_key] = read_attempts.get(msg_key, 0) + 1
                self.log(
                    f"[REG:W{worker_index + 1}] inbox: {msg.get('subject', '')!r} "
                    f"from {msg.get('from') or msg.get('sender')}"
                )
                detail: Dict[str, Any] = {}
                if msg_id:
                    try:
                        detail = tempmail.read_message(msg_id, worker_index)
                    except Exception as exc:
                        self.log(f"[REG:W{worker_index + 1}] read_message error: {exc}")
                        continue
                body = self._message_text_for_link(msg, detail)
                link = extract_verify_link(body)
                if link:
                    self.log(f"[REG:W{worker_index + 1}] Verify link: {link[:120]}")
                    return link
                if msg_key not in no_link_logged:
                    no_link_logged.add(msg_key)
                    preview = " ".join(_decode_mail_text(body).split())[:260]
                    detail_keys = ",".join(sorted((detail or {}).keys())[:12]) if isinstance(detail, dict) else ""
                    msg_keys = ",".join(sorted((msg or {}).keys())[:12]) if isinstance(msg, dict) else ""
                    self.log(
                        f"[REG:W{worker_index + 1}] Verify mail found but no link parsed "
                        f"(msg_keys={msg_keys}; detail_keys={detail_keys}; preview={preview!r})"
                    )
            self._wait_interruptibly(5)
        return None

    def _message_text_for_link(self, msg: Dict[str, Any], detail: Dict[str, Any]) -> str:
        parts: List[str] = []

        def add_string(value: Any) -> None:
            if isinstance(value, str) and value.strip():
                parts.append(value)

        def walk(value: Any) -> None:
            if isinstance(value, str):
                add_string(value)
            elif isinstance(value, dict):
                preferred = (
                    "html", "body", "content", "text", "message", "subject",
                    "body_html", "body_text", "html_body", "data",
                )
                for key in preferred:
                    if key in value:
                        walk(value.get(key))
                for key, item in value.items():
                    if key not in preferred:
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(msg or {})
        walk(detail or {})
        if not parts:
            parts.append(json.dumps({"msg": msg, "detail": detail}, ensure_ascii=False))
        return "\n".join(parts)

    def _open_verify_link(self, verify_link: str, worker_index: int) -> None:
        # Cheap path: hit the URL with requests so we don't pay for another
        # browser session. NoteGPT's confirm-email endpoint just needs to
        # be visited; cookies on a freshly-registered account are not
        # required for the verification itself.
        try:
            resp = interruptible_call(requests.get, self.should_stop, verify_link, allow_redirects=True, timeout=30)
            self.log(
                f"[REG:W{worker_index + 1}] Verify GET {verify_link[:80]}... -> "
                f"HTTP {resp.status_code}"
            )
        except Exception as exc:
            raise RuntimeError(f"verify_request: {exc}") from exc
