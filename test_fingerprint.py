"""
dHash指纹算法 - 真机测试

在真实手机App上验证指纹算法的效果:
  1. 同一页面多次dump → 指纹应相同
  2. 同模板不同内容（如不同视频详情页）→ 指纹应相同
  3. 不同页面 → 指纹应不同
  4. 手动滚动页面后 → 指纹应相同（列表子项被忽略）

运行方式 (需USB连接手机, B站已打开):
    python test_fingerprint.py

运行前确保:
    - 手机USB连接电脑
    - B站App已打开在首页
    - 已安装依赖: pip install -r requirements.txt
"""

import sys
import os
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.device import connect
from core.fingerprint import (
    generate, find_similar, add_fingerprint,
    is_same_page, _hamming_distance, _draw_skeleton,
    _extract_components, CANVAS_W, CANVAS_H,
)
from core.adb_bin import ADB
from core import metadata

PACKAGE_NAME = "tv.danmaku.bili"


def main():
    print()
    print("=" * 60)
    print("  dHash指纹算法 - 真机测试")
    print("=" * 60)
    print()

    # 连接设备
    print("[1] 连接设备...")
    dev, poco, serial = connect()
    print()

    # 测试1: 同一页面稳定性
    test_stability(poco, serial)

    # 测试2: 滚动后指纹不变
    test_scroll_stability(poco, serial)

    # 测试3: 不同页面指纹不同
    test_different_pages(poco, serial)

    # 测试4: 同模板不同内容
    test_same_template(poco, serial)

    print()
    print("=" * 60)
    print("  全部测试完成!")
    print("=" * 60)


def test_stability(poco, serial):
    """测试1: 同一页面连续dump 5次，指纹应完全相同"""
    print("-" * 60)
    print("[测试1] 同一页面稳定性 (连续5次dump)")
    print("-" * 60)

    activity = metadata.get_current_activity(serial)
    print(f"  当前页面: {activity}")

    fingerprints = []
    for i in range(5):
        hierarchy = poco.agent.hierarchy.dump()
        fp = generate(hierarchy, activity)
        dhash = fp.split("|")[1] if "|" in fp else fp
        fingerprints.append(dhash)
        print(f"  第{i+1}次: ...{dhash[-20:]}")
        time.sleep(0.3)

    # 检查两两之间的汉明距离
    all_same = True
    for i in range(1, len(fingerprints)):
        dist = _hamming_distance(fingerprints[0], fingerprints[i])
        if dist > 0:
            all_same = False
            print(f"  [!] 第1次 vs 第{i+1}次 汉明距离={dist}")

    if all_same:
        print(f"  结果: 5次指纹完全相同 ✓")
    else:
        print(f"  结果: 存在差异（但只要汉明距离<5就不影响去重）")
    print()


def test_scroll_stability(poco, serial):
    """测试2: 滚动页面后指纹应保持不变（列表子项被忽略）"""
    print("-" * 60)
    print("[测试2] 滚动后指纹稳定性")
    print("-" * 60)

    activity = metadata.get_current_activity(serial)
    print(f"  当前页面: {activity}")

    # 滚动前
    hierarchy_before = poco.agent.hierarchy.dump()
    fp_before = generate(hierarchy_before, activity)
    hash_before = fp_before.split("|")[1] if "|" in fp_before else fp_before
    print(f"  滚动前: ...{hash_before[-20:]}")

    # 向下滚动
    print(f"  执行向下滚动...")
    _swipe_down(serial)
    time.sleep(1)

    # 滚动后
    hierarchy_after = poco.agent.hierarchy.dump()
    fp_after = generate(hierarchy_after, activity)
    hash_after = fp_after.split("|")[1] if "|" in fp_after else fp_after
    print(f"  滚动后: ...{hash_after[-20:]}")

    distance = _hamming_distance(hash_before, hash_after)
    same = distance < 5
    print(f"  汉明距离: {distance}")
    print(f"  结果: {'指纹稳定 ✓' if same else '指纹变化 (距离=' + str(distance) + ')'}")

    # 滚回去
    _swipe_up(serial)
    time.sleep(0.5)
    print()


def test_different_pages(poco, serial):
    """测试3: 跳转到不同页面，指纹应不同"""
    print("-" * 60)
    print("[测试3] 不同页面指纹差异")
    print("-" * 60)

    # 当前页面（应该是首页）
    activity1 = metadata.get_current_activity(serial)
    hierarchy1 = poco.agent.hierarchy.dump()
    fp1 = generate(hierarchy1, activity1)
    hash1 = fp1.split("|")[1] if "|" in fp1 else fp1
    print(f"  页面A ({activity1.split('/')[-1]}): ...{hash1[-20:]}")

    # 打开搜索页
    print(f"  正在点击搜索...")
    _open_search(poco)
    time.sleep(1.5)

    activity2 = metadata.get_current_activity(serial)
    hierarchy2 = poco.agent.hierarchy.dump()
    fp2 = generate(hierarchy2, activity2)
    hash2 = fp2.split("|")[1] if "|" in fp2 else fp2
    print(f"  页面B ({activity2.split('/')[-1]}): ...{hash2[-20:]}")

    if activity1 != activity2:
        distance = _hamming_distance(hash1, hash2)
        same = is_same_page(fp1, fp2)
        print(f"  Activity不同: {activity1.split('/')[-1]} vs {activity2.split('/')[-1]}")
        print(f"  汉明距离: {distance}")
        print(f"  结果: {'不同页面 ✓' if not same else '误判为相同 ✗'}")
    else:
        print(f"  [!] 没跳转成功，Activity仍然是 {activity1}")

    # 返回
    _go_back(serial)
    time.sleep(1)
    print()


