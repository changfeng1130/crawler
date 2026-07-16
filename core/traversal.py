"""DFS 遍历引擎——核心模块

遍历策略:
  1. _traverse_tabs: 逐个点击底部 Tab，对每个 Tab 页做 DFS
  2. _dfs: 解析当前页所有可点击节点，依次点击；
     - 点击后若页面**未跳转**（点赞/tab切换等非导航操作）-> 不递归、不回退，直接下一个
     - 点击后若页面**真的跳转** -> 递归进入新页面，结束后按返回键回退
  3. 回退只按返回键 1-2 次；只有**真的退出了 App** 才重启回首页。
     回不到目标页但在 App 内时，放弃本页剩余动作、向上退出，避免连环重启/跳出App。
"""

import subprocess
import time

from core import fingerprint, popup_handler, screenshot, metadata, privacy
from core.adb_bin import ADB
from config import (
    PACKAGE_NAME,
    MAX_DEPTH,
    LIST_ITEM_MAX_CLICK,
    MAX_SCREENSHOTS,
    MAX_SAME_TEMPLATE_COUNT,
    SCROLL_MAX_TIMES,
    SCROLL_SEGMENT_WAIT,
    SCROLLABLE_TYPES,
    SKIP_PERSONAL_PAGES,
    SKIP_BLOCKED_POPUPS,
    SKIP_TEXTS,
    SKIP_ACTIVITY_KEYWORDS,
    PAGE_LOAD_WAIT,
    BACK_WAIT,
)

# 同一Activity最多截图次数（防止同一页面因内容变化反复截图）
MAX_SAME_ACTIVITY_SCREENSHOTS = 3


LIST_CONTAINERS = {"RecyclerView", "ListView", "GridView", "ViewPager2"}

# 底部 Tab 栏相关 id 关键词 —— DFS 中不点击这些，避免 tab 间切换导致返回键失效
TAB_ID_KEYWORDS = ("tab", "bottom_nav", "navigation")


