# -*- coding: utf-8 -*-
"""GalCoplit 打包脚本：在干净的 build_venv 里用 PyInstaller 生成产物。

用法：
    python build.py                 单文件版 GalCoplit.exe（仅 Rapid，<150MB）
    python build.py --dry-run       只做环境准备与自检
    python build.py --install       目录版 + Inno Setup 安装包（启动更快）
    python build.py --paddle-worker 可选：当前解释器打成 PaddleOcrWorker.exe

build.bat / build_install.bat 为对应启动器。
"""
import os
import shutil
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(APP_DIR, "build_venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
SPEC = "GalCoplit.spec"
INSTALL_SPEC = "GalCoplit-install.spec"
PADDLE_SPEC = "PaddleOcrWorker.spec"
ISS = "GalCoplit.iss"
EXE_REL = os.path.join("dist", "GalCoplit.exe")
INSTALL_DIR_REL = os.path.join("dist", "GalCoplit-install")
SETUP_REL = os.path.join("dist", "GalCoplit-Setup.exe")
SIZE_LIMIT_MB = 150

HEAVY_MODULES = ("torch", "torchvision", "transformers", "paddle", "sklearn", "matplotlib")

ISCC_CANDIDATES = (
    os.path.join(os.environ.get("LocalAppData", ""), r"Programs\Inno Setup 6\ISCC.exe"),
    os.path.join(os.environ.get("LocalAppData", ""), r"Programs\Inno Setup 7\ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
)


def log(msg=""):
    print(msg, flush=True)


def fail(msg):
    log(f"[失败] {msg}")
    sys.exit(1)


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=APP_DIR, **kwargs)


def venv_python_works():
    if not os.path.isfile(VENV_PY):
        return False
    try:
        return run([VENV_PY, "-c", "import sys"], capture_output=True).returncode == 0
    except OSError:
        return False


def prepare_venv():
    log("[1/4] 准备干净的构建虚拟环境 build_venv ...")
    if os.path.isdir(VENV_DIR) and not venv_python_works():
        log("    现有 build_venv 不可用，删除后重建 ...")
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    if not os.path.isfile(VENV_PY):
        log("    正在创建 build_venv ...")
        if run([sys.executable, "-m", "venv", VENV_DIR]).returncode != 0:
            fail("创建虚拟环境失败，请确认 python 可用。")
    result = run([VENV_PY, "-c", "import sys;print(sys.prefix)"],
                 capture_output=True, text=True)
    log(f"    构建解释器: {result.stdout.strip()}")


def install_deps():
    log("[2/4] 安装固定版本依赖 + PyInstaller ...")
    run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"],
        stdout=subprocess.DEVNULL)
    if run([VENV_PY, "-m", "pip", "install", "-r", "requirements.txt",
            "pyinstaller"]).returncode != 0:
        fail("依赖安装失败，请检查网络或 requirements.txt。")


def check_clean_env():
    code = (
        "import importlib.util as u\n"
        f"mods = {HEAVY_MODULES!r}\n"
        "found = []\n"
        "for m in mods:\n"
        "    try:\n"
        "        if u.find_spec(m) is not None:\n"
        "            found.append(m)\n"
        "    except Exception:\n"
        "        pass\n"
        "print(','.join(found))\n"
    )
    result = run([VENV_PY, "-c", code], capture_output=True, text=True)
    dirty = [m for m in result.stdout.strip().split(",") if m]
    if dirty:
        fail(f"构建环境里存在重型库：{', '.join(dirty)}。"
             "打包会被污染（体积暴涨甚至弹窗报 DLL 错误），"
             "请删除 build_venv 后重跑本脚本。")
    log("    环境检查通过（无 torch / transformers / paddle）")


def ensure_icon():
    icon = os.path.join(APP_DIR, "icon.ico")
    if not os.path.isfile(icon):
        fail("缺少 icon.ico，无法写入 exe / 安装包 / 桌面快捷方式图标。")
    return icon


def copy_sidecar_files(target_dir: str):
    """把 worker 脚本与图标拷到程序目录，供运行时与「安装环境」使用。"""
    os.makedirs(target_dir, exist_ok=True)
    for name in ("paddle_ocr_worker.py", "icon.ico"):
        src = os.path.join(APP_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target_dir, name))
            log(f"    已复制 {name} -> {target_dir}")


def find_iscc() -> str:
    which = shutil.which("ISCC") or shutil.which("iscc")
    if which and os.path.isfile(which):
        return which
    for path in ISCC_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    return ""


