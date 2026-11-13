"""Local model server for offline AI."""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request

from app.services import ai_mode

log = logging.getLogger(__name__)


class LocalModelError(RuntimeError):
    pass


def base_url() -> str:
    return ai_mode.load()["local_url"].rstrip("/")


def available(timeout=1.0) -> bool:
    """Whether the local model server answers."""
    root = base_url().rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(f"{root}/health", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")).get("status") == "ok"
    except (OSError, urllib.error.URLError, ValueError):
        return False


def chat(messages, max_tokens=400, temperature=0.3, json_object=False, timeout=60.0) -> str:
    """The model's answer to an OpenAI-style message list."""
    body = {
        "model": "local",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}
    request = urllib.request.Request(
        base_url() + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8"))
        return reply["choices"][0]["message"]["content"].strip()
    except (OSError, urllib.error.URLError, ValueError, KeyError, IndexError) as exc:
        raise LocalModelError(f"The local model at {base_url()} didn't answer: {exc}") from exc


def start_command(folder) -> list[str]:
    port = base_url().rsplit(":", 1)[-1].split("/")[0]
    return [
        str(folder / "llama-server.exe"),
        "--model",
        str(folder / "models" / ai_mode.MODEL_FILE),
        "--host",
        "127.0.0.1",
        "--port",
        port,
        "--ctx-size",
        "8192",
        "--jinja",
        "--no-webui",
    ]


class LocalModelServer:
    """Starts and stops llama-server."""

    READY_TIMEOUT_S = 90.0

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.status = "stopped"

    def ensure(self, on_done=None) -> None:
        threading.Thread(target=self._ensure, args=(on_done,), daemon=True).start()

    def _ensure(self, on_done) -> None:
        with self._lock:
            if available():
                self.status = "ready" if self._process else "running elsewhere"
            else:
                folder = ai_mode.llm_dir()
                if folder is None:
                    self.status = "missing"
                else:
                    self._start(folder)
        if on_done:
            on_done(self.status)

    def _start(self, folder) -> None:
        self.status = "starting"
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._process = subprocess.Popen(
                start_command(folder),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            log.warning("local model: can't start llama-server: %s", exc)
            self.status = "failed"
            return
        deadline = time.monotonic() + self.READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.status = "failed"
                self._process = None
                return
            if available():
                self.status = "ready"
                self._warm_up()
                return
            time.sleep(1.0)
        self.status = "failed"

    def _warm_up(self) -> None:
        with contextlib.suppress(LocalModelError):
            chat([{"role": "user", "content": "Reply with OK."}], max_tokens=4, timeout=60)

    def stop(self) -> None:
        with self._lock:
            if self._process is not None:
                self._process.kill()
                self._process = None
            self.status = "stopped"
