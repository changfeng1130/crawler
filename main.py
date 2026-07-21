"""
App 自动遍历截图
============================

使用方式:
    1. USB 连接安卓真机（已安装目标App）
    2. pip install -r requirements.txt
    3. python main.py

产出:
    output/screenshots/  — 截图文件
    output/metadata.csv  — 元数据表
"""

import sys
import os
import subprocess
import time

# 将项目根目录加入 path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import PACKAGE_NAME, SCREENSHOT_DIR, METADATA_CSV, CONNECTION_MODE
from core.adb_bin import ADB
from core.device import connect
from core.metadata import init_csv, get_device_info, get_app_info
from core.traversal import TraversalEngine
from core.popup_handler import handle_onboarding


def launch_app(serial: str):
    """启动目标App"""
    subprocess.run(
        [ADB, "-s", serial, "shell", "am", "force-stop", PACKAGE_NAME],
        capture_output=True, timeout=5
    )
    time.sleep(1)

    subprocess.run(
        [ADB, "-s", serial, "shell", "monkey", "-p", PACKAGE_NAME,
         "-c", "android.intent.category.LAUNCHER", "1"],
        capture_output=True, timeout=5
    )
    print(f"[INFO] 正在启动 {PACKAGE_NAME}...")
    time.sleep(4)


def main():
    resume = "--resume" in sys.argv or "-resume" in sys.argv

    print("=" * 50)
    if resume:
        print(f"  {PACKAGE_NAME} 自动遍历截图 (继续模式)")
    else:
        print(f"  {PACKAGE_NAME} 自动遍历截图")
    print("=" * 50)
    print()

    # 1. 连接设备
    print(f"[INFO] 连接方式: {CONNECTION_MODE}")
    dev, poco, serial = connect()

    # 2. 获取设备和App信息
    device_info = get_device_info(serial)
    app_info = get_app_info(serial, PACKAGE_NAME)
    print(f"[INFO] 设备: {device_info['device_model']} (Android {device_info['android_version']})")
    print(f"[INFO] App: {PACKAGE_NAME} v{app_info['version_name']}")
    print(f"[INFO] 截图输出: {SCREENSHOT_DIR}")
    if resume:
        print(f"[INFO] 模式: 继续上次遍历")
    print()

    # 3. 初始化 CSV
    init_csv()

    # 4. 启动App
    launch_app(serial)

    # 5. 处理冷启动引导（闪屏广告 / 隐私协议 / 青少年模式等）
    handle_onboarding(poco)
    time.sleep(1)

    # 6. 开始遍历
    engine = TraversalEngine(poco, serial, device_info, app_info, resume=resume)
    total = engine.run()

    # 7. 输出结果
    print()
    print("=" * 50)
    print(f"  完成! 共截取 {total} 张不同UI页面")
    print(f"  截图目录: {SCREENSHOT_DIR}")
    print(f"  元数据: {METADATA_CSV}")
    print("=" * 50)


if __name__ == "__main__":
    main()
