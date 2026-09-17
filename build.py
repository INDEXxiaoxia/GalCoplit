# -*- coding: utf-8 -*-
"""GalCoplit 打包脚本：在干净的 build_venv 里用 PyInstaller 生成 onefile exe。

逻辑放在 Python 而不是 build.bat 里，是因为 cmd.exe 在 chcp 65001 下解析含中文的
长命令与嵌套块时会出现按字节定位的错位，导致命令被拆碎执行。build.bat 只做一层
纯 ASCII 的启动器。

用法：
    python build.py            完整打包
    python build.py --dry-run  只做环境准备与自检，不装依赖、不打包
"""
import os
import shutil
import subprocess
import sys

APP_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_DIR = os.path.join(APP_DIR, "build_venv")
VENV_PY = os.path.join(VENV_DIR, "Scripts", "python.exe")
SPEC = "GalCoplit.spec"
EXE_REL = os.path.join("dist", "GalCoplit.exe")
SIZE_LIMIT_MB = 150

# 这些重型库一旦出现在构建环境里，会被 PyInstaller 连带分析：既大幅拖慢打包，
# 也可能因库自身的 DLL 不匹配而弹窗报错（例如 torchvision 与 torch 版本错配
# 触发 0xc0000139 STATUS_ENTRYPOINT_NOT_FOUND）。
HEAVY_MODULES = ("torch", "torchvision", "transformers", "paddle", "sklearn", "matplotlib")


def log(msg=""):
    print(msg, flush=True)


def fail(msg):
    log(f"[失败] {msg}")
    sys.exit(1)


def run(cmd, **kwargs):
    return subprocess.run(cmd, cwd=APP_DIR, **kwargs)


def venv_python_works():
    """venv 里的解释器能否正常启动（拷贝来的环境可能 pyvenv.cfg 已失效）。"""
    if not os.path.isfile(VENV_PY):
        return False
    try:
        return run([VENV_PY, "-c", "import sys"], capture_output=True).returncode == 0
    except OSError:
        return False


def prepare_venv():
    log("[1/4] 准备干净的构建虚拟环境 build_venv ...")
    # 虚拟环境不可搬迁：若 build_venv 是从别的目录拷来的，activate.bat 里写死的
    # VIRTUAL_ENV / PATH 会指向旧目录，激活后会悄悄用回全局环境打包。
    # 所以这里始终用绝对路径直接调用 venv 的解释器，并在其不可用时重建。
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


def build_exe():
    log(f"[3/4] 按 {SPEC} 打包 (onefile / windowed / 体积优化) ...")
    if run([VENV_PY, "-m", "PyInstaller", "--clean", "--noconfirm",
            SPEC]).returncode != 0:
        fail("PyInstaller 打包失败，请向上查看报错。")


def check_size():
    log("[4/4] 检查产物大小 ...")
    exe = os.path.join(APP_DIR, EXE_REL)
    if not os.path.isfile(exe):
        fail(f"未生成 {EXE_REL}，请向上查看报错。")
    size = os.path.getsize(exe)
    mb = size / 1048576
    log(f"产物: {EXE_REL}  {mb:.2f} MB ({size} 字节)")
    if mb >= SIZE_LIMIT_MB:
        log(f"[警告] 体积超过 {SIZE_LIMIT_MB} MB，"
            "请检查是否在脏环境里构建、或依赖版本已变化。")
    else:
        log(f"[OK] 体积在 {SIZE_LIMIT_MB} MB 以内。")


def main():
    dry_run = "--dry-run" in sys.argv
    log("=" * 60)
    log(f" GalCoplit 打包脚本  (目标: {EXE_REL} 小于 {SIZE_LIMIT_MB} MB)")
    log("=" * 60)
    log()

    prepare_venv()
    if dry_run:
        log("[dry-run] 跳过依赖安装与打包，仅做环境自检 ...")
        check_clean_env()
        log("[dry-run] 环境准备就绪。")
        return

    install_deps()
    check_clean_env()
    build_exe()
    check_size()

    log()
    log("打包完成。请把 config.json 与 GalCoplit.exe 放在同一目录下；")
    log("首次运行会自动在同目录生成 records.db。")


if __name__ == "__main__":
    if os.name != "nt":
        fail("本脚本用于 Windows 打包。")
    main()
