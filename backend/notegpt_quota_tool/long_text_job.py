from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.config import PROJECT_ROOT
else:
    from .config import PROJECT_ROOT


@dataclass(frozen=True)
class SplitConfig:
    max_chars: int = 285
    target_chars: int = 270
    min_chars: int = 120

    def normalized(self) -> "SplitConfig":
        max_chars = min(300, max(80, int(self.max_chars or 285)))
        target_chars = min(max_chars, max(50, int(self.target_chars or max_chars)))
        min_chars = min(target_chars, max(20, int(self.min_chars or 120)))
        return SplitConfig(max_chars=max_chars, target_chars=target_chars, min_chars=min_chars)


@dataclass(frozen=True)
class TextChunk:
    index: int
    text: str
    char_count: int
    break_type: str


@dataclass(frozen=True)
class SplitReport:
    source_chars: int
    normalized_chars: int
    config: SplitConfig
    chunks: List[TextChunk]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    @property
    def total_quota_needed(self) -> int:
        return len(self.chunks)

    @property
    def max_chunk_chars(self) -> int:
        return max((chunk.char_count for chunk in self.chunks), default=0)

    @property
    def min_chunk_chars(self) -> int:
        return min((chunk.char_count for chunk in self.chunks), default=0)

    @property
    def over_limit_count(self) -> int:
        return sum(1 for chunk in self.chunks if chunk.char_count > self.config.max_chars)

    def break_type_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for chunk in self.chunks:
            counts[chunk.break_type] = counts.get(chunk.break_type, 0) + 1
        return counts

    def to_summary_text(self) -> str:
        if not self.chunks:
            return "No chunks"
        counts = ", ".join(f"{key}={value}" for key, value in sorted(self.break_type_counts().items()))
        return (
            f"chars={self.normalized_chars:,} | chunks={self.chunk_count:,} | "
            f"quota={self.total_quota_needed:,} | min={self.min_chunk_chars} | "
            f"max={self.max_chunk_chars}/{self.config.max_chars} | over={self.over_limit_count} | {counts}"
        )


class TextSplitter:
    _SPACE_RE = re.compile(r"[ \t\f\v]+")
    _MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
    _SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:\u2026\u3002\uff01\uff1f\uff0c\uff1b\uff1a])")
    _MULTI_SPACE_RE = re.compile(r"\s+")

    _BREAK_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
        (
            "sentence",
            re.compile(r"[.!?\u2026\u3002\uff01\uff1f]+(?:[\"'\)\]\u201d\u2019]+)?(?=\s|$)"),
        ),
        ("separator", re.compile(r"[\n;:\uff1b\uff1a]+(?=\s|$)")),
        ("comma", re.compile(r"[,，、]+(?=\s|$)")),
        ("word", re.compile(r"\s+")),
    )

    def __init__(self, config: Optional[SplitConfig] = None) -> None:
        self.config = (config or SplitConfig()).normalized()

    def normalize_text(self, text: str) -> str:
        value = (text or "").replace("\r\n", "\n").replace("\r", "\n")
        value = self._SPACE_RE.sub(" ", value)
        value = self._MULTI_NEWLINE_RE.sub("\n\n", value)
        value = self._SPACE_BEFORE_PUNCT_RE.sub(r"\1", value)
        value = self._MULTI_SPACE_RE.sub(" ", value)
        return value.strip()

    def split(self, text: str) -> SplitReport:
        source_chars = len(text or "")
        normalized = self.normalize_text(text)
        chunks: List[TextChunk] = []
        pos = 0
        text_len = len(normalized)

        while pos < text_len:
            while pos < text_len and normalized[pos].isspace():
                pos += 1
            if pos >= text_len:
                break

            remaining = text_len - pos
            if remaining <= self.config.max_chars:
                piece = normalized[pos:].strip()
                if piece:
                    chunks.append(TextChunk(len(chunks) + 1, piece, len(piece), "final"))
                break

            cut, break_type = self._choose_cut(normalized, pos)
            if cut <= pos:
                cut = min(text_len, pos + self.config.max_chars)
                break_type = "hard"
            piece = normalized[pos:cut].strip()
            if not piece:
                pos = cut
                continue
            chunks.append(TextChunk(len(chunks) + 1, piece, len(piece), break_type))
            pos = cut

        return SplitReport(
            source_chars=source_chars,
            normalized_chars=len(normalized),
            config=self.config,
            chunks=chunks,
        )

    def _choose_cut(self, text: str, start: int) -> Tuple[int, str]:
        max_end = min(len(text), start + self.config.max_chars)
        target_end = min(max_end, start + self.config.target_chars)
        min_end = min(max_end, start + self.config.min_chars)
        window = text[start:max_end]
        candidates: List[Tuple[int, int, int, str]] = []

        for rank, (break_type, pattern) in enumerate(self._BREAK_PATTERNS):
            for match in pattern.finditer(window):
                cut = start + match.end()
                if cut < min_end or cut > max_end:
                    continue
                distance = abs(cut - target_end)
                candidates.append((rank, distance, -cut, break_type))

        if candidates:
            rank, _distance, neg_cut, break_type = sorted(candidates)[0]
            _ = rank
            return -neg_cut, break_type

        last_space = None
        for match in re.finditer(r"\s+", window):
            cut = start + match.end()
            if cut <= max_end:
                last_space = cut
        if last_space and last_space > start:
            return last_space, "word"

        return max_end, "hard"


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    job_dir: str
    manifest_path: str
    chunk_count: int
    total_chars: int