def test_same_template(poco, serial):
    """测试4: 同模板不同内容（点击两个不同视频，详情页指纹应相似）"""
    print("-" * 60)
    print("[测试4] 同模板不同内容 (两个视频详情页)")
    print("-" * 60)
    print("  说明: 从首页点击两个不同视频，比较详情页指纹")
    print()

    # 确保在首页
    activity = metadata.get_current_activity(serial)
    if "MainActivityV2" not in (activity or ""):
        print("  [!] 当前不在首页，请手动切到B站首页后重新运行")
        return

    # 点击第1个视频
    print("  点击第1个视频...")
    video_nodes = _find_video_items(poco)
    if len(video_nodes) < 2:
        print("  [!] 找不到足够的视频条目，请确保在首页推荐流")
        return

    video_nodes[0].click()
    time.sleep(2)

    activity_v1 = metadata.get_current_activity(serial)
    hierarchy_v1 = poco.agent.hierarchy.dump()
    fp_v1 = generate(hierarchy_v1, activity_v1)
    hash_v1 = fp_v1.split("|")[1] if "|" in fp_v1 else fp_v1
    print(f"  视频A ({activity_v1.split('/')[-1]}): ...{hash_v1[-20:]}")

    # 可视化骨架
    _print_skeleton_preview(hierarchy_v1, "视频A")

    # 返回首页
    _go_back(serial)
    time.sleep(1.5)

    # 点击第2个视频
    print("  点击第2个视频...")
    video_nodes = _find_video_items(poco)
    if len(video_nodes) < 2:
        print("  [!] 返回后找不到视频条目")
        return

    video_nodes[1].click()
    time.sleep(2)

    activity_v2 = metadata.get_current_activity(serial)
    hierarchy_v2 = poco.agent.hierarchy.dump()
    fp_v2 = generate(hierarchy_v2, activity_v2)
    hash_v2 = fp_v2.split("|")[1] if "|" in fp_v2 else fp_v2
    print(f"  视频B ({activity_v2.split('/')[-1]}): ...{hash_v2[-20:]}")

    # 可视化骨架
    _print_skeleton_preview(hierarchy_v2, "视频B")

    # 比较
    if activity_v1 == activity_v2:
        distance = _hamming_distance(hash_v1, hash_v2)
        same = is_same_page(fp_v1, fp_v2)
        print(f"  同一Activity: {activity_v1.split('/')[-1]}")
        print(f"  汉明距离: {distance}")
        print(f"  结果: {'同模板 ✓ (不会重复截图)' if same else '判为不同模板 (距离=' + str(distance) + ')'}")
    else:
        print(f"  [!] 两次进入了不同Activity:")
        print(f"      视频A: {activity_v1}")
        print(f"      视频B: {activity_v2}")

    # 返回首页
    _go_back(serial)
    time.sleep(1)
    print()


# ------------------------------------------------------------------
# 辅助方法
# ------------------------------------------------------------------

def _swipe_down(serial):
    """向下滚动"""
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "swipe", "540", "1600", "540", "800", "400"],
        capture_output=True, timeout=5
    )


def _swipe_up(serial):
    """向上滚动"""
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "swipe", "540", "800", "540", "1600", "400"],
        capture_output=True, timeout=5
    )


def _go_back(serial):
    """按返回键"""
    subprocess.run(
        [ADB, "-s", serial, "shell", "input", "keyevent", "4"],
        capture_output=True, timeout=5
    )


def _open_search(poco):
    """尝试点击搜索入口"""
    try:
        # B站搜索栏通常有这些特征
        search_patterns = ["search", "搜索"]
        nodes = poco(touchable=True)
        for node in nodes:
            try:
                text = node.attr("text") or ""
                name = node.attr("name") or ""
                desc = node.attr("desc") or ""
                pos = node.attr("pos")
                if not pos:
                    continue
                # 在顶部区域的搜索相关节点
                if pos[1] < 0.1:
                    content = text + name + desc
                    if any(kw in content.lower() for kw in search_patterns):
                        node.click()
                        return
            except Exception:
                continue
        # 找不到搜索栏，点击顶部中间区域
        poco.click([0.5, 0.04])
    except Exception:
        pass


def _find_video_items(poco) -> list:
    """找到首页视频列表项（可点击且在主内容区域的节点）"""
    items = []
    try:
        nodes = poco(touchable=True)
        for node in nodes:
            try:
                pos = node.attr("pos")
                if not pos:
                    continue
                x, y = pos
                # 在主内容区域（避开顶栏和底栏）
                if 0.15 < y < 0.85 and 0.05 < x < 0.95:
                    node_type = node.attr("type") or ""
                    # 排除小按钮，找较大的可点击区域
                    size = node.attr("size")
                    if size and size[0] > 0.3 and size[1] > 0.05:
                        items.append(node)
                        if len(items) >= 3:
                            break
            except Exception:
                continue
    except Exception:
        pass
    return items


def _print_skeleton_preview(hierarchy, label):
    """打印骨架图的ASCII预览"""
    components = []
    _extract_components(hierarchy, components, in_list=False)
    canvas = _draw_skeleton(components)

    print(f"  [{label}骨架] 组件数={len(components)}")
    # 缩放到8x16做紧凑预览
    for y in range(0, CANVAS_H, 8):
        row = "    "
        for x in range(0, CANVAS_W, 8):
            val = canvas[y, x]
            if val < 40:
                row += "██"
            elif val < 70:
                row += "▓▓"
            elif val < 100:
                row += "░░"
            elif val < 140:
                row += "··"
            elif val < 180:
                row += "──"
            else:
                row += "□□"
        print(row)
    print()


if __name__ == "__main__":
    main()
