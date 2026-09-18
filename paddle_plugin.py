# -*- coding: utf-8 -*-
"""PaddleOCR 旁路插件：发现目录、拉起 worker、HTTP 调用。

目录约定（与主程序同级）:
    paddle_ocr_cpu/
        PaddleOcrWorker.exe          # 推荐：独立打包的 worker
        或 Scripts/python.exe        # venv
        或 python.exe                # embed / venv 根
    paddle_ocr_gpu/
        （同上）

源码开发时也可把 paddle_ocr_worker.py 放在插件目录或主程序目录。
"""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

from PIL import Image

BACKEND_CPU = "paddle-cpu"
BACKEND_GPU = "paddle-gpu"
PLUGIN_DIR_CPU = "paddle_ocr_cpu"
PLUGIN_DIR_GPU = "paddle_ocr_gpu"
READY_PREFIX = "GALCOPLIT_OCR_READY port="
START_TIMEOUT_SEC = 90.0
OCR_TIMEOUT_SEC = 120.0


def _app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def backend_to_device(backend: str) -> str:
    if backend in (BACKEND_GPU, "paddle_gpu", "gpu"):
        return "gpu"
    return "cpu"


def plugin_dir_name(device: str) -> str:
    return PLUGIN_DIR_GPU if device == "gpu" else PLUGIN_DIR_CPU


def plugin_path(device: str, app_dir: Optional[str] = None) -> str:
    root = app_dir or _app_dir()
    return os.path.join(root, plugin_dir_name(device))


@dataclass
class PluginStatus:
    device: str
    installed: bool
    path: str
    kind: str  # exe | venv | python | missing
    detail: str
    launch_cmd: Optional[list[str]] = None


def _worker_script_candidates(app_dir: str, plugin: str) -> list[str]:
    # 优先用主程序旁的 worker，便于升级程序后无需重装 paddle 包也能修 API 兼容
    names = ("paddle_ocr_worker.py",)
    out = []
    for name in names:
        out.append(os.path.join(app_dir, name))
        out.append(os.path.join(plugin, name))
    return out


def _find_worker_script(app_dir: str, plugin: str) -> Optional[str]:
    for path in _worker_script_candidates(app_dir, plugin):
        if os.path.isfile(path):
            return path
    return None


def inspect_plugin(device: str, app_dir: Optional[str] = None) -> PluginStatus:
    """检查插件是否已安装，并给出启动命令。"""
    root = app_dir or _app_dir()
    device = "gpu" if device == "gpu" else "cpu"
    plugin = plugin_path(device, root)
    script_in_root = _find_worker_script(root, plugin if os.path.isdir(plugin) else root)

    if not os.path.isdir(plugin):
        # 源码运行：即使还没建插件目录，也允许用当前解释器试跑
        if script_in_root and not getattr(sys, "frozen", False):
            return PluginStatus(
                device=device,
                installed=True,
                path=plugin,
                kind="python",
                detail=(
                    f"开发模式：尚未创建 {plugin_dir_name(device)}，"
                    "将用当前 Python 启动 worker（请确认已安装对应 paddle）"
                ),
                launch_cmd=[sys.executable, script_in_root, "--device", device, "--port", "0"],
            )
        return PluginStatus(
            device=device,
            installed=False,
            path=plugin,
            kind="missing",
            detail=f"未找到插件目录：{plugin_dir_name(device)}",
        )

    exe = os.path.join(plugin, "PaddleOcrWorker.exe")
    if os.path.isfile(exe):
        return PluginStatus(
            device=device,
            installed=True,
            path=plugin,
            kind="exe",
            detail="已检测到 PaddleOcrWorker.exe",
            launch_cmd=[exe, "--device", device, "--port", "0"],
        )

    script = _find_worker_script(root, plugin)
    py_candidates = [
        os.path.join(plugin, "Scripts", "python.exe"),
        os.path.join(plugin, "python.exe"),
        os.path.join(plugin, "bin", "python"),
    ]
    for py in py_candidates:
        if os.path.isfile(py) and script:
            return PluginStatus(
                device=device,
                installed=True,
                path=plugin,
                kind="venv",
                detail=f"已检测到 Python 环境：{os.path.relpath(py, plugin)}",
                launch_cmd=[py, script, "--device", device, "--port", "0"],
            )

    # 开发兜底：当前解释器 + worker 脚本
    if script and not getattr(sys, "frozen", False):
        return PluginStatus(
            device=device,
            installed=True,
            path=plugin,
            kind="python",
            detail="开发模式：将用当前 Python 启动 worker（请确认已安装对应 paddle）",
            launch_cmd=[sys.executable, script, "--device", device, "--port", "0"],
        )

    if not script:
        return PluginStatus(
            device=device,
            installed=False,
            path=plugin,
            kind="missing",
            detail="插件目录存在，但缺少 PaddleOcrWorker.exe / python 与 paddle_ocr_worker.py",
        )
    return PluginStatus(
        device=device,
        installed=False,
        path=plugin,
        kind="missing",
        detail="插件目录存在，但未找到可用的 Python 或 PaddleOcrWorker.exe",
    )


