# -*- coding: utf-8 -*-
"""在独立目录中创建 PaddleOCR 旁路插件环境（与主程序分离，CPU/GPU 互不冲突）。

用法:
    python setup_paddle_plugin.py cpu
    python setup_paddle_plugin.py gpu

也可被主程序 UI 调用: install_paddle_plugin(device, app_dir, log=...)
从 GUI 安装时默认弹一个黑窗，整段安装进度都在里面显示。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Callable, List, Optional

APP_DIR = os.path.dirname(os.path.abspath(__file__))

CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# 固定版本：PP-OCRv6_small 需要 paddleocr>=3.7；框架按官方 Quick Start 钉 3.2.0。
PADDLE_OCR_VERSION = "3.7.0"
PADDLE_FRAMEWORK_VERSION = "3.2.0"
PADDLE_CPU_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cpu/"
# Windows 常见 CUDA 11.8；其他 CUDA 需改此源或手动安装对应 paddlepaddle-gpu
PADDLE_GPU_INDEX = "https://www.paddlepaddle.org.cn/packages/stable/cu118/"
COMMON_DEPS = (
    f"paddleocr=={PADDLE_OCR_VERSION}",
    "Pillow>=9.0.0",
)


def log_print(msg=""):
    print(msg, flush=True)


def fail(msg):
    log_print(f"[失败] {msg}")
    sys.exit(1)


def find_host_python() -> Optional[str]:
    """找到可用于创建 venv 的宿主 Python（打包后的 exe 本身不能 -m venv）。"""
    if not getattr(sys, "frozen", False):
        return sys.executable

    candidates = []
    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    for base in (
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("PROGRAMFILES", ""),
        r"C:\Python311",
        r"C:\Python312",
        r"C:\Python310",
    ):
        if not base:
            continue
        for rel in (
            r"Programs\Python\Python312\python.exe",
            r"Programs\Python\Python311\python.exe",
            r"Programs\Python\Python310\python.exe",
            r"python.exe",
        ):
            p = os.path.join(base, rel)
            if os.path.isfile(p):
                candidates.append(p)

    seen = set()
    for py in candidates:
        key = os.path.normcase(os.path.abspath(py))
        if key in seen:
            continue
        seen.add(key)
        try:
            r = subprocess.run(
                [py, "-c", "import sys; assert sys.version_info >= (3, 10); print(sys.executable)"],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if r.returncode == 0 and (r.stdout or "").strip():
                return (r.stdout or "").strip()
        except Exception:
            continue
    return None


def _worker_src(app_dir: str) -> str:
    for path in (
        os.path.join(app_dir, "paddle_ocr_worker.py"),
        os.path.join(APP_DIR, "paddle_ocr_worker.py"),
    ):
        if os.path.isfile(path):
            return path
    return os.path.join(app_dir, "paddle_ocr_worker.py")


def _bat_quote(path: str) -> str:
    return '"' + path.replace('"', "") + '"'


def _pip_quote(spec: str) -> str:
    """含比较符的包规格在 bat 里需要引号。"""
    if any(ch in spec for ch in "<> "):
        return _bat_quote(spec)
    return spec


def _isolate_env_lines(plugin: str) -> list[str]:
    """安装时收窄环境，避免 pip 去遍历 PATH 里的不受信任挂载点。"""
    return [
        f'set "PLUGIN={plugin}"',
        'set "PYTHONPATH="',
        'set "PYTHONNOUSERSITE=1"',
        'set "PYTHONSAFEPATH=1"',
        'set "PIP_DISABLE_PIP_VERSION_CHECK=1"',
        'set "PIP_NO_CACHE_DIR=1"',
        'if not exist "%PLUGIN%\\_pip_tmp" mkdir "%PLUGIN%\\_pip_tmp"',
        'set "TEMP=%PLUGIN%\\_pip_tmp"',
        'set "TMP=%PLUGIN%\\_pip_tmp"',
        'set "PATH=%PLUGIN%\\Scripts;%SystemRoot%\\System32;%SystemRoot%"',
    ]


def _framework_pip_args(device: str) -> List[str]:
    if device == "gpu":
        return [
            f"paddlepaddle-gpu=={PADDLE_FRAMEWORK_VERSION}",
            "-i",
            PADDLE_GPU_INDEX,
        ]
    return [
        f"paddlepaddle=={PADDLE_FRAMEWORK_VERSION}",
        "-i",
        PADDLE_CPU_INDEX,
    ]


def _pinned_pip_commands(device: str, py: str) -> List[List[str]]:
    """按顺序：升级 pip → 固定框架 → 固定 paddleocr/依赖。"""
    base = [py, "-u", "-m", "pip", "install", "--no-cache-dir", "--progress-bar", "on"]
    return [
        [py, "-u", "-m", "pip", "install", "--no-cache-dir", "--upgrade", "pip<25"],
        [*base, *_framework_pip_args(device)],
        [*base, *COMMON_DEPS],
    ]


def _write_install_bat(
    bat_path: str,
    *,
    device: str,
    host: str,
    plugin: str,
    worker_src: str,
    need_venv: bool,
) -> None:
    py = os.path.join(plugin, "Scripts", "python.exe")
    worker_dst = os.path.join(plugin, "paddle_ocr_worker.py")
    fw = "paddlepaddle-gpu" if device == "gpu" else "paddlepaddle"
    fw_index = PADDLE_GPU_INDEX if device == "gpu" else PADDLE_CPU_INDEX
    deps_line = " ".join(_pip_quote(p) for p in COMMON_DEPS)

    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        "set PYTHONUNBUFFERED=1",
        *_isolate_env_lines(plugin),
        f"title GalCoplit Paddle-{device} 安装进度",
        "echo ========================================",
        f"echo  GalCoplit Paddle-{device} 环境安装",
        f"echo  固定版本: {fw}=={PADDLE_FRAMEWORK_VERSION}",
        f"echo            paddleocr=={PADDLE_OCR_VERSION}",
        "echo            模型: PP-OCRv6_small",
        "echo  请勿关闭本窗口",
        "echo ========================================",
        "echo.",
    ]
    if need_venv:
        lines += [
            "echo [1/4] 创建虚拟环境 ...",
            f"{_bat_quote(host)} -m venv {_bat_quote(plugin)}",
            "if errorlevel 1 goto FAIL",
            "echo.",
        ]
    else:
        lines += [
            "echo [1/4] 已存在虚拟环境，跳过创建",
            "echo.",
        ]
    lines += [
        f"if not exist {_bat_quote(py)} (",
        "  echo [失败] 未找到 Scripts\\python.exe",
        "  goto FAIL",
        ")",
        "echo [2/4] 升级 pip ...",
        f"{_bat_quote(py)} -u -m pip install --no-cache-dir --upgrade \"pip<25\"",
        "if errorlevel 1 goto FAIL",
        "echo.",
        "echo [3/4] 安装固定版 Paddle 框架 ...",
        f"{_bat_quote(py)} -u -m pip install --no-cache-dir --progress-bar on "
        f"{fw}=={PADDLE_FRAMEWORK_VERSION} -i {fw_index}",
        "if errorlevel 1 goto FAIL",
        "echo.",
        "echo [3/4] 安装固定版 paddleocr 与依赖 ...",
        f"{_bat_quote(py)} -u -m pip install --no-cache-dir --progress-bar on {deps_line}",
        "if errorlevel 1 goto FAIL",
        "echo.",
        "echo [4/4] 复制 worker 脚本 ...",
        f"copy /Y {_bat_quote(worker_src)} {_bat_quote(worker_dst)} >nul",
        "if errorlevel 1 goto FAIL",
        "echo.",
        "echo ========================================",
        "echo  安装成功，可关闭本窗口",
        "echo ========================================",
        "echo.",
        "pause",
        "exit /b 0",
        ":FAIL",
        "echo.",
        "echo ========================================",
        "echo  安装失败，请把本窗口内容截图反馈",
        "echo ========================================",
        "echo.",
        "pause",
        "exit /b 1",
    ]
    with open(bat_path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("\r\n".join(lines) + "\r\n")


def _install_via_console_bat(
    device: str,
    host: str,
    plugin: str,
    worker_src: str,
    log: Callable[[str], None],
) -> None:
    need_venv = not os.path.isfile(os.path.join(plugin, "Scripts", "python.exe"))
    fd, bat_path = tempfile.mkstemp(prefix=f"galcoplit_paddle_{device}_", suffix=".bat")
    os.close(fd)
    try:
        _write_install_bat(
            bat_path,
            device=device,
            host=host,
            plugin=plugin,
            worker_src=worker_src,
            need_venv=need_venv,
        )
        log(f"已打开安装进度窗口：{bat_path}")
        r = subprocess.run(
            ["cmd.exe", "/c", bat_path],
            cwd=os.path.dirname(plugin) or APP_DIR,
            creationflags=CREATE_NEW_CONSOLE if os.name == "nt" else 0,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"Paddle-{device} 安装失败（退出码 {r.returncode}）。"
                "请查看弹出的命令行窗口中的报错信息。"
            )
    finally:
        try:
            os.remove(bat_path)
        except OSError:
            pass


def _install_quiet(
    device: str,
    host: str,
    plugin: str,
    worker_src: str,
    log: Callable[[str], None],
) -> None:
    """命令行模式：输出打到当前终端。"""
    py = os.path.join(plugin, "Scripts", "python.exe")
    flags = CREATE_NO_WINDOW if os.name == "nt" else 0
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = ""
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONSAFEPATH"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_CACHE_DIR"] = "1"
    tmp = os.path.join(plugin, "_pip_tmp")
    os.makedirs(tmp, exist_ok=True)
    env["TEMP"] = tmp
    env["TMP"] = tmp
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join(
        [os.path.join(plugin, "Scripts"), os.path.join(system_root, "System32"), system_root]
    )

    if not os.path.isfile(py):
        log("[1/4] 创建虚拟环境 ...")
        r = subprocess.run(
            [host, "-m", "venv", plugin],
            cwd=os.path.dirname(plugin) or APP_DIR,
            capture_output=True,
            text=True,
            env=env,
            creationflags=flags,
        )
        if r.returncode != 0:
            raise RuntimeError(f"创建 venv 失败：\n{(r.stderr or r.stdout or '').strip()}")
    else:
        log("[1/4] 已存在虚拟环境，跳过创建")
    if not os.path.isfile(py):
        raise RuntimeError("venv 创建后仍找不到 Scripts/python.exe")

    log("[2/4][3/4] 安装固定版本依赖 ...")
    for cmd in _pinned_pip_commands(device, py):
        r = subprocess.run(
            cmd, cwd=plugin, capture_output=True, text=True, env=env, creationflags=flags
        )
        if r.returncode != 0:
            raise RuntimeError(((r.stderr or "") + (r.stdout or "")).strip() or "pip 失败")

    log("[4/4] 复制 worker 脚本 ...")
    shutil.copy2(worker_src, os.path.join(plugin, "paddle_ocr_worker.py"))
    log("安装完成。")


def install_paddle_plugin(
    device: str,
    app_dir: Optional[str] = None,
    host_python: Optional[str] = None,
    log: Callable[[str], None] = log_print,
    show_console: bool = True,
) -> None:
    """安装 paddle-cpu / paddle-gpu 旁路环境。失败抛 RuntimeError。"""
    if device not in ("cpu", "gpu"):
        raise RuntimeError("device 必须是 cpu 或 gpu")
    root = app_dir or APP_DIR
    plugin = os.path.join(root, f"paddle_ocr_{device}")
    worker_src = _worker_src(root)
    if not os.path.isfile(worker_src):
        raise RuntimeError(
            f"找不到 paddle_ocr_worker.py。\n请将其与主程序放在同一目录：\n{root}"
        )

    host = host_python or find_host_python()
    if not host:
        raise RuntimeError(
            "未找到可用的 Python 3.10+。\n"
            "打包版安装插件需要本机已安装 Python，并加入 PATH。\n"
            "请先安装 Python 后再点「安装环境」。"
        )

    fw = "paddlepaddle-gpu" if device == "gpu" else "paddlepaddle"
    log("=" * 50)
    log(f"创建 PaddleOCR {device.upper()} 旁路插件")
    log(f"目标目录: {plugin}")
    log(f"宿主 Python: {host}")
    log(f"固定版本: {fw}=={PADDLE_FRAMEWORK_VERSION}, paddleocr=={PADDLE_OCR_VERSION}")
    log("固定模型: PP-OCRv6_small_det / PP-OCRv6_small_rec")
    log("=" * 50)

    os.makedirs(plugin, exist_ok=True)

    if show_console and os.name == "nt":
        _install_via_console_bat(device, host, plugin, worker_src, log)
    else:
        _install_quiet(device, host, plugin, worker_src, log)

    py = os.path.join(plugin, "Scripts", "python.exe")
    worker_dst = os.path.join(plugin, "paddle_ocr_worker.py")
    if not os.path.isfile(py) or not os.path.isfile(worker_dst):
        raise RuntimeError("安装结束但未找到预期文件，请查看命令行窗口日志。")
    log("安装完成。")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("cpu", "gpu"):
        fail("用法: python setup_paddle_plugin.py cpu|gpu")
    try:
        install_paddle_plugin(
            sys.argv[1], app_dir=APP_DIR, log=log_print, show_console=False
        )
    except Exception as e:
        fail(str(e))
    log_print()
    log_print("请在主程序设置中选择对应后端，并用「测试 OCR」验证。")


if __name__ == "__main__":
    if os.name != "nt":
        fail("当前脚本面向 Windows。")
    main()
