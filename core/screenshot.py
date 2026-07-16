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
    截取当前屏幕并保存。
    segment_index: 滚动分段序号，0 为主图，1..N 为列表页滚动后的分段图。
    返回截图文件路径，质量不合格则返回 None。
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

    if not _quality_check(filepath):
        os.remove(filepath)
        return None

    return filepath


def _quality_check(filepath: str) -> bool:
    """基本质量检查：非纯色、分辨率达标"""
    img = cv2.imread(filepath)
    if img is None:
        return False

    h, w = img.shape[:2]
    if w < 720 or h < 1280:
        return False

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.std() < 10:
        return False

    return True


def _safe_name(name: str) -> str:
    """将Activity名转为安全的文件名"""
    return name.replace("/", "_").replace(".", "_").replace(" ", "")[:60]
