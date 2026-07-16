"""BFS 遍历引擎——广度优先

遍历策略:
  1. 从首页开始，收集当前页面所有可点击节点
  2. 逐个点击 → 如果跳转到新页面 → 截图 → 把新页面加入队列 → 按返回回来
  3. 当前页面所有节点点完后，从队列取下一个待探索页面，导航到达后重复
  4. 广度优先保证先覆盖浅层（更重要的）页面，再逐步深入

核心设计:
  - 队列中存储的是"到达路径"（从首页出发的点击序列），而非页面引用
  - 每次探索新页面时，先从首页重放路径到达目标页，再收集点击
  - 用 Activity 判导航状态，用布局指纹判截图去重
"""

import subprocess
import time
from collections import deque

from core import fingerprint, popup_handler, screenshot, metadata, privacy
from core.adb_bin import ADB
from config import (
    PACKAGE_NAME,
    MAX_DEPTH,
    LIST_ITEM_MAX_CLICK,
    MAX_SCREENSHOTS,
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

MAX_VISITS_PER_ACTIVITY = 3

LIST_CONTAINERS = {"RecyclerView", "ListView", "GridView", "ViewPager2"}
TAB_ID_KEYWORDS = ("tab", "bottom_nav", "navigation")


class TraversalEngine:
    """BFS 遍历引擎"""

    def __init__(self, poco, serial: str, device_info: dict, app_info: dict):
        self.poco = poco
        self.serial = serial
        self.device_info = device_info
        self.app_info = app_info

        self.visited_fingerprints = set()
        self.visited_activities = {}       # Activity -> 进入次数
        self.screenshots_taken = 0

        self.screen_w, self.screen_h = self._parse_resolution(device_info.get("screen_resolution", ""))

    @staticmethod
    def _parse_resolution(res: str):
        try:
            w, h = res.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 1080, 2400

    def run(self) -> int:
        """执行 BFS 遍历，返回截图数量。"""
        print("[INFO] 开始 BFS 遍历...")

        dismissed = popup_handler.dismiss_popups(self.poco)
        if dismissed:
            print(f"[INFO] 关闭了 {dismissed} 个弹窗")

        self._ensure_on_main_page()

        # BFS 队列：每个元素是一个"页面任务" (activity, depth, tab_index)
        # tab_index: 要先切到哪个Tab（-1表示不切Tab）
        queue = deque()

        # Phase 1: 发现所有Tab，每个Tab作为一个起点入队
        tabs_count = self._count_tabs()
        if tabs_count > 0:
            print(f"[INFO] 发现 {tabs_count} 个 Tab，逐个入队")
            for i in range(tabs_count):
                queue.append({"tab_index": i, "depth": 0})
        else:
            # 没有Tab，当前页面直接入队
            queue.append({"tab_index": -1, "depth": 0})

        # Phase 2: BFS 主循环
        while queue and self.screenshots_taken < MAX_SCREENSHOTS:
            task = queue.popleft()
            tab_index = task["tab_index"]
            depth = task["depth"]

            if depth > MAX_DEPTH:
                continue

            # 导航到目标页面
            if not self._navigate_to_task(tab_index):
                continue

            # 处理当前页面：截图 + 收集所有可点击节点 + 逐个点击探索
            self._explore_current_page(depth, queue)

        print(f"[DONE] BFS 遍历完成，共截图 {self.screenshots_taken} 张")
        return self.screenshots_taken

    # ------------------------------------------------------------------
    # BFS 核心
    # ------------------------------------------------------------------

    def _explore_current_page(self, depth: int, queue: deque):
        """
        探索当前页面:
        1. 截图（指纹去重）
        2. 收集所有可点击节点
        3. 逐个点击：跳转了就截图新页面 + 记录到队列，然后返回继续下一个
        """
        current_activity = metadata.get_current_activity(self.serial)
        if not current_activity or PACKAGE_NAME not in current_activity:
            return
        if any(kw in current_activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return

        # 截图当前页
        self._try_screenshot(current_activity, depth)

        # 列表页分段截图
        self._capture_scroll_segments(current_activity, depth)

        # 收集当前页所有可点击节点（一次性全部拿到）
        actions = self._get_actions()
        print(f"  [BFS] depth={depth} activity={current_activity.split('/')[-1]} 可点击 {len(actions)} 个")

        # 逐个点击，发现新页面就截图并返回
        for idx, action in enumerate(actions):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                return

            # 确认还在当前页
            now = metadata.get_current_activity(self.serial)
            if now != current_activity:
                if not self._go_back_to_activity(current_activity):
                    return

            # 点击
            if not self._click(action):
                continue

            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            # 判断是否跳转
            new_activity = metadata.get_current_activity(self.serial)
            if not new_activity or PACKAGE_NAME not in new_activity:
                self._go_back_to_activity(current_activity)
                continue

            if new_activity == current_activity:
                # 没跳转，继续下一个
                continue

            # 跳过功能页
            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                self._go_back_to_activity(current_activity)
                continue

            # 检查该Activity是否已访问太多次
            visit_count = self.visited_activities.get(new_activity, 0)
            if visit_count >= MAX_VISITS_PER_ACTIVITY:
                self._go_back_to_activity(current_activity)
                continue
            self.visited_activities[new_activity] = visit_count + 1

            # 到了新页面！截图
            print(f"  [BFS] depth={depth} 节点{idx} -> {new_activity.split('/')[-1]} (第{visit_count+1}次)")
            new_took = self._try_screenshot(new_activity, depth + 1)

            # 如果是新模板且深度允许，将新页面的子节点探索任务加入队列
            # 这里不直接递归（那就成DFS了），而是在新页面上直接浅层探索
            if new_took and depth + 1 <= MAX_DEPTH:
                self._explore_child_page(new_activity, depth + 1, queue)

            # 返回当前页，继续点击下一个节点
            self._go_back_to_activity(current_activity)

    def _explore_child_page(self, activity: str, depth: int, queue: deque):
        """
        在子页面上做浅层探索：收集节点，逐个点击一层。
        发现的更深层页面不再递归，只截图后返回。
        """
        if self.screenshots_taken >= MAX_SCREENSHOTS:
            return

        # 分段截图
        self._capture_scroll_segments(activity, depth)

        # 收集子页面的可点击节点
        actions = self._get_actions()
        print(f"    [BFS-child] depth={depth} activity={activity.split('/')[-1]} 可点击 {len(actions)} 个")

        for idx, action in enumerate(actions):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                return

            now = metadata.get_current_activity(self.serial)
            if now != activity:
                if not self._go_back_to_activity(activity):
                    return

            if not self._click(action):
                continue

            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            new_activity = metadata.get_current_activity(self.serial)
            if not new_activity or PACKAGE_NAME not in new_activity:
                self._go_back_to_activity(activity)
                continue

            if new_activity == activity:
                continue

            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                self._go_back_to_activity(activity)
                continue

            visit_count = self.visited_activities.get(new_activity, 0)
            if visit_count >= MAX_VISITS_PER_ACTIVITY:
                self._go_back_to_activity(activity)
                continue
            self.visited_activities[new_activity] = visit_count + 1

            print(f"    [BFS-child] 节点{idx} -> {new_activity.split('/')[-1]}")
            self._try_screenshot(new_activity, depth + 1)

            # 返回子页面
            self._go_back_to_activity(activity)

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _navigate_to_task(self, tab_index: int) -> bool:
        """导航到任务指定的页面（重启App + 切Tab）"""
        self._ensure_on_main_page()

        if tab_index < 0:
            return True

        # 切到指定Tab
        tabs = self._find_tabs()
        if not tabs or tab_index >= len(tabs):
            return False

        tab = tabs[tab_index]
        if self._is_publish_button(tab):
            return False

        try:
            tab.click()
            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)
        except Exception:
            return False

        activity = metadata.get_current_activity(self.serial)
        if activity and any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            self._go_back()
            time.sleep(BACK_WAIT)
            return False

        # 检查是否为个人页
        if SKIP_PERSONAL_PAGES:
            hierarchy = self._dump_hierarchy()
            if hierarchy and privacy.is_personal_page(hierarchy, activity or ""):
                print(f"  [SKIP] 个人页Tab: {(activity or '').split('/')[-1]}")
                return False

        return True

    def _go_back_to_activity(self, target_activity: str) -> bool:
        """按返回键回到目标 Activity"""
        for _ in range(5):
            now = metadata.get_current_activity(self.serial)
            if now == target_activity:
                return True
            if not now or PACKAGE_NAME not in now:
                self._restart_app()
                return False

            self._go_back()
            time.sleep(BACK_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=1)

        now = metadata.get_current_activity(self.serial)
        return now == target_activity

    def _ensure_on_main_page(self):
        """确保当前在App主页面上"""
        activity = metadata.get_current_activity(self.serial)
        if not activity or PACKAGE_NAME not in activity:
            self._restart_app()
            return
        if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            for _ in range(3):
                self._go_back()
                time.sleep(BACK_WAIT)
                activity = metadata.get_current_activity(self.serial)
                if activity and PACKAGE_NAME in activity:
                    if not any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
                        return
            self._restart_app()

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def _try_screenshot(self, activity: str, depth: int) -> bool:
        """截图当前页面，用布局指纹去重。返回是否为新模板。"""
        hierarchy = self._dump_hierarchy()
        if not hierarchy:
            return False

        if SKIP_PERSONAL_PAGES and privacy.is_personal_page(hierarchy, activity):
            print(f"  [SKIP] 个人页: {activity.split('/')[-1]} (depth={depth})")
            return False

        fp = fingerprint.generate(hierarchy, activity)

        if fp in self.visited_fingerprints:
            return False

        self.visited_fingerprints.add(fp)

        popup_handler.dismiss_popups(self.poco, max_attempts=2)
        if SKIP_BLOCKED_POPUPS and popup_handler.has_blocking_popup(self.poco):
            return False

        path = screenshot.capture(self.serial, activity, fp, segment_index=0)
        if path:
            record = metadata.build_record(
                path, activity, fp, depth, self.device_info, self.app_info,
                segment_index=0,
            )
            metadata.append_record(record)
            self.screenshots_taken += 1
            print(f"  [{self.screenshots_taken}] 截图: {activity.split('/')[-1]} (depth={depth})")

        return True

    def _capture_scroll_segments(self, activity: str, depth: int):
        """列表页滚动分段截图"""
        hierarchy = self._dump_hierarchy()
        if not hierarchy or not self._find_scrollable(hierarchy):
            return

        fp = fingerprint.generate(hierarchy, activity)

        for i in range(1, SCROLL_MAX_TIMES + 1):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                break
            self._swipe(0.7, 0.3)
            time.sleep(SCROLL_SEGMENT_WAIT)

            now_activity = metadata.get_current_activity(self.serial)
            if now_activity != activity:
                break

            path = screenshot.capture(self.serial, activity, fp, segment_index=i)
            if path:
                record = metadata.build_record(
                    path, activity, fp, depth, self.device_info, self.app_info,
                    segment_index=i,
                )
                metadata.append_record(record)
                self.screenshots_taken += 1
                print(f"  [{self.screenshots_taken}] 分段 s{i}: {activity.split('/')[-1]} (depth={depth})")

        self._scroll_to_top()

    # ------------------------------------------------------------------
    # Tab 相关
    # ------------------------------------------------------------------

    def _count_tabs(self) -> int:
        """返回Tab数量"""
        tabs = self._find_tabs()
        if not tabs:
            return 0
        # 过滤掉发布按钮
        valid = [t for t in tabs if not self._is_publish_button(t)]
        return len(valid)

    def _find_tabs(self) -> list:
        """查找底部 Tab 节点列表"""
        tab_patterns = [
            "main_tab", "tab_bar", "bottom_nav", "navigation_bar",
            "BottomNavigationView", "RadioGroup",
        ]
        for pattern in tab_patterns:
            try:
                node = self.poco(nameMatches=f".*{pattern}.*")
                if node.exists():
                    children = list(node.children())
                    if len(children) >= 3:
                        return children
            except Exception:
                continue
        return self._find_bottom_clickable_nodes()

    def _find_bottom_clickable_nodes(self) -> list:
        """通用底部Tab发现"""
        try:
            all_touchable = self.poco(touchable=True)
        except Exception:
            return []

        bottom_nodes = []
        for node in all_touchable:
            try:
                pos = node.attr("pos")
                if not pos:
                    continue
                x, y = pos
                if y > 0.88:
                    bottom_nodes.append((x, node))
            except Exception:
                continue

        if len(bottom_nodes) < 3:
            return []

        bottom_nodes.sort(key=lambda t: t[0])
        return [t[1] for t in bottom_nodes]

    def _is_publish_button(self, node) -> bool:
        try:
            text = node.attr("text") or ""
            desc = node.attr("desc") or ""
            name = node.attr("name") or ""
            content = text + desc + name
            keywords = ["+", "发布", "拍摄", "publish", "create",
                        "CenterPlus", "centerplus", "投稿"]
            return any(kw in content for kw in keywords)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 控件操作
    # ------------------------------------------------------------------

    def _get_actions(self) -> list:
        """获取当前页面所有可点击控件，按优先级排序"""
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

                if any(kw in content for kw in SKIP_TEXTS):
                    continue
                if not pos:
                    continue
                if self._is_tab_node(name):
                    continue

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
        if not name:
            return False
        return any(kw in name.lower() for kw in TAB_ID_KEYWORDS)

    def _click(self, action: dict) -> bool:
        try:
            action["node"].click()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # ADB
    # ------------------------------------------------------------------

    def _go_back(self):
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "input", "keyevent", "4"],
            capture_output=True, timeout=5
        )

    def _restart_app(self):
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

    def _dump_hierarchy(self):
        try:
            return self.poco.agent.hierarchy.dump()
        except Exception:
            return None

    def _swipe(self, y1_ratio: float, y2_ratio: float):
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

    def _scroll_to_top(self):
        for _ in range(SCROLL_MAX_TIMES):
            self._swipe(0.3, 0.7)
            time.sleep(0.3)

    def _find_scrollable(self, hierarchy: dict) -> bool:
        found = False

        def _walk(node):
            nonlocal found
            if found:
                return
            payload = node.get("payload", {})
            if payload.get("visible", True):
                if payload.get("type", "") in SCROLLABLE_TYPES:
                    found = True
                    return
            for child in node.get("children", []):
                _walk(child)
                if found:
                    return

        _walk(hierarchy)
        return found

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _make_action_id(self, node_type: str, name: str, text: str, pos: list) -> str:
        if name and name != "None":
            return f"{node_type}:{name}"
        if text:
            return f"{node_type}:{text[:20]}"
        grid_x = round(pos[0] * 10) / 10
        grid_y = round(pos[1] * 10) / 10
        return f"{node_type}:@{grid_x},{grid_y}"

    def _calc_priority(self, node_type: str, name: str, text: str) -> int:
        score = 0
        nav_keywords = ["menu", "drawer", "more", "setting", "search"]
        if any(kw in name.lower() for kw in nav_keywords):
            score += 100
        entry_keywords = ["设置", "更多", "全部", "频道", "搜索", "分类", "详情"]
        if any(kw in text for kw in entry_keywords):
            score += 80
        if node_type in ["TextView", "Button", "ImageButton"]:
            score += 40
        return score

    def _get_parent_type(self, node) -> str:
        try:
            return node.parent().attr("type") or ""
        except Exception:
            return ""

    def _get_parent_id(self, node) -> str:
        try:
            return node.parent().attr("name") or "unknown_parent"
        except Exception:
            return "unknown_parent"