class JobStore:
    def __init__(self, root_dir: str | Path = "data/jobs") -> None:
        root = Path(root_dir)
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        self.root_dir = root
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def create_job(
        self,
        *,
        source_text: str,
        report: SplitReport,
        name: str = "",
        language: str = "Vietnamese",
        audio_path: str = "",
        output_dir: str = "data/clone_outputs",
    ) -> JobRecord:
        if not report.chunks:
            raise ValueError("cannot create job without chunks")
        if report.over_limit_count:
            raise ValueError(f"{report.over_limit_count} chunks are over the limit")

        created_at = datetime.now()
        slug = self._safe_slug(name or "long_text")
        job_id = f"{created_at:%Y%m%d_%H%M%S}_{slug}"
        job_dir = self.root_dir / job_id
        suffix = 1
        while job_dir.exists():
            suffix += 1
            job_dir = self.root_dir / f"{job_id}_{suffix}"
        job_dir.mkdir(parents=True, exist_ok=False)

        chunk_dir = job_dir / "chunks"
        output_path = job_dir / "outputs"
        chunk_dir.mkdir()
        output_path.mkdir()

        normalized_text = TextSplitter(report.config).normalize_text(source_text)
        (job_dir / "source_text.txt").write_text(source_text or "", encoding="utf-8")
        (job_dir / "normalized_text.txt").write_text(normalized_text, encoding="utf-8")

        chunk_items = []
        for chunk in report.chunks:
            filename = f"chunk_{chunk.index:04d}.txt"
            chunk_file = chunk_dir / filename
            chunk_file.write_text(chunk.text, encoding="utf-8")
            chunk_items.append(
                {
                    "index": chunk.index,
                    "status": "pending",
                    "chars": chunk.char_count,
                    "break_type": chunk.break_type,
                    "text_file": f"chunks/{filename}",
                    "output_file": f"outputs/chunk_{chunk.index:04d}.mp3",
                    "account_email": "",
                    "mp3_url": "",
                    "error": "",
                    "started_at": "",
                    "finished_at": "",
                }
            )

        manifest: Dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_dir.name,
            "name": name or "Long text clone job",
            "status": "ready",
            "created_at": created_at.isoformat(timespec="seconds"),
            "source": {
                "mode": "pasted_text",
                "source_chars": report.source_chars,
                "normalized_chars": report.normalized_chars,
                "sha256": hashlib.sha256((source_text or "").encode("utf-8")).hexdigest(),
            },
            "split": {
                "config": asdict(report.config),
                "break_type_counts": report.break_type_counts(),
            },
            "totals": {
                "chunks": report.chunk_count,
                "quota_needed": report.total_quota_needed,
                "min_chunk_chars": report.min_chunk_chars,
                "max_chunk_chars": report.max_chunk_chars,
                "over_limit_count": report.over_limit_count,
            },
            "settings": {
                "language": language,
                "audio_path": audio_path,
                "output_dir": output_dir,
            },
            "chunks": chunk_items,
        }
        manifest_path = job_dir / "job.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        return JobRecord(
            job_id=job_dir.name,
            job_dir=str(job_dir),
            manifest_path=str(manifest_path),
            chunk_count=report.chunk_count,
            total_chars=report.normalized_chars,
        )

    def load_job(self, job_dir: str | Path) -> Dict[str, Any]:
        path = Path(job_dir)
        if not path.is_absolute():
            path = self.root_dir / path
        manifest_path = path / "job.json"
        with manifest_path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def _safe_slug(self, value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", value.strip())[:48].strip("._-")
        return slug or "long_text"


@dataclass(frozen=True)
class JobQueueSummary:
    total: int
    pending: int
    running: int
    done: int
    failed: int


class JobQueue:
    def __init__(self, job_dir: str | Path) -> None:
        path = Path(job_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        if path.is_file():
            self.job_dir = path.parent.resolve()
            self.manifest_path = path.resolve()
        else:
            self.job_dir = path.resolve()
            self.manifest_path = (self.job_dir / "job.json").resolve()
        if not self.manifest_path.exists():
            raise FileNotFoundError(f"job.json not found: {self.manifest_path}")
        self._lock = threading.RLock()

    def load(self) -> Dict[str, Any]:
        with self._lock:
            return self._load_unlocked()

    def summary(self) -> JobQueueSummary:
        manifest = self.load()
        chunks = [chunk for chunk in manifest.get("chunks", []) if isinstance(chunk, dict)]
        return JobQueueSummary(
            total=len(chunks),
            pending=sum(1 for chunk in chunks if chunk.get("status") == "pending"),
            running=sum(1 for chunk in chunks if chunk.get("status") == "running"),
            done=sum(1 for chunk in chunks if chunk.get("status") == "done"),
            failed=sum(1 for chunk in chunks if chunk.get("status") == "failed"),
        )

    def reset_running(self) -> int:
        with self._lock:
            manifest = self._load_unlocked()
            count = 0
            for chunk in manifest.get("chunks", []):
                if isinstance(chunk, dict) and chunk.get("status") == "running":
                    chunk["status"] = "pending"
                    chunk["error"] = ""
                    chunk["started_at"] = ""
                    chunk["finished_at"] = ""
                    count += 1
            if count:
                self._set_progress_unlocked(manifest, "ready")
                self._save_unlocked(manifest)
            return count

    def set_status(self, status: str, *, error: str = "") -> None:
        with self._lock:
            manifest = self._load_unlocked()
            self._set_progress_unlocked(manifest, status, error=error)
            self._save_unlocked(manifest)

    def claim_next(self, account_email: str, *, retry_failed: bool = False) -> Optional[Dict[str, Any]]:
        allowed = {"pending"}
        if retry_failed:
            allowed.add("failed")
        return self._claim_next_with_statuses(account_email, allowed)

    def claim_failed_next(self, account_email: str) -> Optional[Dict[str, Any]]:
        return self._claim_next_with_statuses(account_email, {"failed"})

    def _claim_next_with_statuses(self, account_email: str, allowed: set[str]) -> Optional[Dict[str, Any]]:
        with self._lock:
            manifest = self._load_unlocked()
            for chunk in manifest.get("chunks", []):
                if not isinstance(chunk, dict) or chunk.get("status") not in allowed:
                    continue
                now = datetime.now().isoformat(timespec="seconds")
                chunk["status"] = "running"
                chunk["account_email"] = account_email
                chunk["started_at"] = now
                chunk["finished_at"] = ""
                chunk["error"] = ""
                self._set_progress_unlocked(manifest, "running")
                self._save_unlocked(manifest)
                return dict(chunk)
            return None

    def release_claim(self, chunk_index: int, *, account_email: str = "") -> None:
        with self._lock:
            manifest = self._load_unlocked()
            chunk = self._find_chunk_unlocked(manifest, chunk_index)
            if chunk.get("status") != "running":
                return
            if account_email and str(chunk.get("account_email") or "") != account_email:
                return
            chunk["status"] = "pending"
            chunk["started_at"] = ""
            chunk["finished_at"] = ""
            chunk["error"] = ""
            self._set_progress_unlocked(manifest, "ready")
            self._save_unlocked(manifest)

    def mark_done(self, chunk_index: int, *, account_email: str, mp3_url: str, output_file: str) -> None:
        with self._lock:
            manifest = self._load_unlocked()
            chunk = self._find_chunk_unlocked(manifest, chunk_index)
            now = datetime.now().isoformat(timespec="seconds")
            chunk["status"] = "done"
            chunk["account_email"] = account_email
            chunk["mp3_url"] = mp3_url
            chunk["output_file"] = output_file.replace("\\", "/")
            chunk["finished_at"] = now
            chunk["error"] = ""
            self._set_progress_unlocked(manifest, "running")
            self._save_unlocked(manifest)

    def mark_failed(self, chunk_index: int, *, account_email: str, error: str) -> None:
        with self._lock:
            manifest = self._load_unlocked()
            chunk = self._find_chunk_unlocked(manifest, chunk_index)
            now = datetime.now().isoformat(timespec="seconds")
            chunk["status"] = "failed"
            chunk["account_email"] = account_email
            chunk["finished_at"] = now
            chunk["error"] = error
            self._set_progress_unlocked(manifest, "paused_error", error=error)
            self._save_unlocked(manifest)

    def _load_unlocked(self) -> Dict[str, Any]:
        with self.manifest_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("invalid job manifest")
        return data

    def _save_unlocked(self, manifest: Dict[str, Any]) -> None:
        manifest["updated_at"] = datetime.now().isoformat(timespec="seconds")
        tmp = self.manifest_path.with_name("job.json.tmp")
        payload = json.dumps(manifest, ensure_ascii=False, indent=2)
        tmp.write_text(payload, encoding="utf-8")
        try:
            tmp.replace(self.manifest_path)
        except PermissionError:
            self.manifest_path.write_text(payload, encoding="utf-8")
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def _find_chunk_unlocked(self, manifest: Dict[str, Any], chunk_index: int) -> Dict[str, Any]:
        for chunk in manifest.get("chunks", []):
            if isinstance(chunk, dict) and int(chunk.get("index") or 0) == int(chunk_index):
                return chunk
        raise KeyError(f"chunk not found: {chunk_index}")

    def _set_progress_unlocked(self, manifest: Dict[str, Any], status: str, error: str = "") -> None:
        chunks = [chunk for chunk in manifest.get("chunks", []) if isinstance(chunk, dict)]
        progress = manifest.setdefault("progress", {})
        progress["pending"] = sum(1 for chunk in chunks if chunk.get("status") == "pending")
        progress["running"] = sum(1 for chunk in chunks if chunk.get("status") == "running")
        progress["done"] = sum(1 for chunk in chunks if chunk.get("status") == "done")
        progress["failed"] = sum(1 for chunk in chunks if chunk.get("status") == "failed")
        if progress["done"] == len(chunks) and chunks:
            manifest["status"] = "done"
        else:
            manifest["status"] = status
        if error:
            manifest["last_error"] = error


def _build_sample_text(target_chars: int = 70000) -> str:
    sentences = [
        "Day la cau thu nhat de kiem tra cat doan theo dau cham.",
        "Khi cau ngan hon gioi han, tool nen ghep them cau tiep theo cho gan muc tieu.",
        "Neu gap dau phay, dau cham phay, hoac khoang trang thi van cat mem de khong vo tu.",
        "Van ban dai can duoc chia thanh nhieu phan nho hon ba tram ky tu.",
    ]
    parts: List[str] = []
    size = 0
    index = 0
    while size < target_chars:
        sentence = sentences[index % len(sentences)]
        parts.append(sentence)
        size += len(sentence) + 1
        index += 1
    return " ".join(parts)[:target_chars]


def self_test(target_chars: int = 70000) -> SplitReport:
    text = _build_sample_text(target_chars)
    splitter = TextSplitter(SplitConfig(max_chars=285, target_chars=270, min_chars=120))
    report = splitter.split(text)
    reconstructed = " ".join(chunk.text for chunk in report.chunks)
    assert report.over_limit_count == 0, report.to_summary_text()
    assert report.max_chunk_chars <= report.config.max_chars, report.to_summary_text()
    assert reconstructed == splitter.normalize_text(text), "splitter lost or changed text content"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Split long pasted text into NoteGPT clone chunks.")
    parser.add_argument("--self-test", action="store_true", help="Generate and split a 70k sample text")
    parser.add_argument("--chars", type=int, default=70000, help="Sample size for --self-test")
    args = parser.parse_args()
    if args.self_test:
        report = self_test(args.chars)
        print(report.to_summary_text())
        print(json.dumps({"break_types": report.break_type_counts()}, ensure_ascii=False, indent=2))
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