def build_onefile():
    ensure_icon()
    log(f"[3/4] 按 {SPEC} 打包 onefile ...")
    if run([VENV_PY, "-m", "PyInstaller", "--clean", "--noconfirm",
            SPEC]).returncode != 0:
        fail("PyInstaller 打包失败，请向上查看报错。")
    exe = os.path.join(APP_DIR, EXE_REL)
    if not os.path.isfile(exe):
        fail(f"未生成 {EXE_REL}")
    # onefile 旁再放一份 worker/icon，方便绿色包用户装 paddle
    copy_sidecar_files(os.path.join(APP_DIR, "dist"))
    mb = os.path.getsize(exe) / 1048576
    log(f"[4/4] 产物: {EXE_REL}  {mb:.2f} MB")
    if mb >= SIZE_LIMIT_MB:
        log(f"[警告] 体积超过 {SIZE_LIMIT_MB} MB。")
    else:
        log(f"[OK] 体积在 {SIZE_LIMIT_MB} MB 以内。")
    log("单文件版启动时会解压到临时目录，一般比安装版慢一点。")


def build_install():
    """目录版（启动更快）+ Setup 安装包；覆盖安装保留配置/组件/数据。"""
    ensure_icon()
    log(f"[3/5] 按 {INSTALL_SPEC} 打包 onedir（安装版内核）...")
    install_dir = os.path.join(APP_DIR, INSTALL_DIR_REL)
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir, ignore_errors=True)
    if run([VENV_PY, "-m", "PyInstaller", "--clean", "--noconfirm",
            INSTALL_SPEC]).returncode != 0:
        fail("PyInstaller 目录版打包失败。")
    exe = os.path.join(install_dir, "GalCoplit.exe")
    if not os.path.isfile(exe):
        fail(f"未生成 {INSTALL_DIR_REL}\\GalCoplit.exe")
    copy_sidecar_files(install_dir)

    log("[4/5] 编译 Inno Setup 安装包 ...")
    iscc = find_iscc()
    if not iscc:
        log("[警告] 未找到 Inno Setup 6（ISCC.exe）。")
        log("    已生成目录版: dist\\GalCoplit-install\\")
        log("    请安装 https://jrsoftware.org/isinfo.php 后重跑 build_install.bat")
        log("    亦可直接分发该目录（绿色目录版，启动同样较快）。")
        return

    iss = os.path.join(APP_DIR, ISS)
    if not os.path.isfile(iss):
        fail(f"缺少 {ISS}")
    # 清掉旧安装包，避免误发
    setup = os.path.join(APP_DIR, SETUP_REL)
    if os.path.isfile(setup):
        os.remove(setup)
    if run([iscc, iss]).returncode != 0:
        fail("Inno Setup 编译失败。")
    if not os.path.isfile(setup):
        fail(f"未生成 {SETUP_REL}")
    mb = os.path.getsize(setup) / 1048576
    log(f"[5/5] 安装包: {SETUP_REL}  {mb:.2f} MB")
    log("覆盖安装不会改动已有 config.json / records.db / paddle_ocr_* 。")
    log("桌面快捷方式与卸载项图标均使用程序 icon。")


def build_paddle_worker():
    ensure_icon()
    log("=" * 60)
    log(" 打包 PaddleOcrWorker.exe（可选旁路）")
    log("=" * 60)
    try:
        import paddleocr  # noqa: F401
        import paddle  # noqa: F401
    except Exception as e:
        fail(
            f"当前解释器未安装 paddleocr/paddlepaddle：{e}\n"
            "请先：python setup_paddle_plugin.py cpu|gpu\n"
            "再在对应插件 venv 里执行：python build.py --paddle-worker"
        )
    if not os.path.isfile(os.path.join(APP_DIR, PADDLE_SPEC)):
        fail(f"找不到 {PADDLE_SPEC}")
    if run([sys.executable, "-m", "pip", "install", "pyinstaller"]).returncode != 0:
        fail("安装 pyinstaller 失败")
    if run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
            PADDLE_SPEC]).returncode != 0:
        fail("PyInstaller 打包 PaddleOcrWorker 失败")
    exe = os.path.join(APP_DIR, "dist", "PaddleOcrWorker.exe")
    if not os.path.isfile(exe):
        fail("未生成 dist/PaddleOcrWorker.exe")
    mb = os.path.getsize(exe) / 1048576
    log(f"产物: {exe}  ({mb:.1f} MB，已带 icon.ico）")
    log("请复制到主程序旁的 paddle_ocr_cpu\\ 或 paddle_ocr_gpu\\。")


def build_main(install_mode: bool):
    dry_run = "--dry-run" in sys.argv
    title = "安装版" if install_mode else "单文件版"
    log("=" * 60)
    log(f" GalCoplit 打包（{title}）")
    log("=" * 60)
    log()
    prepare_venv()
    if dry_run:
        log("[dry-run] 跳过依赖安装与打包，仅做环境自检 ...")
        check_clean_env()
        ensure_icon()
        log("[dry-run] 环境准备就绪。")
        return
    install_deps()
    check_clean_env()
    if install_mode:
        build_install()
    else:
        build_onefile()
    log()
    log("完成。")


def main():
    if "--paddle-worker" in sys.argv:
        build_paddle_worker()
        return
    install_mode = "--install" in sys.argv
    build_main(install_mode)


if __name__ == "__main__":
    if os.name != "nt":
        fail("本脚本用于 Windows 打包。")
    main()
