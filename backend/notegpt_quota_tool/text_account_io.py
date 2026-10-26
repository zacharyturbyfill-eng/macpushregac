"""
TSV-based account file IO used by the Text source in the Accounts tab.

Format mirrors what ``auto_register.AutoRegisterRunner`` writes:
tab-separated rows with a header line. Required column: ``email``
(aliases ``mail``, ``gmail`` are normalized). Cookies, password, name,
status, verify, quota fields are optional.

Files are read and written in UTF-8 with header preserved verbatim so
re-saving keeps any extra columns the user added by hand. ``cookies``
column may carry inline JSON; we leave it untouched on read and only
overwrite when ``run`` produced a new cookie list.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from notegpt_quota_tool.sheet_api import normalize_account
else:
    from .sheet_api import normalize_account


# Header columns we always make sure exist when overwriting. Keeps the
# downstream toolchain (Auto Register output, etc.) consistent.
REQUIRED_OUTPUT_COLUMNS = (
    "email",
    "password",
    "name",
    "status",
    "verify",
    "quota",
    "cookies",
)


def exhausted_file_path(source: str | Path) -> Path:
    """Compute the sibling exhausted-accounts file for a given source TSV.

    Example: ``data/registered_accounts.txt`` ->
    ``data/registered_accounts_no_quota.txt``.
    """
    source_path = Path(source)
    return source_path.with_name(f"{source_path.stem}_no_quota{source_path.suffix or '.txt'}")


@dataclass
class TextAccountFile:
    path: Path
    header: List[str]
    rows: List[Dict[str, str]] = field(default_factory=list)


def read_text_accounts(path: str | Path) -> TextAccountFile:
    """Read a TSV account file and return raw rows + normalized accounts.

    The returned ``rows`` keep every original column as-is, indexed by the
    header. ``read_text_accounts`` does not modify or re-shape columns;
    use ``normalize_account`` (re-exported here) on each row to get the
    standardized email/password/cookies dict the run engine expects.
    """
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Text account file not found: {file_path}")
    text = file_path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return TextAccountFile(path=file_path, header=[], rows=[])

    reader = csv.reader(io.StringIO(text), delimiter="\t")
    raw_rows = [row for row in reader if row]
    if not raw_rows:
        return TextAccountFile(path=file_path, header=[], rows=[])

    header = [str(cell or "").strip() for cell in raw_rows[0]]
    if not any(header):
        # Empty header means the first non-blank row was data; treat as headerless.
        header = [f"col_{i}" for i in range(len(raw_rows[0]))]
        data_rows = raw_rows
    else:
        data_rows = raw_rows[1:]

    rows: List[Dict[str, str]] = []
    for raw in data_rows:
        if not any((cell or "").strip() for cell in raw):
            continue
        row: Dict[str, str] = {}
        for index, key in enumerate(header):
            row[key] = str(raw[index]).strip() if index < len(raw) else ""
        rows.append(row)
    return TextAccountFile(path=file_path, header=header, rows=rows)


def to_runtime_accounts(file: TextAccountFile) -> List[Dict[str, Any]]:
    """Convert raw rows to the dict shape the run engine expects.

    Reuses ``sheet_api.normalize_account`` so any synonym handling that
    works for the Sheet source (mail/gmail aliases, etc.) also works here.
    Each returned dict carries a ``__row_index`` key pointing back to the
    original raw row in ``file.rows``; the writer uses it to update the
    correct line on save.
    """
    accounts: List[Dict[str, Any]] = []
    for index, raw in enumerate(file.rows):
        normalized = normalize_account(raw)
        # Preserve the raw row so we can write it back later. We also keep
        # the index because rows might shift when re-saving.
        normalized["__row_index"] = index
        normalized["__raw"] = dict(raw)
        accounts.append(normalized)
    return accounts


def cookies_to_text(cookies: Any) -> str:
    """Serialize cookies (list[dict] or pre-stringified) to a single TSV cell."""
    if not cookies:
        return ""
    if isinstance(cookies, str):
        return cookies.replace("\n", " ").replace("\r", " ")
    try:
        return json.dumps(cookies, ensure_ascii=False)
    except Exception:
        return str(cookies)


def update_row_with_result(
    raw: Dict[str, str],
    *,
    status: str = "",
    verify: str = "",
    quota: str = "",
    cookies: Any = None,
    extra: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return a copy of ``raw`` with run-result columns updated.

    Columns that don't yet exist in the row dict will be added. The caller
    is responsible for adding any new keys to the file header before
    saving so the column order stays predictable.
    """
    new_row = dict(raw)
    if status:
        new_row["status"] = status
    if verify:
        new_row["verify"] = verify
    if quota:
        new_row["quota"] = quota
    cookies_text = cookies_to_text(cookies)
    if cookies_text:
        new_row["cookies"] = cookies_text
    if extra:
        for key, value in extra.items():
            new_row[key] = str(value)
    return new_row


