"""Independent NoteGPT Studio: localhost UI, TTS and account registration.

Run with START.bat or python service.py --port 8830.
All account data, imported voices, caches and outputs belong to data-dir.
The original NoteGPT browser automation is reused through local adapters.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import uuid
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='replace')

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from notegpt_quota_tool.config import AppConfig  # noqa: E402
from notegpt_quota_tool.proxy_manager import ProxyManager  # noqa: E402
from notegpt_quota_tool.long_text_job import JobStore, SplitConfig, TextSplitter  # noqa: E402
from notegpt_quota_tool.multi_long_text_runner import MultiAccountLongTextRunner  # noqa: E402
from notegpt_quota_tool.audio_merger import JobAudioMerger  # noqa: E402
from studio import Studio, ConfiguredMerger
from notegpt_quota_tool.cancellation import run_process, check_stop
from urllib.parse import urlparse, parse_qs
from pool import AccountPool  # noqa: E402

DEFAULT_RESOURCES: Dict[str, Any] = {
    "proxy": {"provider": "none", "kiot_key": "", "static_list": [], "zing_key": ""},
    "mail": {"provider": "unlimitmail", "tokens": []},
    "farm": {"target_ready": 100, "min_quota": 5, "threads": 2, "wave": 5, "pause_between_waves_s": 60, "enabled": False},
    # workers=8: đo 23/10/2026 qua Kiot 1 IP, 8 phiên không lỗi, 358 s audio / 146 s (xem docs/THIET_KE_GIONG_NOTEGPT.md)
    "tts": {"workers": 2, "max_workers": 12, "max_chars": 285, "target_chars": 260, "chunk_timeout_s": 100, "fail_stop": 3},
}


from collections import deque  # noqa: E402

LOG_RING: deque = deque(maxlen=400)


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    LOG_RING.append(line)
    print(line, flush=True)


class Service:
    def __init__(self, data_dir: Path, ffmpeg: str = "") -> None:
        data_dir = data_dir.resolve()
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.res_path = data_dir / "resources.json"
        self.resources = self._load_resources()
        self.pool = AccountPool(data_dir / "pool.sqlite", data_dir / "cookies")
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(exist_ok=True)
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg") or ""
        self.config = self._build_config()
        self.proxy = ProxyManager(self.config, log=log)
        self._lock = threading.Lock()
        self.operation_lock = threading.RLock()
        self.stop_event = threading.Event()
        self.studio = Studio(self)
        self.job: Optional[Dict[str, Any]] = None
        self.runner: Optional[MultiAccountLongTextRunner] = None
        self.started_at = time.time()
        self.farm = None
        try:
            from farm import Farm  # tuỳ chọn: file farm.py (treo tạo tài khoản) có thể chưa có
            self.farm = Farm(self, log)
        except ImportError:
            log("[FARM] farm.py chưa có: chỉ chạy TTS + quản lý kho")

    def save_resources(self, data: Dict[str, Any]) -> Dict[str, Any]:
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError('Đang có tác vụ khác; lưu cấu hình sau.')
        try:
            return self._save_resources(data)
        finally:
            self.operation_lock.release()

    def _save_resources(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Ghi resources.json từ trang quản lý rồi dựng lại AppConfig + ProxyManager (áp dụng cho job sau)."""
        if (self.job and self.job.get("busy")) or self.studio.state.get("busy") or (self.farm and self.farm.status()["state"] != "idle"):
            raise RuntimeError("Dừng voice và farm trước khi lưu tài nguyên.")
        self._validate_resources(data)
        for k in ("proxy", "mail", "farm", "tts"):
            if isinstance(data.get(k), dict):
                self.resources[k].update(data[k])
        self.res_path.write_text(json.dumps(self.resources, ensure_ascii=False, indent=2), encoding="utf-8")
        self.config = self._build_config()
        self.proxy = ProxyManager(self.config, log=log)
        log("[RES] đã lưu tài nguyên, dựng lại proxy")
        return self.resources

    def import_voice(self, body):
        suffix = Path(str(body.get('filename') or '')).suffix.lower()
        if suffix not in ('.mp3', '.wav', '.m4a'):
            raise ValueError('Mẫu giọng cần là MP3, WAV hoặc M4A.')
        content = base64.b64decode(body.get('content') or '', validate=True)
        if not content or len(content) > 20 * 1024 * 1024:
            raise ValueError('Mẫu giọng phải lớn hơn 0 và tối đa 20 MB.')
        folder = self.data_dir / 'voices' / uuid.uuid4().hex[:12]
        folder.mkdir(parents=True)
        filename = 'sample' + suffix
        (folder / filename).write_bytes(content)
        spec = {'name': str(body.get('name') or body.get('filename') or 'Giọng mới')[:100], 'ref_file': filename}
        (folder / 'giong.json').write_text(json.dumps(spec, ensure_ascii=False), encoding='utf-8')
        return {'ok': True, 'path': str(folder), **spec}

    def import_accounts(self, body):
        if self.studio.state.get('busy') or (self.farm and self.farm.state != 'idle'):
            raise RuntimeError('Dừng voice và farm trước khi nhập tài khoản.')
        if not self.operation_lock.acquire(blocking=False):
            raise RuntimeError('Đang có tác vụ khác.')
        try:
            content = str(body.get('text') or '')
            if not content or len(content) > 20_000_000:
                raise ValueError('TSV trống hoặc quá lớn.')
            path = self.data_dir / 'import.tsv'
            path.write_text(content.lstrip('\ufeff'), encoding='utf-8')
            try:
                count = self.pool.import_tsv(path)
            finally:
                path.unlink(missing_ok=True)
            if not count:
                raise ValueError('Không tìm thấy tài khoản. File cần cột email và password phân cách bằng Tab.')
            return {'ok': True, 'imported': count}
        finally:
            self.operation_lock.release()

    def _validate_resources(self, data):
        limits = {'tts': {'workers': (1, 12), 'max_workers': (1, 12), 'max_chars': (100, 285),
                          'target_chars': (80, 285), 'chunk_timeout_s': (30, 600), 'fail_stop': (1, 10)},
                  'farm': {'target_ready': (0, 10000), 'min_quota': (1, 1000), 'threads': (1, 30),
                           'wave': (1, 5000), 'pause_between_waves_s': (5, 86400)}}
        for section, values in limits.items():
            for key, (low, high) in values.items():
                if key in data.get(section, {}):
                    value = data[section][key]
                    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                        raise ValueError(f'{section}.{key}: cần số nguyên từ {low} đến {high}')
        if data.get('proxy', {}).get('provider', 'none') not in ('none', 'kiot', 'zing', 'static'):
            raise ValueError('Proxy provider không hợp lệ')

    def check_resources(self) -> Dict[str, Any]:
        """Kiểm tra nhanh: API Kiot/Zing trả proxy?, proxy mở được notegpt.io?, token UnlimitMail lấy được domain?"""
        import requests
        out: Dict[str, Any] = {}
        p = self.resources["proxy"]
        provider = str(p.get("provider") or "none")
        try:
            if provider == "none":
                r = requests.get("https://notegpt.io/", timeout=15)
                out["proxy"] = f"không proxy, notegpt.io HTTP {r.status_code}"
            else:
                pm = ProxyManager(self.config, log=lambda _m: None)
                px = pm.playwright_proxy("clone")
                if not px:
                    out["proxy"] = "không lấy được proxy (key sai hoặc danh sách rỗng)"
                else:
                    server = px.get("server", "")
                    auth = ""
                    if px.get("username"):
                        auth = f"{px['username']}:{px.get('password', '')}@"
                    url = server if "://" in server else f"http://{auth}{server}"
                    r = requests.get("https://notegpt.io/", proxies={"http": url, "https": url}, timeout=20)
                    out["proxy"] = f"{provider} {server} → notegpt.io HTTP {r.status_code}"
        except Exception as exc:  # noqa: BLE001
            out["proxy"] = f"lỗi: {exc}"
        try:
            tokens = [t for t in self.resources["mail"].get("tokens") or [] if t.strip()]
            if not tokens:
                out["mail"] = "chưa có token"
            else:
                from notegpt_quota_tool.unlimitmail import UnlimitMailClient
                doms = UnlimitMailClient(tokens, log=lambda _m: None).get_domains(0)
                out["mail"] = f"UnlimitMail OK, {len(doms)} domain"
        except Exception as exc:  # noqa: BLE001
            out["mail"] = f"lỗi: {exc}"
        return out

    def pool_list(self, limit: int = 200) -> list:
        with self.pool._lock:
            rows = self.pool._conn.execute(
                "SELECT email,name,status,quota_left,last_used_at,last_scan_at,created_at,note FROM accounts "
                "ORDER BY status='ready' DESC, quota_left DESC, created_at DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ---------- cấu hình ----------
    def _load_resources(self) -> Dict[str, Any]:
        data = json.loads(json.dumps(DEFAULT_RESOURCES))
        if self.res_path.exists():
            try:
                user = json.loads(self.res_path.read_text(encoding="utf-8"))
                for k, v in user.items():
                    if isinstance(v, dict) and isinstance(data.get(k), dict):
                        data[k].update(v)
                    else:
                        data[k] = v
            except Exception as exc:  # noqa: BLE001
                log(f"[RES] resources.json lỗi, dùng mặc định: {exc}")
        else:
            self.res_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def _build_config(self) -> AppConfig:
        """Dịch resources.json sang AppConfig + ui_settings mà lõi tool cũ đọc. Luôn dùng đường upload strict."""
        p = self.resources["proxy"]
        provider = str(p.get("provider") or "none").lower()
        ui: Dict[str, Any] = {
            "proxy_enabled": provider != "none",
            "proxy_provider": provider if provider != "none" else "static",
            "proxy_kiot_api_key": p.get("kiot_key", ""),
            "proxy_zing_open_api_key": p.get("zing_key", ""),
            "proxy_static_list": "\n".join(p.get("static_list") or []),
            "proxy_static_distribute": True,
            "proxy_use_clone": True, "proxy_use_run": True, "proxy_use_scan": True, "proxy_use_register": True,
            "proxy_upload_use_button": True,
            "proxy_upload_wait_sec": 15, "proxy_upload_retries": 2,
            "proxy_rotate_threshold": 2, "proxy_error_window_sec": 180, "proxy_cooldown_sec": 65, "proxy_settle_sec": 10,
            "long_fast_reuse_voice": True,
            "register_mail_provider": self.resources["mail"].get("provider", "unlimitmail"),
            "register_unlimitmail_tokens": "\n".join(self.resources["mail"].get("tokens") or []),
        }
        return AppConfig(
            cookie_dir=str(self.data_dir / "cookies"), headless=True, max_workers=1,
            timeout_ms=60000, slow_mo_ms=50, reuse_cookies=True, save_cookies=True,
            quota_threshold=0, ui_settings=ui, config_path=self.data_dir / "config.generated.json",
        )

    # ---------- giọng mẫu ----------
    def _sample_for(self, body: Dict[str, Any]) -> Path:
        """NoteGPT nhận MP3/M4A/WAV 10 s–5 phút, ≤20 MB. Thư viện giọng local có mau.wav → chuyển mp3 1 lần, cache cạnh giọng."""
        ref = body.get("ref_audio")
        vdir = body.get("voice_dir")
        if vdir:
            vdir_p = Path(vdir)
            spec_p = vdir_p / "giong.json"
            spec = json.loads(spec_p.read_text(encoding="utf-8")) if spec_p.exists() else {}
            ref = str(vdir_p / (spec.get("ref_file") or "mau.wav"))
            cache = vdir_p / "notegpt_sample.mp3"
        else:
            if not ref:
                raise ValueError("thiếu voice_dir hoặc ref_audio")
            cache = Path(ref).with_suffix(".notegpt.mp3")
        src = Path(ref)
        cache_root = self.data_dir / 'samples'
        cache_root.mkdir(exist_ok=True)
        cache = cache_root / (hashlib.sha256(str(src.resolve()).encode()).hexdigest() + '.mp3')
        if not src.exists():
            raise FileNotFoundError(f"không thấy mẫu giọng: {src}")
        if cache.exists() and cache.stat().st_mtime >= src.stat().st_mtime:
            return cache
        if not self.ffmpeg:
            raise RuntimeError("không tìm thấy ffmpeg để chuyển mẫu giọng sang mp3")
        # NoteGPT đòi 10 s–5 phút: mẫu ngắn (thư viện local hay 9–10 s) đệm im lặng tới 11 s, dài thì cắt 4 phút 50 giây.
        # mono 32 kHz 96 kbps: nhỏ, đủ cho clone.
        cmd = [self.ffmpeg, "-y", "-v", "error", "-i", str(src), "-t", "290", "-af", "apad=whole_dur=11",
               "-ac", "1", "-ar", "32000", "-b:a", "96k", str(cache)]
        result = run_process(cmd, should_stop=self.stop_event.is_set, timeout=120, output_path=cache,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("Không chuyển được mẫu giọng.")
        return cache

    # ---------- TTS ----------
    def tts(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if self.studio.state.get('busy') or not self.operation_lock.acquire(blocking=False):
            raise RuntimeError('Đang chạy voice hoặc farm.')
        try:
            self.stop_event.clear()
            return self._tts(body)
        finally:
            self.operation_lock.release()

    def _tts(self, body: Dict[str, Any]) -> Dict[str, Any]:
        check_stop(self.stop_event.is_set)
        text = str(body.get("text") or "").strip()
        out_path = body.get("out_path")
        if not text or not out_path:
            raise ValueError("cần text và out_path")
        with self._lock:
            if self.job and self.job.get("busy"):
                raise RuntimeError("đang bận job khác")
            self.job = {"busy": True, "id": body.get("job_id") or "", "label": body.get("label") or "", "phase": "prepare",
                        "done": 0, "total": 0, "started": time.time()}
        try:
            check_stop(self.stop_event.is_set)
            self.proxy = ProxyManager(self.config, log=log, should_stop=self.stop_event.is_set)
            sample = self._sample_for(body)
            tcfg = self.resources["tts"]
            split = SplitConfig(max_chars=int(body.get("max_chars") or tcfg["max_chars"]),
                                target_chars=min(int(body.get("max_chars") or tcfg["max_chars"]), int(tcfg.get("target_chars") or 260)), min_chars=80)
            report = TextSplitter(split).split(text)
            if report.over_limit_count:
                raise ValueError(f"{report.over_limit_count} đoạn vượt giới hạn ký tự")
            job = JobStore(self.jobs_dir).create_job(source_text=text, report=report, name=self.job["label"] or "tts",
                                                     language=body.get("language") or "Vietnamese", audio_path=str(sample))
            total = len(report.chunks)
            self.job.update({"total": total, "phase": "render", "job_dir": job.job_dir})
            workers = max(1, min(int(tcfg.get("max_workers") or 12), int(body.get("workers") or tcfg.get("workers") or 8)))
            workers = min(workers, max(1, total))   # job ngắn không mở thừa trình duyệt
            requested_accounts = body.get('account_emails')
            if requested_accounts:
                selected = set(requested_accounts)
                accounts = [a for a in self.pool.ready_accounts(min_quota=1) if a['email'] in selected]
            else:
                accounts = self.pool.ready_accounts(limit=max(workers * 3, (total // 15) + workers + 1), min_quota=1)
            if not accounts:
                raise RuntimeError("kho không còn tài khoản có quota")
            accounts = self._sync_quota(accounts)
            if not accounts:
                raise RuntimeError("các tài khoản chọn ra đều hết quota hoặc cookie hỏng")
            need = total
            have = sum(a["quota_left"] for a in accounts)
            if have < need:
                log(f"[TTS] cảnh báo: quota khả dụng {have} < {need} chunk, có thể dừng giữa chừng")

            def progress(done: int, tot: int) -> None:
                self.job.update({"done": done, "total": tot})

            def quota_zero(email: str, quota_text: str) -> None:
                self.pool.set_quota(email, 0, quota_text)

            runner = MultiAccountLongTextRunner(self.config, log=log, progress=progress, proxy_manager=self.proxy,
                                                on_account_quota_zero=quota_zero, should_stop=self.stop_event.is_set)
            self.runner = runner
            result = runner.run(accounts, job.job_dir, audio_path=str(sample), language=body.get("language") or "Vietnamese",
                                parallel_accounts=workers, timeout_sec=int(tcfg.get("chunk_timeout_s") or 100),
                                max_consecutive_failures=int(tcfg.get("fail_stop") or 3), fast_reuse_voice=True)
            # trừ quota theo chunk đã đọc của từng tài khoản
            manifest = json.loads((Path(job.job_dir) / "job.json").read_text(encoding="utf-8"))
            spent: Dict[str, int] = {}
            for ch in manifest.get("chunks", []):
                if ch.get("status") == "done" and ch.get("account_email"):
                    spent[ch["account_email"]] = spent.get(ch["account_email"], 0) + 1
            for email, n in spent.items():
                self.pool.spend(email, n)
            if runner.should_stop():
                return {"ok": False, "cancelled": True, "job_dir": str(job.job_dir)}
            if result.status not in ("done", "retry_done"):
                raise RuntimeError(f"đọc chưa xong: status={result.status} {getattr(result, 'error', '')}")
            self.job["phase"] = "merge"
            merge = ConfiguredMerger(self.ffmpeg, log=log, should_stop=self.stop_event.is_set).merge_job(job.job_dir, out_path,
                                                      pause_map={"comma": 0.3, "period": 0.4, "other": 0.5})
            if not merge.success:
                raise RuntimeError(f"ghép mp3 lỗi: {merge.error}")
            audio_s = self._duration(out_path)
            return {"ok": True, "out_path": str(out_path), "audio_s": audio_s, "chunks": total,
                    "accounts_used": sorted(spent), "quota_spent": sum(spent.values()),
                    "render_s": round(time.time() - self.job["started"], 1), "job_dir": str(job.job_dir)}
        finally:
            self.runner = None
            with self._lock:
                self.job = dict(self.job or {}, busy=False, phase="idle")

    def _sync_quota(self, accounts):
        """Quét quota thật bằng cookie (bộ quét của tool cũ, không mở trình duyệt) và ghi lại kho.
        Loại tài khoản hết quota; tài khoản cookie hỏng vẫn giữ (runner sẽ đăng nhập lại bằng mật khẩu)."""
        from notegpt_quota_tool.quota_scanner import AccountQuotaScanner
        summary = AccountQuotaScanner(self.config, log=lambda _m: None, proxy_manager=self.proxy, should_stop=self.stop_event.is_set).scan_accounts(
            accounts, max_workers=min(4, len(accounts)))
        keep = []
        by_email = {a["email"]: a for a in accounts}
        for r in summary.results:
            if r.quota is None or not r.quota.success:
                keep.append(by_email[r.email])          # không đọc được: để runner tự xử lý
                continue
            self.pool.set_quota(r.email, r.quota_left, r.quota_text)
            if r.quota_left > 0:
                acc = by_email[r.email]
                acc["quota_left"] = r.quota_left
                keep.append(acc)
        log(f"[POOL] đồng bộ quota {len(accounts)} tài khoản: dùng được {len(keep)}")
        return keep

    def _duration(self, path: str) -> float:
        probe = shutil.which("ffprobe") or (str(Path(self.ffmpeg).with_name("ffprobe.exe")) if self.ffmpeg else "")
        if not probe or not Path(probe).exists():
            return 0.0
        try:
            out = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return round(float(out.stdout.strip() or 0), 2)
        except Exception:  # noqa: BLE001
            return 0.0

    def cancel(self) -> Dict[str, Any]:
        self.stop_event.set()
        r = self.runner
        if r is not None:
            r.request_stop()
            return {"ok": True, "cancelling": True}
        return {"ok": True, "cancelling": bool((self.job or {}).get("busy") or self.studio.state.get("busy"))}

    def health(self) -> Dict[str, Any]:
        j = self.job or {}
        return {"ok": True, "service": "notegpt-standalone", "busy": bool(j.get("busy")), "uptime_s": int(time.time() - self.started_at),
                "pool": self.pool.stats(), "proxy": self.resources["proxy"].get("provider"), "ffmpeg": bool(self.ffmpeg),
                "data_dir": str(self.data_dir)}

    def progress(self) -> Dict[str, Any]:
        j = self.job or {}
        return {"busy": bool(j.get("busy")), "id": j.get("id", ""), "label": j.get("label", ""), "phase": j.get("phase", "idle"),
                "done": j.get("done", 0), "total": j.get("total", 0),
                "elapsed_s": round(time.time() - j["started"], 1) if j.get("started") else 0}


class Handler(BaseHTTPRequestHandler):
    service: Service
    server_ref: ThreadingHTTPServer

    def log_message(self, fmt, *args):  # im lặng, tự log
        return

    def _send(self, obj: Dict[str, Any], status: int = 200) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> Dict[str, Any]:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > 30_000_000:
            raise ValueError("Request quá lớn")
        raw = self.rfile.read(n) if n else b""
        return json.loads(raw.decode("utf-8")) if raw else {}

    def _send_html(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        s = self.service
        p = self.path.split("?")[0]
        if p == '/voices':
            return self._send({'ok': True, 'voices': s.studio.voices()})
        if p == '/studio/status':
            return self._send({'ok': True, **s.studio.status()})
        if p == '/audio':
            try:
                path = s.studio.audio_path(parse_qs(urlparse(self.path).query).get('id', [''])[0])
                data = path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            except (ValueError, OSError):
                return self._send({'ok': False, 'error': 'Audio không tồn tại'}, 404)
        if p == "/health":
            return self._send(s.health())
        if p == "/progress":
            return self._send(s.progress())
        if p == "/pool":
            return self._send({"ok": True, **s.pool.stats()})
        if p == "/pool/list":
            return self._send({"ok": True, "accounts": s.pool_list()})
        if p == "/resources":
            return self._send({"ok": True, **s.resources})
        if p == "/logs":
            return self._send({"ok": True, "lines": list(LOG_RING)})
        if p == "/farm/status":
            return self._send({"ok": True, **(s.farm.status() if s.farm else {"available": False})})
        if p in ("/", "/admin"):
            return self._send_html(HERE / "admin" / "index.html")
        return self._send({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        s = self.service
        try:
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers.get('Host', ''):
                return self._send({'ok': False, 'error': 'Origin không hợp lệ'}, 403)
            if self.path == '/voices/import':
                return self._send(s.import_voice(self._body()))
            if self.path == '/pool/import':
                return self._send(s.import_accounts(self._body()))
            if self.path == '/studio/start':
                return self._send(s.studio.start(self._body()), 202)
            if self.path == "/tts":
                return self._send(s.tts(self._body()))
            if self.path == "/cancel":
                return self._send(s.cancel())
            if self.path == "/resources":
                return self._send({"ok": True, **s.save_resources(self._body())})
            if self.path == "/resources/check":
                return self._send({"ok": True, **s.check_resources()})
            if self.path == "/pool/scan":
                if not s.farm:
                    return self._send({"ok": False, "error": "farm.py chưa có"})
                return self._send(s.farm.scan_now())
            if self.path.startswith("/farm/"):
                if not s.farm:
                    return self._send({"ok": False, "error": "farm.py chưa có"})
                action = self.path.rsplit("/", 1)[-1]
                body = self._body()
                if action == "start":
                    s.farm.start(); return self._send({"ok": True})
                if action == "stop":
                    s.farm.stop(); return self._send({"ok": True})
                if action == "run-once":
                    return self._send(s.farm.run_once(int(body.get("count") or 0), int(body.get("threads") or 0)))
                return self._send({"ok": False, "error": "farm action?"}, 404)
            if self.path == "/shutdown":
                s.cancel()
                if s.farm:
                    s.farm.stop()
                self._send({"ok": True})
                threading.Thread(target=self.server_ref.shutdown, daemon=True).start()
                return None
            return self._send({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            log(f"[ERR] {self.path}: {exc}")
            return self._send({"ok": False, "error": str(exc)}, 500)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8830)
    ap.add_argument("--data-dir", default=str(HERE / "data"))
    ap.add_argument("--ffmpeg", default="")
    ap.add_argument("--import-tsv", default="", help="nhập file TSV tài khoản của tool cũ rồi thoát")
    ap.add_argument("--import-cookies", default="", help="thư mục cookies của tool cũ (đi cùng --import-tsv)")
    args = ap.parse_args()
    svc = Service(Path(args.data_dir), args.ffmpeg)
    if args.import_tsv:
        n = svc.pool.import_tsv(args.import_tsv, args.import_cookies or None)
        log(f"[POOL] nhập {n} tài khoản: {svc.pool.stats()}")
        return 0
    Handler.service = svc
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    Handler.server_ref = httpd
    log(f"[SVC] NoteGPT Studio nghe 127.0.0.1:{args.port} | data={svc.data_dir} | pool={svc.pool.stats()} | proxy={svc.resources['proxy'].get('provider')}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    svc.cancel()
    if svc.farm:
        svc.farm.stop()
        if svc.farm._thread:
            svc.farm._thread.join(timeout=15)
    if svc.studio._thread:
        svc.studio._thread.join(timeout=15)
    httpd.server_close()
    log("[SVC] tắt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
