"""页面结构指纹生成

设计目标:
  - 同一个页面（即使重新渲染、滚动、懒加载）应产生**相同**指纹 -> 不重复截图
  - 不同的页面应产生**不同**指纹 -> 不漏截图

实现要点:
  - 列表容器（RecyclerView/ListView/ScrollView 等）的子项是动态的（懒加载、滚动），
    完全忽略其子树，只保留容器本身的签名 -> 滚动/重渲染不改变指纹
  - 非列表容器是静态布局，编码其结构 + 稳定 resource-id + 可见子节点数
    （静态布局的子节点数稳定，不会像列表那样波动）
  - 过滤掉纯数字/无字母的动态 id（如 "1"、"item_3" 里的序号）
"""

import hashlib


# 这些容器的子项是动态的（懒加载/滚动），指纹中忽略其子树
LIST_CONTAINERS = {
    "RecyclerView", "ListView", "GridView",
    "ScrollView", "NestedScrollView", "HorizontalScrollView",
}


def generate(hierarchy: dict, activity: str) -> str:
    """
    生成页面结构指纹。
    只保留控件骨架（类型+稳定id+静态结构），忽略动态列表内容。
    """
    skeleton = _extract_skeleton(hierarchy, depth=0, in_list=False)
    raw = f"{activity}|{skeleton}"
    return hashlib.md5(raw.encode()).hexdigest()


def generate_coarse(hierarchy: dict, activity: str) -> str:
    """
    生成粗粒度指纹（只看Activity + 前两层结构）。
    用于判断"同一个页面模板的不同实例"（如搜索页搜了不同关键词）。
    """
    skeleton = _extract_shallow_skeleton(hierarchy, max_depth=2)
    raw = f"{activity}|{skeleton}"
    return hashlib.md5(raw.encode()).hexdigest()


def _meaningful(id_suffix: str) -> bool:
    """id 后缀是否有意义（含字母）。纯数字/下划线的动态 id 视为无意义。"""
    return any(c.isalpha() for c in id_suffix)


def _extract_skeleton(node: dict, depth: int, in_list: bool) -> str:
    """递归提取控件骨架字符串"""
    payload = node.get("payload", {})

    if not payload.get("visible", True):
        return ""

    node_type = payload.get("type", "Unknown")
    resource_id = payload.get("name", "") or ""
    id_suffix = resource_id.split("/")[-1] if resource_id else ""

    # 过滤动态 id
    if id_suffix and not _meaningful(id_suffix):
        id_suffix = ""

    is_list = node_type in LIST_CONTAINERS

    # 处于列表子树内 -> 完全忽略（列表项内容是动态的）
    if in_list:
        return ""

    children = node.get("children", [])

    # 列表容器本身：只记录容器签名，不编码其子项
    if is_list:
        return f"[{depth}:{node_type}:{id_suffix}]"

    # 非列表容器：编码结构 + 可见子节点数（静态布局，数量稳定）
    child_sigs = []
    for child in children:
        sig = _extract_skeleton(child, depth + 1, in_list=False)
        if sig:
            child_sigs.append(sig)

    node_sig = f"{depth}:{node_type}:{id_suffix}:{len(child_sigs)}"
    return f"[{node_sig}{''.join(child_sigs)}]"


def _extract_shallow_skeleton(node: dict, max_depth: int, current_depth: int = 0) -> str:
    """浅层骨架提取——只看前 max_depth 层结构，用于粗粒度去重"""
    if current_depth > max_depth:
        return ""

    payload = node.get("payload", {})
    if not payload.get("visible", True):
        return ""

    node_type = payload.get("type", "Unknown")
    resource_id = payload.get("name", "") or ""
    id_suffix = resource_id.split("/")[-1] if resource_id else ""

    if id_suffix and not _meaningful(id_suffix):
        id_suffix = ""

    children = node.get("children", [])
    child_types = []
    for child in children:
        cp = child.get("payload", {})
        if cp.get("visible", True):
            child_types.append(cp.get("type", ""))

    child_sigs = []
    if current_depth < max_depth:
        for child in children:
            sig = _extract_shallow_skeleton(child, max_depth, current_depth + 1)
            if sig:
                child_sigs.append(sig)

    return f"[{node_type}:{id_suffix}:{len(child_types)}{''.join(child_sigs)}]"
