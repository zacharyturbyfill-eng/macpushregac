from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Optional
from urllib.parse import quote

import requests

from .config import AppConfig
from .cancellation import interruptible_call, check_stop, interruptible_sleep


LogCallback = Callable[[str], None]
StopCallback = Callable[[], bool]


def parse_static_proxy_entry(entry: str) -> tuple[str, str, str]:
    """Parse one static proxy line into ``(server, username, password)``.

    ``server`` is always returned as ``host:port`` without scheme so it can be
    fed straight into :meth:`ProxyManager._server_url`. Supported input shapes::

        host:port
        host:port:user:pass
        user:pass@host:port
        scheme://host:port
        scheme://user:pass@host:port

    Returns ``("", "", "")`` when the line cannot be parsed into host:port.
    """
    value = (entry or "").strip()
    if not value:
        return "", "", ""
    if "://" in value:
        value = value.split("://", 1)[1]

    username = ""
    password = ""
    if "@" in value:
        cred, host_port = value.rsplit("@", 1)
        if ":" in cred:
            username, password = cred.split(":", 1)
        else:
            username = cred
        parts = host_port.split(":")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            return "", "", ""
        return f"{parts[0]}:{parts[1]}", username, password

    parts = value.split(":")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return "", "", ""
    server = f"{parts[0]}:{parts[1]}"
    username = parts[2] if len(parts) >= 3 else ""
    password = ":".join(parts[3:]) if len(parts) >= 4 else ""
    return server, username, password


def parse_static_proxy_list(text: str) -> list[str]:
    """Split a multi-line blob into clean, de-duplicated static proxy lines.

    Accepts newlines, commas and semicolons as separators. Lines that do not
    parse into a valid ``host:port`` are dropped so the rotation pool only ever
    contains usable entries.
    """
    if not text:
        return []
    raw = text.replace(",", "\n").replace(";", "\n").splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in raw:
        line = line.strip()
        if not line:
            continue
        server, _user, _pwd = parse_static_proxy_entry(line)
        if not server:
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def check_static_proxy(
    entry: str,
    *,
    timeout: int = 12,
    test_url: str = "https://api.ipify.org?format=json",
) -> Dict[str, Any]:
    """Probe a single static proxy line and report whether it is alive.

    Returns a dict with: ``entry`` (original line), ``server`` (host:port),
    ``ok`` (bool), ``ip`` (exit IP when reachable), ``latency_ms`` and
    ``error``. Never raises — failures are reported in the ``error`` field so a
    batch check can keep going.
    """
    server, username, password = parse_static_proxy_entry(entry)
    if not server:
        return {
            "entry": entry,
            "server": "",
            "ok": False,
            "ip": "",
            "latency_ms": 0,
            "error": "invalid format (need host:port[:user:pass])",
        }

    scheme_server = server if "://" in server else f"http://{server}"
    if username:
        scheme, rest = scheme_server.split("://", 1)
        user = quote(username, safe="")
        pwd = quote(password, safe="")
        proxy_url = f"{scheme}://{user}:{pwd}@{rest}"
    else:
        proxy_url = scheme_server
    proxies = {"http": proxy_url, "https": proxy_url}

    start = time.time()
    try:
        resp = requests.get(test_url, proxies=proxies, timeout=timeout)
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code != 200:
            return {
                "entry": entry,
                "server": server,
                "ok": False,
                "ip": "",
                "latency_ms": latency_ms,
                "error": f"HTTP {resp.status_code}",
            }
        ip = ""
        try:
            ip = str((resp.json() or {}).get("ip") or "").strip()
        except Exception:
            ip = (resp.text or "").strip()[:60]
        return {
            "entry": entry,
            "server": server,
            "ok": True,
            "ip": ip,
            "latency_ms": latency_ms,
            "error": "",
        }
    except Exception as exc:
        latency_ms = int((time.time() - start) * 1000)
        return {
            "entry": entry,
            "server": server,
            "ok": False,
            "ip": "",
            "latency_ms": latency_ms,
            "error": str(exc)[:160],
        }


