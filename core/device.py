"""设备连接模块 — 支持 USB 和 WiFi ADB"""

import subprocess
import sys
import time

from airtest.core.api import connect_device
from poco.drivers.android.uiautomation import AndroidUiautomationPoco

from config import CONNECTION_MODE, WIFI_ADB_HOST, WIFI_ADB_PORT, EMULATOR_HOST, EMULATOR_PORT
from core.adb_bin import ADB


def get_connected_serial() -> str:
    """获取已连接的设备序列号（USB 或 WiFi）"""
    result = subprocess.run(
        [ADB, "devices"],
        capture_output=True, text=True, timeout=10
    )
    for line in result.stdout.strip().split("\n")[1:]:
        if "\tdevice" in line:
            return line.split("\t")[0]
    return ""


def _connect_wifi() -> str:
    """
    通过 WiFi ADB 连接设备（Mac 连手机热点场景）。

    前置准备（只需做一次，手机重启前一直有效）：

    方式A — 借一次USB线（推荐，最简单）:
        1. 借一根USB线连接手机和电脑
        2. 终端执行: adb tcpip 5555
        3. 拔掉USB线
        4. Mac连接手机热点，运行本脚本

    方式B — 纯手机端操作（无需USB线）:
        1. 手机安装 Termux (从F-Droid下载)
        2. Termux中执行: su -c "setprop service.adb.tcp.port 5555 && stop adbd && start adbd"
           (需要root权限)
        3. 如果没有root: 安装 "ADB WiFi" 类App（部分免root机型可用）
        4. Mac连接手机热点，运行本脚本

    方式C — 通过 adb pair 配对 (Android 11+, 开发者选项中有"配对码配对"):
        1. 开发者选项 → 无线调试 → 使用配对码配对设备
        2. 终端执行: adb pair <ip>:<pair_port> <配对码>
        3. 然后执行: adb connect <ip>:<connect_port>

    返回设备序列号 (host:port)
    """
    serial = f"{WIFI_ADB_HOST}:{WIFI_ADB_PORT}"

    # 检查是否已连接
    existing = get_connected_serial()
    if existing == serial:
        print(f"[INFO] WiFi ADB 已连接: {serial}")
        return serial

    print(f"[INFO] 正在通过 WiFi ADB 连接 {serial} ...")

    result = subprocess.run(
        [ADB, "connect", serial],
        capture_output=True, text=True, timeout=10
    )
    output = result.stdout.strip()
    print(f"[INFO] adb connect 输出: {output}")

    # 等待连接稳定
    time.sleep(2)

    # 验证连接
    check = get_connected_serial()
    if not check:
        print("[ERROR] WiFi ADB 连接失败！")
        print()
        print(f"  当前配置: {WIFI_ADB_HOST}:{WIFI_ADB_PORT}")
        print(f"  请确认 Mac 已连接到手机热点")
        print()
        print("  ===== 首次配对方法（只需做一次）=====")
        print()
        print("  方法1: 借一次USB线（最简单）")
        print("    → USB连手机，终端执行: adb tcpip 5555")
        print("    → 拔掉USB，Mac连手机热点，重新运行")
        print()
        print("  方法2: 手机端开启（需Termux+Root）")
        print("    → Termux执行: su -c 'setprop service.adb.tcp.port 5555 && stop adbd && start adbd'")
        print()
        print("  方法3: Android 11+ 配对码")
        print("    → 开发者选项 → 无线调试 → 使用配对码配对设备")
        print("    → 终端: adb pair <ip>:<port> <配对码>")
        print()
        print("  配对成功后再次运行本脚本即可")
        sys.exit(1)

    print(f"[INFO] WiFi ADB 连接成功: {check}")
    return check


def _connect_usb() -> str:
    """通过 USB 连接设备"""
    serial = get_connected_serial()
    if not serial:
        print("[ERROR] 未检测到 USB 设备，请确认：")
        print("  1. 手机已通过 USB 连接")
        print("  2. 已开启 USB 调试")
        print("  3. 已授权此电脑调试")
        sys.exit(1)
    print(f"[INFO] USB 设备: {serial}")
    return serial


def _connect_emulator() -> str:
    """连接本地 Android 模拟器（MuMu 等）"""
    serial = f"{EMULATOR_HOST}:{EMULATOR_PORT}"

    existing = get_connected_serial()
    if existing == serial:
        print(f"[INFO] 模拟器已连接: {serial}")
        return serial

    print(f"[INFO] 正在连接模拟器 {serial} ...")

    result = subprocess.run(
        [ADB, "connect", serial],
        capture_output=True, text=True, timeout=10
    )
    output = result.stdout.strip()
    print(f"[INFO] adb connect 输出: {output}")

    time.sleep(2)

    check = get_connected_serial()
    if not check:
        print("[ERROR] 模拟器连接失败！请确认：")
        print(f"  1. 模拟器已启动")
        print(f"  2. ADB 端口正确（当前: {EMULATOR_PORT}）")
        print()
        print("  常见模拟器端口:")
        print("    MuMu 12:  16384, 16416, 16448 (多开)")
        print("    MuMu 旧版: 7555")
        print("    雷电:      5555, 5557, 5559 (多开)")
        print("    夜神:      62001, 62025, 62026 (多开)")
        sys.exit(1)

    print(f"[INFO] 模拟器连接成功: {check}")
    return check


def connect() -> tuple:
    """
    连接设备，返回 (device, poco, serial) 元组。
    根据 config.CONNECTION_MODE 选择连接方式。
    """
    if CONNECTION_MODE == "emulator":
        serial = _connect_emulator()
    elif CONNECTION_MODE == "wifi":
        serial = _connect_wifi()
    else:
        serial = _connect_usb()

    dev = connect_device(f"android:///{serial}")
    poco = AndroidUiautomationPoco(dev, use_airtest_input=True, screenshot_each_action=False)

    print("[INFO] Poco 初始化完成")
    return dev, poco, serial
