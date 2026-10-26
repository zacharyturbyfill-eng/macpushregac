from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.clone_probe import CLONE_URL, CloneProbeRunner, find_mp3_urls
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
    from .clone_probe import CLONE_URL, CloneProbeRunner, find_mp3_urls
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


@dataclass
class LongTextRunResult:
    success: bool
    job_dir: str
    email: str
    status: str
    completed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    error: str = ""


class LongTextJobRunner:
    def __init__(
        self,
        config: AppConfig,
        log: Optional[Callable[[str], None]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        proxy_manager: Optional[ProxyManager] = None,
    ) -> None:
        self.config = config
        self.log = log or print
        self.progress = progress
        self.should_stop = should_stop or (lambda: False)
        self.proxy_manager = proxy_manager or ProxyManager(config, log=self.log, should_stop=self.should_stop)

    def run(
        self,
        account: Dict[str, Any],
        job_dir: str | Path,
        *,
        audio_path: str | Path = "",
        language: str = "",
        keep_open: bool = False,
        timeout_sec: int = 240,
        max_chunks: int = 0,
    ) -> LongTextRunResult:
        job_root, manifest_path = self._resolve_job(job_dir)
        manifest = self._load_manifest(manifest_path)
        email = (account.get("email") or "").strip()
        password = (account.get("password") or "").strip()
        if not email:
            return LongTextRunResult(False, str(job_root), email, "error", error="missing email")

        settings = manifest.get("settings", {}) if isinstance(manifest.get("settings"), dict) else {}
        audio = self._resolve_file(audio_path or settings.get("audio_path", ""))
        if not audio.exists():
            return LongTextRunResult(False, str(job_root), email, "error", error=f"audio file not found: {audio}")
        lang = (language or settings.get("language") or "Vietnamese").strip()

        chunks = [chunk for chunk in manifest.get("chunks", []) if isinstance(chunk, dict)]
        total = len(chunks)
        if not chunks:
            return LongTextRunResult(False, str(job_root), email, "error", error="job has no chunks")

        helper = CloneProbeRunner(
            self.config,
            log=self.log,
            should_stop=self.should_stop,
            proxy_manager=self.proxy_manager,
        )
        auth = NoteGPTAutomation(self.config, log=self.log, proxy_manager=self.proxy_manager)
        cookie_file = self.config.cookie_path / f"{safe_filename(email)}.json"
        cookie_candidates = []
        if self.config.reuse_cookies:
            cookie_candidates.extend(load_cookies_from_text(account.get("cookies", "")))
            cookie_candidates.extend(load_cookies_from_file(cookie_file))

        mp3_candidates: List[str] = []
        api_candidates: List[str] = []
        capture_after_clone = {"enabled": False}

        self.log(f"[JOB:{email}] Start job: {job_root}")
        self._set_manifest_status(manifest, "running", email)
        self._save_manifest(manifest_path, manifest)

        with sync_playwright() as p, BrowserStopGuard(p, self.should_stop):
            self.proxy_manager.wait_if_rotating(f"job {email}")
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
                    if helper._is_direct_mp3_url(url) or "audio/mpeg" in content_type.lower():
                        if url not in mp3_candidates:
                            mp3_candidates.append(url)
                            self.log(f"[JOB:{email}] MP3 candidate: {url}")
                    if "application/json" in content_type.lower() and "notegpt.io" in url:
                        try:
                            for candidate in find_mp3_urls(response.json()):
                                if helper._is_direct_mp3_url(candidate) and candidate not in api_candidates:
                                    api_candidates.append(candidate)
                                    self.log(f"[JOB:{email}] MP3 in API: {candidate}")
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                cookies = clean_cookies(cookie_candidates)
                if cookies:
                    context.add_cookies(cookies)
                    self.log(f"[JOB:{email}] Loaded {len(cookies)} cookies")

                goto_with_retry(
                    page,
                    NOTE_GPT_HOME,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[JOB:{email}]",
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
                if not quota.success or self._quota_left(quota) <= 0:
                    self._set_manifest_status(manifest, "paused_no_quota", email)
                    self._save_manifest(manifest_path, manifest)
                    return LongTextRunResult(
                        False,
                        str(job_root),
                        email,
                        "paused_no_quota",
                        completed=self._count_status(manifest, "done"),
                        failed=self._count_status(manifest, "failed"),
                        skipped=self._count_done_with_file(manifest, job_root),
                        total=total,
                        error=quota.to_text(self.config.quota_threshold),
                    )
                self.log(f"[JOB:{email}] Quota before job: {quota.to_text(self.config.quota_threshold)}")

                goto_with_retry(
                    page,
                    CLONE_URL,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[JOB:{email}]",
                    should_stop=self.should_stop,
                )
                helper._upload_audio(page, audio)

                completed_now = 0
                skipped = 0
                clone_page_ready = True
                for chunk in chunks:
                    if self.should_stop():
                        self._set_manifest_status(manifest, "stopped", email)
                        self._save_manifest(manifest_path, manifest)
                        return LongTextRunResult(
                            True,
                            str(job_root),
                            email,
                            "stopped",
                            completed=self._count_status(manifest, "done"),
                            failed=self._count_status(manifest, "failed"),
                            skipped=skipped,
                            total=total,
                        )
                    if max_chunks and completed_now >= max_chunks:
                        self._set_manifest_status(manifest, "paused_limit", email)
                        self._save_manifest(manifest_path, manifest)
                        return LongTextRunResult(
                            True,
                            str(job_root),
                            email,
                            "paused_limit",
                            completed=self._count_status(manifest, "done"),
                            failed=self._count_status(manifest, "failed"),
                            skipped=skipped,
                            total=total,
                        )

                    output_path = job_root / str(chunk.get("output_file") or f"outputs/chunk_{int(chunk.get('index', 0)):04d}.mp3")
                    if chunk.get("status") == "done" and output_path.exists() and output_path.stat().st_size > 1024:
                        skipped += 1
                        self._emit_progress(manifest, total)
                        continue

                    quota = auth._fetch_quota(context)
                    if not quota.success or self._quota_left(quota) <= 0:
                        self.log(f"[JOB:{email}] Stop: no quota left before chunk {int(chunk.get('index', 0)):04d}")
                        self._set_manifest_status(manifest, "paused_no_quota", email)
                        self._save_manifest(manifest_path, manifest)
                        return LongTextRunResult(
                            True,
                            str(job_root),
                            email,
                            "paused_no_quota",
                            completed=self._count_status(manifest, "done"),
                            failed=self._count_status(manifest, "failed"),
                            skipped=skipped,
                            total=total,
                        )

                    text_path = job_root / str(chunk.get("text_file") or "")
                    text = text_path.read_text(encoding="utf-8").strip()
                    if not text:
                        raise RuntimeError(f"chunk {chunk.get('index')} is empty")
                    if len(text) > 300:
                        raise RuntimeError(f"chunk {chunk.get('index')} has {len(text)} chars, over NoteGPT limit")

                    chunk_index = int(chunk.get("index") or 0)
                    self.log(f"[JOB:{email}] Chunk {chunk_index:04d}/{total} | {len(text)} chars | quota_left={self._quota_left(quota)}")
                    self._mark_chunk(manifest, chunk, "running", email=email)
                    self._save_manifest(manifest_path, manifest)

                    if not clone_page_ready:
                        self._reset_clone_page(helper, page, audio, chunk_index)
                        clone_page_ready = True

                    mp3_candidates.clear()
                    api_candidates.clear()
                    self._open_text_preview_with_reset(helper, page, audio, chunk_index)
                    helper._configure_text_preview(page, text, lang)
                    capture_after_clone["enabled"] = True
                    started_at = time.time()
                    helper._click_clone_voice(page)
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        mp3_url = helper._wait_and_download_mp3(
                            context,
                            page,
                            mp3_candidates,
                            api_candidates,
                            started_at,
                            timeout_sec,
                            output_path,
                        )
                    finally:
                        capture_after_clone["enabled"] = False

                    self._mark_chunk(
                        manifest,
                        chunk,
                        "done",
                        email=email,
                        mp3_url=mp3_url,
                        output_file=str(output_path.relative_to(job_root)).replace("\\", "/"),
                    )
                    self._save_manifest(manifest_path, manifest)
                    completed_now += 1
                    self._emit_progress(manifest, total)
                    clone_page_ready = False
                    page.wait_for_timeout(1200)

                self._set_manifest_status(manifest, "done", email)
                self._save_manifest(manifest_path, manifest)
                cookies_out = auth._get_all_cookies(context, page)
                if self.config.save_cookies and cookies_out:
                    cookie_file.write_text(json.dumps(cookies_out, ensure_ascii=False, indent=2), encoding="utf-8")

                if keep_open and not self.config.headless:
                    self.log(f"[JOB:{email}] Browser kept open for review. Close it manually when done.")
                    helper._wait_until_browser_closed(page)

                return LongTextRunResult(
                    True,
                    str(job_root),
                    email,
                    "done",
                    completed=self._count_status(manifest, "done"),
                    failed=self._count_status(manifest, "failed"),
                    skipped=skipped,
                    total=total,
                )
            except Exception as exc:
                if self.should_stop():
                    current = self._current_running_chunk(manifest)
                    if current:
                        current["status"] = "pending"
                        current["started_at"] = ""
                        current["finished_at"] = ""
                        current["error"] = ""
                    self._set_manifest_status(manifest, "stopped", email)
                    self._save_manifest(manifest_path, manifest)
                    self.log(f"[JOB:{email}] Stop requested")
                    return LongTextRunResult(
                        True,
                        str(job_root),
                        email,
                        "stopped",
                        completed=self._count_status(manifest, "done"),
                        failed=self._count_status(manifest, "failed"),
                        skipped=self._count_done_with_file(manifest, job_root),
                        total=total,
                    )
                current = self._current_running_chunk(manifest)
                if current:
                    self._mark_chunk(manifest, current, "failed", email=email, error=str(exc))
                self._set_manifest_status(manifest, "paused_error", email, error=str(exc))
                self._save_manifest(manifest_path, manifest)
                self.log(f"[JOB:{email}] Error: {exc}")
                self.proxy_manager.report_error(f"JOB:{email}", exc)
                if keep_open and not self.config.headless:
                    self.log(f"[JOB:{email}] Browser kept open after error for debugging.")
                    helper._wait_until_browser_closed(page)
                return LongTextRunResult(
                    False,
                    str(job_root),
                    email,
                    "paused_error",
                    completed=self._count_status(manifest, "done"),
                    failed=self._count_status(manifest, "failed"),
                    skipped=self._count_done_with_file(manifest, job_root),
                    total=total,
                    error=str(exc),
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

    def _reset_clone_page(self, helper: CloneProbeRunner, page, audio: Path, chunk_index: int) -> None:
        if self.should_stop():
            raise RuntimeError("stopped by user")
        self.log(f"[JOB] Reset clone page before chunk {chunk_index:04d}...")
        page.goto(CLONE_URL, wait_until="domcontentloaded", timeout=self.config.timeout_ms)
        if self.should_stop():
            raise RuntimeError("stopped by user")
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PlaywrightTimeoutError:
            pass
        if self.should_stop():
            raise RuntimeError("stopped by user")
        helper._upload_audio(page, audio)

    def _open_text_preview_with_reset(self, helper: CloneProbeRunner, page, audio: Path, chunk_index: int) -> None:
        try:
            helper._open_text_preview(page)
            return
        except Exception as exc:
            self.log(f"[JOB] Text preview not ready for chunk {chunk_index:04d}: {exc}")
        self._reset_clone_page(helper, page, audio, chunk_index)
        helper._open_text_preview(page)

    def _wait_and_download_network_mp3(
        self,
        helper: CloneProbeRunner,
        context,
        mp3_candidates: List[str],
        api_candidates: List[str],
        started_at: float,
        timeout_sec: int,
        output_path: Path,
    ) -> str:
        self.log("[JOB] Waiting for clone MP3 response...")
        deadline = time.time() + timeout_sec
        attempted: set[str] = set()
        min_wait_sec = 10
        last_log = 0.0
        while time.time() < deadline:
            if self.should_stop():
                raise RuntimeError("stopped by user")
            elapsed = time.time() - started_at
            if elapsed - last_log >= 5:
                last_log = elapsed
                self.log(f"[JOB] Still waiting... {int(elapsed)}s")

            candidates = [
                helper._absolute_mp3_url(url)
                for url in list(dict.fromkeys(api_candidates + mp3_candidates))
                if helper._is_direct_mp3_url(url)
            ]
            if elapsed >= min_wait_sec:
                for url in candidates:
                    if url in attempted:
                        continue
                    attempted.add(url)
                    try:
                        helper._download_mp3(context, url, output_path)
                        return url
                    except Exception as exc:
                        self.log(f"[JOB] Candidate not ready/invalid: {exc}")
            interruptible_sleep(1, self.should_stop)
        raise RuntimeError("timeout waiting for MP3 response")

    def _resolve_job(self, job_dir: str | Path) -> tuple[Path, Path]:
        path = Path(job_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.is_file():
            manifest_path = path
            job_root = path.parent
        else:
            job_root = path
            manifest_path = job_root / "job.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"job.json not found: {manifest_path}")
        return job_root.resolve(), manifest_path.resolve()

    def _resolve_file(self, path: str | Path) -> Path:
        value = Path(path).expanduser()
        if not value.is_absolute():
            value = PROJECT_ROOT / value
        return value.resolve()

    def _load_manifest(self, manifest_path: Path) -> Dict[str, Any]:
        with manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("invalid job manifest")
        return data

    def _save_manifest(self, manifest_path: Path, manifest: Dict[str, Any]) -> None:
        manifest.setdefault("updated_at", datetime.now().isoformat(timespec="seconds"))
        manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = manifest_path.with_name("job.json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(manifest_path)

    def _set_manifest_status(self, manifest: Dict[str, Any], status: str, email: str, error: str = "") -> None:
        manifest["status"] = status
        manifest["last_account_email"] = email
        manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
        if error:
            manifest["last_error"] = error
        totals = manifest.setdefault("progress", {})
        totals["done"] = self._count_status(manifest, "done")
        totals["failed"] = self._count_status(manifest, "failed")
        totals["pending"] = self._count_status(manifest, "pending")
        totals["running"] = self._count_status(manifest, "running")

    def _mark_chunk(
        self,
        manifest: Dict[str, Any],
        chunk: Dict[str, Any],
        status: str,
        *,
        email: str,
        mp3_url: str = "",
        output_file: str = "",
        error: str = "",
    ) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        chunk["status"] = status
        chunk["account_email"] = email
        if status == "running":
            chunk["started_at"] = now
            chunk["error"] = ""
        if status in ("done", "failed"):
            chunk["finished_at"] = now
        if mp3_url:
            chunk["mp3_url"] = mp3_url
        if output_file:
            chunk["output_file"] = output_file
        if error:
            chunk["error"] = error
        self._set_manifest_status(manifest, "running", email)

    def _current_running_chunk(self, manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for chunk in manifest.get("chunks", []):
            if isinstance(chunk, dict) and chunk.get("status") == "running":
                return chunk
        return None

    def _quota_left(self, quota) -> int:
        values = [v for v in (quota.basic_left, quota.premium_left, quota.premium_sub_left) if v is not None]
        return max(values) if values else 0

    def _count_status(self, manifest: Dict[str, Any], status: str) -> int:
        return sum(1 for chunk in manifest.get("chunks", []) if isinstance(chunk, dict) and chunk.get("status") == status)

    def _count_done_with_file(self, manifest: Dict[str, Any], job_root: Path) -> int:
        count = 0
        for chunk in manifest.get("chunks", []):
            if not isinstance(chunk, dict) or chunk.get("status") != "done":
                continue
            output_path = job_root / str(chunk.get("output_file") or "")
            if output_path.exists() and output_path.stat().st_size > 1024:
                count += 1
        return count

    def _emit_progress(self, manifest: Dict[str, Any], total: int) -> None:
        if self.progress:
            self.progress(self._count_status(manifest, "done"), total)


def _pick_account(accounts: List[Dict[str, Any]], email: str) -> Optional[Dict[str, Any]]:
    target = email.strip().lower()
    for account in accounts:
        if str(account.get("email", "")).strip().lower() == target:
            return account
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a NoteGPT long text job with one account.")
    parser.add_argument("--job-dir", required=True, help="Job directory containing job.json")
    parser.add_argument("--email", required=True, help="Account email from Sheet")
    parser.add_argument("--audio", default="", help="Audio sample path, overrides job setting")
    parser.add_argument("--language", default="", help="Language, overrides job setting")
    parser.add_argument("--config", default="", help="Path to config.json")
    parser.add_argument("--headless", action="store_true", help="Run browser hidden")
    parser.add_argument("--keep-open", action="store_true", help="Keep browser open after run")
    parser.add_argument("--max-chunks", type=int, default=0, help="Optional test limit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config or None)
    if args.headless:
        cfg.headless = True
    accounts = SheetApi(cfg, log=print).get_accounts()
    account = _pick_account(accounts, args.email)
    if not account:
        print(f"Account not found: {args.email}")
        return 2
    result = LongTextJobRunner(cfg, log=print).run(
        account,
        args.job_dir,
        audio_path=args.audio,
        language=args.language,
        keep_open=args.keep_open,
        max_chunks=args.max_chunks,
    )
    print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
