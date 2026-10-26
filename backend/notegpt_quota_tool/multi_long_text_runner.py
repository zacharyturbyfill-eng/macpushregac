from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.clone_probe import CLONE_URL, CloneProbeRunner, find_mp3_urls
    from notegpt_quota_tool.config import AppConfig, PROJECT_ROOT
    from notegpt_quota_tool.cancellation import BrowserStopGuard, interruptible_sleep
    from notegpt_quota_tool.long_text_job import JobQueue
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
    from notegpt_quota_tool.quota_scanner import AccountQuotaScan, AccountQuotaScanner, quota_left_from_quota
else:
    from .clone_probe import CLONE_URL, CloneProbeRunner, find_mp3_urls
    from .config import AppConfig, PROJECT_ROOT
    from .cancellation import BrowserStopGuard, interruptible_sleep
    from .long_text_job import JobQueue
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
from .quota_scanner import AccountQuotaScan, AccountQuotaScanner, quota_left_from_quota


FAST_REUSE_POPUP_ROOT_SELECTOR = (
    r"body > div.bg-default.divide-y.divide-default.flex.flex-col.focus\:outline-none"
    r".data-\[state\=open\]\:animate-\[scale-in_200ms_ease-out\]"
    r".data-\[state\=closed\]\:animate-\[scale-out_200ms_ease-in\]"
    r".w-\[calc\(100vw-2rem\)\].max-w-lg.ring.ring-default.fixed.top-1\/2.left-1\/2"
    r".-translate-x-1\/2.-translate-y-1\/2.max-h-\[calc\(100dvh-2rem\)\]"
    r".sm\:max-h-\[calc\(100dvh-4rem\)\].overflow-hidden.rounded-2xl"
    r".shadow-none.sm\:w-160.z-42"
)
FAST_REUSE_POPUP_BODY_DISMISS_SELECTOR = (
    FAST_REUSE_POPUP_ROOT_SELECTOR
    + r" > div.flex-1.p-4.overflow-y-auto.border-0.px-5.pb-5.sm\:p-0.sm\:pb-10"
    + r".sm\:w-100.mx-auto > button.inline-flex.items-center.disabled\:cursor-not-allowed"
    + r".aria-disabled\:cursor-not-allowed.disabled\:opacity-75.aria-disabled\:opacity-75"
    + r".transition-colors.px-2\.5.py-1\.5.gap-1\.5.ring.ring-inset.ring-accented"
    + r".bg-default.hover\:bg-elevated.active\:bg-elevated.disabled\:bg-default"
    + r".aria-disabled\:bg-default.focus\:outline-none.focus-visible\:ring-2"
    + r".focus-visible\:ring-inverted.rounded-lg.text-sm.h-10.w-full.justify-center"
    + r".text-muted-500.dark\:text-muted-400.font-normal"
)
FAST_REUSE_POPUP_HEADER_CLOSE_SELECTOR = (
    FAST_REUSE_POPUP_ROOT_SELECTOR
    + r" > div.flex.items-center.gap-1\.5.p-4.sm\:px-6.min-h-16.border-0 > button"
)


@dataclass
class MultiWorkerResult:
    email: str
    success: bool
    status: str
    quota_start: int = 0
    completed: int = 0
    failed: int = 0
    error: str = ""


@dataclass
class JobBatchSpec:
    """One job inside a batch run.

    file_index/total_files are user-facing indices used by callbacks.
    expected_chunks lets the provider compute a smooth global progress.
    """

    file_index: int
    queue: JobQueue
    language: str = ""
    retry_failed: bool = True
    failed_only: bool = False
    max_total_chunks: int = 0
    expected_chunks: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class _BatchJobProvider:
    """Thread-safe FIFO over multiple JobQueues.

    Workers call claim(email) to get the next chunk regardless of which file
    it belongs to. The provider keeps track of which files are in flight and
    fires on_job_started/on_job_done callbacks exactly once per file.
    """

    def __init__(
        self,
        specs: List[JobBatchSpec],
        *,
        on_job_started: Optional[Callable[[JobBatchSpec], None]] = None,
        on_job_done: Optional[Callable[[JobBatchSpec], None]] = None,
        on_chunk_progress: Optional[Callable[[JobBatchSpec, int, int], None]] = None,
    ) -> None:
        self.specs: List[JobBatchSpec] = list(specs)
        self._lock = threading.RLock()
        self._started: set = set()
        self._done_notified: set = set()
        self._claimed_per_spec: Dict[int, int] = {spec.file_index: 0 for spec in self.specs}
        self.on_job_started = on_job_started
        self.on_job_done = on_job_done
        self.on_chunk_progress = on_chunk_progress
        # Cache expected chunks per spec; fall back to current total in queue.
        self._expected_per_spec: Dict[int, int] = {}
        for spec in self.specs:
            expected = int(spec.expected_chunks or 0)
            if expected <= 0:
                try:
                    expected = spec.queue.summary().total
                except Exception:
                    expected = 0
            self._expected_per_spec[spec.file_index] = max(0, expected)
        self._expected_total = sum(self._expected_per_spec.values())

    @property
    def total_files(self) -> int:
        return len(self.specs)

    @property
    def expected_total_chunks(self) -> int:
        return self._expected_total

    @property
    def done_notified(self) -> set:
        return set(self._done_notified)

    def claim(self, email: str) -> Optional[tuple]:
        """Return (spec, chunk_dict) or None when exhausted."""
        with self._lock:
            for spec in self.specs:
                idx = spec.file_index
                if idx in self._done_notified:
                    continue
                if spec.max_total_chunks and self._claimed_per_spec.get(idx, 0) >= spec.max_total_chunks:
                    continue
                if spec.failed_only:
                    chunk = spec.queue.claim_failed_next(email)
                else:
                    chunk = spec.queue.claim_next(email, retry_failed=spec.retry_failed)
                if chunk:
                    if idx not in self._started:
                        self._started.add(idx)
                        if self.on_job_started:
                            try:
                                self.on_job_started(spec)
                            except Exception:
                                pass
                    self._claimed_per_spec[idx] = self._claimed_per_spec.get(idx, 0) + 1
                    return spec, chunk
        return None

    def notify_chunk_finished(self, spec: JobBatchSpec) -> None:
        """Recompute progress and possibly fire on_job_done for this spec."""
        callback_to_run: Optional[Callable[[JobBatchSpec], None]] = None
        progress_to_emit: Optional[tuple] = None
        with self._lock:
            try:
                summary = spec.queue.summary()
            except Exception:
                summary = None
            done_total = 0
            for other in self.specs:
                try:
                    done_total += other.queue.summary().done
                except Exception:
                    pass
            progress_to_emit = (spec, done_total, max(self._expected_total, done_total))
            if summary is not None and spec.file_index not in self._done_notified:
                claimable = summary.pending
                if spec.retry_failed and not spec.failed_only:
                    claimable += summary.failed
                if spec.failed_only:
                    claimable = summary.failed
                if claimable == 0 and summary.running == 0:
                    self._done_notified.add(spec.file_index)
                    callback_to_run = self.on_job_done
        if self.on_chunk_progress and progress_to_emit:
            try:
                self.on_chunk_progress(progress_to_emit[0], progress_to_emit[1], progress_to_emit[2])
            except Exception:
                pass
        if callback_to_run:
            try:
                callback_to_run(spec)
            except Exception:
                pass

    def force_finalize(self) -> None:
        """Emit on_job_done for any spec we never explicitly notified.

        Useful when the run is stopped or no chunk was ever claimed for a spec.
        """
        callbacks: List[tuple] = []
        with self._lock:
            for spec in self.specs:
                if spec.file_index in self._done_notified:
                    continue
                self._done_notified.add(spec.file_index)
                if self.on_job_done:
                    callbacks.append((spec, self.on_job_done))
        for spec, cb in callbacks:
            try:
                cb(spec)
            except Exception:
                pass

    def claimable_chunks(self) -> int:
        """Number of chunks across all specs that can still be claimed.

        Mirrors run()'s ``available_chunks`` calculation but aggregated over
        every spec. Specs already finalized or hit by their own
        max_total_chunks cap are excluded.
        """
        total = 0
        with self._lock:
            for spec in self.specs:
                idx = spec.file_index
                if idx in self._done_notified:
                    continue
                claimed_here = self._claimed_per_spec.get(idx, 0)
                if spec.max_total_chunks and claimed_here >= spec.max_total_chunks:
                    continue
                try:
                    summary = spec.queue.summary()
                except Exception:
                    continue
                if spec.failed_only:
                    available = summary.failed
                else:
                    available = summary.pending + (summary.failed if spec.retry_failed else 0)
                if spec.max_total_chunks:
                    available = min(available, spec.max_total_chunks - claimed_here)
                if available > 0:
                    total += available
        return total


