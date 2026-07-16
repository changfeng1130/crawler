"""个人隐私页判定

用于在遍历中识别并跳过个人详情页（用户空间/我的/编辑资料等），
确保不产出含个人隐私信息的截图。
"""

from config import (
    PRIVACY_ACTIVITY_KEYWORDS,
    PRIVACY_ID_KEYWORDS,
    PRIVACY_TEXT_KEYWORDS,
)


def is_personal_page(hierarchy: dict, activity: str) -> bool:
    """
    判断当前页是否为个人详情页。
    依据: activity 名 / 节点 resource-id 后缀 / 节点文本 命中隐私关键词。
    """
    # 1. activity 命中
    if activity and any(kw in activity for kw in PRIVACY_ACTIVITY_KEYWORDS):
        return True

    # 2. 遍历可见节点，匹配 id 后缀或文本
    found = False

    def _walk(node):
        nonlocal found
        if found:
            return
        payload = node.get("payload", {})
        if not payload.get("visible", True):
            return

        # resource-id 后缀
        rid = payload.get("name", "") or ""
        id_suffix = rid.split("/")[-1] if rid else ""
        if id_suffix and any(kw in id_suffix.lower() for kw in PRIVACY_ID_KEYWORDS):
            found = True
            return

        # 文本
        text = payload.get("text", "") or ""
        if text and any(kw in text for kw in PRIVACY_TEXT_KEYWORDS):
            found = True
            return

        for child in node.get("children", []):
            _walk(child)
            if found:
                return

    _walk(hierarchy)
    return found
