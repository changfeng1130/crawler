"""个人隐私页判定

用于在遍历中识别并跳过个人详情页（用户空间/我的/编辑资料等），
确保不产出含个人隐私信息的截图。
"""

from config import (
    PRIVACY_ACTIVITY_KEYWORDS,
    PRIVACY_ID_KEYWORDS,
    PRIVACY_TEXT_KEYWORDS,
    PRIVACY_TEXT_MIN_HITS,
)


def is_personal_page(hierarchy: dict, activity: str) -> bool:
    """
    判断当前页是否为个人详情页。
    依据: activity 名 / 节点 resource-id 后缀 / 节点文本命中隐私关键词(需达到阈值)。
    """
    # 1. activity 命中
    if activity and any(kw in activity for kw in PRIVACY_ACTIVITY_KEYWORDS):
        return True

    # 2. 遍历可见节点，匹配 id 后缀或文本
    id_hit = False
    text_hits = set()

    def _walk(node):
        nonlocal id_hit
        if id_hit:
            return
        payload = node.get("payload", {})
        if not payload.get("visible", True):
            return

        # resource-id 后缀
        rid = payload.get("name", "") or ""
        id_suffix = rid.split("/")[-1] if rid else ""
        if id_suffix and any(kw in id_suffix.lower() for kw in PRIVACY_ID_KEYWORDS):
            id_hit = True
            return

        # 文本
        text = payload.get("text", "") or ""
        if text:
            for kw in PRIVACY_TEXT_KEYWORDS:
                if kw in text:
                    text_hits.add(kw)

        for child in node.get("children", []):
            _walk(child)
            if id_hit:
                return

    _walk(hierarchy)

    if id_hit:
        return True
    return len(text_hits) >= PRIVACY_TEXT_MIN_HITS
