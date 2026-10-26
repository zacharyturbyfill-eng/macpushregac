from __future__ import annotations

import json
import random
import re
import threading
from typing import Any, Callable, Dict, List, Optional

import requests

from .cancellation import interruptible_call


API_URL = "https://unlimitmail.com/api/v1/emailDomain"
DOMAIN_LIST_URL = "https://unlimitmail.com/api/getAllDomain"


class UnlimitMailError(RuntimeError):
    pass


class UnlimitMailClient:
    """Adapt the 1secmail-compatible API to the Auto Register mail interface."""

    def __init__(
        self,
        tokens: List[str],
        log: Optional[Callable[[str], None]] = None,
        timeout: int = 20,
        proxies: Optional[Dict[str, str]] = None,
        should_stop: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._tokens = [token.strip() for token in tokens if token and token.strip()]
        self.log = log or (lambda *_: None)
        self.timeout = timeout
        self.proxies = proxies or None
        self.should_stop = should_stop or (lambda: False)
        self._domains: Dict[int, List[Dict[str, Any]]] = {}
        self._domains_lock = threading.Lock()

    @property
    def has_tokens(self) -> bool:
        return bool(self._tokens)

    def _request(self, action: str, worker_index: int, **params: Any) -> Any:
        if self.should_stop():
            raise UnlimitMailError("UnlimitMail request stopped")
        if not self._tokens:
            raise UnlimitMailError("UnlimitMail token is empty")
        token = self._tokens[worker_index % len(self._tokens)]
        domain_list = action == "getAllDomain"
        try:
            response = interruptible_call(requests.get, self.should_stop,
                DOMAIN_LIST_URL if domain_list else API_URL,
                params={} if domain_list else {**params, "action": action, "token": token},
                headers={"Accept": "application/json"},
                proxies=self.proxies,
                timeout=(10, self.timeout),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # requests exceptions contain the complete query URL, including token.
            raise UnlimitMailError(f"UnlimitMail {action}: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise UnlimitMailError(f"UnlimitMail {action}: HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise UnlimitMailError(f"UnlimitMail {action}: invalid JSON response") from None
        if isinstance(payload, dict):
            failed = any(
                str(payload.get(key, "")).lower() in ("false", "0", "error", "failed")
                for key in ("success", "status")
            )
            if failed or payload.get("error"):
                message = str(payload.get("message") or payload.get("error") or "request rejected")
                for secret in self._tokens:
                    message = message.replace(secret, "[REDACTED]")
                message = re.sub(r"https?://\S+", "[URL REDACTED]", message)
                raise UnlimitMailError(f"UnlimitMail {action}: {message[:180]}")
        return payload

    @staticmethod
    def _items(payload: Any, *keys: str) -> List[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in keys:
                if key in payload:
                    value = payload[key]
                    if isinstance(value, list):
                        return value
                    if isinstance(value, dict):
                        return UnlimitMailClient._items(value, *keys)
        raise UnlimitMailError("UnlimitMail returned an unexpected list format")

    def get_domains(self, worker_index: int = 0) -> List[Dict[str, Any]]:
        if not self._tokens:
            raise UnlimitMailError("UnlimitMail token is empty")
        index = worker_index % len(self._tokens)
        with self._domains_lock:
            if index not in self._domains:
                # The compatibility getDomainList endpoint returns a stale list.
                payload = self._request("getAllDomain", worker_index)
                domains = []
                for item in self._items(payload, "data", "domains", "items"):
                    name = item if isinstance(item, str) else (
                        item.get("domain") or item.get("name") if isinstance(item, dict) else ""
                    )
                    name = str(name or "").strip().lower()
                    if name and "." in name and not any(ch in name for ch in " /@\\"):
                        domains.append({"name": name})
                if not domains:
                    raise UnlimitMailError("UnlimitMail has no available domains")
                self._domains[index] = domains
            return [dict(item) for item in self._domains[index]]

    def create_email(
        self,
        worker_index: int = 0,
        *,
        domain: str = "",
        prefer_edu: bool = False,
    ) -> Dict[str, Any]:
        chosen = domain.strip().lower()
        if chosen or prefer_edu:
            domains = [item["name"] for item in self.get_domains(worker_index)]
            if chosen and chosen not in domains:
                raise UnlimitMailError("Selected domain is not available on UnlimitMail")
            if not chosen:
                candidates = [name for name in domains if ".edu" in name]
                if candidates:
                    chosen = random.choice(candidates)
        params: Dict[str, Any] = {"count": 1}
        if chosen:
            params["domain"] = chosen
        # In Auto mode let the server select a creatable domain. The advertised
        # domain list can include names that genRandomMailbox currently rejects.
        # Creating a mailbox is not retried automatically: a timeout may happen
        # after allocation, so retrying could consume a second mailbox/quota.
        payload = self._request("genRandomMailbox", worker_index, **params)
        items = self._mailbox_items(payload)
        if len(items) != 1:
            raise UnlimitMailError("UnlimitMail did not return exactly one mailbox")
        item = items[0]
        address = item if isinstance(item, str) else (
            item.get("email") or item.get("address") if isinstance(item, dict) else ""
        )
        address = str(address or "").strip()
        if address.lower() == "domain not found":
            raise UnlimitMailError("UnlimitMail: Domain not found; select Domain=Auto")
        if "@" not in address:
            message = address
            for secret in self._tokens:
                message = message.replace(secret, "[REDACTED]")
            message = re.sub(r"https?://\S+", "[URL REDACTED]", message)
            raise UnlimitMailError(f"UnlimitMail mailbox allocation rejected: {message[:180]}")
        self._mailbox_parts(address)
        return {"email": address, "id": address}

    @staticmethod
    def _mailbox_items(payload: Any) -> List[Any]:
        """Normalize the API's single-mailbox and legacy list responses."""
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise UnlimitMailError("UnlimitMail returned an unexpected mailbox format")

        for key in ("email", "address"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return [value]

        for key in ("data", "emails", "items", "mailboxes"):
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                for address_key in ("email", "address"):
                    address = value.get(address_key)
                    if isinstance(address, str) and address.strip():
                        return [value]
            if isinstance(value, str) and "@" in value:
                return [value]

        raise UnlimitMailError("UnlimitMail returned an unexpected mailbox format")

    @staticmethod
    def _mailbox_parts(address: str) -> tuple[str, str]:
        login, separator, domain = address.partition("@")
        if not separator or not login or "." not in domain or any(ch.isspace() for ch in address) or "@" in domain:
            raise UnlimitMailError("UnlimitMail returned an invalid mailbox address")
        return login, domain

    def list_messages(self, mail_id: str, worker_index: int = 0) -> List[Dict[str, Any]]:
        login, domain = self._mailbox_parts(mail_id)
        payload = self._request("getMessages", worker_index, login=login, domain=domain)
        messages = []
        for item in self._items(payload, "data", "messages", "items"):
            if not isinstance(item, dict):
                raise UnlimitMailError("UnlimitMail returned an invalid message")
            message = dict(item)
            message_id = message.get("id") or message.get("_id") or message.get("message_id")
            if message_id is None:
                raise UnlimitMailError("UnlimitMail message is missing its ID")
            # Carry mailbox context with the ID. Parallel workers can receive the
            # same numeric message ID in different mailboxes without mixing mail.
            message["id"] = json.dumps([mail_id, str(message_id)], separators=(",", ":"))
            message["from"] = message.get("from") or message.get("sender") or ""
            messages.append(message)
        return messages

    def read_message(self, message_id: str, worker_index: int = 0) -> Dict[str, Any]:
        try:
            address, original_id = json.loads(message_id)
            if not isinstance(address, str) or not isinstance(original_id, str):
                raise ValueError
            login, domain = self._mailbox_parts(address)
        except (ValueError, TypeError):
            raise UnlimitMailError("UnlimitMail message reference is invalid") from None
        payload = self._request("readMessage", worker_index, login=login, domain=domain, id=original_id)
        if not isinstance(payload, dict):
            raise UnlimitMailError("UnlimitMail returned an invalid message detail")
        return payload
