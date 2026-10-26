from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.config import PROJECT_ROOT
    from notegpt_quota_tool.cancellation import run_process, check_stop, OperationStopped
else:
    from .config import PROJECT_ROOT
    from .cancellation import run_process, check_stop, OperationStopped


LogCallback = Callable[[str], None]


def _hidden_subprocess_kwargs(hide: bool = True) -> Dict[str, Any]:
    """Return subprocess kwargs that suppress the Windows console window.

    On Windows ffmpeg/ffprobe are console programs; calling them from a
    GUI process opens a brief black flash. Combining ``CREATE_NO_WINDOW``
    with a hidden ``STARTUPINFO.wShowWindow`` keeps the console fully
    out of the user's way. On non-Windows hosts this is a no-op so the
    function is safe to use cross-platform.
    """
    if not hide or sys.platform != "win32":
        return {}
    flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    startupinfo = None
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
    except Exception:
        startupinfo = None
    kwargs: Dict[str, Any] = {"creationflags": flags} if flags else {}
    if startupinfo is not None:
        kwargs["startupinfo"] = startupinfo
    return kwargs


@dataclass(frozen=True)
class AudioMergeResult:
    success: bool
    job_dir: str
    output_path: str = ""
    total_chunks: int = 0
    merged_chunks: int = 0
    total_input_bytes: int = 0
    output_bytes: int = 0
    method: str = "ffmpeg_copy"
    inserted_pauses: int = 0
    inserted_pause_ms: int = 0
    missing_chunks: List[Dict[str, Any]] = field(default_factory=list)
    error: str = ""

    @property
    def size_ratio(self) -> float:
        if not self.total_input_bytes:
            return 0.0
        return self.output_bytes / self.total_input_bytes


