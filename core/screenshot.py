"""截图与质量检查模块"""

import os
import subprocess
import time

import cv2
import numpy as np

from config import SCREENSHOT_DIR
from core.adb_bin import ADB


def capture(serial: str, activity: str, fingerprint: str, segment_index: int = 0) -> str | None:
    """
    截取当前屏幕, 裁掉系统状态栏和导航栏后保存.
    返回截图文件路径, 质量不合格则返回 None.
    """
    timestamp = int(time.time() * 1000)
    filename = f"{_safe_name(activity)}_{fingerprint[:8]}_s{segment_index}_{timestamp}.png"
    filepath = os.path.join(SCREENSHOT_DIR, filename)

    # ADB截图
    remote_path = "/sdcard/_crawler_tmp.png"
    subprocess.run(
        [ADB, "-s", serial, "shell", "screencap", "-p", remote_path],
        capture_output=True, timeout=10
    )
    subprocess.run(
        [ADB, "-s", serial, "pull", remote_path, filepath],
        capture_output=True, timeout=10
    )
    subprocess.run(
        [ADB, "-s", serial, "shell", "rm", remote_path],
        capture_output=True, timeout=5
    )

    if not os.path.exists(filepath):
        return None

    # 裁掉系统状态栏和导航栏
    if not _crop_system_bars(filepath, serial):
        os.remove(filepath)
        return None

    if not _quality_check(filepath):
        os.remove(filepath)
        return None

    return filepath


def _crop_system_bars(filepath: str, serial: str) -> bool:
    """
    裁掉顶部系统状态栏和底部系统导航栏.
    保留App自身的标题栏和底部Tab.

    通过ADB获取状态栏和导航栏的精确像素高度, 按实际设备裁切.
    """
    img = cv2.imread(filepath)
    if img is None:
        return False

    h, w = img.shape[:2]

    # 获取系统状态栏高度(顶部: 时间/信号/电量)
    status_bar_height = _get_status_bar_height(serial, h)

    # 获取系统导航栏高度(底部: 三键导航/手势白条)
    nav_bar_height = _get_nav_bar_height(serial, h)

    # 裁切
    top = status_bar_height
    bottom = h - nav_bar_height

    if top >= bottom or (bottom - top) < 100:
        return False

    cropped = img[top:bottom, 0:w]
    cv2.imwrite(filepath, cropped)
    return True


def _get_status_bar_height(serial: str, screen_height: int) -> int:
    """通过ADB获取状态栏像素高度"""
    try:
        result = subprocess.run(
            [ADB, "-s", serial, "shell",
             "dumpsys", "window", "StatusBar"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            # 查找 "mFrame=[0,0][1080,XXX]" 格式
            if "mFrame=" in line and "StatusBar" in line:
                # 提取高度
                import re
                match = re.search(r'mFrame=\[\d+,\d+\]\[\d+,(\d+)\]', line)
                if match:
                    return int(match.group(1))
    except Exception:
        pass

    # 备选方案: 通过资源获取
    try:
        result = subprocess.run(
            [ADB, "-s", serial, "shell",
             "cmd", "window", "size"],
            capture_output=True, text=True, timeout=5
        )
    except Exception:
        pass

    # 默认值: 根据屏幕高度估算
    # 常见设备状态栏高度约为屏幕高度的2.5%-4%
    if screen_height >= 2400:
        return 80  # 高分辨率设备
    elif screen_height >= 1920:
        return 66  # 1080p设备
    else:
        return 50  # 低分辨率


def _get_nav_bar_height(serial: str, screen_height: int) -> int:
    """通过ADB获取导航栏像素高度, 如果隐藏则返回0"""
    try:
        # 检查导航栏是否显示
        result = subprocess.run(
            [ADB, "-s", serial, "shell",
             "dumpsys", "window", "NavigationBar"],
            capture_output=True, text=True, timeout=5
        )
        output = result.stdout

        # 如果导航栏被隐藏(全面屏手势)
        if "isVisibleLw=false" in output or "mHasSurface=false" in output:
            # 手势白条通常很小, 约20-30px
            if screen_height >= 2400:
                return 30
            return 20

        # 导航栏可见, 查找高度
        for line in output.split("\n"):
            if "mFrame=" in line:
                import re
                # 格式: mFrame=[0,YYYY][1080,2340] → 高度 = screen_height - YYYY
                match = re.search(r'mFrame=\[\d+,(\d+)\]\[\d+,\d+\]', line)
                if match:
                    nav_top = int(match.group(1))
                    if nav_top > screen_height * 0.8:
                        return screen_height - nav_top
    except Exception:
        pass

    # 默认值
    if screen_height >= 2400:
        return 126  # 高分辨率设备三键导航
    elif screen_height >= 1920:
        return 96   # 1080p设备
    else:
        return 72


def _quality_check(filepath: str) -> bool:
    """基本质量检查: 非纯色、分辨率达标"""
    img = cv2.imread(filepath)
    if img is None:
        return False

    h, w = img.shape[:2]
    if w < 720 or h < 1000:
        return False

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.std() < 10:
        return False

    return True


def _safe_name(name: str) -> str:
    """将Activity名转为安全的文件名"""
    return name.replace("/", "_").replace(".", "_").replace(" ", "")[:60]
