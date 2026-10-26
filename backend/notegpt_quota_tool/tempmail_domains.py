from __future__ import annotations

import hashlib
import json
import threading
from typing import Any, Sequence


def normalize_domains(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return list(dict.fromkeys(
        value.strip().lower() for value in values
        if isinstance(value, str) and value.strip() and value.strip().lower() != "auto"
    ))


def token_fingerprint(tokens: Sequence[str]) -> str:
    return hashlib.sha256(json.dumps(list(tokens)).encode("utf-8")).hexdigest()


class TempMailDomainSelection:
    """Manual-refresh state; only user choices are persisted, never API tokens."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        settings = settings or {}
        legacy = (
            settings.get("register_mail_domain", "")
            if str(settings.get("register_mail_provider", "tempmail")).lower() != "unlimitmail"
            else ""
        )
        self.selected = normalize_domains(settings.get("register_tempmail_domains", legacy))
        mode = settings.get("register_tempmail_domain_mode")
        self.mode = mode if mode in ("auto", "selected") else ("selected" if self.selected else "auto")
        self.domains_by_token: list[list[str]] = []
        self.fingerprint: str | None = None

    @property
    def available(self) -> list[str]:
        return sorted({name for names in self.domains_by_token for name in names})

    @property
    def unavailable(self) -> list[str]:
        return [name for name in self.selected if name not in self.available]

    def matches(self, tokens: Sequence[str]) -> bool:
        return self.fingerprint == token_fingerprint(tokens)

    def update(self, tokens: Sequence[str], domains_by_token: list[list[str]]) -> None:
        if not tokens or len(tokens) != len(domains_by_token):
            raise ValueError("Domain list does not match the current tokens. Refresh the list.")
        self.domains_by_token = [normalize_domains(names) for names in domains_by_token]
        self.fingerprint = token_fingerprint(tokens)

    def snapshot(self, tokens: Sequence[str], worker_count: int) -> tuple[list[str], list[list[str]]]:
        if not self.selected:
            raise ValueError("Select at least one domain or use Auto.")
        if not self.matches(tokens):
            raise ValueError("Refresh the domain list for the current tokens before running.")
        pool = TempMailDomainPool(self.selected, self.domains_by_token)
        pool.validate_workers(worker_count)
        return list(self.selected), [list(names) for names in self.domains_by_token]

    def settings(self) -> dict[str, Any]:
        return {
            "register_tempmail_domain_mode": self.mode,
            "register_tempmail_domains": list(self.selected),
        }


class TempMailDomainPool:
    """Choose the least-used eligible domain atomically across mail workers."""

    def __init__(self, selected: Sequence[str], domains_by_token: Sequence[Sequence[str]]) -> None:
        self.selected = normalize_domains(selected)
        if not self.selected:
            raise ValueError("Select at least one domain or use Auto.")
        allowed = [set(normalize_domains(names)) for names in domains_by_token]
        if not allowed:
            raise ValueError("Refresh the domain list before running.")
        missing = set(self.selected).difference(set().union(*allowed))
        if missing:
            raise ValueError("Selected domains are unavailable: " + ", ".join(sorted(missing)))
        self._eligible = [tuple(name for name in self.selected if name in names) for names in allowed]
        self._counts = dict.fromkeys(self.selected, 0)
        self._lock = threading.Lock()

    def validate_workers(self, worker_count: int) -> None:
        for index in range(min(max(1, worker_count), len(self._eligible))):
            if not self._eligible[index]:
                raise ValueError(f"Token #{index + 1} cannot use any selected domain.")

    def next(self, worker_index: int) -> str:
        index = worker_index % len(self._eligible)
        eligible = self._eligible[index]
        if not eligible:
            raise ValueError(f"Token #{index + 1} cannot use any selected domain.")
        with self._lock:
            name = min(eligible, key=self._counts.__getitem__)
            self._counts[name] += 1
            return name
