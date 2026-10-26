from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


def _detect_project_root() -> Path:
    # PyInstaller sets sys.frozen; Nuitka sets the module-level __compiled__.
    # In both packaged modes the app lives next to the executable.
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = _detect_project_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
LOCAL_CONFIG_PATH = PROJECT_ROOT / "config.local.json"


@dataclass
class AppConfig:
    sheet_api_url: str = ""
    sheet_id: str = ""
    cookie_dir: str = "data/cookies"
    headless: bool = False
    max_workers: int = 1
    timeout_ms: int = 60000
    slow_mo_ms: int = 50
    reuse_cookies: bool = True
    save_cookies: bool = True
    quota_threshold: int = 10
    ui_settings: Dict[str, Any] = field(default_factory=dict)
    config_path: Path = DEFAULT_CONFIG_PATH

    @property
    def cookie_path(self) -> Path:
        path = Path(self.cookie_dir)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.mkdir(parents=True, exist_ok=True)
        return path


def load_config(path: str | os.PathLike[str] | None = None) -> AppConfig:
    raw_path = path or os.environ.get("NOTEGPT_QUOTA_CONFIG")
    if not raw_path:
        raw_path = LOCAL_CONFIG_PATH if LOCAL_CONFIG_PATH.exists() else DEFAULT_CONFIG_PATH
    cfg_path = Path(raw_path).expanduser().resolve()
    data: Dict[str, Any] = {}
    if cfg_path.exists():
        with cfg_path.open("r", encoding="utf-8") as f:
            data = json.load(f)

    cfg = AppConfig(
        sheet_api_url=str(data.get("sheet_api_url", "")).strip(),
        sheet_id=str(data.get("sheet_id", "")).strip(),
        cookie_dir=str(data.get("cookie_dir", "data/cookies")).strip() or "data/cookies",
        headless=bool(data.get("headless", False)),
        max_workers=max(1, int(data.get("max_workers", 1) or 1)),
        timeout_ms=max(10000, int(data.get("timeout_ms", 60000) or 60000)),
        slow_mo_ms=max(0, int(data.get("slow_mo_ms", 50) or 0)),
        reuse_cookies=bool(data.get("reuse_cookies", True)),
        save_cookies=bool(data.get("save_cookies", True)),
        quota_threshold=max(0, int(data.get("quota_threshold", 10) or 0)),
        ui_settings=dict(data.get("ui_settings", {}) or {}),
        config_path=cfg_path,
    )
    cfg.cookie_path.mkdir(parents=True, exist_ok=True)
    return cfg


def save_config(config: AppConfig) -> None:
    data = {
        "sheet_api_url": config.sheet_api_url,
        "sheet_id": config.sheet_id,
        "cookie_dir": config.cookie_dir,
        "headless": config.headless,
        "max_workers": config.max_workers,
        "timeout_ms": config.timeout_ms,
        "slow_mo_ms": config.slow_mo_ms,
        "reuse_cookies": config.reuse_cookies,
        "save_cookies": config.save_cookies,
        "quota_threshold": config.quota_threshold,
        "ui_settings": config.ui_settings,
    }
    config.config_path.parent.mkdir(parents=True, exist_ok=True)
    with config.config_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