@dataclass
class MultiBatchResult:
    success: bool
    job_results: List["MultiLongTextRunResult"] = field(default_factory=list)
    worker_results: List[MultiWorkerResult] = field(default_factory=list)
    accounts_total: int = 0
    accounts_usable: int = 0
    total_quota: int = 0
    error: str = ""


@dataclass
class MultiLongTextRunResult:
    success: bool
    job_dir: str
    status: str
    accounts_total: int
    accounts_usable: int
    accounts_started: int
    total_quota: int
    completed: int
    failed: int
    pending: int
    running: int
    total: int
    worker_results: List[MultiWorkerResult] = field(default_factory=list)
    error: str = ""
    failed_chunks: List[Dict[str, Any]] = field(default_factory=list)


class MultiAccountLongTextRunner:
    """
    Multi-account runner for long text jobs.

    Each account owns one browser worker. Workers claim chunks through one shared JobQueue,
    so two accounts cannot process the same chunk in this process.
    """

    def __init__(
        self,
        config: AppConfig,
        log: Optional[Callable[[str], None]] = None,
        progress: Optional[Callable[[int, int], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        proxy_manager: Optional[ProxyManager] = None,
        on_account_quota_zero: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        self.config = config
        self.log = log or print
        self.progress = progress
        self._external_should_stop = should_stop or (lambda: False)
        self._stop_event = threading.Event()
        self.should_stop = lambda: self._stop_event.is_set() or self._external_should_stop()
        self.proxy_manager = proxy_manager or ProxyManager(config, log=self.log, should_stop=self.should_stop)
        self._claim_limit_lock = threading.RLock()
        self._claimed_this_run = 0
        self._active_browsers_lock = threading.RLock()
        self._active_browsers: Dict[int, Dict[str, Any]] = {}
        # Optional callback fired when a worker confirms an account hit
        # quota=0 (after a successful API check). Called at most once per
        # email per run via the dedupe set below.
        self.on_account_quota_zero = on_account_quota_zero
        self._quota_zero_seen: set = set()
        self._quota_zero_lock = threading.Lock()

    def request_stop(self) -> None:
        if self._stop_event.is_set():
            return
        self._stop_event.set()
        # Per-session guards interrupt owned drivers without cross-thread APIs.

    def _track_browser(self, email: str, browser: Any, context: Any = None, page: Any = None) -> int:
        token = id(browser)
        transport_pid = 0
        try:
            transport = browser._impl_obj._connection._transport
            transport_pid = int(getattr(getattr(transport, "_proc", None), "pid", 0) or 0)
        except Exception:
            pass
        with self._active_browsers_lock:
            self._active_browsers[token] = {
                "email": email,
                "browser": browser,
                "context": context,
                "page": page,
                "transport_pid": transport_pid,
            }
        return token

    def _untrack_browser(self, token: int) -> None:
        with self._active_browsers_lock:
            self._active_browsers.pop(token, None)

    def _close_active_browsers(self) -> None:
        with self._active_browsers_lock:
            active = list(self._active_browsers.values())
        for item in active:
            email = str(item.get("email") or "unknown")
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
            self.log(f"[MULTI:{email}] Stop requested: browser close signaled")

    def _emit_quota_zero(self, email: str, quota_text: str) -> None:
        """Throttle + dispatch the quota-zero callback to the GUI layer.

        Each ``email`` only fires once per runner instance regardless of
        how many phases (scan, worker drain, etc.) detect the same state.
        Errors in the callback are isolated so they cannot break the run.
        """
        if not self.on_account_quota_zero or not email:
            return
        key = email.strip().lower()
        if not key:
            return
        with self._quota_zero_lock:
            if key in self._quota_zero_seen:
                return
            self._quota_zero_seen.add(key)
        try:
            self.on_account_quota_zero(email, quota_text or "")
        except Exception as exc:
            self.log(f"[MULTI:{email}] quota_zero callback error: {exc}")

    def _fire_quota_zero_from_scan(self, scan_summary) -> None:
        """Call the callback for every account whose quota probe succeeded
        with zero remaining. Skips accounts whose probe failed (cookie
        invalid, login expired, network errors) so transient issues do
        not get marked as exhausted.
        """
        if not self.on_account_quota_zero or scan_summary is None:
            return
        for item in getattr(scan_summary, "results", []) or []:
            try:
                quota = getattr(item, "quota", None)
                quota_left = int(getattr(item, "quota_left", 0) or 0)
                quota_ok = bool(getattr(quota, "success", False)) if quota is not None else False
                # Real "no quota" only when API succeeded AND value is 0.
                if quota_ok and quota_left <= 0:
                    self._emit_quota_zero(
                        getattr(item, "email", ""),
                        getattr(item, "quota_text", "") or "",
                    )
            except Exception as exc:
                self.log(f"[MULTI] scan quota_zero check error: {exc}")

    def _usable_scans_with_login_fallback(self, scan_summary) -> List[AccountQuotaScan]:
        """Keep valid cookie scans and defer auth recovery to the worker.

        Workers already know how to fall back to email/password, fetch the
        real quota, and save refreshed cookies. The pre-scan must not discard
        those accounts merely because their cookie is missing or expired.
        """
        usable: List[AccountQuotaScan] = []
        fallback: List[AccountQuotaScan] = []
        auth_error_markers = (
            "missing saved cookies",
            "cookie",
            "http 401",
            "http 403",
            "unauthorized",
            "forbidden",
            "login",
            "log in",
            "sign in",
            "not authenticated",
            "authentication",
            "session expired",
        )

        for item in getattr(scan_summary, "results", []) or []:
            if item.usable and item.quota_left > 0:
                usable.append(item)
                continue

            account = item.account if isinstance(item.account, dict) else {}
            password = str(account.get("password") or "").strip()
            quota_succeeded = bool(item.quota is not None and item.quota.success)
            error_text = " ".join(
                part
                for part in (
                    str(item.error or ""),
                    str(getattr(item.quota, "error", "") or ""),
                    str(item.quota_text or ""),
                )
                if part
            ).lower()
            can_login = (
                bool(password)
                and not quota_succeeded
                and any(marker in error_text for marker in auth_error_markers)
            )
            if not can_login:
                continue

            fallback.append(item)
            self.log(
                f"[MULTI:{item.email}] Cookie pre-scan unavailable; "
                "worker will login with email/password"
            )

        if fallback:
            self.log(
                f"[MULTI] Login fallback armed for {len(fallback)} account(s) "
                "with missing/expired cookies"
            )
        return usable + fallback

    def run(
        self,
        accounts: List[Dict[str, Any]],
        job_dir: str | Path,
        *,
        audio_path: str | Path = "",
        language: str = "",
        parallel_accounts: int = 1,
        max_total_chunks: int = 0,
        retry_failed: bool = False,
        failed_only: bool = False,
        timeout_sec: int = 100,
        max_consecutive_failures: int = 3,
        fast_reuse_voice: bool = False,
    ) -> MultiLongTextRunResult:
        queue = JobQueue(job_dir)
        reset_count = queue.reset_running()
        if reset_count:
            self.log(f"[MULTI] Reset {reset_count} stale running chunks to pending")

        manifest = queue.load()
        job_root = queue.job_dir
        settings = manifest.get("settings", {}) if isinstance(manifest.get("settings"), dict) else {}
        audio = self._resolve_file(audio_path or settings.get("audio_path", ""))
        if not audio.exists():
            return self._result_from_queue(
                queue,
                status="error",
                success=False,
                accounts_total=len(accounts),
                accounts_usable=0,
                accounts_started=0,
                total_quota=0,
                error=f"audio file not found: {audio}",
            )

        lang = (language or settings.get("language") or "Vietnamese").strip()
        summary = queue.summary()
        if summary.total <= 0:
            return self._result_from_queue(
                queue,
                status="error",
                success=False,
                accounts_total=len(accounts),
                accounts_usable=0,
                accounts_started=0,
                total_quota=0,
                error="job has no chunks",
            )
        if summary.done >= summary.total:
            queue.set_status("done")
            return self._result_from_queue(
                queue,
                status="done",
                success=True,
                accounts_total=len(accounts),
                accounts_usable=0,
                accounts_started=0,
                total_quota=0,
            )

        self._claimed_this_run = 0
        self.log(
            f"[MULTI] Start job: {job_root} | accounts={len(accounts)} | "
            f"parallel={parallel_accounts} | pending={summary.pending} | failed={summary.failed}"
        )
        self.log(
            f"[MULTI] Chunk timeout={timeout_sec}s | continue_on_fail=on | "
            f"worker_stop_after_failures={max_consecutive_failures} | "
            f"fast_reuse_voice={'on' if fast_reuse_voice else 'off'}"
        )

        scan_summary = AccountQuotaScanner(
            self.config,
            log=self.log,
            should_stop=self.should_stop,
            proxy_manager=self.proxy_manager,
        ).scan_accounts(accounts, max_workers=max(1, int(parallel_accounts or 1)))
        self._fire_quota_zero_from_scan(scan_summary)
        usable_scans = self._usable_scans_with_login_fallback(scan_summary)
        if self.should_stop():
            queue.set_status("stopped")
            return self._result_from_queue(
                queue, status="stopped", success=False,
                accounts_total=scan_summary.total_accounts, accounts_usable=0,
                accounts_started=0, total_quota=0,
            )
        if not usable_scans:
            queue.set_status("paused_no_quota", error="no usable account quota")
            return self._result_from_queue(
                queue,
                status="paused_no_quota",
                success=False,
                accounts_total=scan_summary.total_accounts,
                accounts_usable=0,
                accounts_started=0,
                total_quota=0,
                error="no usable accounts with quota",
            )

        summary = queue.summary()
        available_chunks = summary.failed if failed_only else summary.pending + (summary.failed if retry_failed else 0)
        if max_total_chunks:
            available_chunks = min(available_chunks, max_total_chunks)
        if available_chunks <= 0:
            status = self._final_status(summary, max_total_chunks, failed_only=failed_only)
            queue.set_status(status)
            return self._result_from_queue(
                queue,
                status=status,
                success=summary.done > 0 or status == "done",
                accounts_total=scan_summary.total_accounts,
                accounts_usable=len(usable_scans),
                accounts_started=0,
                total_quota=scan_summary.total_quota,
            )

        worker_count = max(1, min(int(parallel_accounts or 1), len(usable_scans), available_chunks))
        self.log(
            f"[MULTI] Usable accounts={len(usable_scans)}/{scan_summary.total_accounts}, "
            f"total_quota={scan_summary.total_quota}, workers={worker_count}"
        )

        worker_results: List[MultiWorkerResult] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            scan_index = 0
            active = {}

            def can_start_more() -> bool:
                if self.should_stop() or self._limit_reached(max_total_chunks):
                    return False
                current = queue.summary()
                available = current.failed if failed_only else current.pending + (current.failed if retry_failed else 0)
                return available > 0

            def submit_next() -> bool:
                nonlocal scan_index
                if scan_index >= len(usable_scans) or not can_start_more():
                    return False
                scan = usable_scans[scan_index]
                scan_index += 1
                future = executor.submit(
                    self._run_account_worker,
                    scan,
                    queue,
                    audio,
                    lang,
                    max_total_chunks,
                    retry_failed,
                    failed_only,
                    timeout_sec,
                    max_consecutive_failures,
                    fast_reuse_voice,
                )
                active[future] = scan.email
                return True

            for _ in range(worker_count):
                submit_next()

            while active:
                done, _pending = wait(set(active), timeout=0.5, return_when=FIRST_COMPLETED)
                if self.should_stop():
                    self.request_stop()
                    if not done:
                        continue
                for future in done:
                    worker_email = active.pop(future, "")
                    try:
                        result = future.result()
                    except Exception as exc:
                        error = str(exc) or exc.__class__.__name__
                        result = MultiWorkerResult(worker_email, False, "fatal_error", error=error)
                        self.log(f"[MULTI:{worker_email or 'unknown'}] Worker crashed before completion: {error}")
                    worker_results.append(result)
                    self.log(
                        f"[MULTI:{result.email or 'unknown'}] {result.status} | "
                        f"done={result.completed}, failed={result.failed}"
                    )
                if not self.should_stop():
                    while len(active) < worker_count and submit_next():
                        pass

        final_summary = queue.summary()
        status = self._final_status(final_summary, max_total_chunks, failed_only=failed_only)
        if self.should_stop():
            status = "stopped"
        queue.set_status(status)
        success = final_summary.done > 0 or status in ("done", "retry_done")
        result = self._result_from_queue(
            queue,
            status=status,
            success=success,
            accounts_total=scan_summary.total_accounts,
            accounts_usable=len(usable_scans),
            accounts_started=sum(1 for item in worker_results if item.email),
            total_quota=scan_summary.total_quota,
        )
        result.worker_results = worker_results
        if not result.success and not result.error:
            worker_errors = [
                f"{item.email or 'unknown'}: {item.status}: {item.error}"
                for item in worker_results
                if item.error
            ]
            if worker_errors:
                result.error = " | ".join(worker_errors[:5])
                if len(worker_errors) > 5:
                    result.error += f" | ... {len(worker_errors) - 5} more"
        return result

    def run_batch(
        self,
        accounts: List[Dict[str, Any]],
        specs: List[JobBatchSpec],
        *,
        audio_path: str | Path,
        language: str = "Vietnamese",
        parallel_accounts: int = 1,
        timeout_sec: int = 100,
        max_consecutive_failures: int = 3,
        fast_reuse_voice: bool = True,
        on_job_started: Optional[Callable[[JobBatchSpec], None]] = None,
        on_job_done: Optional[Callable[[JobBatchSpec], None]] = None,
        on_chunk_progress: Optional[Callable[[JobBatchSpec, int, int], None]] = None,
    ) -> MultiBatchResult:
        """
        Run multiple jobs (one per file) using long-lived browser sessions.

        Each worker uploads the voice sample once and then keeps consuming
        chunks from any spec that still has work. This avoids the heavy
        per-file overhead of spinning up Chromium and re-uploading audio.

        run() is unchanged so the Long Text tab keeps its current behavior.
        """
        if not specs:
            return MultiBatchResult(success=True)

        # Reset stale running rows in every queue so resumed runs do not stall.
        for spec in specs:
            try:
                reset_count = spec.queue.reset_running()
                if reset_count:
                    self.log(
                        f"[MULTI] Reset {reset_count} stale running chunks in {spec.queue.job_dir.name}"
                    )
            except Exception:
                pass

        audio = self._resolve_file(audio_path)
        if not audio.exists():
            return MultiBatchResult(success=False, error=f"audio file not found: {audio}")

        provider = _BatchJobProvider(
            specs,
            on_job_started=on_job_started,
            on_job_done=on_job_done,
            on_chunk_progress=on_chunk_progress,
        )

        # Pre-summary: any pending chunk across all specs?
        total_pending = 0
        for spec in specs:
            try:
                summary = spec.queue.summary()
            except Exception:
                continue
            if spec.failed_only:
                total_pending += summary.failed
            else:
                total_pending += summary.pending + (summary.failed if spec.retry_failed else 0)
        if total_pending <= 0:
            self.log("[MULTI] Batch has no pending chunks; finalizing without workers.")
            provider.force_finalize()
            return MultiBatchResult(
                success=True,
                job_results=self._collect_batch_job_results(specs, accounts_total=len(accounts)),
            )

        scan_summary = AccountQuotaScanner(
            self.config,
            log=self.log,
            should_stop=self.should_stop,
            proxy_manager=self.proxy_manager,
        ).scan_accounts(accounts, max_workers=max(1, int(parallel_accounts or 1)))
        self._fire_quota_zero_from_scan(scan_summary)
        usable_scans = self._usable_scans_with_login_fallback(scan_summary)
        if not usable_scans:
            self.log("[MULTI] No usable accounts with quota for batch run.")
            provider.force_finalize()
            return MultiBatchResult(
                success=False,
                accounts_total=scan_summary.total_accounts,
                error="Stopped by user" if self.should_stop() else "no usable accounts with quota",
                job_results=self._collect_batch_job_results(
                    specs,
                    accounts_total=scan_summary.total_accounts,
                    error_status="stopped" if self.should_stop() else "paused_no_quota",
                ),
            )

        worker_count = max(
            1,
            min(
                int(parallel_accounts or 1),
                len(usable_scans),
                max(1, provider.claimable_chunks()),
            ),
        )
        self.log(
            f"[MULTI] Batch start: files={len(specs)}, expected_chunks={provider.expected_total_chunks}, "
            f"usable_accounts={len(usable_scans)}/{scan_summary.total_accounts}, total_quota={scan_summary.total_quota}, "
            f"workers={worker_count}, fast_reuse_voice={'on' if fast_reuse_voice else 'off'}"
        )
        lang_default = (language or "Vietnamese").strip()

        worker_results: List[MultiWorkerResult] = []
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            scan_index = 0
            active = {}

            def can_start_more() -> bool:
                if self.should_stop():
                    return False
                # Stop spinning new workers when there is no work left to claim.
                return provider.claimable_chunks() > 0

            def submit_next() -> bool:
                nonlocal scan_index
                if scan_index >= len(usable_scans):
                    return False
                if not can_start_more():
                    return False
                scan = usable_scans[scan_index]
                scan_index += 1
                future = executor.submit(
                    self._run_batch_account_worker,
                    scan,
                    provider,
                    audio,
                    lang_default,
                    timeout_sec,
                    max_consecutive_failures,
                    fast_reuse_voice,
                )
                active[future] = scan.email
                self.log(
                    f"[MULTI] Submit worker for {scan.email} "
                    f"(slot used={len(active)}/{worker_count}, "
                    f"queued_accounts_left={max(0, len(usable_scans) - scan_index)})"
                )
                return True

            for _ in range(worker_count):
                submit_next()

            while active:
                done, _pending = wait(set(active), timeout=0.5, return_when=FIRST_COMPLETED)
                if self.should_stop():
                    self.request_stop()
                    if not done:
                        continue
                for future in done:
                    worker_email = active.pop(future, "")
                    try:
                        result = future.result()
                    except Exception as exc:
                        error = str(exc) or exc.__class__.__name__
                        result = MultiWorkerResult(worker_email, False, "fatal_error", error=error)
                        self.log(
                            f"[MULTI:{worker_email or 'unknown'}] Worker crashed before completion: {error}"
                        )
                    worker_results.append(result)
                    self.log(
                        f"[MULTI:{result.email or 'unknown'}] {result.status} | "
                        f"done={result.completed}, failed={result.failed}"
                    )
                # Refill empty slots with unused accounts (Long Text run() parity).
                if not self.should_stop():
                    while len(active) < worker_count and submit_next():
                        pass
                if self.should_stop() and not active:
                    break

        provider.force_finalize()
        job_results = self._collect_batch_job_results(specs, accounts_total=scan_summary.total_accounts)
        accounts_started = sum(1 for r in worker_results if r.email)
        success = all(r.success for r in job_results) and not self.should_stop()
        result = MultiBatchResult(
            success=success,
            job_results=job_results,
            worker_results=worker_results,
            accounts_total=scan_summary.total_accounts,
            accounts_usable=len(usable_scans),
            total_quota=scan_summary.total_quota,
        )
        if not result.success and not result.error:
            errs = [
                f"{r.email or 'unknown'}: {r.status}: {r.error}" for r in worker_results if r.error
            ]
            if errs:
                result.error = " | ".join(errs[:5])
        return result

    def _collect_batch_job_results(
        self,
        specs: List[JobBatchSpec],
        *,
        accounts_total: int,
        error_status: str = "",
    ) -> List["MultiLongTextRunResult"]:
        results: List[MultiLongTextRunResult] = []
        for spec in specs:
            try:
                summary = spec.queue.summary()
                final_status = self._final_status(
                    summary, spec.max_total_chunks, failed_only=spec.failed_only
                )
                if error_status:
                    final_status = error_status
                if self.should_stop():
                    final_status = "stopped"
                spec.queue.set_status(final_status)
                success = (
                    summary.total > 0
                    and summary.done >= summary.total
                    and final_status in ("done", "retry_done")
                )
                results.append(
                    self._result_from_queue(
                        spec.queue,
                        status=final_status,
                        success=success,
                        accounts_total=accounts_total,
                        accounts_usable=0,
                        accounts_started=0,
                        total_quota=0,
                    )
                )
            except Exception as exc:
                results.append(
                    MultiLongTextRunResult(
                        success=False,
                        job_dir=str(spec.queue.job_dir),
                        status="error",
                        accounts_total=accounts_total,
                        accounts_usable=0,
                        accounts_started=0,
                        total_quota=0,
                        completed=0,
                        failed=0,
                        pending=0,
                        running=0,
                        total=0,
                        error=str(exc),
                    )
                )
        return results

    def _run_batch_account_worker(
        self,
        scan: AccountQuotaScan,
        provider: _BatchJobProvider,
        audio: Path,
        default_language: str,
        timeout_sec: int,
        max_consecutive_failures: int,
        fast_reuse_voice: bool,
    ) -> MultiWorkerResult:
        """Long-lived worker that consumes chunks from many JobQueues."""
        account = scan.account
        email = scan.email
        password = (account.get("password") or "").strip()
        result = MultiWorkerResult(
            email=email, success=True, status="started", quota_start=scan.quota_left
        )
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
        downloaded_urls: set[str] = set()
        capture_after_clone = {"enabled": False}

        self.log(f"[MULTI:{email}] Batch worker start | quota_start={scan.quota_left}")
        browser_token = 0
        with sync_playwright() as p, BrowserStopGuard(p, self.should_stop):
            self.proxy_manager.wait_if_rotating(f"multi_batch {email}")
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
            browser_token = self._track_browser(email, browser, context, page)

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
                            self.log(f"[MULTI:{email}] MP3 candidate: {url}")
                    if "application/json" in content_type.lower() and "notegpt.io" in url:
                        try:
                            for candidate in find_mp3_urls(response.json()):
                                if helper._is_direct_mp3_url(candidate) and candidate not in api_candidates:
                                    api_candidates.append(candidate)
                                    self.log(f"[MULTI:{email}] MP3 in API: {candidate}")
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                cookies = clean_cookies(cookie_candidates)
                if cookies:
                    context.add_cookies(cookies)
                    self.log(f"[MULTI:{email}] Loaded {len(cookies)} cookies")

                goto_with_retry(
                    page,
                    NOTE_GPT_HOME,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[MULTI:{email}]",
                    should_stop=self.should_stop,
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                quota = auth._fetch_quota(context)
                if not scan.usable:
                    if not password:
                        raise RuntimeError(f"cookie invalid and missing password: {scan.error}")
                    auth._login(page, context, email, password)
                    quota = auth._fetch_quota(context)
                elif not quota.success:
                    if not password:
                        raise RuntimeError(f"cookie invalid and missing password: {quota.error}")
                    auth._login(page, context, email, password)
                    quota = auth._fetch_quota(context)
                quota_left = quota_left_from_quota(quota) if quota.success else 0
                if quota_left <= 0:
                    result.status = "no_quota"
                    return result
                self.log(
                    f"[MULTI:{email}] Quota before worker: {quota.to_text(self.config.quota_threshold)}"
                )

                # ONE upload that lives for the rest of this worker's lifetime.
                goto_with_retry(
                    page,
                    CLONE_URL,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[MULTI:{email}]",
                    should_stop=self.should_stop,
                )
                helper._upload_audio(page, audio)
                clone_page_ready = True
                fast_reuse_ready = False
                local_quota_left = (
                    min(scan.quota_left, quota_left) if scan.quota_left > 0 else quota_left
                )
                consecutive_failures = 0
                if fast_reuse_voice:
                    self.log(f"[MULTI:{email}] Fast reuse voice armed after first upload")

                while not self.should_stop():
                    if local_quota_left <= 0:
                        result.status = "paused_no_quota"
                        self.log(f"[MULTI:{email}] Stop: no quota left")
                        # Only flag exhausted accounts that actually proved
                        # they could process at least one chunk this run;
                        # otherwise a flaky scan could mass-mark live ones.
                        if result.completed > 0:
                            self._emit_quota_zero(email, "")
                        break

                    claim = provider.claim(email)
                    if not claim:
                        result.status = "done_or_limit"
                        break

                    spec, chunk = claim
                    chunk_index = int(chunk.get("index") or 0)
                    queue = spec.queue
                    chunk_language = (spec.language or default_language or "Vietnamese").strip()

                    try:
                        if self.should_stop():
                            queue.release_claim(chunk_index, account_email=email)
                            result.status = "stopped"
                            break
                        fast_reuse_ready = self._process_chunk(
                            helper,
                            auth,
                            context,
                            page,
                            queue,
                            chunk,
                            audio,
                            chunk_language,
                            email,
                            mp3_candidates,
                            api_candidates,
                            capture_after_clone,
                            timeout_sec,
                            clone_page_ready,
                            fast_reuse_ready,
                            local_quota_left,
                            downloaded_urls,
                            fast_reuse_voice=fast_reuse_voice,
                        )
                        result.completed += 1
                        local_quota_left -= 1
                        clone_page_ready = bool(
                            fast_reuse_voice and fast_reuse_ready and local_quota_left > 0
                        )
                        consecutive_failures = 0
                        provider.notify_chunk_finished(spec)
                    except Exception as exc:
                        if self.should_stop():
                            queue.release_claim(chunk_index, account_email=email)
                            result.status = "stopped"
                            self.log(
                                f"[MULTI:{email}] Stop requested during chunk {chunk_index:04d}"
                            )
                            break
                        result.failed += 1
                        result.success = result.completed > 0
                        result.status = "running_with_failed_chunks"
                        result.error = str(exc)
                        consecutive_failures += 1
                        clone_page_ready = False
                        fast_reuse_ready = False
                        queue.mark_failed(chunk_index, account_email=email, error=str(exc))
                        provider.notify_chunk_finished(spec)
                        self._emit_progress(queue)
                        self.log(f"[MULTI:{email}] Chunk {chunk_index:04d} error: {exc}")
                        self.proxy_manager.report_error(
                            f"MULTI:{email}:chunk_{chunk_index:04d}", exc
                        )
                        if consecutive_failures >= max(1, max_consecutive_failures):
                            result.status = "paused_error"
                            self.log(
                                f"[MULTI:{email}] Stop worker after {consecutive_failures} consecutive failures"
                            )
                            break
                        self.log(
                            f"[MULTI:{email}] Continue after failed chunk {chunk_index:04d}; "
                            "next chunk will reload clone page"
                        )
                        page.wait_for_timeout(1500)
                        continue

                cookies_out = auth._get_all_cookies(context, page)
                if self.config.save_cookies and cookies_out:
                    cookie_file.write_text(
                        json.dumps(cookies_out, ensure_ascii=False, indent=2), encoding="utf-8"
                    )

                if result.status == "started":
                    result.status = "stopped" if self.should_stop() else "done_or_empty"
                return result
            except Exception as exc:
                if self.should_stop():
                    result.success = result.completed > 0
                    result.status = "stopped"
                    result.error = ""
                    self.log(f"[MULTI:{email}] Stop requested")
                    return result
                result.success = result.completed > 0
                result.status = "fatal_error"
                result.error = str(exc)
                self.log(f"[MULTI:{email}] Fatal error: {exc}")
                self.proxy_manager.report_error(f"MULTI:{email}:fatal", exc)
                return result
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
                if browser_token:
                    self._untrack_browser(browser_token)

    def _run_account_worker(
        self,
        scan: AccountQuotaScan,
        queue: JobQueue,
        audio: Path,
        language: str,
        max_total_chunks: int,
        retry_failed: bool,
        failed_only: bool,
        timeout_sec: int,
        max_consecutive_failures: int,
        fast_reuse_voice: bool,
    ) -> MultiWorkerResult:
        account = scan.account
        email = scan.email
        password = (account.get("password") or "").strip()
        result = MultiWorkerResult(email=email, success=True, status="started", quota_start=scan.quota_left)
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
        downloaded_urls: set[str] = set()
        capture_after_clone = {"enabled": False}

        self.log(f"[MULTI:{email}] Worker start | quota_start={scan.quota_left}")
        browser_token = 0
        with sync_playwright() as p, BrowserStopGuard(p, self.should_stop):
            self.proxy_manager.wait_if_rotating(f"multi {email}")
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
            browser_token = self._track_browser(email, browser, context, page)

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
                            self.log(f"[MULTI:{email}] MP3 candidate: {url}")
                    if "application/json" in content_type.lower() and "notegpt.io" in url:
                        try:
                            for candidate in find_mp3_urls(response.json()):
                                if helper._is_direct_mp3_url(candidate) and candidate not in api_candidates:
                                    api_candidates.append(candidate)
                                    self.log(f"[MULTI:{email}] MP3 in API: {candidate}")
                        except Exception:
                            pass
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                cookies = clean_cookies(cookie_candidates)
                if cookies:
                    context.add_cookies(cookies)
                    self.log(f"[MULTI:{email}] Loaded {len(cookies)} cookies")

                goto_with_retry(
                    page,
                    NOTE_GPT_HOME,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[MULTI:{email}]",
                    should_stop=self.should_stop,
                )
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeoutError:
                    pass

                quota = auth._fetch_quota(context)
                if not scan.usable:
                    if not password:
                        raise RuntimeError(f"cookie invalid and missing password: {scan.error}")
                    auth._login(page, context, email, password)
                    quota = auth._fetch_quota(context)
                elif not quota.success:
                    if not password:
                        raise RuntimeError(f"cookie invalid and missing password: {quota.error}")
                    auth._login(page, context, email, password)
                    quota = auth._fetch_quota(context)
                quota_left = quota_left_from_quota(quota) if quota.success else 0
                if quota_left <= 0:
                    result.status = "no_quota"
                    return result
                self.log(f"[MULTI:{email}] Quota before worker: {quota.to_text(self.config.quota_threshold)}")

                goto_with_retry(
                    page,
                    CLONE_URL,
                    timeout_ms=self.config.timeout_ms,
                    log=self.log,
                    label=f"[MULTI:{email}]",
                    should_stop=self.should_stop,
                )
                helper._upload_audio(page, audio)
                clone_page_ready = True
                fast_reuse_ready = False
                local_quota_left = min(scan.quota_left, quota_left) if scan.quota_left > 0 else quota_left
                consecutive_failures = 0
                if fast_reuse_voice:
                    self.log(f"[MULTI:{email}] Fast reuse voice armed after first upload")

                while not self.should_stop():
                    quota = auth._fetch_quota(context)
                    quota_left = quota_left_from_quota(quota) if quota.success else 0
                    local_quota_left = min(local_quota_left, quota_left)
                    if local_quota_left <= 0:
                        result.status = "paused_no_quota"
                        self.log(f"[MULTI:{email}] Stop: no quota left")
                        # Only flag accounts that proved they were healthy
                        # this run before hitting zero. Avoids marking live
                        # accounts when a flaky scan returns a stale 0.
                        if result.completed > 0:
                            quota_text_now = (
                                quota.to_text(self.config.quota_threshold)
                                if quota and getattr(quota, "success", False)
                                else ""
                            )
                            self._emit_quota_zero(email, quota_text_now)
                        break

                    chunk = self._claim_next_limited(queue, email, max_total_chunks, retry_failed, failed_only)
                    if not chunk:
                        result.status = "done_or_limit"
                        break

                    chunk_index = int(chunk.get("index") or 0)
                    try:
                        if self.should_stop():
                            queue.release_claim(chunk_index, account_email=email)
                            result.status = "stopped"
                            break
                        fast_reuse_ready = self._process_chunk(
                            helper,
                            auth,
                            context,
                            page,
                            queue,
                            chunk,
                            audio,
                            language,
                            email,
                            mp3_candidates,
                            api_candidates,
                            capture_after_clone,
                            timeout_sec,
                            clone_page_ready,
                            fast_reuse_ready,
                            local_quota_left,
                            downloaded_urls,
                            fast_reuse_voice=fast_reuse_voice,
                        )
                        result.completed += 1
                        local_quota_left -= 1
                        clone_page_ready = bool(fast_reuse_voice and fast_reuse_ready and local_quota_left > 0)
                        consecutive_failures = 0
                    except Exception as exc:
                        if self.should_stop():
                            queue.release_claim(chunk_index, account_email=email)
                            result.status = "stopped"
                            self.log(f"[MULTI:{email}] Stop requested during chunk {chunk_index:04d}")
                            break
                        result.failed += 1
                        result.success = result.completed > 0
                        result.status = "running_with_failed_chunks"
                        result.error = str(exc)
                        consecutive_failures += 1
                        clone_page_ready = False
                        fast_reuse_ready = False
                        queue.mark_failed(chunk_index, account_email=email, error=str(exc))
                        self._emit_progress(queue)
                        self.log(f"[MULTI:{email}] Chunk {chunk_index:04d} error: {exc}")
                        self.proxy_manager.report_error(f"MULTI:{email}:chunk_{chunk_index:04d}", exc)
                        if consecutive_failures >= max(1, max_consecutive_failures):
                            result.status = "paused_error"
                            self.log(
                                f"[MULTI:{email}] Stop worker after {consecutive_failures} consecutive failures"
                            )
                            break
                        self.log(
                            f"[MULTI:{email}] Continue after failed chunk {chunk_index:04d}; "
                            "next chunk will reload clone page"
                        )
                        page.wait_for_timeout(1500)
                        continue

                cookies_out = auth._get_all_cookies(context, page)
                if self.config.save_cookies and cookies_out:
                    cookie_file.write_text(json.dumps(cookies_out, ensure_ascii=False, indent=2), encoding="utf-8")

                if result.status == "started":
                    result.status = "stopped" if self.should_stop() else "done_or_empty"
                return result
            except Exception as exc:
                if self.should_stop():
                    result.success = result.completed > 0
                    result.status = "stopped"
                    result.error = ""
                    self.log(f"[MULTI:{email}] Stop requested")
                    return result
                result.success = result.completed > 0
                result.status = "fatal_error"
                result.error = str(exc)
                self.log(f"[MULTI:{email}] Fatal error: {exc}")
                self.proxy_manager.report_error(f"MULTI:{email}:fatal", exc)
                return result
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
                if browser_token:
                    self._untrack_browser(browser_token)

    def _process_chunk(
        self,
        helper: CloneProbeRunner,
        auth: NoteGPTAutomation,
        context,
        page,
        queue: JobQueue,
        chunk: Dict[str, Any],
        audio: Path,
        language: str,
        email: str,
        mp3_candidates: List[str],
        api_candidates: List[str],
        capture_after_clone: Dict[str, bool],
        timeout_sec: int,
        clone_page_ready: bool,
        fast_reuse_ready: bool,
        quota_left: int,
        downloaded_urls: set[str],
        *,
        fast_reuse_voice: bool = False,
    ) -> bool:
        _ = auth
        job_root = queue.job_dir
        chunk_index = int(chunk.get("index") or 0)
        text_path = job_root / str(chunk.get("text_file") or "")
        text = text_path.read_text(encoding="utf-8").strip()
        if not text:
            raise RuntimeError(f"chunk {chunk_index:04d} is empty")
        if len(text) > 300:
            raise RuntimeError(f"chunk {chunk_index:04d} has {len(text)} chars, over NoteGPT limit")

        output_rel = str(chunk.get("output_file") or f"outputs/chunk_{chunk_index:04d}.mp3")
        output_path = job_root / output_rel
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self.log(
            f"[MULTI:{email}] Chunk {chunk_index:04d} | "
            f"{len(text)} chars | quota_left={quota_left}"
        )
        reuse_this_chunk = bool(fast_reuse_voice and clone_page_ready and fast_reuse_ready)
        if not clone_page_ready:
            self._reset_clone_page(helper, page, audio, chunk_index, email)

        mp3_candidates.clear()
        api_candidates.clear()
        self._open_text_preview_with_reset(
            helper,
            page,
            audio,
            chunk_index,
            email,
            fast_reuse_voice=fast_reuse_voice,
        )
        helper._configure_text_preview(page, text, language)
        capture_after_clone["enabled"] = True
        started_at = time.time()
        helper._click_clone_voice(page)
        try:
            if fast_reuse_voice and reuse_this_chunk:
                mp3_url = self._wait_and_download_network_mp3(
                    helper,
                    context,
                    page,
                    mp3_candidates,
                    api_candidates,
                    started_at,
                    timeout_sec,
                    output_path,
                    downloaded_urls,
                )
            else:
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

        downloaded_urls.add(helper._absolute_mp3_url(mp3_url))
        queue.mark_done(
            chunk_index,
            account_email=email,
            mp3_url=mp3_url,
            output_file=str(output_path.relative_to(job_root)).replace("\\", "/"),
        )
        self._emit_progress(queue)
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
        if fast_reuse_voice:
            return self._discard_success_popup_for_reuse(helper, page, email, chunk_index)
        return False

    def _wait_and_download_network_mp3(
        self,
        helper: CloneProbeRunner,
        context,
        page,
        mp3_candidates: List[str],
        api_candidates: List[str],
        started_at: float,
        timeout_sec: int,
        output_path: Path,
        downloaded_urls: set[str],
    ) -> str:
        self.log("[CLONE] Waiting for clone MP3 response (fast reuse mode)...")
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
                self.log(f"[CLONE] Still waiting... {int(elapsed)}s")
            candidates = [
                helper._absolute_mp3_url(url)
                for url in list(dict.fromkeys(api_candidates + mp3_candidates))
                if helper._is_direct_mp3_url(url)
            ]
            candidates.extend(
                url
                for url in self._clone_success_dom_mp3_urls(helper, page)
                if url not in candidates
            )
            if elapsed >= min_wait_sec:
                for url in candidates:
                    if url in attempted or url in downloaded_urls:
                        continue
                    attempted.add(url)
                    try:
                        helper._download_mp3(context, url, output_path)
                        return url
                    except Exception as exc:
                        self.log(f"[CLONE] Candidate not ready/invalid: {exc}")
            interruptible_sleep(1, self.should_stop)
        raise RuntimeError("timeout waiting for MP3 response")

    def _clone_success_dom_mp3_urls(self, helper: CloneProbeRunner, page) -> List[str]:
        try:
            urls = page.evaluate(
                """
                () => {
                    const roots = Array.from(document.querySelectorAll('[role="dialog"], [id^="reka-dialog-content"]'))
                        .filter(el => {
                            const text = (el.innerText || el.textContent || '').toLowerCase();
                            return text.includes('clone successful') || text.includes('preview');
                        });
                    const found = [];
                    for (const root of roots) {
                        for (const el of root.querySelectorAll('audio,source,a[href]')) {
                            const url = el.src || el.href || '';
                            if (url && url.toLowerCase().includes('.mp3')) {
                                found.push(url);
                            }
                        }
                    }
                    return found;
                }
                """
            )
        except Exception:
            return []
        normalized = []
        for url in urls or []:
            absolute = helper._absolute_mp3_url(str(url))
            if helper._is_direct_mp3_url(absolute) and absolute not in normalized:
                normalized.append(absolute)
        return normalized

    def _discard_success_popup_for_reuse(
        self,
        helper: CloneProbeRunner,
        page,
        email: str,
        chunk_index: int,
    ) -> bool:
        if not self._close_fast_reuse_success_popup(page, email, chunk_index):
            self.log(f"[MULTI:{email}] Fast reuse disabled after chunk {chunk_index:04d}: discard popup not found")
            return False
        try:
            helper._wait_clone_button_ready(page, timeout_sec=8)
            self.log(f"[MULTI:{email}] Fast reuse ready after chunk {chunk_index:04d}")
            return True
        except Exception as exc:
            self.log(f"[MULTI:{email}] Fast reuse disabled after chunk {chunk_index:04d}: {exc}")
            return False

    def _close_fast_reuse_success_popup(
        self,
        page,
        email: str,
        chunk_index: int,
        *,
        log_not_found: bool = True,
    ) -> bool:
        close_selectors = [
            FAST_REUSE_POPUP_BODY_DISMISS_SELECTOR,
            FAST_REUSE_POPUP_HEADER_CLOSE_SELECTOR,
            "div[role='dialog']:has-text('Clone Successful') button:has-text('Discard')",
            "div:has-text('Clone Successful') button:has-text('Discard')",
            "button:has-text('Discard')",
            "button[aria-label='Close']",
            "button:has-text('Close')",
            "button:has-text('Cancel')",
        ]
        popup_seen = self._fast_reuse_success_popup_visible(page)
        clicked = False
        for selector in close_selectors:
            if self.should_stop():
                raise RuntimeError("stopped by user")
            try:
                loc = page.locator(selector).last
                if loc.count() <= 0 or not loc.is_visible(timeout=800):
                    continue
                popup_seen = True
                clicked = True
                loc.click(timeout=5000)
                if self._wait_fast_reuse_popup_closed(page, timeout_sec=5):
                    return True
            except Exception:
                continue

        if self._fast_reuse_success_popup_visible(page):
            popup_seen = True
            try:
                page.keyboard.press("Escape")
                if self._wait_fast_reuse_popup_closed(page, timeout_sec=3):
                    return True
            except Exception:
                pass

        if popup_seen and clicked and not self._fast_reuse_success_popup_visible(page):
            return True

        if log_not_found:
            self.log(
                f"[MULTI:{email}] Fast reuse popup still open after close attempts "
                f"for chunk {chunk_index:04d}"
            )
        return False

    def _wait_fast_reuse_popup_closed(self, page, timeout_sec: int = 5) -> bool:
        deadline = time.time() + max(1, timeout_sec)
        while time.time() < deadline:
            if self.should_stop():
                return False
            if not self._fast_reuse_success_popup_visible(page):
                return True
            try:
                page.wait_for_timeout(250)
            except Exception:
                interruptible_sleep(0.25, self.should_stop)
        return not self._fast_reuse_success_popup_visible(page)

    def _fast_reuse_success_popup_visible(self, page) -> bool:
        selectors = [
            FAST_REUSE_POPUP_ROOT_SELECTOR,
            "div[role='dialog']:has-text('Clone Successful')",
            "div:has-text('Clone Successful')",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector).last
                if loc.count() > 0 and loc.is_visible(timeout=500):
                    return True
            except Exception:
                continue
        return False

    def _claim_next_limited(
        self,
        queue: JobQueue,
        email: str,
        max_total_chunks: int,
        retry_failed: bool,
        failed_only: bool,
    ) -> Optional[Dict[str, Any]]:
        with self._claim_limit_lock:
            if max_total_chunks and self._claimed_this_run >= max_total_chunks:
                return None
            chunk = queue.claim_failed_next(email) if failed_only else queue.claim_next(email, retry_failed=retry_failed)
            if chunk:
                self._claimed_this_run += 1
            return chunk

    def _limit_reached(self, max_total_chunks: int) -> bool:
        with self._claim_limit_lock:
            return bool(max_total_chunks and self._claimed_this_run >= max_total_chunks)

    def _reset_clone_page(self, helper: CloneProbeRunner, page, audio: Path, chunk_index: int, email: str) -> None:
        if self.should_stop():
            raise RuntimeError("stopped by user")
        self.log(f"[MULTI:{email}] Reset clone page before chunk {chunk_index:04d}...")
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

    def _open_text_preview_with_reset(
        self,
        helper: CloneProbeRunner,
        page,
        audio: Path,
        chunk_index: int,
        email: str,
        *,
        fast_reuse_voice: bool = False,
    ) -> None:
        try:
            helper._open_text_preview(page)
            return
        except Exception as exc:
            self.log(f"[MULTI:{email}] Text preview not ready for chunk {chunk_index:04d}: {exc}")
        if fast_reuse_voice and self._close_fast_reuse_success_popup(
            page,
            email,
            chunk_index,
            log_not_found=False,
        ):
            try:
                helper._open_text_preview(page)
                return
            except Exception as exc:
                self.log(
                    f"[MULTI:{email}] Text preview still not ready after closing popup "
                    f"for chunk {chunk_index:04d}: {exc}"
                )
        self._reset_clone_page(helper, page, audio, chunk_index, email)
        helper._open_text_preview(page)

    def _emit_progress(self, queue: JobQueue) -> None:
        if not self.progress:
            return
        summary = queue.summary()
        self.progress(summary.done, summary.total)

    def _final_status(self, summary, max_total_chunks: int, *, failed_only: bool = False) -> str:
        if summary.total and summary.done >= summary.total:
            return "done"
        if max_total_chunks and self._claimed_this_run >= max_total_chunks:
            return "paused_limit"
        if failed_only:
            return "paused_error" if summary.failed > 0 else "retry_done"
        if summary.pending > 0:
            return "paused_no_quota"
        if summary.failed > 0:
            return "paused_error"
        return "done"

    def _result_from_queue(
        self,
        queue: JobQueue,
        *,
        status: str,
        success: bool,
        accounts_total: int,
        accounts_usable: int,
        accounts_started: int,
        total_quota: int,
        error: str = "",
    ) -> MultiLongTextRunResult:
        summary = queue.summary()
        return MultiLongTextRunResult(
            success=success,
            job_dir=str(queue.job_dir),
            status=status,
            accounts_total=accounts_total,
            accounts_usable=accounts_usable,
            accounts_started=accounts_started,
            total_quota=total_quota,
            completed=summary.done,
            failed=summary.failed,
            pending=summary.pending,
            running=summary.running,
            total=summary.total,
            error=error,
            failed_chunks=self._failed_chunks(queue),
        )

    def _failed_chunks(self, queue: JobQueue) -> List[Dict[str, Any]]:
        manifest = queue.load()
        failed: List[Dict[str, Any]] = []
        for chunk in manifest.get("chunks", []):
            if not isinstance(chunk, dict) or chunk.get("status") != "failed":
                continue
            failed.append(
                {
                    "index": int(chunk.get("index") or 0),
                    "account_email": str(chunk.get("account_email") or ""),
                    "error": str(chunk.get("error") or ""),
                    "text_file": str(chunk.get("text_file") or ""),
                    "output_file": str(chunk.get("output_file") or ""),
                    "finished_at": str(chunk.get("finished_at") or ""),
                }
            )
        failed.sort(key=lambda item: item["index"])
        return failed

    def _resolve_file(self, path: str | Path) -> Path:
        value = Path(path).expanduser()
        if not value.is_absolute():
            value = PROJECT_ROOT / value
        return value.resolve()
