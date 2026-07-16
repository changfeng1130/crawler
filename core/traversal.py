"""BFS 遍历引擎——广度优先

流程:
  1. 依次切换每个 Tab，在每个 Tab 页面上做广度探索
  2. 广度探索: 收集当前页面所有可点击节点，逐个点击
     - 跳转到新Activity → 截图 → 立即返回（不深入）
     - 没跳转 → 继续下一个
  3. 当前页面节点全部点完后，切到下一个 Tab 重复
  4. 所有 Tab 的第一层点完后，再对发现的"有价值的子页面"做第二轮探索

优势:
  - 先把所有 Tab × 所有浅层页面覆盖一遍（广度）
  - 不会一头扎进某个列表无限深入（之前的问题）
  - 同一个Activity只进1次，节省大量时间
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

# 同一个Activity只进入1次（1次就够拿到UI模板）
MAX_VISITS_PER_ACTIVITY = 1

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
        self.visited_activities = set()    # 已进入过的Activity集合
        self.screenshots_taken = 0

        # 第二轮要深入探索的页面列表: [(tab_index, action_id), ...]
        self.pending_deep_explore = []

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

        # === 第一轮: 遍历所有Tab的第一层节点 ===
        tabs_indices = self._get_valid_tab_indices()
        if tabs_indices:
            print(f"[INFO] 发现 {len(tabs_indices)} 个有效 Tab")
            for tab_idx in tabs_indices:
                if self.screenshots_taken >= MAX_SCREENSHOTS:
                    break
                self._explore_tab(tab_idx, depth=0)
        else:
            # 没有Tab，直接探索当前页
            print("[INFO] 未找到 Tab，直接探索当前页")
            self._ensure_on_main_page()
            self._explore_page(depth=0)

        # === 第二轮: 深入探索第一轮发现的子页面 ===
        if self.pending_deep_explore and self.screenshots_taken < MAX_SCREENSHOTS:
            print(f"\n[INFO] === 第二轮: 深入探索 {len(self.pending_deep_explore)} 个子页面 ===")
            for task in self.pending_deep_explore:
                if self.screenshots_taken >= MAX_SCREENSHOTS:
                    break
                tab_idx = task["tab_index"]
                action_id = task["action_id"]
                depth = task["depth"]

                # 导航: 回到首页 → 切Tab → 点击对应节点进入子页面
                if self._navigate_and_click(tab_idx, action_id):
                    self._explore_page(depth)
                    self._ensure_on_main_page()

        print(f"\n[DONE] BFS 遍历完成，共截图 {self.screenshots_taken} 张")
        return self.screenshots_taken

    # ------------------------------------------------------------------
    # 第一轮: Tab级广度探索
    # ------------------------------------------------------------------

    def _explore_tab(self, tab_index: int, depth: int):
        """切到指定Tab，探索该Tab页面的所有第一层节点"""
        print(f"\n[INFO] === Tab {tab_index} 探索开始 ===")

        # 确保在首页
        self._ensure_on_main_page()

        # 切到目标Tab
        if not self._switch_tab(tab_index):
            print(f"[WARN] Tab {tab_index} 切换失败，跳过")
            return

        time.sleep(PAGE_LOAD_WAIT)
        popup_handler.dismiss_popups(self.poco, max_attempts=2)

        # 检查是否为需要跳过的页面
        activity = metadata.get_current_activity(self.serial)
        if not activity or PACKAGE_NAME not in activity:
            return
        if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return
        if SKIP_PERSONAL_PAGES:
            hierarchy = self._dump_hierarchy()
            if hierarchy and privacy.is_personal_page(hierarchy, activity):
                print(f"  [SKIP] 个人页 Tab: {activity.split('/')[-1]}")
                return

        # 探索当前Tab页面
        self._explore_page(depth, tab_index=tab_index)

    def _explore_page(self, depth: int, tab_index: int = -1):
        """
        广度探索当前页面:
        1. 截图当前页
        2. 收集所有可点击节点
        3. 逐个点击: 跳转了 → 截图新页面 → 立即返回; 没跳转 → 下一个
        """
        current_activity = metadata.get_current_activity(self.serial)
        if not current_activity or PACKAGE_NAME not in current_activity:
            return

        # 截图当前页
        self._try_screenshot(current_activity, depth)

        # 列表页分段截图
        self._capture_scroll_segments(current_activity, depth)

        # 收集当前页所有可点击节点
        actions = self._get_actions()
        if not actions:
            return

        print(f"  [BFS] depth={depth} {current_activity.split('/')[-1]} 节点数={len(actions)}")

        skipped = 0
        for idx, action in enumerate(actions):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                return

            # 确认还在当前页
            now = metadata.get_current_activity(self.serial)
            if now != current_activity:
                if not self._go_back_to_activity(current_activity):
                    print(f"  [BFS] 无法回到 {current_activity.split('/')[-1]}，结束本页")
                    return

            # 点击
            if not self._click(action):
                continue

            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            # 判断是否跳转
            new_activity = metadata.get_current_activity(self.serial)
            if not new_activity or PACKAGE_NAME not in new_activity:
                # 跳出App，回来
                self._ensure_on_main_page()
                if not self._go_back_to_activity(current_activity):
                    return
                continue

            if new_activity == current_activity:
                # 没跳转
                continue

            # 跳过功能页
            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                self._go_back_to_activity(current_activity)
                continue

            # 已经访问过该Activity → 直接返回（不截图、不浪费时间）
            if new_activity in self.visited_activities:
                skipped += 1
                self._go_back_to_activity(current_activity)
                continue

            # 新Activity！标记已访问
            self.visited_activities.add(new_activity)

            # 截图
            print(f"  [BFS] 节点{idx} -> {new_activity.split('/')[-1]}")
            took = self._try_screenshot(new_activity, depth + 1)

            # 记录到第二轮深入列表（如果深度允许）
            if took and depth + 1 < MAX_DEPTH:
                self.pending_deep_explore.append({
                    "tab_index": tab_index,
                    "action_id": action["id"],
                    "depth": depth + 1,
                })

            # 立即返回当前页，继续下一个节点
            self._go_back_to_activity(current_activity)

        if skipped > 0:
            print(f"  [BFS] 跳过 {skipped} 个已访问Activity的节点")

    # ------------------------------------------------------------------
    # 第二轮: 深入探索
    # ------------------------------------------------------------------

    def _navigate_and_click(self, tab_index: int, action_id: str) -> bool:
        """导航到首页 → 切Tab → 找到并点击指定节点"""
        self._ensure_on_main_page()

        if tab_index >= 0:
            if not self._switch_tab(tab_index):
                return False
            time.sleep(PAGE_LOAD_WAIT)

        # 在当前页面找到对应action并点击
        actions = self._get_actions()
        for action in actions:
            if action["id"] == action_id:
                if not self._click(action):
                    return False
                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)
                # 确认跳转了
                activity = metadata.get_current_activity(self.serial)
                if activity and PACKAGE_NAME in activity:
                    return True
                return False

        return False

    # ------------------------------------------------------------------
    # Tab 操作
    # ------------------------------------------------------------------

    def _get_valid_tab_indices(self) -> list:
        """返回有效（非发布按钮）的Tab索引列表"""
        tabs = self._find_tabs()
        if not tabs:
            return []
        valid = []
        for i, tab in enumerate(tabs):
            if not self._is_publish_button(tab):
                valid.append(i)
        return valid

    def _switch_tab(self, tab_index: int) -> bool:
        """切换到指定Tab"""
        tabs = self._find_tabs()
        if not tabs or tab_index >= len(tabs):
            return False
        try:
            tabs[tab_index].click()
            return True
        except Exception:
            return False

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
    # 截图
    # ------------------------------------------------------------------

    def _try_screenshot(self, activity: str, depth: int) -> bool:
        """截图当前页面，用布局指纹去重。"""
        hierarchy = self._dump_hierarchy()
        if not hierarchy:
            return False

        if SKIP_PERSONAL_PAGES and privacy.is_personal_page(hierarchy, activity):
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

            now = metadata.get_current_activity(self.serial)
            if now != activity:
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
    # 导航
    # ------------------------------------------------------------------

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
        """确保在App主页面"""
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
    # 控件
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
