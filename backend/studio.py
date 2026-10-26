"""Small asynchronous voice studio, preserving dialogue turn order."""
from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path

from notegpt_quota_tool.cancellation import check_stop, run_process
from notegpt_quota_tool.audio_merger import JobAudioMerger


class ConfiguredMerger(JobAudioMerger):
    def __init__(self, ffmpeg, **kwargs):
        super().__init__(**kwargs)
        self.ffmpeg = ffmpeg

    def _find_ffmpeg(self):
        return self.ffmpeg or super()._find_ffmpeg()


def parse_dialogue(text):
    turns = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.match(r'^\s*(A|B)\s*[:：]\s*(.*)$', line, re.I)
        if match:
            if not match[2].strip():
                raise ValueError('Lượt thoại không được để trống.')
            turns.append({'speaker': match[1].upper(), 'text': match[2].strip()})
        elif turns:
            if re.match(r'^\s*[A-Za-z]\s*[:：]', line):
                raise ValueError('Hội thoại chỉ nhận nhãn A: và B:.')
            turns[-1]['text'] += '\n' + line.strip()
        else:
            raise ValueError('Mỗi lượt thoại bắt đầu bằng A: hoặc B:.')
    if not turns:
        raise ValueError('Chưa có hội thoại.')
    return turns


class Studio:
    def __init__(self, service):
        self.service = service
        self._lock = threading.RLock()
        self.state = {'busy': False, 'status': 'idle'}
        self.root = service.data_dir / 'studio'
        self.root.mkdir(exist_ok=True)
        self._thread = None
        previous = sorted(self.root.glob('*/result.json'), key=lambda p: p.stat().st_mtime, reverse=True)
        if previous:
            try:
                saved = json.loads(previous[0].read_text(encoding='utf-8'))
                if saved.get('status') == 'done' and (previous[0].parent / 'voice.mp3').is_file():
                    self.state = {**saved, 'busy': False}
            except (OSError, ValueError):
                pass

    def voices(self):
        root = self.service.data_dir / 'voices'
        root.mkdir(exist_ok=True)
        result = []
        if root.exists():
            for folder in sorted(root.iterdir()):
                if not folder.is_dir():
                    continue
                try:
                    spec = json.loads((folder / 'giong.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    spec = {}
                if (folder / spec.get('ref_file', 'mau.wav')).is_file():
                    result.append({'name': str(spec.get('name') or folder.name), 'path': str(folder)})
        return result

    def status(self):
        with self._lock:
            return {**self.state, 'progress': self.service.progress()}

    def start(self, body):
        text = str(body.get('text') or '').strip()
        if not text or len(text) > 200000:
            raise ValueError('Nhập văn bản từ 1 đến 200.000 ký tự.')
        mode = body.get('mode', 'mono')
        if mode not in ('mono', 'dialogue'):
            raise ValueError('Chế độ không hợp lệ.')
        turns = parse_dialogue(text) if mode == 'dialogue' else [{'speaker': 'A', 'text': text}]
        voices = body.get('voices') or {}
        for role in {t['speaker'] for t in turns}:
            voice = voices.get(role) or {}
            value = voice.get('voice_dir') or voice.get('ref_audio')
            if not value or not Path(value).exists():
                raise ValueError(f'Chọn mẫu giọng {role} hợp lệ.')
        if not self.service.ffmpeg:
            raise ValueError('Chưa tìm thấy FFmpeg. Chọn đường dẫn khi khởi động tool.')
        workers = int(body.get('workers') or 2)
        if not 1 <= workers <= int(self.service.resources['tts']['max_workers']):
            raise ValueError('Số phiên vượt giới hạn cấu hình.')
        account_emails = body.get('account_emails') or []
        if not isinstance(account_emails, list) or not all(isinstance(x, str) for x in account_emails):
            raise ValueError('Danh sách tài khoản không hợp lệ.')
        known = {a['email'] for a in self.service.pool.ready_accounts()}
        if account_emails and not set(account_emails).issubset(known):
            raise ValueError('Có tài khoản được chọn không còn sẵn sàng.')
        with self._lock:
            if self.state.get('busy') or (self.service.job or {}).get('busy'):
                raise RuntimeError('Đang có tác vụ tạo voice.')
            task_id = uuid.uuid4().hex
            self.service.stop_event.clear()
            self.state = dict(busy=True, id=task_id, status='queued', mode=mode,
                              turn=0, total_turns=len(turns), error='')
            self._thread = threading.Thread(target=self._run, args=(task_id, turns, voices, workers,
                str(body.get('language') or 'Vietnamese'), account_emails), daemon=True)
            self._thread.start()
        return {'ok': True, 'id': task_id}

    def _run(self, task_id, turns, voices, workers, language, account_emails):
        svc = self.service
        acquired = False
        folder = self.root / task_id
        folder.mkdir()
        try:
            while not acquired:
                check_stop(svc.stop_event.is_set)
                acquired = svc.operation_lock.acquire(timeout=0.2)
            check_stop(svc.stop_event.is_set)
            files, details = [], []
            for index, turn in enumerate(turns, 1):
                check_stop(svc.stop_event.is_set)
                with self._lock:
                    self.state.update(status='rendering', turn=index)
                output = folder / f'turn_{index:04d}.mp3'
                result = svc._tts({**voices[turn['speaker']], 'text': turn['text'],
                    'out_path': str(output), 'workers': workers, 'language': language,
                    'label': f'{task_id[:8]}_{index}_{turn["speaker"]}', 'job_id': task_id,
                    'account_emails': account_emails})
                if not result.get('ok'):
                    check_stop(svc.stop_event.is_set)
                    raise RuntimeError(result.get('error') or 'Tạo voice chưa hoàn tất.')
                files.append(output)
                details.append({**turn, **result})
                (folder / 'turns.json').write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding='utf-8')
            with self._lock:
                self.state['status'] = 'merging'
            output = folder / 'voice.mp3'
            playlist = folder / 'concat.txt'
            playlist.write_text(''.join("file '" + str(p).replace('\\', '/').replace("'", "'\\''") + "'\n" for p in files), encoding='utf-8')
            proc = run_process([svc.ffmpeg, '-y', '-v', 'error', '-f', 'concat', '-safe', '0',
                '-i', str(playlist), '-ac', '1', '-ar', '32000', '-b:a', '128k', str(output)],
                should_stop=svc.stop_event.is_set, timeout=900, capture_output=True,
                output_path=output, creationflags=getattr(__import__('subprocess'), 'CREATE_NO_WINDOW', 0))
            if proc.returncode:
                raise RuntimeError('Ghép hội thoại thất bại: ' + proc.stderr.decode(errors='replace')[-500:])
            with self._lock:
                self.state.update(status='done', out_path=str(output), audio_url=f'/audio?id={task_id}',
                    quota_spent=sum(r['quota_spent'] for r in details), audio_s=svc._duration(str(output)),
                    accounts_used=len({email for r in details for email in r.get('accounts_used', [])}))
        except Exception as exc:
            with self._lock:
                self.state.update(status='cancelled' if svc.stop_event.is_set() else 'error', error=str(exc))
        finally:
            if acquired:
                svc.operation_lock.release()
            with self._lock:
                self.state['busy'] = False
                (folder / 'result.json').write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')

    def audio_path(self, task_id):
        if not re.fullmatch('[0-9a-f]{32}', task_id):
            raise ValueError('Mã audio không hợp lệ.')
        return self.root / task_id / 'voice.mp3'
