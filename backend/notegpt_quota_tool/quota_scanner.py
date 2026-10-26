from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import requests

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.config import AppConfig, load_config
    from notegpt_quota_tool.cancellation import interruptible_call, check_stop, OperationStopped
    from notegpt_quota_tool.notegpt import (
        NOTE_GPT_HOME,
        QUOTA_ENDPOINTS,
        NoteGPTAutomation,
        QuotaResult,
        clean_cookies,
        load_cookies_from_file,
        load_cookies_from_text,
        safe_filename,
    )
    from notegpt_quota_tool.proxy_manager import ProxyManager
    from notegpt_quota_tool.sheet_api import SheetApi
else:
    from .config import AppConfig, load_config
    from .cancellation import interruptible_call, check_stop, OperationStopped
    from .notegpt import (
        NOTE_GPT_HOME,
        QUOTA_ENDPOINTS,
        NoteGPTAutomation,
        QuotaResult,
        clean_cookies,
        load_cookies_from_file,
        load_cookies_from_text,
        safe_filename,
    )
    from .proxy_manager import ProxyManager
    from .sheet_api import SheetApi


@dataclass
class AccountQuotaScan:
    email: str
    usable: bool
    quota_left: int
    quota_text: str
    verify: str
    account: Dict[str, Any]
    quota: Optional[QuotaResult] = None
    error: str = ""


@dataclass
class QuotaScanSummary:
    total_accounts: int
    usable_accounts: int
    total_quota: int
    failed_accounts: int
    results: List[AccountQuotaScan]


def quota_left_from_quota(quota: QuotaResult) -> int:
    values = [v for v in (quota.basic_left, quota.premium_left, quota.premium_sub_left) if v is not None]
    return max(values) if values else 0


