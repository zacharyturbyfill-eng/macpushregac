from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.config import AppConfig, PROJECT_ROOT, load_config
    from notegpt_quota_tool.cancellation import BrowserStopGuard, interruptible_sleep
    from notegpt_quota_tool.notegpt import (
        NOTE_GPT_HOME,
        NoteGPTAutomation,
        clean_cookies,
        goto_with_retry,
        load_cookies_from_file,
        load_cookies_from_text,
        safe_filename,
    )
    from notegpt_quota_tool.proxy_manager import ProxyManager
    from notegpt_quota_tool.sheet_api import SheetApi
else:
    from .config import AppConfig, PROJECT_ROOT, load_config
    from .cancellation import BrowserStopGuard, interruptible_sleep
    from .notegpt import (
        NOTE_GPT_HOME,
        NoteGPTAutomation,
        clean_cookies,
        goto_with_retry,
        load_cookies_from_file,
        load_cookies_from_text,
        safe_filename,
    )
    from .proxy_manager import ProxyManager
    from .sheet_api import SheetApi


CLONE_URL = "https://notegpt.io/ai-voice-cloning"

UPLOAD_AUDIO_BUTTON_SELECTOR = (
    r"#__nuxt > div.min-h-screen.w-full.bg-white.dark\:bg-muted-900 > div:nth-child(1) > "
    r"div.w-full.xl\:w-\[calc\(100\%-\(var\(--tairo-sidebar-icon-width\)\+var\(--tairo-sidebar-subsidebar-width\)\)\)\]."
    r"xl\:ms-\[calc\(var\(--tairo-sidebar-icon-width\)\+var\(--tairo-sidebar-subsidebar-width\)\)\].bg-muted-50."
    r"dark\:bg-muted-900.transition-\[width\,margin\].duration-200.min-h-screen.relative > div:nth-child(3) > "
    r"section > div.w-full > div > div:nth-child(2) > div > div > div.flex.items-center.gap-6.flex-col.md\:flex-row > button"
)
UPLOAD_AUDIO_BUTTON_SELECTOR_V12 = (
    r"#__nuxt > div.min-h-screen.w-full.bg-white.dark\:bg-muted-900."
    r"\[--tairo-sidebar-subsidebar-width\:0px\].\[--tairo-sidebar-icon-width\:230px\] > "
    r"div:nth-child(1) > div.w-full.xl\:w-\[calc\(100\%-\(var\(--tairo-sidebar-icon-width\)\)\)\]."
    r"xl\:ms-\[calc\(var\(--tairo-sidebar-icon-width\)\)\].bg-muted-50.dark\:bg-muted-900."
    r"transition-\[width\,margin\].duration-200.min-h-screen.relative > div:nth-child(3) > "
    r"section > div.w-full > div > div:nth-child(2) > div > div > "
    r"div.flex.items-center.gap-6.flex-col.md\:flex-row > button"
)

TEXT_TO_PREVIEW_SELECTOR = (
    r"#__nuxt > div.min-h-screen.w-full.bg-white.dark\:bg-muted-900 > div:nth-child(1) > "
    r"div.w-full.xl\:w-\[calc\(100\%-\(var\(--tairo-sidebar-icon-width\)\+var\(--tairo-sidebar-subsidebar-width\)\)\)\]."
    r"xl\:ms-\[calc\(var\(--tairo-sidebar-icon-width\)\+var\(--tairo-sidebar-subsidebar-width\)\)\].bg-muted-50."
    r"dark\:bg-muted-900.transition-\[width\,margin\].duration-200.min-h-screen.relative > div:nth-child(3) > "
    r"section > div.w-full > div > div.w-full.py-4 > div > div.h-10.px-4.text-sm.flex.items-center.justify-center."
    r"cursor-pointer.bg-primary-500\/15.rounded-lg.text-primary-500"
)

TEXT_PREVIEW_SAVE_SELECTOR = (
    r"#reka-dialog-content-v-0-58 > div.flex-1.p-4.sm\:p-6.overflow-y-auto.border-0.sm\:pt-2 > "
    r"div.flex.items-center.justify-end.mt-5 > button.font-medium.inline-flex.items-center.disabled\:cursor-not-allowed."
    r"aria-disabled\:cursor-not-allowed.disabled\:opacity-75.aria-disabled\:opacity-75.transition-colors.py-1\.5."
    r"gap-1\.5.bg-primary.hover\:bg-primary\/75.active\:bg-primary\/75.disabled\:bg-primary.aria-disabled\:bg-primary."
    r"focus-visible\:outline-2.focus-visible\:outline-offset-2.focus-visible\:outline-primary.text-white.h-8.rounded-lg.text-sm.px-5"
)

CLONE_VOICE_BUTTON_SELECTOR = (
    r"#__nuxt > div.min-h-screen.w-full.bg-white.dark\:bg-muted-900 > div:nth-child(1) > "
    r"div.w-full.xl\:w-\[calc\(100\%-\(var\(--tairo-sidebar-icon-width\)\+var\(--tairo-sidebar-subsidebar-width\)\)\)\]."
    r"xl\:ms-\[calc\(var\(--tairo-sidebar-icon-width\)\+var\(--tairo-sidebar-subsidebar-width\)\)\].bg-muted-50."
    r"dark\:bg-muted-900.transition-\[width\,margin\].duration-200.min-h-screen.relative > div:nth-child(3) > "
    r"section > div.w-full > div > button"
)