def check_static_proxy_list(
    text: str,
    *,
    timeout: int = 12,
    max_workers: int = 10,
    test_url: str = "https://api.ipify.org?format=json",
    on_result: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> list[Dict[str, Any]]:
    """Concurrently probe every static proxy in ``text``.

    Calls ``on_result`` (if given) as each proxy finishes so a UI can stream
    progress. Returns the full list of per-proxy result dicts.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    entries = parse_static_proxy_list(text)
    if not entries:
        return []
    workers = max(1, min(int(max_workers or 1), len(entries)))
    results: list[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(check_static_proxy, entry, timeout=timeout, test_url=test_url): entry
            for entry in entries
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_result is not None:
                try:
                    on_result(result)
                except Exception:
                    pass
    return results


@dataclass(frozen=True)
class ProxySettings:
    enabled: bool = False
    server: str = ""
    username: str = ""
    password: str = ""
    change_ip_url: str = ""
    zing_open_api_key: str = ""
    zing_api_base_url: str = "https://api.zingproxy.com"
    rotate_threshold: int = 3
    error_window_sec: int = 180
    cooldown_sec: int = 65
    settle_sec: int = 10
    use_for_run: bool = True
    use_for_scan: bool = True
    use_for_clone: bool = True
    use_for_register: bool = False
    # Provider selector. ``"zing"`` keeps the original Zing Open API path
    # exactly as before; ``"kiot"`` switches the IP source to KiotProxy
    # using the documented GET ``/api/v1/proxies/{new,current}``
    # endpoints; ``"static"`` rotates through a user-supplied list of static
    # proxies. Anything else falls back to the static single-server credentials.
    provider: str = "zing"
    kiot_api_key: str = ""
    kiot_api_base_url: str = "https://api.kiotproxy.com"
    # Static rotation pool: one ``host:port[:user:pass]`` per line.
    static_proxy_list: tuple[str, ...] = ()
    # Opt-in: when True (and provider == "static"), each worker thread is
    # pinned to its own proxy from the pool (round-robin) so N threads spread
    # evenly across the proxies. Default False keeps the legacy single-active
    # behaviour untouched.
    static_proxy_distribute: bool = False

    @classmethod
    def from_config(cls, config: AppConfig) -> "ProxySettings":
        settings = getattr(config, "ui_settings", {}) or {}

        def bool_value(name: str, default: bool) -> bool:
            if name not in settings:
                return default
            value = settings.get(name)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)

        def int_value(name: str, default: int, minimum: int) -> int:
            try:
                return max(minimum, int(str(settings.get(name, default)).strip()))
            except Exception:
                return default

        return cls(
            enabled=bool_value("proxy_enabled", False),
            server=str(settings.get("proxy_server", "") or "").strip(),
            username=str(settings.get("proxy_username", "") or "").strip(),
            password=str(settings.get("proxy_password", "") or "").strip(),
            change_ip_url=str(settings.get("proxy_change_ip_url", "") or "").strip(),
            zing_open_api_key=str(settings.get("proxy_zing_open_api_key", "") or "").strip(),
            zing_api_base_url=str(settings.get("proxy_zing_api_base_url", "https://api.zingproxy.com") or "").strip()
            or "https://api.zingproxy.com",
            rotate_threshold=int_value("proxy_rotate_threshold", 3, 1),
            error_window_sec=int_value("proxy_error_window_sec", 180, 30),
            cooldown_sec=int_value("proxy_cooldown_sec", 65, 1),
            settle_sec=int_value("proxy_settle_sec", 10, 0),
            use_for_run=bool_value("proxy_use_run", True),
            use_for_scan=bool_value("proxy_use_scan", True),
            use_for_clone=bool_value("proxy_use_clone", True),
            use_for_register=bool_value("proxy_use_register", False),
            provider=str(settings.get("proxy_provider", "zing") or "zing").strip().lower() or "zing",
            kiot_api_key=str(settings.get("proxy_kiot_api_key", "") or "").strip(),
            kiot_api_base_url=str(settings.get("proxy_kiot_api_base_url", "https://api.kiotproxy.com") or "").strip()
            or "https://api.kiotproxy.com",
            static_proxy_list=tuple(parse_static_proxy_list(str(settings.get("proxy_static_list", "") or ""))),
            static_proxy_distribute=bool_value("proxy_static_distribute", False),
        )


class ProxyManager:
    """
    Shared proxy control for browser/API workers.

    It is intentionally passive: when proxy is disabled every method returns no-op
    values, so existing automation paths behave as before.
    """

    _PROXY_ERROR_HINTS = (
        "err_proxy",
        "err_tunnel",
        "proxy",
        "tunnel connection failed",
        "connection reset",
        "connection aborted",
        "connection refused",
        "timed out",
        "timeout",
        "net::",
        "cannot upload audio",
        "upload still settling",
        "timeout waiting for mp3",
        "cannot open text to preview",
        "http 403",
        "http 429",
        "http 500",
        "http 502",
        "http 503",
        "http 504",
    )
    _NON_PROXY_HINTS = (
        "no quota",
        "quota is zero",
        "missing email",
        "missing password",
        "wrong password",
        "login failed",
        "cookie invalid",
        "cookie expired",
        "audio file not found",
        "preview text is empty",
        "over notegpt limit",
        "sheet",
    )

    # Shared rotation cursor for the ``static`` provider. Every worker builds
    # its own ProxyManager from the same config, so the current index must be
    # shared at the class level (guarded by ``_static_lock``) for all workers to
    # advance through the same pool together. Keyed by the pool signature so two
    # different static lists never clash.
    _static_lock = threading.RLock()
    _static_index_by_pool: Dict[tuple[str, ...], int] = {}

    # Distribute mode (opt-in, provider == "static" + proxy_static_distribute):
    # pin each worker THREAD to a fixed proxy from the pool so N threads spread
    # evenly across the proxies (round-robin by thread). Keyed by pool so two
    # different pools never collide. ``_static_thread_pos`` is the next index to
    # hand out per pool; ``_static_thread_map`` remembers which proxy index a
    # given thread already received so the same thread stays on one proxy.
    _static_thread_pos: Dict[tuple[str, ...], int] = {}
    _static_thread_map: Dict[tuple, int] = {}

    def __init__(
        self,
        config: AppConfig,
        log: Optional[LogCallback] = None,
        should_stop: Optional[StopCallback] = None,
        alert: Optional[LogCallback] = None,
    ) -> None:
        self.settings = ProxySettings.from_config(config)
        self.log = log or print
        self.should_stop = should_stop or (lambda: False)
        # Optional high-visibility notifier. When a single static proxy keeps
        # failing we cannot rotate, so we surface a warning here instead. Falls
        # back to the normal log channel when no dedicated alert sink is given.
        self.alert = alert or self.log
        self._lock = threading.RLock()
        self._events: Deque[float] = deque()
        self._last_rotate_at = 0.0
        self._pause_until = 0.0
        self._rotating = False
        self._api_loaded = False
        self._runtime_server = self.settings.server
        self._runtime_username = self.settings.username
        self._runtime_password = self.settings.password
        self._runtime_change_ip_url = self.settings.change_ip_url
        self._kiot_next_rotate_at = 0.0
        # Monotonic revision of the active proxy. Consumers that receive a
        # provider-specific response can wait for one shared rotation instead
        # of independently issuing duplicate rotate requests.
        self._proxy_generation = 0
        # Static rotation: the index this manager has currently acquired from
        # the shared pool. ``None`` until first use so different workers get
        # different proxies (round-robin) instead of all starting on entry 0.
        self._static_assigned_index: Optional[int] = None

    @property
    def enabled(self) -> bool:
        if not self.settings.enabled:
            return False
        if self.settings.provider == "kiot":
            # Kiot needs at least the API key to fetch a proxy.
            return bool(self._runtime_server or self.settings.kiot_api_key)
        if self.settings.provider == "static":
            # Static needs at least one parsed proxy in the pool, or a single
            # server typed in the legacy proxy_server box.
            return bool(self.settings.static_proxy_list or self._runtime_server)
        return bool(self._runtime_server or self.settings.zing_open_api_key)

    def enabled_for(self, purpose: str) -> bool:
        if not self.enabled:
            return False
        purpose = purpose.lower().strip()
        if purpose == "run":
            return self.settings.use_for_run
        if purpose == "scan":
            return self.settings.use_for_scan
        if purpose == "clone":
            return self.settings.use_for_clone
        if purpose == "register":
            return self.settings.use_for_register
        return True

    @property
    def proxy_generation(self) -> int:
        """Return the active proxy revision without triggering an API call."""
        with self._lock:
            return self._proxy_generation

    def _ensure_provider_proxy(self, *, force: bool = False) -> bool:
        """Pick the right runtime proxy depending on the configured provider.

        Both implementations populate ``self._runtime_server`` etc., so
        the rest of the manager doesn't care which one was used. This
        keeps the new Kiot path completely additive.
        """
        provider = (self.settings.provider or "zing").lower()
        if provider == "kiot":
            return self._ensure_kiot_proxy(force=force)
        if provider == "static":
            return self._ensure_static_proxy(force=force)
        return self._ensure_zing_open_api_proxy(force=force)

    def playwright_proxy(self, purpose: str) -> Optional[Dict[str, str]]:
        if not self.enabled_for(purpose):
            return None
        # Distribute mode: pin this thread to its own proxy from the pool.
        if self._distribute_active():
            creds = self._distributed_credentials()
            if creds:
                server, username, password = creds
                proxy: Dict[str, str] = {
                    "server": self._build_server_url(server, username, password, with_auth=False)
                }
                if username:
                    proxy["username"] = username
                if password:
                    proxy["password"] = password
                return proxy
        self._ensure_provider_proxy()
        if not self._runtime_server:
            return None
        proxy = {"server": self._server_url(with_auth=False)}
        if self._runtime_username:
            proxy["username"] = self._runtime_username
        if self._runtime_password:
            proxy["password"] = self._runtime_password
        return proxy

    def requests_proxies(self, purpose: str) -> Optional[Dict[str, str]]:
        if not self.enabled_for(purpose):
            return None
        # Distribute mode: pin this thread to its own proxy from the pool.
        if self._distribute_active():
            creds = self._distributed_credentials()
            if creds:
                server, username, password = creds
                url = self._build_server_url(server, username, password, with_auth=True)
                return {"http": url, "https": url}
        self._ensure_provider_proxy()
        if not self._runtime_server:
            return None
        url = self._server_url(with_auth=True)
        return {"http": url, "https": url}

    def refresh_from_api(self) -> bool:
        return self._ensure_provider_proxy(force=True)

    def current_proxy_summary(self) -> str:
        if self._distribute_active():
            creds = self._distributed_credentials()
            if creds:
                server, username, _pwd = creds
                pool = self._static_pool()
                auth = " (with auth)" if username else ""
                return f"[STATIC:distribute over {len(pool)}] {server}{auth}"
        self._ensure_provider_proxy()
        if not self._runtime_server:
            return "proxy not configured"
        provider_label = (self.settings.provider or "zing").upper()
        if provider_label == "STATIC":
            pool = self._static_pool()
            pos = ""
            if pool and self._static_assigned_index is not None:
                pos = f" [{self._static_assigned_index + 1}/{len(pool)}]"
            auth = " (with auth)" if self._runtime_username else ""
            return f"[STATIC]{pos} {self._runtime_server}{auth}"
        if self._runtime_username:
            return f"[{provider_label}] {self._runtime_server} (with auth)"
        return f"[{provider_label}] {self._runtime_server}"

    def export_runtime_settings(self) -> Dict[str, str]:
        self._ensure_provider_proxy()
        return {
            "server": self._runtime_server,
            "username": self._runtime_username,
            "password": self._runtime_password,
            "change_ip_url": self._runtime_change_ip_url,
        }

    def wait_if_rotating(self, label: str = "") -> None:
        if not self.enabled:
            return
        while not self.should_stop():
            with self._lock:
                remaining = self._pause_until - time.time()
            if remaining <= 0:
                return
            name = f" {label}" if label else ""
            self.log(f"[PROXY] Waiting{name}: rotating/settling {int(remaining) + 1}s")
            interruptible_sleep(min(2.0, max(0.2, remaining)), self.should_stop)

    def _is_single_static(self) -> bool:
        """True when the active provider is static with only one usable proxy.

        In this case there is nothing to rotate to, so error bursts should
        raise a warning instead of triggering a pointless rotate + cooldown.
        """
        if (self.settings.provider or "").lower() != "static":
            return False
        return len(self._static_pool()) < 2

    def report_error(self, source: str, error: Any) -> None:
        if self.should_stop() or not self.enabled:
            return
        text = str(error or "").strip()
        if not self._looks_like_proxy_error(text):
            return
        now = time.time()
        should_rotate = False
        should_warn = False
        with self._lock:
            window_start = now - self.settings.error_window_sec
            while self._events and self._events[0] < window_start:
                self._events.popleft()
            self._events.append(now)
            count = len(self._events)
            self.log(
                f"[PROXY] Error signal {count}/{self.settings.rotate_threshold} "
                f"from {source}: {text[:160]}"
            )
            if count >= self.settings.rotate_threshold:
                if self._distribute_active() or self._is_single_static():
                    # Distribute mode (per-thread pinned proxy) or a single
                    # static IP: there is nothing to rotate to globally. Warn
                    # once per window, then reset the counter so we don't spam.
                    should_warn = True
                    self._events.clear()
                else:
                    should_rotate = True
        if should_warn:
            self.alert(
                f"[PROXY][ALERT] Static proxy reported "
                f"{count} proxy-like errors within {self.settings.error_window_sec}s. "
                f"Last: {text[:160]}. Check the proxy or your network."
            )
        elif should_rotate:
            self.rotate(reason=f"{count} proxy-like errors")

    def rotate(self, reason: str = "") -> bool:
        if self.should_stop() or not self.enabled:
            return False
        # Distribute mode: each thread is pinned to its own proxy, so a global
        # rotate makes no sense (and must not pause workers). Treat as no-op;
        # report_error still warns so the user sees the bad proxy.
        if self._distribute_active():
            return False
        # Single static proxy: nothing to rotate to. Skip entirely so we never
        # impose a settle/cooldown pause that would just stall the workers.
        if self._is_single_static():
            self.log("[PROXY] Static single proxy: rotate is a no-op")
            return False
        with self._lock:
            now = time.time()
            if self._rotating:
                return False
            remaining_cooldown = self._rotation_wait_remaining_locked(now)
            if remaining_cooldown > 0:
                self.log(f"[PROXY] Rotate skipped: cooldown {int(remaining_cooldown) + 1}s")
                return False
            self._rotating = True

        provider = (self.settings.provider or "zing").lower()
        rotation_succeeded = False
        try:
            if provider == "kiot":
                rotation_succeeded = self._rotate_kiot(reason)
            elif provider == "static":
                rotation_succeeded = self._rotate_static(reason)
            else:
                rotation_succeeded = self._rotate_zing(reason)
            return rotation_succeeded
        except Exception as exc:
            self.log(f"[PROXY] Rotate IP failed: {exc}")
            return False
        finally:
            with self._lock:
                self._events.clear()
                # A failed Kiot request is not a rotation. Keeping the last
                # successful timestamp lets strict AutoReg retry in 30s
                # instead of incorrectly restarting the 240s cooldown.
                if rotation_succeeded or provider != "kiot":
                    self._last_rotate_at = time.time()
                    self._pause_until = self._last_rotate_at + max(
                        0, self.settings.settle_sec
                    )
                if rotation_succeeded:
                    self._proxy_generation += 1
                self._rotating = False

    def _rotation_wait_remaining_locked(self, now: Optional[float] = None) -> float:
        current = time.time() if now is None else now
        provider = (self.settings.provider or "zing").lower()
        cooldown_sec = float(self.settings.cooldown_sec)
        local_deadline = (
            self._last_rotate_at + cooldown_sec if self._last_rotate_at else 0.0
        )
        provider_deadline = self._kiot_next_rotate_at if provider == "kiot" else 0.0
        return max(0.0, local_deadline - current, provider_deadline - current)

    def rotation_wait_remaining(self) -> float:
        with self._lock:
            return self._rotation_wait_remaining_locked()

    def rotate_until_success(
        self,
        reason: str = "",
        *,
        retry_interval_sec: float = 30.0,
    ) -> bool:
        """Keep workers blocked until rotation succeeds or Stop is requested.

        ``rotate()`` remains a single-attempt API for existing flows. AutoReg
        uses this stricter helper only at the boundary between account waves.
        """
        attempt = 0
        while not self.should_stop():
            remaining = self.rotation_wait_remaining()
            if remaining > 0:
                provider = (self.settings.provider or "proxy").upper()
                self.log(
                    f"[PROXY] {provider} rotate queued; workers blocked, "
                    f"next API call in {max(1, int(remaining + 0.999))}s"
                )
                next_progress_log = time.time() + 60.0
                while not self.should_stop():
                    remaining = self.rotation_wait_remaining()
                    if remaining <= 0:
                        break
                    now = time.time()
                    if now >= next_progress_log:
                        self.log(
                            f"[PROXY] {provider} rotate still waiting: "
                            f"{max(1, int(remaining + 0.999))}s"
                        )
                        next_progress_log = now + 60.0
                    time.sleep(min(0.25, max(0.01, remaining)))
                if self.should_stop():
                    return False

            attempt += 1
            if self.rotate(reason=reason):
                self.wait_if_rotating("after successful rotate")
                return not self.should_stop()
            wait_sec = max(float(retry_interval_sec), self.rotation_wait_remaining())
            self.log(
                f"[PROXY] Rotate attempt {attempt} failed; workers remain "
                f"blocked, next retry in {max(1, int(wait_sec + 0.999))}s"
            )
            deadline = time.time() + wait_sec
            while not self.should_stop() and time.time() < deadline:
                time.sleep(min(0.25, max(0.01, deadline - time.time())))
        return False

    def _rotate_zing(self, reason: str) -> bool:
        self._ensure_zing_open_api_proxy()
        change_ip_url = self._runtime_change_ip_url
        if not change_ip_url and self.settings.zing_open_api_key:
            change_ip_url = self._zing_api_url(f"/open/change-ip/{self.settings.zing_open_api_key}")
        if not change_ip_url:
            self.log("[PROXY] Rotate needed but Change IP URL is empty")
            return False
        message = f" | {reason}" if reason else ""
        self.log(f"[PROXY] Rotate IP start (zing){message}")
        resp = interruptible_call(requests.get, self.should_stop, change_ip_url, timeout=30)
        ok = 200 <= resp.status_code < 300
        if ok:
            try:
                data = resp.json() or {}
                api_ok = str(data.get("status", "")).lower() in ("", "success") and int(data.get("statusCode", 200) or 200) < 400
                if not api_ok:
                    self.log(f"[PROXY] Rotate IP API error: {data.get('error') or data.get('message') or data}")
                    return False
            except Exception:
                pass
            self.log("[PROXY] Rotate IP OK (zing)")
            self._ensure_zing_open_api_proxy(force=True)
        else:
            self.log(f"[PROXY] Rotate IP HTTP {resp.status_code}: {resp.text[:180]}")
        return ok

    def _rotate_kiot(self, reason: str) -> bool:
        """Ask KiotProxy for the next proxy and update runtime credentials.

        Uses GET ``/api/v1/proxies/new`` with the proxy key query parameter.
        KiotProxy enforces
        its own per-key cooldown via ``nextRequestAt``; the manager's
        ``cooldown_sec`` is still respected on top so worker pause logic
        keeps working.
        """
        if not self.settings.kiot_api_key:
            self.log("[PROXY] Rotate needed but Kiot API key is empty")
            return False
        message = f" | {reason}" if reason else ""
        self.log(f"[PROXY] Rotate IP start (kiot){message}")
        ok = self._fetch_kiot_proxy(force=True)
        if ok:
            self.log("[PROXY] Rotate IP OK (kiot)")
        return ok

    # ---------- Static proxy list rotation -----------------------------

    def _static_pool(self) -> tuple[str, ...]:
        """Effective static pool: parsed list, or the single legacy server."""
        if self.settings.static_proxy_list:
            return self.settings.static_proxy_list
        if self.settings.server:
            return (self.settings.server,)
        return ()

    def _apply_static_entry(self, entry: str) -> bool:
        server, username, password = parse_static_proxy_entry(entry)
        if not server:
            return False
        with self._lock:
            self._runtime_server = server
            self._runtime_username = username
            self._runtime_password = password
            self._runtime_change_ip_url = ""
            self._api_loaded = True
        return True

    # ---------- Distribute mode (opt-in, additive) ---------------------

    def _distribute_active(self) -> bool:
        """True when per-thread proxy distribution should be used.

        Only kicks in for provider == "static" with the opt-in flag set and a
        pool that actually has more than one proxy. Otherwise callers fall back
        to the original single-active path, so legacy behaviour is untouched.
        """
        if not self.settings.static_proxy_distribute:
            return False
        if (self.settings.provider or "").lower() != "static":
            return False
        return len(self._static_pool()) >= 2

    def _distributed_entry(self) -> Optional[str]:
        """Return the proxy line pinned to the CURRENT thread (round-robin).

        Each distinct thread gets the next proxy index the first time it asks,
        then keeps that same proxy for the rest of its life. Assignment is
        class-level + lock-guarded so all workers across the run share one
        fair round-robin sequence per pool.
        """
        pool = self._static_pool()
        if not pool:
            return None
        thread_id = threading.get_ident()
        key = (pool, thread_id)
        with ProxyManager._static_lock:
            idx = ProxyManager._static_thread_map.get(key)
            if idx is None:
                pos = ProxyManager._static_thread_pos.get(pool, 0)
                idx = pos % len(pool)
                ProxyManager._static_thread_pos[pool] = (pos + 1) % len(pool)
                ProxyManager._static_thread_map[key] = idx
        return pool[idx]

    def _distributed_credentials(self) -> Optional[tuple[str, str, str]]:
        entry = self._distributed_entry()
        if not entry:
            return None
        server, username, password = parse_static_proxy_entry(entry)
        if not server:
            return None
        return server, username, password

    def _ensure_static_proxy(self, *, force: bool = False) -> bool:
        """Load a static proxy for this worker from the shared rotation cursor.

        On first acquisition each ProxyManager instance grabs the *next* index
        from the shared cursor and advances it, so concurrent workers spread
        across the pool (round-robin) instead of all hammering entry 0.
        ``force=True`` (rotation) moves this worker to the next proxy in the
        pool. The shared cursor is class-level and guarded by ``_static_lock``.
        """
        pool = self._static_pool()
        if not pool:
            return bool(self._runtime_server)

        with self._lock:
            already_loaded = self._api_loaded and bool(self._runtime_server)
        if already_loaded and not force and self._static_assigned_index is not None:
            return True

        with ProxyManager._static_lock:
            cursor = ProxyManager._static_index_by_pool.get(pool, 0)
            if self._static_assigned_index is None or force:
                idx = cursor % len(pool)
                ProxyManager._static_index_by_pool[pool] = (idx + 1) % len(pool)
            else:
                idx = self._static_assigned_index % len(pool)
            self._static_assigned_index = idx
            entry = pool[idx]

        if self._apply_static_entry(entry):
            self.log(
                f"[PROXY] Static proxy [{idx + 1}/{len(pool)}]: {self._runtime_server}"
                + (" (with auth)" if self._runtime_username else "")
            )
            return True
        self.log(f"[PROXY] Static proxy entry invalid: {entry}")
        return False

    def _rotate_static(self, reason: str) -> bool:
        pool = self._static_pool()
        if not pool:
            self.log("[PROXY] Rotate needed but static proxy list is empty")
            return False
        if len(pool) < 2:
            # Nothing to rotate to; keep the single proxy and let the caller
            # decide. Report as no-op so the cooldown logic still applies.
            self.log("[PROXY] Static list has a single proxy; nothing to rotate to")
            return False
        message = f" | {reason}" if reason else ""
        self.log(f"[PROXY] Rotate IP start (static){message}")
        ok = self._ensure_static_proxy(force=True)
        if ok:
            self.log("[PROXY] Rotate IP OK (static)")
        return ok

    def _server_url(self, *, with_auth: bool) -> str:
        server = self._runtime_server.strip()
        if not server:
            return ""
        if "://" not in server:
            server = f"http://{server}"
        if with_auth and self._runtime_username:
            scheme, rest = server.split("://", 1)
            user = quote(self._runtime_username, safe="")
            password = quote(self._runtime_password, safe="")
            return f"{scheme}://{user}:{password}@{rest}"
        return server

    @staticmethod
    def _build_server_url(server: str, username: str, password: str, *, with_auth: bool) -> str:
        """Build a proxy URL from explicit credentials (distribute mode helper).

        Unlike :meth:`_server_url` this does not read the shared ``_runtime_*``
        fields, so it is safe to call concurrently from many threads.
        """
        server = (server or "").strip()
        if not server:
            return ""
        if "://" not in server:
            server = f"http://{server}"
        if with_auth and username:
            scheme, rest = server.split("://", 1)
            user = quote(username, safe="")
            pwd = quote(password or "", safe="")
            return f"{scheme}://{user}:{pwd}@{rest}"
        return server

    def _ensure_zing_open_api_proxy(self, *, force: bool = False) -> bool:
        key = self.settings.zing_open_api_key
        if not key:
            return bool(self._runtime_server)
        with self._lock:
            if self._api_loaded and not force and self._runtime_server:
                return True
        try:
            url = self._zing_api_url(f"/open/get-proxy/{key}")
            self.log("[PROXY] Load ZingProxy Open API proxy info...")
            resp = interruptible_call(requests.get, self.should_stop, url, timeout=30)
            if resp.status_code != 200:
                self.log(f"[PROXY] Zing API HTTP {resp.status_code}: {resp.text[:180]}")
                return False
            data = resp.json() or {}
            if str(data.get("status", "")).lower() not in ("", "success") or int(data.get("statusCode", 200) or 200) >= 400:
                self.log(f"[PROXY] Zing API error: {data.get('error') or data.get('message') or data}")
                return False
            proxy = data.get("proxy") or {}
            if not isinstance(proxy, dict):
                self.log("[PROXY] Zing API response missing proxy object")
                return False

            server, username, password = self._proxy_credentials_from_zing(proxy)
            if not server:
                self.log("[PROXY] Zing API response missing httpProxy/loginCredential")
                return False

            change_ip_url = str(proxy.get("linkChangeIp") or "").strip()
            if not change_ip_url:
                change_ip_url = self._zing_api_url(f"/open/change-ip/{key}")

            with self._lock:
                self._runtime_server = server
                self._runtime_username = username
                self._runtime_password = password
                self._runtime_change_ip_url = change_ip_url
                self._api_loaded = True
            self.log(f"[PROXY] Zing API proxy loaded: {server}")
            return True
        except Exception as exc:
            self.log(f"[PROXY] Zing API load failed: {exc}")
            return False

    def _proxy_credentials_from_zing(self, proxy: Dict[str, Any]) -> tuple[str, str, str]:
        credential = str(proxy.get("httpProxy") or "").strip()
        if not credential:
            credentials = proxy.get("loginCredentials") or []
            if isinstance(credentials, list) and credentials:
                first = credentials[0]
                if isinstance(first, dict):
                    credential = str(first.get("loginCredential") or "").strip()
                else:
                    credential = str(first or "").strip()
        if credential:
            parsed = self._parse_proxy_credential(credential)
            if parsed[0]:
                return parsed

        host = str(proxy.get("ipHostname") or proxy.get("hostIp") or proxy.get("ip") or "").strip()
        port = str(proxy.get("portHttp") or proxy.get("port") or "").strip()
        username = str(proxy.get("username") or "").strip()
        password = str(proxy.get("password") or "").strip()
        if host and port:
            return f"{host}:{port}", username, password
        return "", "", ""

    def _parse_proxy_credential(self, credential: str) -> tuple[str, str, str]:
        value = credential.strip()
        if "://" in value:
            value = value.split("://", 1)[1]
        parts = value.split(":")
        if len(parts) < 2:
            return "", "", ""
        server = f"{parts[0]}:{parts[1]}"
        username = parts[2] if len(parts) >= 3 else ""
        password = ":".join(parts[3:]) if len(parts) >= 4 else ""
        return server, username, password

    def _zing_api_url(self, path: str) -> str:
        base = self.settings.zing_api_base_url.rstrip("/") or "https://api.zingproxy.com"
        return f"{base}/{path.lstrip('/')}"

    # ---------- KiotProxy ----------------------------------------------

    def _kiot_api_url(self, path: str) -> str:
        base = (self.settings.kiot_api_base_url or "https://api.kiotproxy.com").rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    @staticmethod
    def _kiot_wait_seconds(payload: Any) -> float:
        if not isinstance(payload, dict):
            return 0.0
        for key in ("ttc", "retryAfter", "retry_after", "waitSeconds"):
            value = payload.get(key)
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds >= 0:
                return seconds
        raw = payload.get("nextRequestAt") or payload.get("next_request_at")
        if raw in (None, ""):
            nested = payload.get("data")
            return ProxyManager._kiot_wait_seconds(nested)
        try:
            timestamp = float(raw)
            if timestamp > 10_000_000_000:
                timestamp /= 1000.0
            return max(0.0, timestamp - time.time())
        except (TypeError, ValueError):
            pass
        try:
            parsed = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, parsed.timestamp() - time.time())
        except (TypeError, ValueError):
            return 0.0

    def _remember_kiot_wait(self, payload: Any) -> None:
        seconds = self._kiot_wait_seconds(payload)
        if seconds <= 0:
            return
        with self._lock:
            self._kiot_next_rotate_at = max(
                self._kiot_next_rotate_at,
                time.time() + seconds + 1.0,
            )

    def _ensure_kiot_proxy(self, *, force: bool = False) -> bool:
        """Make sure the current proxy from KiotProxy is loaded.

        On the first call (or when ``force=True``) we hit
        ``/api/v1/proxies/current`` so we don't burn the per-key
        rotate cooldown just for an initial probe. If KiotProxy returns
        ``PROXY_NOT_FOUND_BY_KEY`` (no active proxy) we fall back to
        ``/new`` which actually allocates one.
        """
        key = self.settings.kiot_api_key
        if not key:
            return bool(self._runtime_server)
        with self._lock:
            if self._api_loaded and not force and self._runtime_server:
                return True
        return self._fetch_kiot_proxy(force=False)

    def _fetch_kiot_proxy(self, *, force: bool) -> bool:
        key = self.settings.kiot_api_key
        if not key:
            return False
        # When ``force`` is True we want a brand-new IP, so go straight to
        # ``get-new``. Otherwise prefer ``get-current`` (no cooldown).
        first_path = "/api/v1/proxies/new" if force else "/api/v1/proxies/current"
        order = [first_path]
        if first_path == "/api/v1/proxies/current":
            order.append("/api/v1/proxies/new")
        last_error = ""
        for path in order:
            try:
                resp = interruptible_call(requests.get, self.should_stop,
                    self._kiot_api_url(path),
                    params={"key": key},
                    timeout=20,
                )
            except Exception as exc:
                last_error = type(exc).__name__
                break
            try:
                data = resp.json() or {}
            except Exception:
                last_error = f"non-json response (HTTP {resp.status_code})"
                break
            if not isinstance(data, dict):
                last_error = "invalid response object"
                break
            if resp.status_code == 200 and data.get("success"):
                proxy_info = data.get("data") or {}
                http_pair = str(proxy_info.get("http") or "").strip()
                host = str(proxy_info.get("host") or "").strip()
                port = proxy_info.get("httpPort") or proxy_info.get("port") or 0
                if not http_pair and host and port:
                    http_pair = f"{host}:{port}"
                if not http_pair:
                    last_error = "kiot response missing http host/port"
                    break
                with self._lock:
                    self._runtime_server = http_pair
                    # KiotProxy tokens authorize by key (whitelisted IP), not
                    # by basic auth, so leave user/password empty.
                    self._runtime_username = ""
                    self._runtime_password = ""
                    self._runtime_change_ip_url = ""
                    self._api_loaded = True
                self._remember_kiot_wait(proxy_info)
                self.log(
                    f"[PROXY] Kiot proxy loaded: {http_pair} | "
                    f"location={proxy_info.get('location') or '?'} "
                    f"region={proxy_info.get('region') or '?'} "
                    f"ttc={proxy_info.get('ttc') or '?'}s"
                )
                return True
            # Non-success: capture and try next path.
            last_error = (
                f"{path} HTTP {resp.status_code}: "
                f"{str(data.get('error') or data.get('code') or 'request failed').replace(key, '[redacted]')}"
            )
            self._remember_kiot_wait(data)
            if data.get('error') != 'PROXY_NOT_FOUND_BY_KEY' or path != '/api/v1/proxies/current':
                break
        self.log(f"[PROXY] Kiot API load failed: {last_error}")
        return False

    def _looks_like_proxy_error(self, text: str) -> bool:
        lowered = text.lower()
        if not lowered:
            return False
        if any(hint in lowered for hint in self._NON_PROXY_HINTS):
            return False
        return any(hint in lowered for hint in self._PROXY_ERROR_HINTS)
