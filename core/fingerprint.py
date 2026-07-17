"""页面唯一性指纹——基于组件布局dHash

方法:
  1. 从控件树提取所有可见组件的 类型+位置+大小
  2. 将动态内容(图片/视频/文字)替换为固定色块，只保留布局骨架
  3. 在64x128画布上绘制骨架图
  4. 对骨架图做dHash，得到128bit指纹
  5. 用汉明距离判断两个页面是否为同一模板（距离<5为同模板）

优势:
  - 同一页面换内容（不同视频/不同商品）→ 骨架不变 → dHash相同
  - 不同页面（搜索页 vs 详情页）→ 骨架不同 → dHash不同
  - 允许微小差异（按钮隐藏/角标出现）→ 汉明距离容错
"""

import numpy as np

# 骨架图尺寸
CANVAS_W = 64
CANVAS_H = 128

# 汉明距离阈值：小于此值认为是同一页面模板
# 实测B站同页面滚动后距离约10-14，不同页面距离通常>20
HAMMING_THRESHOLD = 12

# 动态内容控件类型 → 绘制为黑块（内容不稳定，忽略具体内容）
DYNAMIC_TYPES = {
    "ImageView", "VideoView", "WebView", "SurfaceView",
    "TextureView", "GifImageView", "SimpleDraweeView",
    "RoundImageView", "CircleImageView", "ShapeableImageView",
}

# 文本控件 → 绘制为灰块
TEXT_TYPES = {
    "TextView", "EditText", "AppCompatTextView",
    "MaterialTextView", "AutoCompleteTextView",
}

# 列表容器 → 绘制为深灰块（子项动态，忽略子树）
LIST_TYPES = {
    "RecyclerView", "ListView", "GridView",
    "ScrollView", "NestedScrollView", "HorizontalScrollView",
    "ViewPager", "ViewPager2",
}

# 按钮类 → 绘制为白块带边框
BUTTON_TYPES = {
    "Button", "ImageButton", "AppCompatButton",
    "MaterialButton", "FloatingActionButton", "Chip",
}

# 色值
COLOR_DYNAMIC = 20     # 图片/视频 → 深黑
COLOR_TEXT = 80        # 文字 → 深灰
COLOR_LIST = 50        # 列表容器 → 中黑
COLOR_BUTTON = 220     # 按钮 → 白
COLOR_CONTAINER = 150  # 普通容器 → 中灰边框
COLOR_BG = 128         # 背景 → 中性灰


def generate(hierarchy: dict, activity: str) -> str:
    """
    生成页面布局dHash指纹。
    返回: Activity名 + 16进制dHash字符串（如 "MainActivityV2|a3f5c8e1..."）
    用于去重时先比较Activity名（快），再比较dHash汉明距离（精确）。
    """
    if not hierarchy:
        return ""

    # 提取所有可见组件的位置信息
    components = []
    _extract_components(hierarchy, components, in_list=False)

    # 绘制骨架图
    canvas = _draw_skeleton(components)

    # 计算dHash
    dhash = _compute_dhash(canvas)

    return f"{activity}|{dhash}"


def is_same_page(fp1: str, fp2: str) -> bool:
    """
    判断两个指纹是否代表同一页面模板。
    先比Activity名，再比dHash汉明距离。
    """
    if not fp1 or not fp2:
        return False

    parts1 = fp1.split("|", 1)
    parts2 = fp2.split("|", 1)

    if len(parts1) != 2 or len(parts2) != 2:
        return fp1 == fp2

    activity1, hash1 = parts1
    activity2, hash2 = parts2

    # Activity不同 → 一定不同（快速排除）
    if activity1 != activity2:
        return False

    # 计算汉明距离
    distance = _hamming_distance(hash1, hash2)
    return distance < HAMMING_THRESHOLD


def quick_structure_key(hierarchy: dict, activity: str) -> str:
    """
    快速结构key：Activity名 + 第1层可见子节点TypeName列表。
    用于快速预判同模板（比dHash更快更稳定）。
    同Activity+同第1层结构 = 一定是同模板。
    """
    if not hierarchy:
        return ""
    children = _get_visible_children(hierarchy)
    types = [c.get("payload", {}).get("type", "?") for c in children]
    return f"{activity}|{','.join(types)}"


def find_similar(fp: str, visited_dict: dict) -> bool:
    """
    检查fp是否与已访问指纹中任一相似（同模板）。
    visited_dict: {activity_name: [dhash1, dhash2, ...]}
    按Activity分组，只比较同Activity的hash，避免全量遍历。
    """
    if not fp or "|" not in fp:
        return False

    parts = fp.split("|", 1)
    if len(parts) != 2:
        return False

    activity, dhash = parts
    existing_hashes = visited_dict.get(activity, [])

    for existing_hash in existing_hashes:
        if _hamming_distance(dhash, existing_hash) < HAMMING_THRESHOLD:
            return True

    return False


