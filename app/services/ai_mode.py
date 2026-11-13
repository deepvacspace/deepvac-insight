"""Online or offline AI mode."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import app.common as common

FILE_NAME = "ai_mode.json"
ONLINE, OFFLINE = "online", "offline"
MODES = (ONLINE, OFFLINE)
MODEL_FILE = "Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
DEFAULTS = {
    "mode": ONLINE,
    "local_url": "http://127.0.0.1:8089/v1",
    "llm_dir": "",
    "start_local_model": True,
}


def path() -> Path:
    return common.DATA_DIR / FILE_NAME


def load() -> dict:
    config = dict(DEFAULTS)
    with contextlib.suppress(OSError, ValueError):
        config.update(json.loads(path().read_text(encoding="utf-8")))
    if config.get("mode") not in MODES:
        config["mode"] = ONLINE
    return config


def save(**changes) -> dict:
    stored = {}
    with contextlib.suppress(OSError, ValueError):
        stored = json.loads(path().read_text(encoding="utf-8"))
    stored.update(changes)
    path().parent.mkdir(parents=True, exist_ok=True)
    path().write_text(json.dumps(stored, indent=2), encoding="utf-8")
    return load()


def mode() -> str:
    return load()["mode"]


def is_offline() -> bool:
    return mode() == OFFLINE


def set_mode(value: str) -> dict:
    if value not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    return save(mode=value)


def llm_dir() -> Path | None:
    """Returns the folder with llama-server and the local model."""
    candidates = []
    if load().get("llm_dir"):
        candidates.append(Path(load()["llm_dir"]))
    if os.environ.get("DEEPVAC_LLM_DIR"):
        candidates.append(Path(os.environ["DEEPVAC_LLM_DIR"]))
    root = Path(__file__).resolve().parents[2]
    candidates += [
        root / "llm",
        root.parent / "control2-client" / "llm",
        root.parent.parent / "control2-client" / "llm",
        Path.home() / "Desktop" / "control2-client" / "llm",
    ]
    for folder in candidates:
        if (folder / "llama-server.exe").is_file() and (folder / "models" / MODEL_FILE).is_file():
            return folder
    return None