class AccountQuotaScanner:
    """
    Fast live quota scan using saved cookies only.

    This intentionally avoids Playwright/login so scanning 100 accounts cannot hang on browser UI.
    Accounts with expired/missing cookies are marked unusable and can be refreshed by the normal
    login/check flow later.
    """

    def __init__(
        self,
        config: AppConfig,
        log: Optional[Callable[[str], None]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
        proxy_manager: Optional[ProxyManager] = None,
    ) -> None:
        self.config = config
        self.log = log or print
        self.should_stop = should_stop or (lambda: False)
        self.proxy_manager = proxy_manager or ProxyManager(config, log=self.log, should_stop=self.should_stop)
        self._quota_parser = NoteGPTAutomation(
            config,
            log=lambda _msg: None,
            proxy_manager=self.proxy_manager,
        )

    def scan_accounts(self, accounts: List[Dict[str, Any]], *, max_workers: int = 1, limit: int = 0) -> QuotaScanSummary:
        selected = [account for account in accounts if (account.get("email") or "").strip()]
        if limit > 0:
            selected = selected[:limit]
        total = len(selected)
        if not selected:
            return QuotaScanSummary(0, 0, 0, 0, [])

        workers = max(1, min(max_workers or 1, total))
        results: List[AccountQuotaScan] = []
        self.log(f"[SCAN] Start live quota scan by cookie API: accounts={total}, workers={workers}")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for account in selected:
                if self.should_stop():
                    break
                futures[executor.submit(self._scan_one, account)] = account

            pending = set(futures)
            while pending:
                if self.should_stop():
                    for future in pending:
                        future.cancel()
                    self.log("[SCAN] Stopped; pending requests cancelled.")
                    break
                done, pending = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    if result.error == "stopped":
                        continue
                    results.append(result)
                    status = "usable" if result.usable else "skip"
                    self.log(f"[SCAN:{result.email}] {status} | quota_left={result.quota_left} | {result.quota_text}")

        results.sort(key=lambda item: item.quota_left, reverse=True)
        usable = [item for item in results if item.usable]
        failed = [item for item in results if not item.usable]
        summary = QuotaScanSummary(
            total_accounts=total,
            usable_accounts=len(usable),
            total_quota=sum(item.quota_left for item in usable),
            failed_accounts=len(failed),
            results=results,
        )
        self.log(
            f"[SCAN] Done: usable={summary.usable_accounts}/{summary.total_accounts}, "
            f"total_quota={summary.total_quota}, failed_or_zero={summary.failed_accounts}"
        )
        return summary

    def _scan_one(self, account: Dict[str, Any]) -> AccountQuotaScan:
        email = (account.get("email") or "").strip()
        try:
            check_stop(self.should_stop)
            self.proxy_manager.wait_if_rotating(f"scan {email}")
            cookies = self._load_account_cookies(account, email)
            if not cookies:
                return self._failed(account, email, "missing saved cookies")

            quota = self._fetch_quota_with_requests(cookies)
            if not quota.success:
                self.proxy_manager.report_error(f"SCAN:{email}", quota.error)
                return self._failed(account, email, quota.error or "quota api failed", quota)

            quota_left = quota_left_from_quota(quota)
            usable = quota_left > 0
            return AccountQuotaScan(
                email=email,
                usable=usable,
                quota_left=quota_left,
                quota_text=quota.to_text(self.config.quota_threshold),
                verify="OK" if quota.success else "X",
                account=account,
                quota=quota,
                error="" if usable else "quota is zero",
            )
        except OperationStopped:
            return self._failed(account, email, "stopped")
        except Exception as exc:
            self.proxy_manager.report_error(f"SCAN:{email}", exc)
            return self._failed(account, email, str(exc))

    def _load_account_cookies(self, account: Dict[str, Any], email: str) -> List[Dict[str, Any]]:
        cookie_file = self.config.cookie_path / f"{safe_filename(email)}.json"
        candidates = []
        if self.config.reuse_cookies:
            candidates.extend(load_cookies_from_text(account.get("cookies", "")))
            candidates.extend(load_cookies_from_file(cookie_file))
        return clean_cookies(candidates)

    def _fetch_quota_with_requests(self, cookies: List[Dict[str, Any]]) -> QuotaResult:
        session = requests.Session()
        session.headers.update(
            {
                "Accept": "application/json, text/plain, */*",
                "Referer": NOTE_GPT_HOME,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
            }
        )
        for cookie in cookies:
            session.cookies.set(
                str(cookie.get("name", "")),
                str(cookie.get("value", "")),
                domain=str(cookie.get("domain") or ".notegpt.io"),
                path=str(cookie.get("path") or "/"),
            )

        last_error = ""
        for endpoint in QUOTA_ENDPOINTS:
            try:
                resp = interruptible_call(session.get, self.should_stop, endpoint, timeout=20, proxies=self.proxy_manager.requests_proxies("scan"))
                if resp.status_code != 200:
                    last_error = f"{endpoint} HTTP {resp.status_code}"
                    continue
                data = resp.json() or {}
                if data.get("code") not in (None, 100000) and data.get("success") is not True:
                    last_error = f"{endpoint}: {data.get('message') or data.get('error') or data.get('code')}"
                    continue
                quota = self._quota_parser._parse_quota(data, endpoint)
                if quota.has_values():
                    return quota
                last_error = f"{endpoint}: empty quota data"
            except OperationStopped:
                raise
            except Exception as exc:
                last_error = f"{endpoint}: {exc}"
        return QuotaResult(success=False, error=last_error or "all quota endpoints failed")

    def _failed(
        self,
        account: Dict[str, Any],
        email: str,
        error: str,
        quota: Optional[QuotaResult] = None,
    ) -> AccountQuotaScan:
        quota = quota or QuotaResult(success=False, error=error)
        return AccountQuotaScan(
            email=email,
            usable=False,
            quota_left=0,
            quota_text=quota.to_text(self.config.quota_threshold),
            verify="X",
            account=account,
            quota=quota,
            error=error,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan live NoteGPT quota by saved cookies.")
    parser.add_argument("--config", default="", help="Path to config.json")
    parser.add_argument("--limit", type=int, default=0, help="Optional account limit")
    parser.add_argument("--workers", type=int, default=1, help="Concurrent quota scans")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(args.config or None)
    accounts = SheetApi(cfg, log=print).get_accounts()
    summary = AccountQuotaScanner(cfg, log=print).scan_accounts(accounts, max_workers=args.workers, limit=args.limit)
    print(f"usable={summary.usable_accounts}/{summary.total_accounts} total_quota={summary.total_quota}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