def ensure_plugin_dir(device: str, app_dir: Optional[str] = None) -> str:
    path = plugin_path(device, app_dir)
    os.makedirs(path, exist_ok=True)
    return path


def _python_has_paddle(python_exe: str) -> bool:
    """快速判断解释器是否具备 paddleocr（优先看 site-packages，避免完整 import）。"""
    try:
        exe = os.path.abspath(python_exe)
        # venv: <root>/Scripts/python.exe → <root>/Lib/site-packages/paddleocr
        scripts_dir = os.path.dirname(exe)
        root = os.path.dirname(scripts_dir)
        for base in (
            os.path.join(root, "Lib", "site-packages"),
            os.path.join(root, "lib", "site-packages"),
        ):
            if os.path.isdir(os.path.join(base, "paddleocr")) and (
                os.path.isdir(os.path.join(base, "paddle"))
                or os.path.isdir(os.path.join(base, "paddlepaddle"))
            ):
                return True

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = os.environ.copy()
        env["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
        r = subprocess.run(
            [
                exe,
                "-c",
                "import importlib.util as u; "
                "print('1' if u.find_spec('paddleocr') and u.find_spec('paddle') else '0')",
            ],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=creationflags,
            env=env,
        )
        return r.returncode == 0 and (r.stdout or "").strip() == "1"
    except Exception:
        return False


def environment_ready(device: str, app_dir: Optional[str] = None) -> PluginStatus:
    """UI 用的环境检测：通过 = 可真正拉起带 paddle 的 worker。

    比 inspect_plugin 更严：开发兜底 / 空 venv 若缺 paddleocr 也算未通过。
    """
    status = inspect_plugin(device, app_dir)
    if not status.installed or not status.launch_cmd:
        return PluginStatus(
            device=status.device,
            installed=False,
            path=status.path,
            kind=status.kind,
            detail=status.detail or "未检测到插件环境",
            launch_cmd=None,
        )
    if status.kind == "exe":
        return status
    # venv / python：校验解释器里确实有 paddleocr
    py = status.launch_cmd[0]
    if not _python_has_paddle(py):
        return PluginStatus(
            device=status.device,
            installed=False,
            path=status.path,
            kind="missing",
            detail="已找到解释器，但未安装 paddleocr / paddlepaddle",
            launch_cmd=None,
        )
    return PluginStatus(
        device=status.device,
        installed=True,
        path=status.path,
        kind=status.kind,
        detail="环境检测通过",
        launch_cmd=status.launch_cmd,
    )


class _WorkerHandle:
    def __init__(self, device: str, proc: subprocess.Popen, port: int):
        self.device = device
        self.proc = proc
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"


class PaddleWorkerManager:
    """按 device 懒启动、复用 worker 进程。"""

    def __init__(self, app_dir: Optional[str] = None):
        self.app_dir = app_dir or _app_dir()
        self._lock = threading.Lock()
        self._workers: dict[str, _WorkerHandle] = {}

    def status(self, device: str) -> PluginStatus:
        return inspect_plugin(device, self.app_dir)

    def _alive(self, handle: _WorkerHandle) -> bool:
        if handle.proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(handle.base_url + "/health", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return bool(data.get("ok"))
        except Exception:
            return False

    def _start(self, device: str) -> _WorkerHandle:
        status = inspect_plugin(device, self.app_dir)
        if not status.installed or not status.launch_cmd:
            raise RuntimeError(
                f"未安装 PaddleOCR {device.upper()} 插件。\n"
                f"请将插件放到：{status.path}\n"
                f"详情：{status.detail}"
            )
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        proc = subprocess.Popen(
            status.launch_cmd,
            cwd=status.path if status.kind == "exe" else self.app_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        port = self._wait_ready(proc, START_TIMEOUT_SEC)
        handle = _WorkerHandle(device, proc, port)
        # 确认 health（允许 paddle 尚未 import 成功时给出明确错误）
        try:
            with urllib.request.urlopen(handle.base_url + "/health", timeout=5) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            if not info.get("ok"):
                err = info.get("error") or "paddle 未能加载"
                self._kill(proc)
                raise RuntimeError(f"PaddleOCR {device} worker 启动失败：{err}")
            if device == "gpu" and not info.get("cuda_compiled"):
                # 仍允许尝试；很多环境编译信息不准，仅作提示写到 stderr
                pass
        except RuntimeError:
            raise
        except Exception as e:
            self._kill(proc)
            raise RuntimeError(f"无法连接 PaddleOCR {device} worker：{e}") from e
        return handle

    def _wait_ready(self, proc: subprocess.Popen, timeout: float) -> int:
        deadline = time.monotonic() + timeout
        assert proc.stdout is not None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                err = ""
                if proc.stderr is not None:
                    try:
                        err = proc.stderr.read() or ""
                    except Exception:
                        err = ""
                raise RuntimeError(
                    "PaddleOCR worker 进程已退出。"
                    + (f"\n{err.strip()}" if err.strip() else "")
                )
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.strip()
            if line.startswith(READY_PREFIX):
                try:
                    return int(line[len(READY_PREFIX) :].strip())
                except ValueError as e:
                    self._kill(proc)
                    raise RuntimeError(f"无法解析 worker 端口：{line}") from e
        self._kill(proc)
        raise RuntimeError(f"等待 PaddleOCR worker 就绪超时（>{int(timeout)}s）")

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except Exception:
                    proc.kill()
        except Exception:
            pass

    def ensure(self, device: str) -> _WorkerHandle:
        device = "gpu" if device == "gpu" else "cpu"
        with self._lock:
            handle = self._workers.get(device)
            if handle and self._alive(handle):
                return handle
            if handle:
                self._kill(handle.proc)
                self._workers.pop(device, None)
            handle = self._start(device)
            self._workers[device] = handle
            return handle

    def ocr_image(
        self, pil_image: Image.Image, lang: str, device: str, det_limit: int = 0
    ) -> str:
        handle = self.ensure(device)
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        payload = json.dumps(
            {
                "image_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
                "lang": lang,
                "det_limit": int(det_limit) if det_limit else 0,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            handle.base_url + "/ocr",
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=OCR_TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(body)
            except Exception:
                raise RuntimeError(f"PaddleOCR worker HTTP {e.code}: {body}") from e
            raise RuntimeError(data.get("error") or body) from e
        except Exception as e:
            # 连接失败时丢掉句柄，下次重建
            key = "gpu" if device == "gpu" else "cpu"
            with self._lock:
                old = self._workers.pop(key, None)
                if old:
                    self._kill(old.proc)
            raise RuntimeError(f"调用 PaddleOCR worker 失败：{e}") from e
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "OCR 失败")
        return data.get("text") or ""

    def shutdown_all(self) -> None:
        with self._lock:
            for handle in list(self._workers.values()):
                self._kill(handle.proc)
            self._workers.clear()


_manager: Optional[PaddleWorkerManager] = None
_manager_lock = threading.Lock()


def get_manager(app_dir: Optional[str] = None) -> PaddleWorkerManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = PaddleWorkerManager(app_dir=app_dir)
        return _manager


def shutdown_all() -> None:
    global _manager
    with _manager_lock:
        if _manager is not None:
            _manager.shutdown_all()
            _manager = None


def describe_backends(app_dir: Optional[str] = None) -> dict[str, PluginStatus]:
    root = app_dir or _app_dir()
    return {
        BACKEND_CPU: inspect_plugin("cpu", root),
        BACKEND_GPU: inspect_plugin("gpu", root),
    }
