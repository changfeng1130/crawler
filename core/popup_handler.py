"""弹窗/引导页处理

包含三类能力:
  - dismiss_popups: 按关键词关闭弹窗（隐私协议/青少年模式/广告等）
  - has_blocking_popup: 检测是否有遮挡面积超过阈值的弹窗仍未关闭（用于跳过截图）
  - handle_onboarding: 冷启动引导处理（等闪屏 → 关闭引导弹窗）
"""

import time

from config import POPUP_CLOSE_KEYWORDS, POPUP_OCCLUSION_RATIO


def dismiss_popups(poco, max_attempts: int = 5) -> int:
    """
    检测并关闭弹窗。
    返回关闭的弹窗数量。
    """
    dismissed = 0

    for _ in range(max_attempts):
        closed = _try_close_one(poco)
        if not closed:
            break
        dismissed += 1
        time.sleep(0.5)

    return dismissed


def has_blocking_popup(poco, threshold: float = POPUP_OCCLUSION_RATIO) -> bool:
    """
    检测是否存在遮挡面积 >= threshold 的未关闭弹窗。
    判定: 顶层有 ≥2 个可见节点，且最末（最上层）那个的面积占比 >= threshold。
    （正常页面顶层通常只有 1 个主内容容器；多出的全屏顶层节点一般是弹窗/遮罩）
    """
    try:
        hierarchy = poco.agent.hierarchy.dump()
    except Exception:
        return False

    top_children = hierarchy.get("children", []) if hierarchy else []
    visible_tops = []
    for node in top_children:
        payload = node.get("payload", {})
        if not payload.get("visible", True):
            continue
        size = payload.get("size") or [0, 0]
        area = (size[0] or 0) * (size[1] or 0)
        visible_tops.append(area)

    if len(visible_tops) < 2:
        return False

    # 最末一个即渲染层级最上层
    return visible_tops[-1] >= threshold


def handle_onboarding(poco, splash_wait: float = 5.0) -> int:
    """
    冷启动引导处理: 等待闪屏广告 → 关闭隐私协议/青少年模式/广告等引导弹窗。
    返回关闭的弹窗数量。
    """
    # 等闪屏广告播放或出现"跳过"
    time.sleep(splash_wait)
    dismissed = dismiss_popups(poco, max_attempts=5)
    if dismissed:
        print(f"[INFO] 引导处理: 关闭了 {dismissed} 个弹窗")
    return dismissed


def _try_close_one(poco) -> bool:
    """尝试关闭一个弹窗，返回是否成功"""
    try:
        touchable_nodes = poco(touchable=True)
    except Exception:
        return False

    for node in touchable_nodes:
        try:
            text = node.attr("text") or ""
            desc = node.attr("desc") or ""
            content = text + desc

            for keyword in POPUP_CLOSE_KEYWORDS:
                if keyword in content:
                    node.click()
                    time.sleep(0.5)
                    return True
        except Exception:
            continue

    return False