def write_text_accounts(
    file: TextAccountFile,
    *,
    log: Optional[Callable[[str], None]] = None,
) -> None:
    """Overwrite the source file with the current rows and header.

    Writes the file atomically: serialize to a temp file in the same
    directory, then ``os.replace`` it onto the original. This avoids
    leaving the user with a half-written file if something fails midway.
    """
    log_fn = log or (lambda *_: None)
    target = Path(file.path)
    target.parent.mkdir(parents=True, exist_ok=True)

    # Make sure every required column is present in the header (preserve
    # the user's column order though — only append the missing ones).
    header = list(file.header)
    for col in REQUIRED_OUTPUT_COLUMNS:
        if col not in header:
            header.append(col)
    file.header = header

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    for row in file.rows:
        writer.writerow([
            str(row.get(col, "")).replace("\r", " ").replace("\n", " ")
            for col in header
        ])

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(buffer.getvalue(), encoding="utf-8")
    try:
        tmp.replace(target)
    except PermissionError:
        # Best-effort fallback if replace failed (e.g. file is open in Notepad).
        target.write_text(buffer.getvalue(), encoding="utf-8")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
    log_fn(f"[TEXT] Saved {len(file.rows)} accounts -> {target}")


class TextAccountStore:
    """Thread-safe holder of a TextAccountFile for the GUI to consume."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file: Optional[TextAccountFile] = None
        self._accounts: List[Dict[str, Any]] = []

    @property
    def file(self) -> Optional[TextAccountFile]:
        return self._file

    @property
    def accounts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._accounts)

    def load(self, path: str | Path) -> List[Dict[str, Any]]:
        file = read_text_accounts(path)
        accounts = to_runtime_accounts(file)
        with self._lock:
            self._file = file
            self._accounts = accounts
        return accounts

    def update_account(
        self,
        email: str,
        *,
        status: str = "",
        verify: str = "",
        quota: str = "",
        cookies: Any = None,
    ) -> None:
        """Apply a result to the matching row keyed by email (case-insensitive)."""
        if not email:
            return
        target = email.strip().lower()
        with self._lock:
            file = self._file
            if file is None:
                return
            for index, row in enumerate(file.rows):
                row_email = (
                    row.get("email")
                    or row.get("mail")
                    or row.get("gmail")
                    or ""
                )
                if str(row_email).strip().lower() != target:
                    continue
                file.rows[index] = update_row_with_result(
                    row,
                    status=status,
                    verify=verify,
                    quota=quota,
                    cookies=cookies,
                )
                # Mirror into the in-memory account list too so the table
                # reflects the change without a full reload.
                for acc in self._accounts:
                    if (acc.get("email") or "").strip().lower() == target:
                        if status:
                            acc["status"] = status
                        if verify:
                            acc["verify"] = verify
                        if quota:
                            acc["quota"] = quota
                        if cookies:
                            acc["cookies"] = cookies_to_text(cookies)
                        acc["__raw"] = dict(file.rows[index])
                break

    def has_account(self, email: str) -> bool:
        """True if ``email`` is in the loaded text source (case-insensitive)."""
        if not email:
            return False
        target = email.strip().lower()
        with self._lock:
            for acc in self._accounts:
                if (acc.get("email") or "").strip().lower() == target:
                    return True
        return False

    def find_row_index(self, email: str) -> int:
        """Locate the source-file row index for ``email`` (case-insensitive)."""
        if not email:
            return -1
        target = email.strip().lower()
        with self._lock:
            file = self._file
            if file is None:
                return -1
            for index, row in enumerate(file.rows):
                row_email = (
                    row.get("email") or row.get("mail") or row.get("gmail") or ""
                )
                if str(row_email).strip().lower() == target:
                    return index
        return -1

    def delete_account(
        self,
        email: str,
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Remove a single account by email. Returns True if anything was removed.

        Atomic: rebuilds the source file from in-memory state.
        Sheet-source data is never touched here — this only edits the
        text TSV the user loaded.
        """
        return self.delete_accounts([email], log=log) > 0

    def delete_accounts(
        self,
        emails: List[str],
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Batch-delete multiple accounts. Returns the count actually removed."""
        log_fn = log or (lambda *_: None)
        targets = {e.strip().lower() for e in emails if e and e.strip()}
        if not targets:
            return 0
        with self._lock:
            file = self._file
            if file is None:
                return 0
            kept_rows: List[Dict[str, str]] = []
            removed = 0
            for row in file.rows:
                row_email = str(
                    row.get("email") or row.get("mail") or row.get("gmail") or ""
                ).strip().lower()
                if row_email and row_email in targets:
                    removed += 1
                    continue
                kept_rows.append(row)
            if removed == 0:
                return 0
            file.rows = kept_rows
            self._accounts = [
                acc
                for acc in self._accounts
                if (acc.get("email") or "").strip().lower() not in targets
            ]
        write_text_accounts(file, log=log_fn)
        log_fn(f"[TEXT] Deleted {removed} account(s)")
        return removed

    def replace_account(
        self,
        email: str,
        new_row: Dict[str, str],
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Update fields of an existing row keyed by ``email``.

        ``new_row`` is the user-edited row; missing keys are kept from
        the original. The row's email field can be changed too — the
        original ``email`` arg locates the row, the new value rewrites it.
        """
        log_fn = log or (lambda *_: None)
        if not email:
            return False
        target = email.strip().lower()
        with self._lock:
            file = self._file
            if file is None:
                return False
            for index, row in enumerate(file.rows):
                row_email = str(
                    row.get("email") or row.get("mail") or row.get("gmail") or ""
                ).strip().lower()
                if row_email != target:
                    continue
                merged = dict(row)
                for key, value in new_row.items():
                    merged[key] = "" if value is None else str(value)
                # Make sure any new keys the user introduced surface in the header
                # so the round-trip preserves them.
                for key in merged:
                    if key not in file.header and not key.startswith("__"):
                        file.header.append(key)
                file.rows[index] = {
                    k: v for k, v in merged.items() if not k.startswith("__")
                }
                # Mirror into _accounts so the GUI table reflects the change
                # without a full reload.
                normalized = normalize_account(file.rows[index])
                normalized["__row_index"] = index
                normalized["__raw"] = dict(file.rows[index])
                if 0 <= index < len(self._accounts):
                    self._accounts[index] = normalized
                else:
                    self._accounts.append(normalized)
                break
            else:
                return False
        write_text_accounts(file, log=log_fn)
        log_fn(f"[TEXT] Updated account row for {email}")
        return True

    def add_account(
        self,
        new_row: Dict[str, str],
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> bool:
        """Append a new row to the source file.

        Requires ``email`` to be present and not already taken (case-
        insensitive). Returns False if validation fails so the GUI can
        surface a friendly error.
        """
        log_fn = log or (lambda *_: None)
        email = str(
            new_row.get("email") or new_row.get("mail") or new_row.get("gmail") or ""
        ).strip()
        if not email:
            log_fn("[TEXT] add_account rejected: missing email")
            return False
        target = email.lower()
        with self._lock:
            file = self._file
            if file is None:
                # Bootstrap an empty store on first add — useful when user
                # creates a fresh accounts file from the GUI.
                file = TextAccountFile(
                    path=Path("data/accounts.txt"),
                    header=list(REQUIRED_OUTPUT_COLUMNS),
                    rows=[],
                )
                self._file = file
            for row in file.rows:
                row_email = str(
                    row.get("email") or row.get("mail") or row.get("gmail") or ""
                ).strip().lower()
                if row_email == target:
                    log_fn(f"[TEXT] add_account rejected: {email} already exists")
                    return False
            row: Dict[str, str] = {}
            for key, value in new_row.items():
                row[key] = "" if value is None else str(value)
            row["email"] = email  # canonical key column
            for key in row:
                if key not in file.header and not key.startswith("__"):
                    file.header.append(key)
            file.rows.append(row)
            normalized = normalize_account(row)
            normalized["__row_index"] = len(file.rows) - 1
            normalized["__raw"] = dict(row)
            self._accounts.append(normalized)
        write_text_accounts(file, log=log_fn)
        log_fn(f"[TEXT] Added account row for {email}")
        return True

    def move_to_exhausted(
        self,
        email: str,
        quota_text: str = "",
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> Optional[Path]:
        """Move a row from the source TSV to a sibling ``<name>_no_quota.txt``.

        Atomic enough for our purposes: append to the exhausted file, then
        remove from the source list, then save both. Both files share the
        source header so users can copy rows back manually if needed.

        Returns the exhausted-file path, or ``None`` if no row was moved
        (account not present, no source file loaded, etc.).
        """
        log_fn = log or (lambda *_: None)
        if not email:
            return None
        target = email.strip().lower()
        with self._lock:
            file = self._file
            if file is None:
                return None
            row_index: Optional[int] = None
            row: Optional[Dict[str, str]] = None
            for index, candidate in enumerate(file.rows):
                row_email = (
                    candidate.get("email")
                    or candidate.get("mail")
                    or candidate.get("gmail")
                    or ""
                )
                if str(row_email).strip().lower() == target:
                    row_index = index
                    row = candidate
                    break
            if row is None or row_index is None:
                return None

            # Stamp the row with no_quota status before moving so the
            # exhausted file row is self-explanatory.
            stamped = update_row_with_result(
                row,
                status="no_quota",
                quota=quota_text,
            )

            exhausted_path = exhausted_file_path(file.path)
            exhausted = read_text_accounts(exhausted_path) if exhausted_path.exists() else TextAccountFile(
                path=exhausted_path,
                header=list(file.header),
                rows=[],
            )
            # Make sure the exhausted file uses the source header so the
            # columns line up across the pair.
            for col in file.header:
                if col not in exhausted.header:
                    exhausted.header.append(col)
            # Replace any existing row for the same email (so re-running
            # on the same account doesn't duplicate entries).
            exhausted.rows = [
                r for r in exhausted.rows
                if str(r.get("email") or r.get("mail") or r.get("gmail") or "")
                .strip().lower() != target
            ]
            exhausted.rows.append(stamped)
            write_text_accounts(exhausted, log=log_fn)

            # Remove from the live source.
            file.rows.pop(row_index)
            self._accounts = [
                acc
                for acc in self._accounts
                if (acc.get("email") or "").strip().lower() != target
            ]
            write_text_accounts(file, log=log_fn)
            log_fn(
                f"[TEXT] Moved {email} -> exhausted file {exhausted_path.name}"
            )
            return exhausted_path

    def restore_from_exhausted(
        self,
        email: str = "",
        *,
        log: Optional[Callable[[str], None]] = None,
    ) -> int:
        """Move rows back from ``<name>_no_quota.txt`` to the source file.

        If ``email`` is given, only that row moves (case-insensitive).
        Otherwise every row in the exhausted file is restored. Returns the
        number of rows moved.
        """
        log_fn = log or (lambda *_: None)
        with self._lock:
            file = self._file
            if file is None:
                return 0
            exhausted_path = exhausted_file_path(file.path)
            if not exhausted_path.exists():
                return 0
            exhausted = read_text_accounts(exhausted_path)
            if not exhausted.rows:
                return 0
            target = email.strip().lower() if email else ""
            restored: List[Dict[str, str]] = []
            kept: List[Dict[str, str]] = []
            for row in exhausted.rows:
                row_email = str(
                    row.get("email")
                    or row.get("mail")
                    or row.get("gmail")
                    or ""
                ).strip().lower()
                if target and row_email != target:
                    kept.append(row)
                    continue
                # Clear the no_quota stamp so the engine treats the row
                # as live again.
                row = dict(row)
                if row.get("status", "").strip().lower() == "no_quota":
                    row["status"] = ""
                restored.append(row)
            if not restored:
                return 0
            file.rows.extend(restored)
            self._accounts.extend(to_runtime_accounts(TextAccountFile(
                path=file.path,
                header=file.header,
                rows=restored,
            )))
            exhausted.rows = kept
        write_text_accounts(file, log=log_fn)
        # Save (or remove) the exhausted file.
        if exhausted.rows:
            write_text_accounts(exhausted, log=log_fn)
        else:
            try:
                exhausted_path.unlink(missing_ok=True)
            except Exception:
                pass
        log_fn(
            f"[TEXT] Restored {len(restored)} accounts from {exhausted_path.name}"
        )
        return len(restored)

    def save(self, log: Optional[Callable[[str], None]] = None) -> Optional[Path]:
        with self._lock:
            file = self._file
        if file is None:
            return None
        write_text_accounts(file, log=log)
        return file.path