class JobAudioMerger:
    def __init__(
        self,
        log: LogCallback | None = None,
        *,
        hide_console: bool = True,
        should_stop=None,
    ) -> None:
        self.log = log or (lambda _message: None)
        self.hide_console = bool(hide_console)
        self.should_stop = should_stop or (lambda: False)

    def merge_job(
        self,
        job_dir: str | Path,
        output_path: str | Path = "",
        *,
        require_all_done: bool = True,
        pause_map: Dict[str, float] | None = None,
        min_mp3_bytes: int = 512,
        timeout_sec: int = 900,
    ) -> AudioMergeResult:
        check_stop(self.should_stop)
        job_root = self._resolve_job_dir(job_dir)
        manifest_path = job_root / "job.json"
        if not manifest_path.exists():
            return AudioMergeResult(False, str(job_root), error=f"job.json not found: {manifest_path}")

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return AudioMergeResult(False, str(job_root), error=f"cannot read job.json: {exc}")

        chunks = sorted(
            [chunk for chunk in manifest.get("chunks", []) if isinstance(chunk, dict)],
            key=lambda item: int(item.get("index") or 0),
        )
        if not chunks:
            return AudioMergeResult(False, str(job_root), error="job has no chunks")

        output = self._resolve_output_path(job_root, output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        input_files: List[Path] = []
        missing: List[Dict[str, Any]] = []
        total_input_bytes = 0
        for chunk in chunks:
            check_stop(self.should_stop)
            index = int(chunk.get("index") or 0)
            status = str(chunk.get("status") or "")
            rel_output = str(chunk.get("output_file") or f"outputs/chunk_{index:04d}.mp3")
            mp3_path = self._resolve_job_path(job_root, rel_output)
            reason = ""
            if require_all_done and status != "done":
                reason = f"status={status or 'unknown'}"
            elif not mp3_path.exists():
                reason = "file_missing"
            else:
                size = mp3_path.stat().st_size
                if size < min_mp3_bytes:
                    reason = f"file_too_small={size} bytes"
                else:
                    input_files.append(mp3_path)
                    total_input_bytes += size
            if reason:
                missing.append(
                    {
                        "index": index,
                        "status": status,
                        "path": str(mp3_path),
                        "reason": reason,
                    }
                )

        if missing:
            return AudioMergeResult(
                False,
                str(job_root),
                output_path=str(output),
                total_chunks=len(chunks),
                merged_chunks=len(input_files),
                total_input_bytes=total_input_bytes,
                missing_chunks=missing,
                error=f"{len(missing)} chunk(s) are not ready for merge",
            )
        if not input_files:
            return AudioMergeResult(False, str(job_root), output_path=str(output), error="no mp3 files to merge")

        ffmpeg = self._find_ffmpeg()
        if not ffmpeg:
            return AudioMergeResult(
                False,
                str(job_root),
                output_path=str(output),
                total_chunks=len(chunks),
                merged_chunks=len(input_files),
                total_input_bytes=total_input_bytes,
                error="ffmpeg not found. Install ffmpeg or put ffmpeg.exe in PATH/project folder.",
            )

        pause_map = self._normalize_pause_map(pause_map)
        concat_paths: List[Path] = []
        inserted_pauses = 0
        inserted_pause_ms = 0
        if pause_map:
            try:
                concat_paths, inserted_pauses, inserted_pause_ms = self._build_concat_paths_with_pauses(
                    job_root,
                    chunks,
                    input_files,
                    pause_map,
                    ffmpeg,
                    timeout_sec,
                )
            except Exception as exc:
                return AudioMergeResult(
                    False,
                    str(job_root),
                    output_path=str(output),
                    total_chunks=len(chunks),
                    merged_chunks=len(input_files),
                    total_input_bytes=total_input_bytes,
                    error=f"cannot prepare pause audio: {exc}",
                )
        else:
            concat_paths = input_files

        concat_file = output.parent / f"{output.stem}_concat_list.txt"
        concat_file.write_text("".join(self._concat_line(path) for path in concat_paths), encoding="utf-8")
        self.log(
            f"[MERGE] Start lossless concat: chunks={len(input_files)} | "
            f"pauses={inserted_pauses} ({inserted_pause_ms}ms) | input={self._format_bytes(total_input_bytes)}"
        )
        self.log(f"[MERGE] Output: {output}")

        command = [
            str(ffmpeg),
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output),
        ]
        try:
            proc = run_process(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_sec,
                output_path=output,
                should_stop=self.should_stop, **_hidden_subprocess_kwargs(self.hide_console),
            )
        except subprocess.TimeoutExpired:
            return AudioMergeResult(
                False,
                str(job_root),
                output_path=str(output),
                total_chunks=len(chunks),
                merged_chunks=len(input_files),
                total_input_bytes=total_input_bytes,
                error=f"ffmpeg merge timed out after {timeout_sec}s",
            )
        except Exception as exc:
            return AudioMergeResult(
                False,
                str(job_root),
                output_path=str(output),
                total_chunks=len(chunks),
                merged_chunks=len(input_files),
                total_input_bytes=total_input_bytes,
                error=f"ffmpeg merge failed: {exc}",
            )

        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip()
            if len(details) > 1200:
                details = details[-1200:]
            return AudioMergeResult(
                False,
                str(job_root),
                output_path=str(output),
                total_chunks=len(chunks),
                merged_chunks=len(input_files),
                total_input_bytes=total_input_bytes,
                error=f"ffmpeg returned {proc.returncode}: {details}",
            )

        if not output.exists() or output.stat().st_size < min_mp3_bytes:
            return AudioMergeResult(
                False,
                str(job_root),
                output_path=str(output),
                total_chunks=len(chunks),
                merged_chunks=len(input_files),
                total_input_bytes=total_input_bytes,
                error="merged mp3 was not created or is too small",
            )

        output_bytes = output.stat().st_size
        result = AudioMergeResult(
            True,
            str(job_root),
            output_path=str(output),
            total_chunks=len(chunks),
            merged_chunks=len(input_files),
            total_input_bytes=total_input_bytes,
            output_bytes=output_bytes,
            inserted_pauses=inserted_pauses,
            inserted_pause_ms=inserted_pause_ms,
        )
        self._write_merge_info(manifest_path, manifest, result)
        self.log(
            f"[MERGE] Saved: {output} | size={self._format_bytes(output_bytes)} "
            f"| input_sum={self._format_bytes(total_input_bytes)} | ratio={result.size_ratio:.3f}"
        )
        return result

    def _resolve_job_dir(self, job_dir: str | Path) -> Path:
        path = Path(job_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.is_file():
            path = path.parent
        return path.resolve()

    def _resolve_output_path(self, job_root: Path, output_path: str | Path) -> Path:
        if output_path:
            path = Path(output_path)
            if not path.is_absolute():
                path = job_root / path
        else:
            path = job_root / "outputs" / f"{job_root.name}_merged.mp3"
        if path.suffix.lower() != ".mp3":
            path = path.with_suffix(".mp3")
        return path.resolve()

    def _resolve_job_path(self, job_root: Path, value: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            path = job_root / path
        return path.resolve()

    def _find_ffmpeg(self) -> str:
        candidates = [
            shutil.which("ffmpeg"),
            str(PROJECT_ROOT / "ffmpeg.exe"),
            str(PROJECT_ROOT / "bin" / "ffmpeg.exe"),
            str(PROJECT_ROOT / "tools" / "ffmpeg.exe"),
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return ""

    def _find_ffprobe(self, ffmpeg: str = "") -> str:
        candidates = [shutil.which("ffprobe")]
        if ffmpeg:
            ffmpeg_path = Path(ffmpeg)
            candidates.append(str(ffmpeg_path.with_name("ffprobe.exe")))
            candidates.append(str(ffmpeg_path.with_name("ffprobe")))
        candidates.extend(
            [
                str(PROJECT_ROOT / "ffprobe.exe"),
                str(PROJECT_ROOT / "bin" / "ffprobe.exe"),
                str(PROJECT_ROOT / "tools" / "ffprobe.exe"),
            ]
        )
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return ""

    def _normalize_pause_map(self, pause_map: Dict[str, float] | None) -> Dict[str, float]:
        raw = pause_map or {}

        def read(*keys: str, fallback: float) -> float:
            for key in keys:
                if key in raw:
                    try:
                        value = str(raw[key] or "0").strip().replace(",", ".")
                        return max(0.0, float(value or 0))
                    except Exception:
                        return fallback
            return fallback

        values = {
            "comma": read("comma", ",", "，", "、", fallback=0.0),
            "period": read("period", "dot", ".", "。", "…", fallback=0.0),
            "other": read("other", ";", ":", "?", "!", "；", "：", "？", "！", fallback=0.0),
        }
        return {key: value for key, value in values.items() if value > 0}

    def _build_concat_paths_with_pauses(
        self,
        job_root: Path,
        chunks: List[Dict[str, Any]],
        input_files: List[Path],
        pause_map: Dict[str, float],
        ffmpeg: str,
        timeout_sec: int,
    ) -> tuple[List[Path], int, int]:
        if not pause_map:
            return input_files, 0, 0

        audio_info = self._probe_audio(input_files[0], ffmpeg)
        silence_dir = job_root / "outputs" / "_silence"
        silence_dir.mkdir(parents=True, exist_ok=True)
        silence_cache: Dict[int, Path] = {}
        concat_paths: List[Path] = []
        inserted = 0
        inserted_ms = 0

        for idx, (chunk, mp3_path) in enumerate(zip(chunks, input_files)):
            check_stop(self.should_stop)
            concat_paths.append(mp3_path)
            if idx >= len(input_files) - 1:
                continue
            category = self._pause_category_for_chunk(job_root, chunk)
            if not category:
                continue
            seconds = pause_map.get(category, 0.0)
            if seconds <= 0:
                continue
            duration_ms = max(1, int(round(seconds * 1000)))
            silence_path = silence_cache.get(duration_ms)
            if not silence_path:
                silence_path = self._ensure_silence_mp3(
                    silence_dir,
                    duration_ms,
                    audio_info,
                    ffmpeg,
                    timeout_sec,
                )
                silence_cache[duration_ms] = silence_path
            concat_paths.append(silence_path)
            inserted += 1
            inserted_ms += duration_ms

        return concat_paths, inserted, inserted_ms

    def _probe_audio(self, sample_path: Path, ffmpeg: str) -> Dict[str, Any]:
        ffprobe = self._find_ffprobe(ffmpeg)
        if not ffprobe:
            return {"sample_rate": 44100, "channels": 2, "layout": "stereo"}
        command = [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels,channel_layout",
            "-of",
            "json",
            str(sample_path),
        ]
        try:
            proc = run_process(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                should_stop=self.should_stop, **_hidden_subprocess_kwargs(self.hide_console),
            )
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
            data = json.loads(proc.stdout or "{}")
            stream = (data.get("streams") or [{}])[0]
            sample_rate = int(stream.get("sample_rate") or 44100)
            channels = int(stream.get("channels") or 2)
            layout = str(stream.get("channel_layout") or "").strip()
            if not layout:
                layout = "mono" if channels == 1 else "stereo"
            return {"sample_rate": sample_rate, "channels": channels, "layout": layout}
        except OperationStopped:
            raise
        except Exception as exc:
            self.log(f"[MERGE] ffprobe failed, use default silence format: {exc}")
            return {"sample_rate": 44100, "channels": 2, "layout": "stereo"}

    def _ensure_silence_mp3(
        self,
        silence_dir: Path,
        duration_ms: int,
        audio_info: Dict[str, Any],
        ffmpeg: str,
        timeout_sec: int,
    ) -> Path:
        sample_rate = int(audio_info.get("sample_rate") or 44100)
        layout = str(audio_info.get("layout") or "stereo")
        safe_layout = "".join(ch for ch in layout if ch.isalnum() or ch in ("_", "-")) or "stereo"
        path = silence_dir / f"silence_{duration_ms}ms_{sample_rate}_{safe_layout}.mp3"
        if path.exists() and path.stat().st_size > 128:
            return path
        duration = f"{duration_ms / 1000:.3f}"
        command = [
            str(ffmpeg),
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=r={sample_rate}:cl={layout}",
            "-t",
            duration,
            "-codec:a",
            "libmp3lame",
            "-q:a",
            "9",
            str(path),
        ]
        proc = run_process(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(30, min(timeout_sec, 180)),
            output_path=path,
            should_stop=self.should_stop, **_hidden_subprocess_kwargs(self.hide_console),
        )
        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"cannot create {duration}s silence mp3: {details}")
        if not path.exists() or path.stat().st_size <= 128:
            raise RuntimeError(f"silence mp3 is too small: {path}")
        return path

    def _pause_category_for_chunk(self, job_root: Path, chunk: Dict[str, Any]) -> str:
        text = self._chunk_text(job_root, chunk).rstrip()
        text = text.rstrip(" \t\r\n\"'”’)]}»")
        if not text:
            return ""
        last = text[-1]
        if last in ",，、":
            return "comma"
        if last in ".。…":
            return "period"
        if last in ";:?!；：？！":
            return "other"
        return ""

    def _chunk_text(self, job_root: Path, chunk: Dict[str, Any]) -> str:
        text_file = str(chunk.get("text_file") or "")
        if text_file:
            path = self._resolve_job_path(job_root, text_file)
            try:
                return path.read_text(encoding="utf-8")
            except Exception:
                pass
        return str(chunk.get("text") or "")

    def _concat_line(self, path: Path) -> str:
        value = path.resolve().as_posix().replace("'", "'\\''")
        return f"file '{value}'\n"

    def _write_merge_info(self, manifest_path: Path, manifest: Dict[str, Any], result: AudioMergeResult) -> None:
        manifest["merge"] = {
            "output_path": result.output_path,
            "method": result.method,
            "total_chunks": result.total_chunks,
            "merged_chunks": result.merged_chunks,
            "inserted_pauses": result.inserted_pauses,
            "inserted_pause_ms": result.inserted_pause_ms,
            "total_input_bytes": result.total_input_bytes,
            "output_bytes": result.output_bytes,
            "size_ratio": result.size_ratio,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        payload = json.dumps(manifest, ensure_ascii=False, indent=2)
        tmp = manifest_path.with_name("job.json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        try:
            tmp.replace(manifest_path)
        except PermissionError:
            manifest_path.write_text(payload, encoding="utf-8")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _format_bytes(self, size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
            value /= 1024
        return f"{size}B"
