"""统一定位 adb 可执行文件路径，供所有模块复用（不依赖系统 PATH）。"""

import os
import shutil
import sys


def _find_adb() -> str:
    """
    定位 adb 可执行文件路径，不依赖系统 PATH。
    查找顺序: PATH -> ANDROID_HOME/ANDROID_SDK_ROOT -> 常见安装位置。
    """
    # 1. PATH 中能否直接找到
    found = shutil.which("adb")
    if found:
        return found

    # 2. 通过 ANDROID_HOME / ANDROID_SDK_ROOT 环境变量
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk = os.environ.get(env_var)
        if sdk:
            candidate = os.path.join(sdk, "platform-tools", "adb.exe")
            if os.path.isfile(candidate):
                return candidate

    # 3. 常见安装位置
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, "AppData", "Local", "Android", "Sdk", "platform-tools", "adb.exe"),
        r"C:\Android\sdk\platform-tools\adb.exe",
        r"D:\Android\sdk\platform-tools\adb.exe",
        r"C:\platform-tools\adb.exe",
        r"D:\platform-tools\adb.exe",
        "/usr/bin/adb",            # macOS / Linux
        "/opt/homebrew/bin/adb",   # macOS Homebrew
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate

    print("[ERROR] 未找到 adb，请确认已安装 Android Platform-Tools")
    print("  可下载: https://developer.android.com/tools/releases/platform-tools")
    sys.exit(1)


ADB = _find_adb()