@dataclass
class CloneProbeResult:
    email: str
    success: bool
    output_path: str = ""
    mp3_url: str = ""
    candidates: List[str] = field(default_factory=list)
    error: str = ""


def clamp_preview_text(text: str, max_chars: int = 285) -> str:
    text = " ".join((text or "").split())
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    for pattern in (r"[.!?…。！？]\s", r"[,;:，；：]\s", r"\s"):
        matches = list(re.finditer(pattern, window))
        if matches:
            cut = matches[-1].end()
            return window[:cut].strip()
    return window.strip()


def find_mp3_urls(data: Any) -> List[str]:
    found: List[str] = []
    if isinstance(data, str):
        if ".mp3" in data.lower():
            found.extend(re.findall(r"https?://[^\s\"'<>]+?\.mp3(?:\?[^\s\"'<>]*)?", data))
            if data.startswith("/") and data.lower().endswith(".mp3"):
                found.append(data)
    elif isinstance(data, dict):
        for value in data.values():
            found.extend(find_mp3_urls(value))
    elif isinstance(data, list):
        for item in data:
            found.extend(find_mp3_urls(item))
    return list(dict.fromkeys(found))


class CloneProbeRunner:
    def __init__(
        self,
        config: AppConfig,
        log=None,
        should_stop: Optional[Callable[[], bool]] = None,
        proxy_manager: Optional[ProxyManager] = None,
    ):
        self.config = config
        self.log = log or print
        self.should_stop = should_stop or (lambda: False)
        self.proxy_manager = proxy_manager or ProxyManager(config, log=self.log, should_stop=self.should_stop)

    def _raise_if_stopped(self) -> None:
        if self.should_stop():
            raise RuntimeError("stopped by user")

    def run(
        self,
        account: Dict[str, Any],
        audio_path: str | Path,
        preview_text: str,
        *,
        language: str = "Vietnamese",
        output_dir: str | Path = "data/clone_outputs",
        keep_open: bool = True,
        timeout_sec: int = 240,
    ) -> CloneProbeResult:
        email = (account.get("email") or "").strip()
        password = (account.get("password") or "").strip()
        audio = Path(audio_path).expanduser().resolve()
        if not email:
            return CloneProbeResult(email=email, success=False, error="missing email")
        if not audio.exists():
            return CloneProbeResult(email=email, success=False, error=f"audio file not found: {audio}")

        text = clamp_preview_text(preview_text)
        if not text:
            return CloneProbeResult(email=email, success=False, error="preview text is empty")
        if len(text) > 300:
            return CloneProbeResult(email=email, success=False, error="preview text must be <= 300 chars")

        out_dir = Path(output_dir)
        if not out_dir.is_absolute():
            out_dir = PROJECT_ROOT / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)

        self.log(f"[CLONE:{email}] Start probe | text={len(text)} chars | audio={audio.name}")
        auth = NoteGPTAutomation(self.config, log=self.log, proxy_manager=self.proxy_manager)
        cookie_file = self.config.cookie_path / f"{safe_filename(email)}.json"
        cookie_candidates = []
        if self.config.reuse_cookies:
            cookie_candidates.extend(load_cookies_from_text(account.get("cookies", "")))
            cookie_candidates.extend(load_cookies_from_file(cookie_file))

        mp3_candidates: List[str] = []
        api_candidates: List[str] = []
        capture_after_clone = {"enabled": False}

        with sync_playwright() as p, BrowserStopGuard(p, self.should_stop):
            self.proxy_manager.wait_if_rotating(f"clone {email}")
            launch_kwargs = {
                "headless": self.config.headless,
                "slow_mo": self.config.slow_mo_ms,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            proxy = self.proxy_manager.playwright_proxy("clone")
            if proxy:
                launch_kwargs["proxy"] = proxy
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                accept_downloads=True,
                viewport={"width": 1366, "height": 768},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()

            def on_response(response) -> None:
                try:
                    if not capture_after_clone["enabled"]:
                        return
                    url = response.url
                    headers = response.headers
                    content_type = headers.get("content-type", "")
                    if self._is_direct_mp3_url(url) or "audio/mpeg" in content_type.lower():
                        if url not in mp3_candidates:
                            mp3_candidates.append(url)
                            self.log(f"[CLONE:{email}] MP3 candidate: {url}")
                    if "application/json" in content_type.lower() and "notegpt.io" in url:
                        try:
                            for candidate in find_mp3_urls(response.json()):
                                if self._is_direct_mp3_url(candidate) and candidate not in api_candidates:
                                    api_candidates.append(candidate)
                                    self.log(f"[CLONE:{email}] MP3 in API: {candidate}")
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                cookies = clean_cookies(cookie_candidates)
                if cookies:
                    context.add_cookies(cookies)
                    self.log(f"[CLONE:{email}] Loaded {len(cookies)} cookies")

                goto_with_retry(
                    page,
                    NOTE_GPT_HOME,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[CLONE:{email}]",
                    should_stop=self.should_stop,
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                quota = auth._fetch_quota(context)
                if not quota.success:
                    if not password:
                        raise RuntimeError(f"cookie invalid and missing password: {quota.error}")
                    auth._login(page, context, email, password)
                    quota = auth._fetch_quota(context)
                if not quota.success or not quota.quota_ok(0):
                    raise RuntimeError(f"quota not available: {quota.to_text(self.config.quota_threshold)}")
                self.log(f"[CLONE:{email}] Quota before clone: {quota.to_text(self.config.quota_threshold)}")

                page.goto(CLONE_URL, wait_until="domcontentloaded", timeout=self.config.timeout_ms)
                self._upload_audio(page, audio)
                self._open_text_preview(page)
                self._configure_text_preview(page, text, language)

                mp3_candidates.clear()
                api_candidates.clear()
                capture_after_clone["enabled"] = True
                before_click = time.time()
                self._click_clone_voice(page)
                output_path = out_dir / f"clone_probe_{safe_filename(email)}_{datetime.now():%Y%m%d_%H%M%S}.mp3"
                mp3_url = self._wait_and_download_mp3(
                    context,
                    page,
                    mp3_candidates,
                    api_candidates,
                    before_click,
                    timeout_sec,
                    output_path,
                )

                cookies_out = auth._get_all_cookies(context, page)
                if self.config.save_cookies and cookies_out:
                    cookie_file.write_text(json.dumps(cookies_out, ensure_ascii=False, indent=2), encoding="utf-8")

                if keep_open and not self.config.headless:
                    self.log(f"[CLONE:{email}] Browser kept open for review. Close it manually when done.")
                    self._wait_until_browser_closed(page)

                return CloneProbeResult(
                    email=email,
                    success=True,
                    output_path=str(output_path),
                    mp3_url=mp3_url,
                    candidates=list(dict.fromkeys(mp3_candidates + api_candidates)),
                )
            except Exception as exc:
                self.log(f"[CLONE:{email}] Error: {exc}")
                self.proxy_manager.report_error(f"CLONE:{email}", exc)
                if keep_open and not self.config.headless:
                    self.log(f"[CLONE:{email}] Browser kept open after error for debugging.")
                    self._wait_until_browser_closed(page)
                return CloneProbeResult(
                    email=email,
                    success=False,
                    error=str(exc),
                    candidates=list(dict.fromkeys(mp3_candidates + api_candidates)),
                )
            finally:
                try:
                    if not page.is_closed():
                        context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

    def _upload_audio(self, page, audio: Path) -> None:
        self._raise_if_stopped()
        self.log("[CLONE] Upload audio...")
        # Remember which audio is currently being uploaded so the proxy-only
        # sanity check before "Clone Voice" can verify the right file landed.
        self._last_audio_for_sanity = audio
        if self._proxy_active() and self._strict_upload_button_enabled():
            if self._upload_audio_via_button(page, audio, log_prefix="[CLONE] Upload"):
                self._wait_upload_ready(page, audio)
                return

        try:
            input_file = page.locator("input[type='file']").first
            if input_file.count() > 0:
                self._raise_if_stopped()
                input_file.set_input_files(str(audio), timeout=15000)
                self._wait_upload_ready(page, audio)
                return
        except Exception:
            pass

        if self._upload_audio_via_button(page, audio, log_prefix="[CLONE] Upload"):
            self._wait_upload_ready(page, audio)
            return
        raise RuntimeError("cannot upload audio: no upload entry point available")

    def _proxy_active(self) -> bool:
        """True when the run is going through the configured clone proxy.

        The strict upload detection + auto-reupload flow is gated on this so
        the legacy behavior is preserved 1:1 when running without a proxy.
        """
        try:
            return bool(self.proxy_manager and self.proxy_manager.enabled_for("clone"))
        except Exception:
            return False

    def _probe_upload_state(self, page, audio_name: str) -> Dict[str, Any]:
        """Read multiple upload-state signals in one DOM round-trip.

        Returned keys are all booleans (plus a small text sample for debug):
        - hasFileName: page text contains the audio file name
        - hasAudioSrc: an <audio>/<source> element has a real src (http/blob)
        - hasUploadAgain: a button labelled "Upload Again"/"Re-upload" exists
        - hasUploadHint: the upload zone still shows the default hint text
          ("Upload MP3, M4A, or WAV file" / "Upload a Audio" / "Upload Audio"
          / "Drop file" / "Must be 10s-5min")
        - loading: spinner or text such as "uploading"/"processing"
        - inputHasFile: client-side <input type=file> populated
        """
        try:
            return page.evaluate(
                """
                (audioName) => {
                    const visibleText = Array.from(document.querySelectorAll('body *'))
                        .filter(el => el.offsetParent !== null)
                        .map(el => (el.innerText || el.textContent || '').trim())
                        .filter(Boolean)
                        .join('\\n')
                        .toLowerCase();

                    const audioSrcOk = Array.from(document.querySelectorAll('audio,source'))
                        .map(el => el.src || '')
                        .some(src => {
                            const lower = (src || '').toLowerCase();
                            return lower.startsWith('http') || lower.startsWith('blob:');
                        });

                    const reuploadButton = Array.from(document.querySelectorAll('button,a,[role="button"]'))
                        .map(el => (el.innerText || el.textContent || '').trim().toLowerCase())
                        .some(text =>
                            text.includes('upload again') ||
                            text.includes('re-upload') ||
                            text.includes('reupload') ||
                            text.includes('change audio') ||
                            text.includes('change file')
                        );

                    const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'));
                    const inputHasFile = fileInputs.some(inp => inp.files && inp.files.length > 0);

                    const hasUploadHint =
                        visibleText.includes('upload mp3, m4a') ||
                        visibleText.includes('upload mp3') ||
                        visibleText.includes('upload a audio') ||
                        visibleText.includes('upload audio') ||
                        visibleText.includes('drop file') ||
                        visibleText.includes('drag and drop') ||
                        visibleText.includes('must be 10s-5min') ||
                        visibleText.includes('max 20mb');

                    const loading =
                        visibleText.includes('uploading') ||
                        visibleText.includes('processing') ||
                        visibleText.includes('please wait') ||
                        document.querySelectorAll('[role="progressbar"], .loading, [class*="spinner"]').length > 0;

                    const hasFileName = !!(audioName && visibleText.includes(audioName));

                    return {
                        hasFileName,
                        hasAudioSrc: audioSrcOk,
                        hasUploadAgain: reuploadButton,
                        hasUploadHint,
                        loading,
                        inputHasFile,
                    };
                }
                """,
                audio_name,
            ) or {}
        except Exception:
            return {}

    def _wait_upload_ready(self, page, audio: Path, timeout_sec: int = 180) -> None:
        if self._proxy_active():
            per_attempt, max_retries = self._strict_upload_settings()
            self._wait_upload_ready_strict(
                page,
                audio,
                per_attempt_sec=per_attempt,
                max_retries=max_retries,
            )
            return
        # Not running through a proxy: keep the legacy fast path so any
        # existing flow that already works does not change behavior.
        self._wait_upload_ready_legacy(page, audio, timeout_sec)

    def _strict_upload_settings(self) -> tuple[int, int]:
        """Read per-attempt seconds and max retries from ui_settings.

        Defaults are chosen to be snappy so a stuck upload escalates to a page
        reload quickly. User-tunable through Settings -> Proxy panel.
        """
        settings = getattr(self.config, "ui_settings", {}) or {}

        def _to_int(value: Any, default: int, minimum: int, maximum: int) -> int:
            try:
                return max(minimum, min(maximum, int(str(value).strip())))
            except Exception:
                return default

        per_attempt = _to_int(
            settings.get("proxy_upload_wait_sec", "30"),
            default=30,
            minimum=10,
            maximum=300,
        )
        max_retries = _to_int(
            settings.get("proxy_upload_retries", "2"),
            default=2,
            minimum=1,
            maximum=5,
        )
        return per_attempt, max_retries

    def _strict_upload_button_enabled(self) -> bool:
        """Opt-in path: use the visible upload button before raw file input.

        Kept behind a UI setting so the current stable upload flow remains the
        default. Useful when NoteGPT's upload widget ignores direct
        input[type=file] changes behind a proxy.
        """
        settings = getattr(self.config, "ui_settings", {}) or {}
        value = settings.get("proxy_upload_use_button", False)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _upload_button_selectors(self, *, reupload: bool = False) -> List[str]:
        base = [
            UPLOAD_AUDIO_BUTTON_SELECTOR_V12,
            UPLOAD_AUDIO_BUTTON_SELECTOR,
            "button:has-text('Upload a Audio')",
            "button:has-text('Upload Audio')",
            "text=Upload a Audio",
            "text=Upload Audio",
        ]
        if reupload:
            return [
                "button:has-text('Upload Again')",
                "button:has-text('Re-upload')",
                "button:has-text('Reupload')",
                "button:has-text('Change Audio')",
                "text=Upload Again",
                "text=Re-upload",
                *base,
            ]
        return [*base, "button:has-text('Upload Again')"]

    def _upload_audio_via_button(
        self,
        page,
        audio: Path,
        *,
        reupload: bool = False,
        log_prefix: str = "[CLONE] Upload",
    ) -> bool:
        last_error = ""
        for selector in self._upload_button_selectors(reupload=reupload):
            try:
                self._raise_if_stopped()
                with page.expect_file_chooser(timeout=10000) as chooser_info:
                    page.locator(selector).first.click(timeout=8000)
                self._raise_if_stopped()
                chooser_info.value.set_files(str(audio))
                self.log(f"{log_prefix} via upload button: {selector}")
                return True
            except Exception as exc:
                last_error = str(exc)
                continue
        if last_error:
            self.log(f"{log_prefix} button path unavailable: {last_error[:160]}")
        return False

    def _wait_upload_ready_legacy(self, page, audio: Path, timeout_sec: int = 180) -> None:
        self.log("[CLONE] Waiting for upload to finish...")
        deadline = time.time() + timeout_sec
        last_log = 0.0
        audio_name = audio.name.lower()
        while time.time() < deadline:
            self._raise_if_stopped()
            # Nếu trang đã đóng thì thoát ngay tránh lỗi vô nghĩa
            try:
                if page.is_closed():
                    return
            except Exception:
                return
            elapsed = timeout_sec - int(deadline - time.time())
            if elapsed - last_log >= 5:
                last_log = elapsed
                self.log(f"[CLONE] Upload still settling... {elapsed}s")

            try:
                state = page.evaluate(
                    """
                    (audioName) => {
                        const visibleText = Array.from(document.querySelectorAll('body *'))
                            .filter(el => el.offsetParent !== null)
                            .map(el => (el.innerText || el.textContent || '').trim())
                            .filter(Boolean)
                            .join('\\n')
                            .toLowerCase();
                        const fileInputs = Array.from(document.querySelectorAll('input[type="file"]'));
                        const inputHasFile = fileInputs.some(inp => inp.files && inp.files.length > 0);
                        const uploadOk =
                            visibleText.includes('upload successfully') ||
                            visibleText.includes('upload successful') ||
                            visibleText.includes('uploaded successfully') ||
                            visibleText.includes('upload again') ||
                            (audioName && visibleText.includes(audioName));
                        const loading =
                            visibleText.includes('uploading') ||
                            visibleText.includes('processing') ||
                            visibleText.includes('please wait');
                        return {uploadOk, inputHasFile, loading, text: visibleText.slice(0, 2000)};
                    }
                    """,
                    audio_name,
                )
                if state.get("uploadOk") and not state.get("loading"):
                    self.log("[CLONE] Upload ready")
                    page.wait_for_timeout(800)
                    return
                if state.get("inputHasFile") and not state.get("loading"):
                    for selector in ("button:has-text('Upload Again')", "text=Upload Again"):
                        try:
                            if page.locator(selector).first.count() > 0:
                                self.log("[CLONE] Upload ready")
                                page.wait_for_timeout(800)
                                return
                        except Exception:
                            pass
                    # NoteGPT đôi khi hiển thị waveform + Clone Voice button thay vì "Upload Again"
                    # → nếu Clone Voice button đã enabled thì coi như upload xong
                    if not state.get("loading"):
                        for clone_sel in (
                            "button:has-text('Clone Voice')",
                            "button:has-text('Clone')",
                            CLONE_VOICE_BUTTON_SELECTOR,
                        ):
                            try:
                                loc = page.locator(clone_sel).first
                                if loc.count() > 0:
                                    btn_state = loc.evaluate(
                                        "(el) => !el.disabled && "
                                        "el.getAttribute('aria-disabled') !== 'true' && "
                                        "window.getComputedStyle(el).pointerEvents !== 'none'"
                                    )
                                    if btn_state:
                                        self.log("[CLONE] Upload ready (Clone Voice button enabled)")
                                        page.wait_for_timeout(800)
                                        return
                            except Exception:
                                pass
            except Exception:
                pass
            page.wait_for_timeout(1000)
        raise RuntimeError("upload did not finish before timeout")

    def _wait_upload_ready_strict(
        self,
        page,
        audio: Path,
        total_timeout_sec: int = 240,
        per_attempt_sec: int = 60,
        max_retries: int = 3,
    ) -> None:
        """Strict upload detection used only when running through a proxy.

        It uses real signals (file name, real audio src, "Upload Again" button)
        and ignores the Clone Voice button state because that button stays
        enabled even before any audio is uploaded.

        Retry escalation when signals do not appear within `per_attempt_sec`:
          attempt 1: re-upload via input[type=file] (cheap)
          attempt 2: re-upload via input[type=file] again
          attempt 3 (last): reload the page + upload again from a fresh state
        After the last retry still fails, raises so the chunk is marked failed.
        """
        self.log(
            f"[CLONE] Waiting for upload to finish (strict, proxy mode, "
            f"per_attempt={per_attempt_sec}s, retries={max_retries})..."
        )
        audio_name = audio.name.lower()
        overall_deadline = time.time() + max(total_timeout_sec, per_attempt_sec * (max_retries + 1))
        attempt = 0
        while True:
            attempt_deadline = min(overall_deadline, time.time() + per_attempt_sec)
            last_log = 0.0
            while time.time() < attempt_deadline:
                self._raise_if_stopped()
                try:
                    if page.is_closed():
                        return
                except Exception:
                    return
                elapsed = int(per_attempt_sec - (attempt_deadline - time.time()))
                if elapsed - last_log >= 5:
                    last_log = elapsed
                    self.log(f"[CLONE] Upload still settling... {elapsed}s (attempt {attempt + 1})")

                state = self._probe_upload_state(page, audio_name)
                strong_signal = bool(
                    state.get("hasFileName")
                    or state.get("hasAudioSrc")
                    or state.get("hasUploadAgain")
                )
                hint_gone = not state.get("hasUploadHint")
                not_loading = not state.get("loading")
                if strong_signal and hint_gone and not_loading:
                    reason = []
                    if state.get("hasFileName"):
                        reason.append("file name in DOM")
                    if state.get("hasAudioSrc"):
                        reason.append("audio src present")
                    if state.get("hasUploadAgain"):
                        reason.append("Upload Again button")
                    self.log(f"[CLONE] Upload ready ({', '.join(reason) or 'strict signals'})")
                    page.wait_for_timeout(800)
                    return
                page.wait_for_timeout(1000)

            # Attempt window exhausted without a strong signal.
            if attempt >= max_retries or time.time() >= overall_deadline:
                raise RuntimeError(
                    "upload did not finish before timeout (strict check)"
                )
            attempt += 1
            # Escalation strategy:
            # - earlier retries: try set_input_files on the same page (cheap)
            # - last retry: reload the page so the upload zone resets, then
            #   re-upload from a fresh state. This is the kill-switch for the
            #   case where the page got into a bad state after a stalled upload.
            is_last_retry = attempt >= max_retries
            if is_last_retry:
                self.log(
                    f"[CLONE] Upload still not confirmed after {attempt - 1} re-upload(s); "
                    f"reloading page and uploading again (retry {attempt}/{max_retries})..."
                )
                try:
                    self._reload_and_reupload(page, audio)
                except Exception as exc:
                    self.log(f"[CLONE] Reload+re-upload failed: {exc}")
                    # Continue waiting; if signals still never arrive, the next
                    # iteration will hit the raise above with the same message.
            else:
                self.log(
                    f"[CLONE] Upload not confirmed, re-uploading audio "
                    f"(retry {attempt}/{max_retries})..."
                )
                try:
                    self._reupload_audio(page, audio)
                except Exception as exc:
                    self.log(f"[CLONE] Re-upload attempt failed: {exc}")
                    # fall through and continue waiting; the next attempt window
                    # will give a fresh per_attempt_sec budget.

    def _reupload_audio(self, page, audio: Path) -> None:
        """Trigger an audio re-upload without rebuilding the page.

        Tries the existing input[type=file] first, then any "Upload Again"
        button via a file chooser. Best-effort; raises on hard failure.
        """
        self._raise_if_stopped()
        if self._strict_upload_button_enabled():
            if self._upload_audio_via_button(
                page,
                audio,
                reupload=True,
                log_prefix="[CLONE] Re-upload",
            ):
                return

        # 1) input[type=file] is still the default stable channel
        try:
            input_file = page.locator("input[type='file']").first
            if input_file.count() > 0:
                input_file.set_input_files(str(audio), timeout=15000)
                self.log("[CLONE] Re-upload via input[type=file]")
                return
        except Exception:
            pass

        # 2) click an "Upload Again" / re-upload button and feed the chooser
        if self._upload_audio_via_button(
            page,
            audio,
            reupload=True,
            log_prefix="[CLONE] Re-upload",
        ):
            return
        raise RuntimeError("no re-upload entry point available")

    def _reload_and_reupload(self, page, audio: Path) -> None:
        """Hard reset: reload the clone page, then upload the audio again.

        Used as the last-resort retry when in-page re-uploads keep failing,
        usually because the upload zone got stuck in a bad state behind a slow
        proxy. After reload the page returns to its initial "Upload a Audio"
        state, then we set_input_files on the fresh input.
        """
        self._raise_if_stopped()
        self.log("[CLONE] Reloading clone page...")
        try:
            page.goto(
                CLONE_URL,
                wait_until="domcontentloaded",
                timeout=self.config.timeout_ms,
            )
        except Exception as exc:
            # If goto fails outright there is nothing else we can do here.
            raise RuntimeError(f"reload clone page failed: {exc}") from exc
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        self._raise_if_stopped()
        if self._strict_upload_button_enabled():
            if self._upload_audio_via_button(
                page,
                audio,
                log_prefix="[CLONE] Re-upload after reload",
            ):
                return

        # Use the same upload entry as the very first upload so we benefit
        # from its fallback chain (input[type=file] -> file chooser button).
        try:
            input_file = page.locator("input[type='file']").first
            if input_file.count() > 0:
                input_file.set_input_files(str(audio), timeout=15000)
                self.log("[CLONE] Re-upload after reload via input[type=file]")
                return
        except Exception:
            pass
        # Fallback: trigger the upload button + file chooser flow.
        if self._upload_audio_via_button(
            page,
            audio,
            log_prefix="[CLONE] Re-upload after reload",
        ):
            return
        raise RuntimeError("no upload entry point after reload")

    def _open_text_preview(self, page) -> None:
        self._raise_if_stopped()
        self.log("[CLONE] Open Text to Preview...")
        selectors = [
            TEXT_TO_PREVIEW_SELECTOR,
            "button:has-text('Text to Preview')",
            "[role='button']:has-text('Text to Preview')",
            "text=Text to Preview",
        ]
        for selector in selectors:
            try:
                self._raise_if_stopped()
                loc = page.locator(selector).first
                if loc.count() > 0:
                    loc.click(timeout=8000)
                    page.wait_for_timeout(1000)
                    if self._preview_dialog_visible(page):
                        return
            except Exception:
                continue
        raise RuntimeError("cannot open Text to Preview dialog")

    def _preview_dialog_visible(self, page) -> bool:
        selectors = [
            "div[role='dialog']:has-text('Text to Preview')",
            "div[role='dialog'] textarea",
            "div[role='dialog'] [contenteditable='true']",
            "div[role='dialog'] button:has-text('Save')",
            "[id^='reka-dialog-content'] textarea",
            "[id^='reka-dialog-content'] button:has-text('Save')",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    return True
            except Exception:
                continue
        return False

    def _configure_text_preview(self, page, text: str, language: str) -> None:
        self._raise_if_stopped()
        self.log(f"[CLONE] Configure preview: {language}, {len(text)}/300 chars")
        self._select_language(page, language)
        self._fill_preview_text(page, text)
        self._click_save_preview(page)

    def _select_language(self, page, language: str) -> None:
        if not language:
            return
        opened = False
        selectors = [
            "div[role='dialog'] [role='combobox']",
            "div[role='dialog'] button:has-text('English')",
            "div[role='dialog'] div:has-text('English')",
            "button:has-text('English')",
        ]
        for selector in selectors:
            try:
                self._raise_if_stopped()
                loc = page.locator(selector).first
                if loc.count() > 0 and loc.is_visible(timeout=1000):
                    loc.click(timeout=5000)
                    opened = True
                    page.wait_for_timeout(800)
                    break
            except Exception:
                continue

        if not opened:
            self.log("[CLONE] Language dropdown not found, continue with current language")
            return

        lang_names = [language, "Vietnamese", "Tiếng Việt", "Vietnam"]
        for name in lang_names:
            try:
                self._raise_if_stopped()
                loc = page.get_by_text(name, exact=True).last
                if loc.count() > 0:
                    loc.scroll_into_view_if_needed(timeout=3000)
                    loc.click(timeout=5000)
                    page.wait_for_timeout(500)
                    return
            except Exception:
                continue

        try:
            clicked = page.evaluate(
                """
                (names) => {
                    const wanted = names.map(n => String(n).toLowerCase());
                    const nodes = Array.from(document.querySelectorAll('*'))
                        .filter(el => el.offsetParent !== null);
                    for (const el of nodes) {
                        const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                        if (wanted.includes(text) || wanted.some(w => text.includes(w))) {
                            el.scrollIntoView({block:'center'});
                            el.click();
                            return text;
                        }
                    }
                    return '';
                }
                """,
                lang_names,
            )
            if clicked:
                page.wait_for_timeout(500)
                return
        except Exception:
            pass
        self.log(f"[CLONE] Cannot select language '{language}', continue with current language")

    def _fill_preview_text(self, page, text: str) -> None:
        self._raise_if_stopped()
        selectors = [
            "div[role='dialog'] textarea",
            "textarea",
            "div[role='dialog'] [contenteditable='true']",
            "[contenteditable='true']",
            "div[role='textbox']",
        ]
        last_error = ""
        for selector in selectors:
            try:
                self._raise_if_stopped()
                loc = page.locator(selector).first
                loc.wait_for(state="visible", timeout=5000)
                if "contenteditable" in selector or "role='textbox'" in selector:
                    loc.evaluate(
                        """
                        (el, value) => {
                            el.focus();
                            el.innerText = value;
                            el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value}));
                            el.dispatchEvent(new Event('change', {bubbles:true}));
                        }
                        """,
                        text,
                    )
                else:
                    loc.fill(text, timeout=5000)
                page.wait_for_timeout(500)
                return
            except Exception as exc:
                last_error = str(exc)
        raise RuntimeError(f"cannot fill preview text: {last_error}")

    def _click_save_preview(self, page) -> None:
        self._raise_if_stopped()
        selectors = [
            TEXT_PREVIEW_SAVE_SELECTOR,
            "div[role='dialog'] button:has-text('Save')",
            "button:has-text('Save')",
        ]
        for selector in selectors:
            try:
                self._raise_if_stopped()
                loc = page.locator(selector).first
                if loc.count() > 0:
                    loc.click(timeout=8000)
                    self._wait_preview_saved(page)
                    return
            except Exception:
                continue
        raise RuntimeError("cannot click Save in preview dialog")

    def _wait_preview_saved(self, page, timeout_sec: int = 60) -> None:
        self.log("[CLONE] Waiting for preview dialog to save...")
        deadline = time.time() + timeout_sec
        last_log = 0.0
        while time.time() < deadline:
            self._raise_if_stopped()
            elapsed = timeout_sec - int(deadline - time.time())
            if elapsed - last_log >= 5:
                last_log = elapsed
                self.log(f"[CLONE] Preview save still pending... {elapsed}s")
            if not self._preview_dialog_visible(page):
                self.log("[CLONE] Preview saved")
                page.wait_for_timeout(800)
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("preview dialog did not close after Save")

    def _click_clone_voice(self, page) -> None:
        self._raise_if_stopped()
        # Proxy-only sanity check: NoteGPT's "Clone Voice" button stays enabled
        # even before any audio is uploaded. Through a slow proxy, the upload
        # may not have reached the server yet — clicking now would queue a
        # clone request without audio and the MP3 response never arrives.
        # When running through the proxy we re-verify the upload landed before
        # clicking. Without proxy we keep the original fast path.
        if self._proxy_active():
            try:
                last_audio = getattr(self, "_last_audio_for_sanity", None)
                if last_audio is not None:
                    self._sanity_check_audio_uploaded(page, last_audio)
            except Exception:
                # Sanity check is best-effort; never block the legacy flow.
                pass
        self.log("[CLONE] Wait Clone Voice button ready...")
        loc = self._wait_clone_button_ready(page)
        self._raise_if_stopped()
        self.log("[CLONE] Click Clone Voice...")
        loc.click(timeout=10000)
        page.wait_for_timeout(1000)

    def _sanity_check_audio_uploaded(self, page, audio: Path, wait_sec: int = 12) -> None:
        """Proxy-only guard: ensure audio is actually present before Clone Voice.

        Polls the same DOM signals as the strict upload check. If the audio is
        not visible (no file name, no <audio src>, no Upload Again), wait up to
        wait_sec, otherwise abort with a clear error rather than clicking.
        """
        audio_name = audio.name.lower()
        deadline = time.time() + max(1, wait_sec)
        while time.time() < deadline:
            state = self._probe_upload_state(page, audio_name)
            if state.get("hasFileName") or state.get("hasAudioSrc") or state.get("hasUploadAgain"):
                return
            page.wait_for_timeout(1000)
        raise RuntimeError("audio not uploaded yet, refusing to click Clone Voice")

    def _wait_clone_button_ready(self, page, timeout_sec: int = 90):
        selectors = [
            CLONE_VOICE_BUTTON_SELECTOR,
            "button:has-text('Clone Voice')",
            "button:has-text('Clone')",
            "[role='button']:has-text('Clone Voice')",
        ]
        deadline = time.time() + timeout_sec
        last_log = 0.0
        last_reason = "not found"
        while time.time() < deadline:
            self._raise_if_stopped()
            elapsed = timeout_sec - int(deadline - time.time())
            if elapsed - last_log >= 5:
                last_log = elapsed
                self.log(f"[CLONE] Clone button not ready yet ({last_reason})... {elapsed}s")
            for selector in selectors:
                try:
                    loc = page.locator(selector).first
                    if loc.count() <= 0:
                        continue
                    try:
                        loc.wait_for(state="visible", timeout=1500)
                    except Exception:
                        last_reason = "not visible"
                        continue
                    state = loc.evaluate(
                        """
                        (el) => {
                            const style = window.getComputedStyle(el);
                            const text = (el.innerText || el.textContent || '').trim().toLowerCase();
                            const disabled =
                                el.disabled ||
                                el.getAttribute('disabled') !== null ||
                                el.getAttribute('aria-disabled') === 'true' ||
                                style.pointerEvents === 'none' ||
                                style.opacity === '0' ||
                                text.includes('uploading') ||
                                text.includes('processing');
                            let reason = 'ready';
                            if (el.disabled) reason = 'disabled property';
                            else if (el.getAttribute('disabled') !== null) reason = 'disabled attribute';
                            else if (el.getAttribute('aria-disabled') === 'true') reason = 'aria-disabled';
                            else if (style.pointerEvents === 'none') reason = 'pointer-events none';
                            else if (style.opacity === '0') reason = 'opacity 0';
                            else if (text.includes('uploading') || text.includes('processing')) reason = text;
                            return {ready: !disabled, reason, text};
                        }
                        """
                    )
                    if state.get("ready"):
                        return loc
                    last_reason = state.get("reason", "disabled")
                except Exception as exc:
                    last_reason = str(exc)[:80]
            page.wait_for_timeout(1000)
        raise RuntimeError(f"Clone Voice button not ready: {last_reason}")

    def _wait_and_download_mp3(
        self,
        context,
        page,
        mp3_candidates: List[str],
        api_candidates: List[str],
        started_at: float,
        timeout_sec: int,
        output_path: Path,
    ) -> str:
        self.log("[CLONE] Waiting for clone to finish and MP3 response...")
        deadline = time.time() + timeout_sec
        attempted: set[str] = set()
        min_wait_sec = 10
        last_log = 0.0
        while time.time() < deadline:
            self._raise_if_stopped()
            elapsed = time.time() - started_at
            if elapsed - last_log >= 5:
                last_log = elapsed
                self.log(f"[CLONE] Still waiting... {int(elapsed)}s")

            candidates = [
                self._absolute_mp3_url(url)
                for url in list(dict.fromkeys(api_candidates + mp3_candidates))
                if self._is_direct_mp3_url(url)
            ]
            if elapsed >= min_wait_sec:
                for url in candidates:
                    if url in attempted:
                        continue
                    attempted.add(url)
                    try:
                        self._download_mp3(context, url, output_path)
                        return url
                    except Exception as exc:
                        self.log(f"[CLONE] Candidate not ready/invalid: {exc}")

            try:
                self._raise_if_stopped()
                dom_urls = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('audio,source,a[href]'))
                        .map(el => el.src || el.href || '')
                        .filter(url => url && url.toLowerCase().includes('.mp3'))
                    """
                )
                for url in dom_urls:
                    if self._is_direct_mp3_url(url) and url not in mp3_candidates:
                        mp3_candidates.append(url)
            except Exception:
                pass
            page.wait_for_timeout(1000)
        raise RuntimeError("timeout waiting for MP3 response")

    def _is_direct_mp3_url(self, url: str) -> bool:
        value = (url or "").strip()
        if not value:
            return False
        lower = value.lower()
        if "api/v2/media-info" in lower:
            return False
        if "tmp/upload/notegpt/voice-clone" in lower:
            return False
        clean = lower.split("?", 1)[0]
        return clean.endswith(".mp3") or ".mp3/" in clean

    def _absolute_mp3_url(self, url: str) -> str:
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return "https://notegpt.io" + url
        return url

    def _download_mp3(self, context, url: str, output_path: Path) -> None:
        self._raise_if_stopped()
        self.log(f"[CLONE] Download MP3: {url}")
        resp = context.request.get(url, timeout=60000)
        if resp.status >= 400:
            raise RuntimeError(f"download mp3 failed HTTP {resp.status}")
        body = resp.body()
        if not body or len(body) < 1024:
            raise RuntimeError(f"downloaded mp3 too small: {len(body) if body else 0} bytes")
        output_path.write_bytes(body)
        self.log(f"[CLONE] Saved MP3: {output_path}")

    def _wait_until_browser_closed(self, page) -> None:
        while True:
            try:
                self._raise_if_stopped()
                if page.is_closed():
                    return
                page.wait_for_timeout(1000)
            except Exception:
                return


def pick_account(accounts: List[Dict[str, Any]], email: str = "") -> Optional[Dict[str, Any]]:
    if email:
        target = email.strip().lower()
        for account in accounts:
            if str(account.get("email", "")).strip().lower() == target:
                return account
        return None
    for account in accounts:
        if account.get("email"):
            return account
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one NoteGPT AI Voice Cloning probe account.")
    parser.add_argument("--config", default="", help="Path to config.json")
    parser.add_argument("--email", default="", help="Account email from Sheet")
    parser.add_argument("--audio", required=True, help="Audio sample path")
    parser.add_argument("--text", default="", help="Preview text <= 300 chars")
    parser.add_argument("--text-file", default="", help="Read preview text from file")
    parser.add_argument("--language", default="Vietnamese", help="Preview language")
    parser.add_argument("--output-dir", default="data/clone_outputs", help="MP3 output folder")
    parser.add_argument("--headless", action="store_true", help="Run browser hidden")
    parser.add_argument("--close-browser", action="store_true", help="Close browser after probe")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config or None)
    if args.headless:
        cfg.headless = True
    text = args.text
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    sheet = SheetApi(cfg, log=print)
    accounts = sheet.get_accounts()
    account = pick_account(accounts, args.email)
    if not account:
        print(f"Account not found: {args.email or '(first account)'}")
        return 2
    result = CloneProbeRunner(cfg, log=print).run(
        account,
        args.audio,
        text,
        language=args.language,
        output_dir=args.output_dir,
        keep_open=not args.close_browser,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
