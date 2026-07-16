"""元数据提取模块"""

import csv
import os
import re
import subprocess
from datetime import datetime

from config import METADATA_CSV
from core.adb_bin import ADB


# CSV 表头
FIELDS = [
    "file_name", "package_name", "app_name", "version_name",
    "activity_name", "page_fingerprint", "segment_index", "depth",
    "device_model", "screen_resolution", "android_version", "capture_time",
]


def init_csv():
    """初始化 CSV 文件（写入表头）"""
    if not os.path.exists(METADATA_CSV):
        with open(METADATA_CSV, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()


def append_record(record: dict):
    """追加一条记录到 CSV"""
    with open(METADATA_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writerow(record)


def get_current_activity(serial: str) -> str:
    """获取当前前台 Activity"""
    result = subprocess.run(
        [ADB, "-s", serial, "shell", "dumpsys", "activity", "activities"],
        capture_output=True, text=True, timeout=10
    )
    for line in result.stdout.split("\n"):
        if "mResumedActivity" in line or "topResumedActivity" in line:
            match = re.search(r"([\w.]+/[\w.]+)", line)
            if match:
                return match.group(1)
    return "Unknown"


def get_device_info(serial: str) -> dict:
    """获取设备基本信息（启动时调用一次）"""
    def _prop(key):
        r = subprocess.run(
            [ADB, "-s", serial, "shell", "getprop", key],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()

    def _shell(cmd):
        r = subprocess.run(
            [ADB, "-s", serial, "shell"] + cmd.split(),
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()

    resolution = ""
    wm_output = _shell("wm size")
    match = re.search(r"(\d+x\d+)", wm_output)
    if match:
        resolution = match.group(1)

    return {
        "device_model": _prop("ro.product.model"),
        "android_version": _prop("ro.build.version.release"),
        "screen_resolution": resolution,
    }


def get_app_info(serial: str, package_name: str) -> dict:
    """获取 App 信息"""
    result = subprocess.run(
        [ADB, "-s", serial, "shell", "dumpsys", "package", package_name],
        capture_output=True, text=True, timeout=10
    )
    info = {"package_name": package_name, "app_name": package_name, "version_name": ""}

    for line in result.stdout.split("\n"):
        line = line.strip()
        if "versionName=" in line:
            info["version_name"] = line.split("versionName=")[-1].strip()
            break

    return info


def build_record(
    screenshot_path: str,
    activity: str,
    fingerprint: str,
    depth: int,
    device_info: dict,
    app_info: dict,
    segment_index: int = 0,
) -> dict:
    """组装一条完整的元数据记录"""
    return {
        "file_name": os.path.basename(screenshot_path),
        "package_name": app_info["package_name"],
        "app_name": app_info["app_name"],
        "version_name": app_info["version_name"],
        "activity_name": activity,
        "page_fingerprint": fingerprint,
        "segment_index": segment_index,
        "depth": depth,
        "device_model": device_info["device_model"],
        "screen_resolution": device_info["screen_resolution"],
        "android_version": device_info["android_version"],
        "capture_time": datetime.now().isoformat(),
    }