class TraversalEngine:
    """DFS 遍历引擎"""

    def __init__(self, poco, serial: str, device_info: dict, app_info: dict):
        self.poco = poco
        self.serial = serial
        self.device_info = device_info
        self.app_info = app_info

        self.visited_fingerprints = set()      # 精确指纹（完整骨架）
        self.visited_coarse_fps = {}           # 粗粒度指纹 -> 已截图次数（同模板限次）
        self.visited_actions = set()
        self.screenshots_taken = 0
        self.consecutive_known = 0
        self._lost = False  # 已退出App后置位，使 DFS 一路向上退出

        # 屏幕分辨率，用于 ADB swipe 滚动
        self.screen_w, self.screen_h = self._parse_resolution(device_info.get("screen_resolution", ""))

    @staticmethod
    def _parse_resolution(res: str):
        """解析 '1080x2340' -> (1080, 2340)；失败给默认值"""
        try:
            w, h = res.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 1080, 2400

    def run(self) -> int:
        """执行完整遍历，返回截图数量。"""
        print("[INFO] 开始遍历...")

        dismissed = popup_handler.dismiss_popups(self.poco)
        if dismissed:
            print(f"[INFO] 关闭了 {dismissed} 个弹窗")

        # Phase 1: 遍历底部 Tab；找不到 Tab 栏则直接对当前页 DFS
        if not self._traverse_tabs():
            self._dfs(depth=0)

        print(f"[DONE] 遍历完成，共截图 {self.screenshots_taken} 张")
        return self.screenshots_taken

    # ------------------------------------------------------------------
    # Tab 遍历
    # ------------------------------------------------------------------

    def _traverse_tabs(self) -> bool:
        """遍历底部 Tab 栏的各个页面。返回是否找到 Tab 栏。"""
        tab_bar = self._find_tab_bar()
        if not tab_bar:
            print("[INFO] 未找到底部 Tab 栏，跳过 Tab 遍历")
            return False

        try:
            tabs = list(tab_bar.children())
        except Exception:
            return False

        tab_count = len(tabs)
        print(f"[INFO] 发现 {tab_count} 个 Tab")

        for i in range(tab_count):
            self._lost = False  # 每个 Tab 独立探索，重置迷路状态
            try:
                # 每次重新获取Tab栏（因为页面可能已变，之前的引用可能失效）
                current_tab_bar = self._find_tab_bar()
                if not current_tab_bar:
                    # Tab栏丢失（可能页面跳转了），重启回首页重试
                    self._restart_app()
                    time.sleep(1)
                    current_tab_bar = self._find_tab_bar()
                    if not current_tab_bar:
                        print(f"[WARN] Tab栏丢失，跳过剩余Tab")
                        break

                current_tabs = list(current_tab_bar.children())
                if i >= len(current_tabs):
                    break

                print(f"[INFO] 切换到 Tab {i+1}/{tab_count}")
                current_tabs[i].click()
                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)
                self._dfs(depth=1)
            except Exception as e:
                print(f"[WARN] Tab {i} 遍历异常: {e}")
                # 尝试重启恢复，以便下一个Tab能正常开始
                self._restart_app()
                continue
        return True

    def _find_tab_bar(self):
        """查找底部 Tab 栏"""
        tab_patterns = [
            "main_tab", "tab_bar", "bottom_nav", "navigation_bar",
            "BottomNavigationView", "RadioGroup",
        ]
        for pattern in tab_patterns:
            try:
                node = self.poco(nameMatches=f".*{pattern}.*")
                if node.exists():
                    return node
            except Exception:
                continue

        # 通用方案：找屏幕底部区域的水平排列可点击节点组
        try:
            bottom_tabs = self._find_bottom_clickable_group()
            if bottom_tabs:
                return bottom_tabs
        except Exception:
            pass

        return None

    def _find_bottom_clickable_group(self):
        """
        通用底部Tab发现：找到屏幕底部20%区域内，水平排列的可点击节点组。
        返回一个伪容器对象（包含 children() 方法），或 None。
        """
        try:
            all_touchable = self.poco(touchable=True)
        except Exception:
            return None

        # 收集底部区域的可点击节点（y > 0.85 即屏幕底部15%）
        bottom_nodes = []
        for node in all_touchable:
            try:
                pos = node.attr("pos")
                if not pos:
                    continue
                x, y = pos
                if y > 0.85:
                    bottom_nodes.append((x, node))
            except Exception:
                continue

        # 底部至少3个横向排列的节点才算是Tab栏
        if len(bottom_nodes) < 3:
            return None

        # 按x坐标排序
        bottom_nodes.sort(key=lambda t: t[0])

        # 检查是否大致均匀分布（横向间距相近）
        xs = [t[0] for t in bottom_nodes]
        if len(xs) >= 3:
            gaps = [xs[i+1] - xs[i] for i in range(len(xs)-1)]
            avg_gap = sum(gaps) / len(gaps)
            # 间距波动不超过平均值50%，认为是均匀排列
            if avg_gap > 0.05 and all(abs(g - avg_gap) < avg_gap * 0.5 for g in gaps):
                nodes_only = [t[1] for t in bottom_nodes]
                return _FakeContainer(nodes_only)

        return None

    # ------------------------------------------------------------------
    # DFS 核心
    # ------------------------------------------------------------------

    def _dfs(self, depth: int):
        """DFS 遍历"""
        if self._lost:
            return
        if depth > MAX_DEPTH:
            return
        if self.screenshots_taken >= MAX_SCREENSHOTS:
            return
        if self.consecutive_known >= MAX_SAME_TEMPLATE_COUNT:
            self.consecutive_known = 0
            return

        # 页面分类: NEW / KNOWN / PERSONAL / BLOCKED
        status, current_fp = self._classify_page(depth)
        if status != "NEW":
            return  # 已知页/隐私页/遮挡页 —— 不截图、不递归

        # 列表页滚动分段截图（s1..sN）
        self._capture_scroll_segments(current_fp, depth)

        # 解析当前页所有可点击节点
        actions = self._get_actions(current_fp)
        print(f"  [DFS] depth={depth} 可点击节点 {len(actions)} 个")

        for idx, action in enumerate(actions):
            if self._lost:
                return
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                return

            action_key = (current_fp, action["id"])
            if action_key in self.visited_actions:
                continue
            self.visited_actions.add(action_key)

            # 如果当前不在目标页（之前回退失败），尝试恢复
            if not self._is_on_page(current_fp):
                if not self._try_recover_to(current_fp):
                    print(f"  [DFS] depth={depth} 无法恢复到当前页，放弃剩余动作")
                    return

            # 执行点击
            if not self._click(action):
                continue

            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            # 关键：判断是否真的发生了页面跳转
            new_fp = self._current_fingerprint()
            if not new_fp:
                continue

            if new_fp == current_fp:
                continue

            # 进入新页面，递归
            print(f"  [DFS] depth={depth} 节点{idx} 跳转 -> 新页面，递归")
            self._dfs(depth + 1)

            # 递归返回后，若已退出App则一路向上退出
            if self._lost:
                return

            # 回退到当前页
            if not self._return_to_page(current_fp):
                # 回退失败但仍在App内 → 不立刻放弃，下一轮循环顶部会尝试恢复
                continue

    # ------------------------------------------------------------------
    # 页面处理
    # ------------------------------------------------------------------

    def _current_fingerprint(self) -> str:
        """获取当前页面指纹"""
        activity, hierarchy = self._dump()
        if not hierarchy:
            return ""
        return fingerprint.generate(hierarchy, activity)

    def _dump(self) -> tuple:
        """返回 (activity, hierarchy)。失败返回 ("", None)。"""
        activity = metadata.get_current_activity(self.serial)
        try:
            hierarchy = self.poco.agent.hierarchy.dump()
        except Exception:
            hierarchy = None
        return activity, hierarchy

    def _classify_page(self, depth: int) -> tuple:
        """
        页面分类: NEW / KNOWN / PERSONAL / BLOCKED。
        返回 (status, fp)。
        - KNOWN: 已访问过，跳过
        - PERSONAL: 个人详情页，不截图不递归
        - BLOCKED: 遮挡弹窗未关闭，不截图
        - NEW: 新页面，已截主图 s0
        """
        activity, hierarchy = self._dump()
        if not hierarchy:
            return ("KNOWN", "")  # dump 失败，按已知处理避免误截图

        # Activity级别跳过（发布/拍摄等功能页）
        if activity and any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            print(f"  [SKIP] 功能页，跳过: {activity} (depth={depth})")
            return ("KNOWN", "")

        fp = fingerprint.generate(hierarchy, activity)

        # 精确指纹已访问过 → 跳过
        if fp in self.visited_fingerprints:
            self.consecutive_known += 1
            return ("KNOWN", fp)

        # 粗粒度指纹检查：同一模板（Activity+浅层结构）已截过太多次 → 跳过
        coarse_fp = fingerprint.generate_coarse(hierarchy, activity)
        coarse_count = self.visited_coarse_fps.get(coarse_fp, 0)
        if coarse_count >= MAX_SAME_ACTIVITY_SCREENSHOTS:
            self.visited_fingerprints.add(fp)
            self.consecutive_known += 1
            print(f"  [SKIP] 同模板已截{coarse_count}次，跳过: {activity} (depth={depth})")
            return ("KNOWN", fp)

        # 新页面
        self.visited_fingerprints.add(fp)
        self.visited_coarse_fps[coarse_fp] = coarse_count + 1
        self.consecutive_known = 0

        # 关弹窗
        popup_handler.dismiss_popups(self.poco, max_attempts=2)

        # 遮挡弹窗未关闭 —— 跳过截图（可由配置关闭）
        if SKIP_BLOCKED_POPUPS and popup_handler.has_blocking_popup(self.poco):
            print(f"  [SKIP] 遮挡弹窗未关闭，跳过截图: {activity} (depth={depth})")
            return ("BLOCKED", fp)

        # 个人详情页 —— 不截图不递归（可由配置关闭）
        if SKIP_PERSONAL_PAGES and privacy.is_personal_page(hierarchy, activity):
            print(f"  [SKIP] 个人详情页，跳过截图: {activity} (depth={depth})")
            return ("PERSONAL", fp)

        # 截主图 s0
        path = screenshot.capture(self.serial, activity, fp, segment_index=0)
        if path:
            record = metadata.build_record(
                path, activity, fp, depth, self.device_info, self.app_info,
                segment_index=0,
            )
            metadata.append_record(record)
            self.screenshots_taken += 1
            print(f"  [{self.screenshots_taken}] 截图: {activity} (depth={depth})")

        return ("NEW", fp)

    def _capture_scroll_segments(self, current_fp: str, depth: int):
        """列表页滚动分段截图: 向下滚动 N 次，每次截一张分段图。"""
        activity, hierarchy = self._dump()
        if not hierarchy or not self._find_scrollable(hierarchy):
            return  # 非列表页，不分段

        for i in range(1, SCROLL_MAX_TIMES + 1):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                break
            self._swipe(0.7, 0.3)  # 向下滑（看下方更多内容）
            time.sleep(SCROLL_SEGMENT_WAIT)

            # 滚动误触发跳转则停止
            if self._current_fingerprint() != current_fp:
                break

            path = screenshot.capture(self.serial, activity, current_fp, segment_index=i)
            if path:
                record = metadata.build_record(
                    path, activity, current_fp, depth, self.device_info, self.app_info,
                    segment_index=i,
                )
                metadata.append_record(record)
                self.screenshots_taken += 1
                print(f"  [{self.screenshots_taken}] 分段截图 s{i}: {activity} (depth={depth})")

        # 回到顶部，保证后续 _get_actions 拿到顶部控件
        self._scroll_to_top()

    def _scroll_to_top(self):
        """向上滚动回到列表顶部"""
        for _ in range(SCROLL_MAX_TIMES):
            self._swipe(0.3, 0.7)  # 向上滑（回到顶部）
            time.sleep(0.3)

    def _swipe(self, y1_ratio: float, y2_ratio: float):
        """ADB input swipe 滚动（比 poco.scroll 稳定）"""
        cx = self.screen_w // 2
        y1 = int(self.screen_h * y1_ratio)
        y2 = int(self.screen_h * y2_ratio)
        try:
            subprocess.run(
                [ADB, "-s", self.serial, "shell", "input", "swipe",
                 str(cx), str(y1), str(cx), str(y2), "400"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    def _find_scrollable(self, hierarchy: dict) -> bool:
        """判断页面是否含可滚动列表容器"""
        found = False

        def _walk(node):
            nonlocal found
            if found:
                return
            payload = node.get("payload", {})
            if payload.get("visible", True):
                node_type = payload.get("type", "")
                if node_type in SCROLLABLE_TYPES:
                    found = True
                    return
            for child in node.get("children", []):
                _walk(child)
                if found:
                    return

        _walk(hierarchy)
        return found

    # ------------------------------------------------------------------
    # 控件操作
    # ------------------------------------------------------------------

    def _get_actions(self, current_fp: str) -> list:
        """获取当前页面所有可点击控件，按优先级排序、去重"""
        actions = []
        seen_ids = set()
        list_item_counts = {}

        try:
            nodes = self.poco(touchable=True)
        except Exception:
            return []

        for node in nodes:
            try:
                text = node.attr("text") or ""
                desc = node.attr("desc") or ""
                node_type = node.attr("type") or ""
                name = node.attr("name") or ""
                pos = node.attr("pos")

                content = text + desc

                # 跳过危险/无效控件
                if any(kw in content for kw in SKIP_TEXTS):
                    continue
                if not pos:
                    continue

                # 跳过底部 Tab 栏节点（由 _traverse_tabs 处理，避免返回键失效）
                if self._is_tab_node(name):
                    continue

                # 列表条目限制（每个列表容器最多点 N 项）
                parent_type = self._get_parent_type(node)
                if parent_type in LIST_CONTAINERS:
                    parent_id = self._get_parent_id(node)
                    count = list_item_counts.get(parent_id, 0)
                    if count >= LIST_ITEM_MAX_CLICK:
                        continue
                    list_item_counts[parent_id] = count + 1

                action_id = self._make_action_id(node_type, name, text, pos)
                if action_id in seen_ids:
                    continue
                seen_ids.add(action_id)

                priority = self._calc_priority(node_type, name, text)
                actions.append({
                    "id": action_id,
                    "node": node,
                    "priority": priority,
                })
            except Exception:
                continue

        actions.sort(key=lambda x: x["priority"], reverse=True)
        return actions

    def _is_tab_node(self, name: str) -> bool:
        """判断是否为底部 Tab 栏节点"""
        if not name:
            return False
        name_lower = name.lower()
        return any(kw in name_lower for kw in TAB_ID_KEYWORDS)

    def _click(self, action: dict) -> bool:
        """点击控件"""
        try:
            action["node"].click()
            return True
        except Exception:
            return False

    def _go_back(self):
        """按返回键"""
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "input", "keyevent", "4"],
            capture_output=True, timeout=5
        )

    def _is_on_page(self, expected_fp: str) -> bool:
        """检查当前页面是否与预期一致"""
        return self._current_fingerprint() == expected_fp

    def _in_app(self) -> bool:
        """当前是否仍在目标 App 内（按 activity 包名判断）"""
        activity = metadata.get_current_activity(self.serial)
        return bool(activity) and PACKAGE_NAME in activity

    def _return_to_page(self, target_fp: str) -> bool:
        """
        尝试返回目标页：按返回键最多 4 次。
        - 已在目标页 -> True
        - 退出了 App -> 重启回首页，置 _lost，返回 False
        - 在 App 内但回不到目标页 -> 返回 False（放弃本页剩余动作）
        """
        if self._is_on_page(target_fp):
            return True

        for _ in range(4):
            self._go_back()
            time.sleep(BACK_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=1)
            if self._is_on_page(target_fp):
                return True
            if not self._in_app():
                print("[WARN] 已离开 App，重启回首页")
                self._restart_app()
                self._lost = True
                return False

        return False  # 在 App 内但回不到目标页

    def _try_recover_to(self, target_fp: str) -> bool:
        """
        尝试恢复到目标页面。先尝试返回键，失败则重启App。
        对于非首页的深层页面，重启后无法回到原位，返回 False。
        对于 depth<=1 的页面（首页/Tab页），重启后可达，返回 True。
        """
        # 先尝试返回键
        for _ in range(3):
            self._go_back()
            time.sleep(BACK_WAIT)
            if self._is_on_page(target_fp):
                return True
            if not self._in_app():
                break

        # 重启App回首页
        self._restart_app()
        # 重启后检查是否恰好在目标页（通常只有首页能匹配）
        return self._is_on_page(target_fp)

    def _restart_app(self):
        """重启App"""
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "am", "force-stop", PACKAGE_NAME],
            capture_output=True, timeout=5
        )
        time.sleep(1)
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "monkey", "-p", PACKAGE_NAME,
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True, timeout=5
        )
        time.sleep(3)
        popup_handler.dismiss_popups(self.poco, max_attempts=3)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _make_action_id(self, node_type: str, name: str, text: str, pos: list) -> str:
        """生成控件唯一标识（优先用稳定 id，其次文本，最后坐标网格）"""
        if name and name != "None":
            return f"{node_type}:{name}"
        if text:
            return f"{node_type}:{text[:20]}"
        grid_x = round(pos[0] * 10) / 10
        grid_y = round(pos[1] * 10) / 10
        return f"{node_type}:@{grid_x},{grid_y}"

    def _calc_priority(self, node_type: str, name: str, text: str) -> int:
        """计算点击优先级"""
        score = 0
        nav_keywords = ["menu", "drawer", "more", "setting", "search"]
        if any(kw in name.lower() for kw in nav_keywords):
            score += 100
        entry_keywords = ["设置", "我的", "个人", "更多", "全部", "频道", "搜索", "分类", "详情"]
        if any(kw in text for kw in entry_keywords):
            score += 80
        if node_type in ["TextView", "Button", "ImageButton"]:
            score += 40
        return score

    def _get_parent_type(self, node) -> str:
        """获取父节点类型"""
        try:
            return node.parent().attr("type") or ""
        except Exception:
            return ""

    def _get_parent_id(self, node) -> str:
        """获取父节点ID"""
        try:
            return node.parent().attr("name") or "unknown_parent"
        except Exception:
            return "unknown_parent"


class _FakeContainer:
    """伪容器，用于包装一组底部Tab节点，提供 children() 接口"""

    def __init__(self, nodes: list):
        self._nodes = nodes

    def children(self):
        return self._nodes

    def exists(self):
        return len(self._nodes) > 0
