"""BFS 遍历引擎——广度优先

流程:
  1. 依次点击每个 Tab，在每个 Tab 页面上做广度探索
  2. 广度探索: 收集当前页面所有可点击节点，逐个点击
     - 跳转到新Activity → 截图 → 立即返回
     - 没跳转 → 继续下一个
  3. 所有 Tab 的第一层点完后，对发现的有价值子页面做第二轮
  4. 同一Activity只进1次，已访问过的直接跳过不点击
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
        self.visited_activities = set()
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
        """执行 BFS 遍历"""
        print("[INFO] 开始 BFS 遍历...")

        dismissed = popup_handler.dismiss_popups(self.poco)
        if dismissed:
            print(f"[INFO] 关闭了 {dismissed} 个弹窗")

        self._ensure_on_main_page()

        # 找出所有有效Tab数量
        tab_count = self._count_valid_tabs()

        if tab_count > 0:
            print(f"[INFO] 发现 {tab_count} 个有效 Tab\n")
            # === 第一轮: 每个Tab做广度探索 ===
            for i in range(tab_count):
                if self.screenshots_taken >= MAX_SCREENSHOTS:
                    break
                self._explore_tab_by_order(i)

            # === 第二轮: 对第一轮发现的有价值子页面，每个进入后再广度一层 ===
            # （已在 _explore_page 中通过递归 depth 实现）
        else:
            print("[INFO] 未找到 Tab，直接探索当前页\n")
            self._ensure_on_main_page()
            self._explore_page(depth=0)

        print(f"\n[DONE] BFS 遍历完成，共截图 {self.screenshots_taken} 张")
        return self.screenshots_taken

    # ------------------------------------------------------------------
    # Tab 遍历——直接按顺序点击，不依赖索引映射
    # ------------------------------------------------------------------

    def _count_valid_tabs(self) -> int:
        """计算有效Tab数量（排除发布按钮和个人页）"""
        tabs = self._find_tabs()
        if not tabs:
            return 0
        count = 0
        for tab in tabs:
            if not self._is_publish_button(tab):
                count += 1
        return count

    def _explore_tab_by_order(self, order: int):
        """
        按顺序点击第 order 个有效Tab（跳过发布按钮）。
        每次从首页重新查找Tab列表，避免引用失效。
        """
        self._ensure_on_main_page()
        time.sleep(0.5)

        tabs = self._find_tabs()
        if not tabs:
            print(f"[WARN] 找不到Tab栏，跳过")
            return

        # 找到第 order 个非发布按钮的Tab
        valid_idx = -1
        for i, tab in enumerate(tabs):
            if self._is_publish_button(tab):
                continue
            valid_idx += 1
            if valid_idx == order:
                # 点击这个Tab
                print(f"[INFO] === 有效Tab {order} (原始位置{i}) 探索开始 ===")
                try:
                    tab.click()
                except Exception:
                    print(f"[WARN] Tab点击失败，跳过")
                    return

                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)

                # 检查是否为功能页或个人页
                activity = metadata.get_current_activity(self.serial)
                if not activity or PACKAGE_NAME not in activity:
                    return
                if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
                    print(f"  [SKIP] 功能页: {activity.split('/')[-1]}")
                    return
                if SKIP_PERSONAL_PAGES:
                    hierarchy = self._dump_hierarchy()
                    if hierarchy and privacy.is_personal_page(hierarchy, activity):
                        print(f"  [SKIP] 个人页: {activity.split('/')[-1]}")
                        return

                # 探索这个Tab页面
                self._explore_page(depth=0)
                return

    # ------------------------------------------------------------------
    # 广度探索核心
    # ------------------------------------------------------------------

    def _explore_page(self, depth: int):
        """
        广度探索当前页面:
        1. 截图当前页
        2. 收集所有可点击节点
        3. 逐个点击: 新Activity → 截图 → 如果depth允许则继续探索一层 → 返回
        """
        if depth > MAX_DEPTH:
            return
        if self.screenshots_taken >= MAX_SCREENSHOTS:
            return

        current_activity = metadata.get_current_activity(self.serial)
        if not current_activity or PACKAGE_NAME not in current_activity:
            return
        if any(kw in current_activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return

        # 截图当前页
        self._try_screenshot(current_activity, depth)

        # 列表页分段截图
        self._capture_scroll_segments(current_activity, depth)

        # 收集当前页所有可点击节点
        actions = self._get_actions()
        if not actions:
            return

        print(f"  [BFS] depth={depth} {current_activity.split('/')[-1]} 节点={len(actions)}")

        discovered_pages = []  # 本页发现的新Activity，第二遍再深入

        for idx, action in enumerate(actions):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                break

            # 确认还在当前页
            now = metadata.get_current_activity(self.serial)
            if now != current_activity:
                if not self._go_back_to_activity(current_activity):
                    return

            # 点击前检查：如果这个节点的文字/id暗示会跳到已知Activity，跳过
            # （无法预判，只能点击后判断）

            if not self._click(action):
                continue

            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            new_activity = metadata.get_current_activity(self.serial)
            if not new_activity or PACKAGE_NAME not in new_activity:
                self._ensure_on_main_page()
                if not self._go_back_to_activity(current_activity):
                    return
                continue

            if new_activity == current_activity:
                continue

            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                self._go_back_to_activity(current_activity)
                continue

            # 已访问过 → 直接返回
            if new_activity in self.visited_activities:
                self._go_back_to_activity(current_activity)
                continue

            # 新Activity！
            self.visited_activities.add(new_activity)
            print(f"  [BFS] 节点{idx} -> {new_activity.split('/')[-1]}")

            # 截图新页面
            took = self._try_screenshot(new_activity, depth + 1)
            if took:
                discovered_pages.append(action)

            # 返回当前页
            self._go_back_to_activity(current_activity)

        # 本页节点全部点完后，对发现的有价值子页面做第二层探索
        if discovered_pages and depth + 1 <= MAX_DEPTH and self.screenshots_taken < MAX_SCREENSHOTS:
            for action in discovered_pages:
                if self.screenshots_taken >= MAX_SCREENSHOTS:
                    break

                # 确认在当前页
                now = metadata.get_current_activity(self.serial)
                if now != current_activity:
                    if not self._go_back_to_activity(current_activity):
                        break

                # 重新点击进入子页面
                if not self._click(action):
                    continue
                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)

                child_activity = metadata.get_current_activity(self.serial)
                if not child_activity or child_activity == current_activity:
                    continue
                if PACKAGE_NAME not in child_activity:
                    self._ensure_on_main_page()
                    self._go_back_to_activity(current_activity)
                    continue

                # 在子页面做广度探索
                self._explore_page(depth + 1)

                # 返回
                self._go_back_to_activity(current_activity)

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def _try_screenshot(self, activity: str, depth: int) -> bool:
        """截图当前页面，布局指纹去重。"""
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
        """按返回键回到目标Activity"""
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

        return metadata.get_current_activity(self.serial) == target_activity

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
    # Tab
    # ------------------------------------------------------------------

    def _find_tabs(self) -> list:
        """查找底部Tab节点列表"""
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
