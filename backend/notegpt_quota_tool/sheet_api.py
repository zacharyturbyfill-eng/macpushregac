from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

import requests

from .config import AppConfig
from .cancellation import interruptible_call, interruptible_sleep


def first_value(row: Dict[str, Any], *keys: str, default: str = "") -> str:
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None and str(value).strip():
            return str(value).strip()
    return default


def normalize_account(row: Dict[str, Any]) -> Dict[str, Any]:
    account = dict(row)
    account["email"] = first_value(
        row,
        "email",
        "mail",
        "gmail",
        "account",
        "tai_khoan",
        "tai khoan",
        "user",
    )
    account["password"] = first_value(
        row,
        "password",
        "pass",
        "pwd",
        "mat_khau",
        "mat khau",
    )
    account["name"] = first_value(row, "name", "ten", "full_name", "fullname")
    account["status"] = first_value(row, "status", "trang_thai", "trang thai")
    account["verify"] = first_value(row, "verify", "check", "login_status")
    account["cookies"] = first_value(row, "cookies", "cookie")
    return account


class SheetApi:
    def __init__(self, config: AppConfig, log=None, should_stop=None):
        self.config = config
        self.log = log or print
        self.should_stop = should_stop or (lambda: False)

    def _with_sheet_id(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self.config.sheet_id:
            payload = dict(payload)
            payload["sheet_id"] = self.config.sheet_id
        return payload

    def _get(self, params: Dict[str, Any], timeout: int = 90, retries: int = 2) -> Dict[str, Any]:
        if not self.config.sheet_api_url:
            raise ValueError("Chua cau hinh sheet_api_url trong config.json")
        params = self._with_sheet_id(params)
        for attempt in range(retries + 1):
            try:
                resp = interruptible_call(requests.get, self.should_stop,self.config.sheet_api_url, params=params, timeout=timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                if attempt < retries:
                    self.log(f"[SHEET] GET timeout lan {attempt + 1}, thu lai...")
                    interruptible_sleep(3, self.should_stop)
                    continue
                raise

    def _post(self, payload: Dict[str, Any], timeout: int = 90, retries: int = 2) -> Dict[str, Any]:
        if not self.config.sheet_api_url:
            raise ValueError("Chua cau hinh sheet_api_url trong config.json")
        payload = self._with_sheet_id(payload)
        body = json.dumps(payload, ensure_ascii=False)
        for attempt in range(retries + 1):
            try:
                resp = interruptible_call(requests.post, self.should_stop,
                    self.config.sheet_api_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    timeout=timeout,
                )
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                if attempt < retries:
                    self.log(f"[SHEET] POST timeout lan {attempt + 1}, thu lai...")
                    interruptible_sleep(3, self.should_stop)
                    continue
                raise

    def ping(self) -> bool:
        try:
            data = self._get({"action": "ping"}, timeout=15, retries=1)
            return data.get("status") == "ok" or data.get("success") is True
        except Exception as exc:
            self.log(f"[SHEET] Ping loi: {exc}")
            return False

    def get_accounts(self) -> List[Dict[str, Any]]:
        data = self._get({"action": "getAccounts"}, timeout=90, retries=2)
        if data.get("status") == "ok":
            rows = data.get("accounts", [])
        elif data.get("success") is True:
            rows = data.get("accounts", data.get("data", []))
        else:
            raise ValueError(data.get("message") or data.get("error") or str(data))
        return [normalize_account(row) for row in rows if isinstance(row, dict)]

    def update_account(
        self,
        email: str,
        *,
        name: str = "",
        status: str = "",
        verify: str = "",
        cookies: str = "",
    ) -> Dict[str, Any]:
        return self._post(
            {
                "action": "update",
                "email": email,
                "name": name,
                "status": status,
                "verify": verify,
                "cookies": cookies,
            },
            timeout=60,
            retries=2,
        )

    def update_quota_column(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self._post(
            {
                "action": "updateQuotaColumn",
                "rows": rows,
            },
            timeout=120,
            retries=2,
        )

    def update_quota_row(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            return self.update_quota_column([row])
        except Exception as exc:
            self.log(f"[SHEET] Ghi quota cot rieng loi: {exc}")
            return None
