"""Background account replenishment; shares the service operation lock with TTS."""
from __future__ import annotations

import threading
import time
import json
import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

from notegpt_quota_tool.auto_register import AutoRegisterRunner
from notegpt_quota_tool.notegpt import safe_filename
from notegpt_quota_tool.quota_scanner import AccountQuotaScanner
from notegpt_quota_tool.proxy_manager import ProxyManager


class Farm:
    def __init__(self, service, log):
        self.service, self.log = service, log
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._runner = None
        self.enabled = False
        self.state = "idle"
        self.current = None
        self.history = []
        self.last_error = ""
        self.paused_until = 0
        self.wave = 0
        self._bad_waves = 0
        self._last_scan = 0
        self._journal = service.data_dir / 'farm-history.json'
        try:
            self.history = json.loads(self._journal.read_text(encoding='utf-8'))[-100:]
            self.wave = max((int(x.get('wave', 0)) for x in self.history), default=0)
        except (OSError, ValueError, TypeError):
            pass
        self.active_threads = 0
        self._consecutive_errors = 0

    def _save_history(self):
        temp = self._journal.with_suffix('.tmp')
        temp.write_text(json.dumps(self.history[-100:], ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self._journal)

    def _ready(self):
        with self.service.pool._lock:
            return self.service.pool._conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE status='ready' AND quota_left>=?",
                (int(self.service.resources['farm']['min_quota']),)).fetchone()[0]

    def status(self):
        with self._lock:
            return dict(available=True, enabled=self.enabled, state=self.state,
                        current=dict(self.current) if self.current else None,
                        history=list(self.history), last_error=self.last_error,
                        paused_until=self.paused_until, wave_no=self.wave,
                        active_threads=self.active_threads,
                        ready_with_min_quota=self._ready(),
                        target_ready=self.service.resources['farm']['target_ready'])

    def _validate(self):
        res = self.service.resources
        if res['proxy']['provider'] != 'kiot' or not res['proxy'].get('kiot_key', '').strip():
            raise ValueError('Chọn Kiot và lưu API key trước khi đăng ký.')
        if not any(str(t).strip() for t in res['mail'].get('tokens', [])):
            raise ValueError('Cần token UnlimitMail trước khi đăng ký.')

    def start(self):
        return self._launch(True)

    def run_once(self, count=0, threads=0):
        return self._launch(False, count, threads)

    def resume(self):
        batch=self.service.batch_results.snapshot()
        remaining=batch['requested']-batch['done']
        if remaining<=0:raise ValueError('Không còn lượt đăng ký để tiếp tục.')
        return self._launch(False,remaining,0,resume=True)

    def _launch(self, continuous, count=0, threads=0,resume=False):
        self._validate()
        with self._lock:
            if (self._thread and self._thread.is_alive()) or self.state != 'idle':
                raise RuntimeError('Farm đang chạy hoặc đang dừng.')
            cfg = self.service.resources['farm']
            count = int(count or cfg['wave'])
            threads = int(threads or cfg['threads'])
            max_threads = 10 if getattr(self.service, 'role', '') == 'accounts' else 30
            if not 1 <= count <= 5000 or not 1 <= threads <= max_threads:
                raise ValueError(f'Mỗi đợt 1–5000 tài khoản, 1–{max_threads} luồng.')
            self._stop.clear()
            self.enabled = continuous
            self.last_error = ''
            self._consecutive_errors = 0
            self.active_threads = threads
            self.state = 'waiting'
            self._resuming=resume
            if hasattr(self.service,'batch_results'):
                if resume:self.service.batch_results.resume()
                else:self.service.batch_results.start(count)
            self._thread = threading.Thread(target=self._loop, args=(continuous, count, threads), daemon=True)
            self._thread.start()
        return {'ok': True}

    def stop(self):
        with self._lock:
            self.enabled = False
            self._stop.set()
            if self._runner:
                self._runner.request_stop()
            if self._thread and self._thread.is_alive():
                self.state = 'stopping'

    def _loop(self, continuous, count, threads):
        keep_awake = False
        if os.name == 'nt':
            import ctypes
            keep_awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        caffeinate = None
        if sys.platform == 'darwin':
            # Mac: caffeinate giữ máy không ngủ, tự thoát nếu lõi chết (-w pid).
            try:
                caffeinate = subprocess.Popen(['caffeinate', '-i', '-w', str(os.getpid())])
            except OSError:
                caffeinate = None
        try:
            while not self._stop.is_set():
                if self.service.studio.state.get('busy'):
                    self.state = 'waiting_tts'
                    self._stop.wait(1)
                    continue
                if not self.service.operation_lock.acquire(blocking=False):
                    self.state = 'waiting_tts'
                    self._stop.wait(1)
                    continue
                try:
                    if self._stop.is_set():
                        break
                    cfg = self.service.resources['farm']
                    if continuous and time.time() - self._last_scan > 6 * 3600:
                        self._scan()
                    needed = max(0, int(cfg['target_ready']) - self._ready())
                    if not continuous or needed:
                        self._register(min(count, needed) if continuous else count, self.active_threads or threads)
                    self._consecutive_errors = 0
                except Exception as exc:
                    self.last_error = str(exc)
                    self.log(f'[FARM] Đợt lỗi: {exc}')
                    self._consecutive_errors += 1
                    if not continuous or self._consecutive_errors >= 3:
                        raise RuntimeError('Đã dừng để kiểm tra cấu hình/kết nối: ' + str(exc)) from exc
                finally:
                    self.service.operation_lock.release()
                if not continuous:
                    break
                delay = max(5, int(cfg['pause_between_waves_s']))
                if self._consecutive_errors:
                    delay = max(delay, 300 * self._consecutive_errors)
                last = self.history[-1] if self.history else {}
                if last.get('rate_limited') or (last.get('requested', 0) and last.get('failed', 0) / last['requested'] > .25):
                    self.active_threads = max(1, (self.active_threads or threads) // 2)
                    delay = max(delay, 600)
                if self._bad_waves >= 2:
                    delay = max(delay, 600)
                    self._bad_waves = 0
                self.paused_until = time.time() + delay
                self.state = 'waiting'
                self._stop.wait(delay)
        except Exception as exc:
            self.last_error = str(exc)
            self.log(f'[FARM] {exc}')
        finally:
            if hasattr(self.service,'batch_results'):
                self.service.batch_results.finish(self._stop.is_set(),self.last_error)
            if keep_awake:
                ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            if caffeinate:
                caffeinate.terminate()
            with self._lock:
                self.enabled = False
                self.state = 'idle'
                self.current = None
                self._runner = None
                self.paused_until = 0
                self.active_threads = 0

    def _register(self, count, threads):
        self.wave += 1
        started = time.time()
        self.state = 'registering'
        self.current = dict(wave=self.wave, count=count, done=0, ok=0, threads=threads)
        proxy = ProxyManager(self.service.config, log=self.log, should_stop=self._stop.is_set)
        rotated = False
        if getattr(self.service, 'role', '') == 'accounts':
            self.state = 'waiting_proxy'
            while not self._stop.is_set():
                try:
                    if proxy.refresh_from_api():
                        rotated = proxy.rotate_until_success(reason='Mandatory rotation before first group of 10')
                        if rotated or self._stop.is_set():
                            break
                    self.log('[FARM] Waiting for Kiot; no registration started. Retry in 30s.')
                except Exception as exc:
                    self.log(f'[FARM] Kiot unavailable: {exc}; retry in 30s.')
                self._stop.wait(30)
            if self._stop.is_set():
                return
            self.state = 'registering'
        elif self.service.resources['farm'].get('rotate_before_wave', False) and not getattr(self,'_resuming',False):
            self.state = 'waiting_proxy'
            # Read the server's cooldown before requesting a new IP.
            if not proxy.refresh_from_api():
                raise RuntimeError('Không đọc được proxy Kiot hiện tại; kiểm tra key/kết nối.')
            deadline = time.monotonic() + 360
            while not self._stop.is_set() and time.monotonic() < deadline:
                delay = proxy.rotation_wait_remaining()
                if delay > 0:
                    self._stop.wait(min(delay, max(0, deadline-time.monotonic())))
                    continue
                if proxy.rotate(reason='Đổi IP trước đợt đăng ký'):
                    rotated = True
                    proxy.wait_if_rotating('farm')
                    break
                self._stop.wait(30)
            if not rotated:
                raise RuntimeError('Chưa xoay được Kiot trong 6 phút hoặc đã dừng; không bắt đầu đăng ký.')
            self.state = 'registering'

        def completed(index, total, result):
            if result.success:
                cookie = self.service.pool.cookie_dir / f'{safe_filename(result.email)}.json'
                if result.cookies:
                    # The original runner returns a file path, not cookie JSON.
                    raw = str(result.cookies).strip()
                    if raw.startswith('['):
                        parsed = json.loads(raw)
                        if not isinstance(parsed, list):
                            raise ValueError('Cookie không hợp lệ')
                        cookie.write_text(raw, encoding='utf-8')
                    elif Path(raw).is_file() and Path(raw).resolve() != cookie.resolve():
                        cookie.write_bytes(Path(raw).read_bytes())
                self.service.pool.upsert(result.email, result.password, name=result.name,
                    quota_left=result.quota_left, quota_text=result.quota_text,
                    cookie_file=str(cookie) if cookie.exists() else '',
                    status='ready' if result.quota_left > 0 else 'exhausted')
            with self._lock:
                self.current['done'] += 1
                self.current['ok'] += int(result.success)
            if hasattr(self.service,'batch_results'):
                try:
                    self.service.batch_results.completed(result,self.service.pool)
                except Exception as exc:
                    self.last_error='Không lưu được kết quả: '+str(exc)
                    self.log('[FARM] '+self.last_error)
                    self._stop.set()
                    raise

        def rate_limit_stop():
            self._stop.set()
            self.enabled=False
            self.state='stopping'
            self.last_error='Dịch vụ báo giới hạn. Đã dừng toàn bộ; không tự tiếp tục hoặc đổi IP.'
            if hasattr(self.service,'batch_results'):self.service.batch_results.stop_for_rate_limit()

        self._runner = AutoRegisterRunner(self.service.config, log=self.log,
            on_account_done=completed, should_stop=self._stop.is_set,
            proxy_manager=proxy,on_rate_limit=rate_limit_stop,
            continue_registration_errors=getattr(self.service, 'role', '') == 'accounts')
        def rotation_wait(waiting):
            self.state = 'stopping' if self._stop.is_set() else ('waiting_proxy' if waiting else 'registering')

        registration_started = time.time()
        result = self._runner.run(tokens=self.service.resources['mail']['tokens'],
            output_path=self.service.data_dir / 'registered_accounts.tsv', count=count,
            parallel=threads, rotate_every=10 if getattr(self.service, 'role', '') == 'accounts' else 0,
            on_rotation_wait=rotation_wait, mail_provider='unlimitmail', wait_for_captcha=False,
            save_cookies=True, check_quota_after_register=True, only_save_success=True,
            retry_count=0)
        with self._lock:
            self._runner = None
            errors = [r.error for r in result.results if r.error]
            if not self.last_error:self.last_error = result.error or (errors[-1] if errors else '')
            self.history.append(dict(wave=self.wave, at=datetime.now().isoformat(),
                ok=result.success, failed=result.failed, seconds=round(time.time()-started, 1),
                threads=threads, requested=count, rotated=rotated,
                proxy_wait_seconds=round(registration_started-started, 1),
                registration_seconds=round(time.time()-registration_started, 1),
                rate_limited=sum('too many' in r.error.lower() or '429' in r.error for r in result.results)))
            self.history = self.history[-100:]
            self._save_history()
            self._bad_waves = self._bad_waves + 1 if result.failed > result.total / 2 else 0

    def _scan(self):
        self.state = 'scanning'
        with self.service.pool._lock:
            rows = self.service.pool._conn.execute("SELECT * FROM accounts WHERE status!='dead'").fetchall()
        accounts = [self.service.pool._to_account(row) for row in rows]
        result = AccountQuotaScanner(self.service.config, log=self.log,
            should_stop=self._stop.is_set,
            proxy_manager=ProxyManager(self.service.config, log=self.log, should_stop=self._stop.is_set)).scan_accounts(accounts, max_workers=4)
        for item in result.results:
            if item.quota is not None and item.quota.success:
                self.service.pool.set_quota(item.email, item.quota_left, item.quota_text)
        self._last_scan = time.time()
        return {'ok': True, 'scanned': len(result.results)}

    def scan_now(self):
        if self._thread and self._thread.is_alive():
            raise RuntimeError('Dừng farm trước khi quét thủ công.')
        if not self.service.operation_lock.acquire(blocking=False):
            raise RuntimeError('Đang chạy voice hoặc đăng ký; thử quét sau.')
        try:
            self._stop.clear()
            return self._scan()
        finally:
            self.state = 'idle'
            self.service.operation_lock.release()