def add_fingerprint(fp: str, visited_dict: dict):
    """将指纹添加到已访问字典"""
    if not fp or "|" not in fp:
        return

    parts = fp.split("|", 1)
    if len(parts) != 2:
        return

    activity, dhash = parts
    if activity not in visited_dict:
        visited_dict[activity] = []
    visited_dict[activity].append(dhash)


# ------------------------------------------------------------------
# 组件提取
# ------------------------------------------------------------------

def _extract_components(node: dict, result: list, in_list: bool):
    """递归提取所有可见组件的类型和位置"""
    payload = node.get("payload", {})

    if not payload.get("visible", True):
        return

    node_type = payload.get("type", "")
    pos = payload.get("pos", None)       # [x, y] 归一化中心坐标
    size = payload.get("size", None)     # [w, h] 归一化宽高

    # 有位置信息的节点才记录
    if pos and size and size[0] > 0 and size[1] > 0:
        result.append({
            "type": node_type,
            "x": pos[0] - size[0] / 2,  # 左上角x
            "y": pos[1] - size[1] / 2,  # 左上角y
            "w": size[0],
            "h": size[1],
        })

    # 列表容器：记录容器本身，但不递归子项（子项是动态的）
    if node_type in LIST_TYPES:
        return

    # 如果已经在列表内部，不继续
    if in_list:
        return

    for child in node.get("children", []):
        _extract_components(child, result, in_list=False)


# ------------------------------------------------------------------
# 骨架图绘制
# ------------------------------------------------------------------

def _draw_skeleton(components: list) -> np.ndarray:
    """在64x128画布上绘制组件骨架图"""
    canvas = np.full((CANVAS_H, CANVAS_W), COLOR_BG, dtype=np.uint8)

    for comp in components:
        x = int(comp["x"] * CANVAS_W)
        y = int(comp["y"] * CANVAS_H)
        w = max(1, int(comp["w"] * CANVAS_W))
        h = max(1, int(comp["h"] * CANVAS_H))

        # 边界裁剪
        x = max(0, min(x, CANVAS_W - 1))
        y = max(0, min(y, CANVAS_H - 1))
        x2 = min(x + w, CANVAS_W)
        y2 = min(y + h, CANVAS_H)

        if x2 <= x or y2 <= y:
            continue

        node_type = comp["type"]

        if node_type in DYNAMIC_TYPES:
            canvas[y:y2, x:x2] = COLOR_DYNAMIC
        elif node_type in TEXT_TYPES:
            canvas[y:y2, x:x2] = COLOR_TEXT
        elif node_type in LIST_TYPES:
            canvas[y:y2, x:x2] = COLOR_LIST
        elif node_type in BUTTON_TYPES:
            canvas[y:y2, x:x2] = COLOR_BUTTON
        else:
            # 普通容器：只画边框
            canvas[y, x:x2] = COLOR_CONTAINER
            canvas[y2-1, x:x2] = COLOR_CONTAINER
            canvas[y:y2, x] = COLOR_CONTAINER
            canvas[y:y2, x2-1] = COLOR_CONTAINER

    return canvas


# ------------------------------------------------------------------
# dHash计算
# ------------------------------------------------------------------

def _compute_dhash(image: np.ndarray, hash_size: int = 16) -> str:
    """
    计算dHash (difference hash)。
    缩放到 (hash_size+1) x hash_size，逐行比较相邻像素。
    返回16进制字符串。
    """
    # 缩放到 (hash_size+1) x hash_size
    resized = _resize(image, hash_size + 1, hash_size)

    # 逐行比较：右边像素 > 左边像素 → 1
    diff = resized[:, 1:] > resized[:, :-1]

    # 转为整数hash
    bits = diff.flatten()
    # 每8位打包为一个字节
    hash_bytes = []
    for i in range(0, len(bits), 8):
        byte_val = 0
        for j in range(min(8, len(bits) - i)):
            if bits[i + j]:
                byte_val |= (1 << (7 - j))
        hash_bytes.append(byte_val)

    return bytes(hash_bytes).hex()


def _resize(image: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """简单的最近邻缩放（不依赖cv2）"""
    src_h, src_w = image.shape[:2]
    result = np.zeros((target_h, target_w), dtype=np.uint8)

    for y in range(target_h):
        for x in range(target_w):
            src_x = int(x * src_w / target_w)
            src_y = int(y * src_h / target_h)
            src_x = min(src_x, src_w - 1)
            src_y = min(src_y, src_h - 1)
            result[y, x] = image[src_y, src_x]

    return result


def _hamming_distance(hash1: str, hash2: str) -> int:
    """计算两个16进制hash字符串的汉明距离"""
    if len(hash1) != len(hash2):
        return 128  # 长度不同视为完全不同

    distance = 0
    for c1, c2 in zip(hash1, hash2):
        diff = int(c1, 16) ^ int(c2, 16)
        distance += bin(diff).count("1")

    return distance
