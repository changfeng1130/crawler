"""页面结构指纹生成

三种粒度的指纹:
  - generate_layout():  主指纹——前2层可见节点TypeName+数量组合
    用于截图去重和导航判断。稳定：列表内容变化不影响；
    精确：不同页面模板结构不同能区分。
  - generate_fine():    细粒度——完整骨架（原方案），仅在需要区分同模板不同状态时用
  - generate():         对外统一接口，默认使用 layout 指纹
"""

import hashlib


LIST_CONTAINERS = {
    "RecyclerView", "ListView", "GridView",
    "ScrollView", "NestedScrollView", "HorizontalScrollView",
}


def generate(hierarchy: dict, activity: str) -> str:
    """
    主指纹：Activity + 前2层可见节点TypeName+数量。
    同一页面模板（不管内容如何变化）→ 相同指纹。
    不同页面模板 → 不同指纹。
    """
    return generate_layout(hierarchy, activity)


def generate_layout(hierarchy: dict, activity: str) -> str:
    """
    布局指纹：取前2层可见节点的 TypeName 列表 + 各节点的直接可见子节点数。

    示例输出（hash前的原始串）:
      "MainActivityV2|FrameLayout(3),ViewPager(5),BottomBar(5)|..."

    设计要点:
      - 第1层: 根节点的可见子节点的TypeName列表
      - 第2层: 每个第1层节点的可见子节点数（不展开具体内容）
      - 列表容器（RecyclerView等）的子节点数用"L"标记而非具体数字
        （因为列表项数量会随滚动/加载变化）
    """
    if not hierarchy:
        return ""

    layer1 = []  # 第1层各节点签名
    children_l1 = _get_visible_children(hierarchy)

    for child in children_l1:
        child_payload = child.get("payload", {})
        child_type = child_payload.get("type", "?")

        # 第2层: 该节点的可见子节点数
        children_l2 = _get_visible_children(child)
        if child_type in LIST_CONTAINERS:
            # 列表容器子节点数不稳定，标记为L
            layer1.append(f"{child_type}(L)")
        else:
            layer1.append(f"{child_type}({len(children_l2)})")

    raw = f"{activity}|{'|'.join(layer1)}"
    return hashlib.md5(raw.encode()).hexdigest()


def generate_fine(hierarchy: dict, activity: str) -> str:
    """
    细粒度指纹（完整骨架）。
    仅在需要严格区分同模板的不同状态时使用（如暗色模式 vs 亮色模式）。
    """
    skeleton = _extract_skeleton(hierarchy, depth=0, in_list=False)
    raw = f"{activity}|{skeleton}"
    return hashlib.md5(raw.encode()).hexdigest()


# ------------------------------------------------------------------
# 内部方法
# ------------------------------------------------------------------

def _get_visible_children(node: dict) -> list:
    """获取节点的所有可见直接子节点"""
    children = node.get("children", [])
    visible = []
    for child in children:
        payload = child.get("payload", {})
        if payload.get("visible", True):
            visible.append(child)
    return visible


def _meaningful(id_suffix: str) -> bool:
    """id 后缀是否有意义（含字母）"""
    return any(c.isalpha() for c in id_suffix)


def _extract_skeleton(node: dict, depth: int, in_list: bool) -> str:
    """递归提取完整控件骨架字符串（细粒度）"""
    payload = node.get("payload", {})

    if not payload.get("visible", True):
        return ""

    node_type = payload.get("type", "Unknown")
    resource_id = payload.get("name", "") or ""
    id_suffix = resource_id.split("/")[-1] if resource_id else ""

    if id_suffix and not _meaningful(id_suffix):
        id_suffix = ""

    is_list = node_type in LIST_CONTAINERS

    if in_list:
        return ""

    children = node.get("children", [])

    if is_list:
        return f"[{depth}:{node_type}:{id_suffix}]"

    child_sigs = []
    for child in children:
        sig = _extract_skeleton(child, depth + 1, in_list=False)
        if sig:
            child_sigs.append(sig)

    node_sig = f"{depth}:{node_type}:{id_suffix}:{len(child_sigs)}"
    return f"[{node_sig}{''.join(child_sigs)}]"
