"""DFS 遍历引擎——核心模块

遍历策略:
  1. _traverse_tabs: 逐个点击底部 Tab，对每个 Tab 页做 DFS
  2. _dfs: 在当前页面收集所有可点击节点，逐个点击:
     - 点击后用 Activity 判断是否跳转（而非指纹，因为指纹对动态页过于严格）
     - 跳转了 → 截图新页面 → 递归 → 按返回键回来
     - 没跳转 → 跳过
  3. 回退判断用 Activity 名而非指纹，避免动态页面指纹漂移导致"迷路"
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

MAX_SAME_ACTIVITY_SCREENSHOTS = 3
# 同一个Activity（不管内容/指纹）最多进入几次就不再递归
MAX_VISITS_PER_ACTIVITY = 3

LIST_CONTAINERS = {"RecyclerView", "ListView", "GridView", "ViewPager2"}
TAB_ID_KEYWORDS = ("tab", "bottom_nav", "navigation")


class TraversalEngine:
    """DFS 遍历引擎"""

    def __init__(self, poco, serial: str, device_info: dict, app_info: dict):
        self.poco = poco
        self.serial = serial
        self.device_info = device_info
        self.app_info = app_info

        self.visited_fingerprints = set()
        self.visited_coarse_fps = {}
        self.visited_actions = set()
        self.activity_visit_count = {}     # Activity -> 进入次数
        self.screenshots_taken = 0
        self.consecutive_known = 0

        self.screen_w, self.screen_h = self._parse_resolution(device_info.get("screen_resolution", ""))

    @staticmethod
    def _parse_resolution(res: str):
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

        self._ensure_on_main_page()

        # Phase 1: 遍历底部 Tab
        tab_found = self._traverse_tabs()

        # Phase 2: 如果Tab遍历无效果，直接DFS当前页
        if not tab_found or self.screenshots_taken == 0:
            print("[INFO] Tab遍历无效果，直接对当前页面DFS")
            self._ensure_on_main_page()
            self._dfs(depth=0)

        print(f"[DONE] 遍历完成，共截图 {self.screenshots_taken} 张")
        return self.screenshots_taken

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
    # Tab 遍历
    # ------------------------------------------------------------------

    def _traverse_tabs(self) -> bool:
        """遍历底部 Tab 栏的各个页面。"""
        tabs = self._find_tabs()
        if not tabs:
            print("[INFO] 未找到底部 Tab，跳过 Tab 遍历")
            return False

        tab_count = len(tabs)
        print(f"[INFO] 发现 {tab_count} 个 Tab")

        for i in range(tab_count):
            try:
                current_tabs = self._find_tabs()
                if not current_tabs or i >= len(current_tabs):
                    self._restart_app()
                    time.sleep(1)
                    current_tabs = self._find_tabs()
                    if not current_tabs or i >= len(current_tabs):
                        break

                tab = current_tabs[i]
                if self._is_publish_button(tab):
                    print(f"[INFO] Tab {i+1} 是发布按钮，跳过")
                    continue

                print(f"[INFO] 切换到 Tab {i+1}/{tab_count}")
                tab.click()
                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)

                activity = metadata.get_current_activity(self.serial)
                if activity and any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
                    self._go_back()
                    time.sleep(BACK_WAIT)
                    continue

                self._dfs(depth=1)
            except Exception as e:
                print(f"[WARN] Tab {i} 遍历异常: {e}")
                self._restart_app()
                continue
        return True

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
        """通用底部Tab发现：屏幕底部水平排列的可点击节点"""
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

    # ------------------------------------------------------------------
    # DFS 核心（用 Activity 判导航状态，用指纹判截图去重）
    # ------------------------------------------------------------------

    def _dfs(self, depth: int):
        """
        DFS 遍历当前页面:
        1. 截图当前页（指纹去重）
        2. 收集当前页所有可点击节点
        3. 逐个点击 → 判断是否跳转（用Activity） → 跳转则递归 → 按返回回来
        """
        if depth > MAX_DEPTH:
            return
        if self.screenshots_taken >= MAX_SCREENSHOTS:
            return
        if self.consecutive_known >= MAX_SAME_TEMPLATE_COUNT:
            self.consecutive_known = 0
            return

        # 记录当前 Activity（用于导航判断——比指纹稳定）
        current_activity = metadata.get_current_activity(self.serial)
        if not current_activity or PACKAGE_NAME not in current_activity:
            return

        # 跳过功能页
        if any(kw in current_activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return

        # 截图去重（用指纹判断是否已截过）
        took_screenshot = self._try_screenshot(current_activity, depth)
        if not took_screenshot:
            return  # 已截过的页面，不继续往下遍历

        # 列表页分段截图
        self._capture_scroll_segments(current_activity, depth)

        # 收集当前页所有可点击节点（一次性收集完）
        actions = self._get_actions()
        print(f"  [DFS] depth={depth} activity={current_activity.split('/')[-1]} 可点击 {len(actions)} 个")

        for idx, action in enumerate(actions):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                return

            action_key = (current_activity, action["id"])
            if action_key in self.visited_actions:
                continue
            self.visited_actions.add(action_key)

            # 确认还在当前页面上（用Activity判断）
            now_activity = metadata.get_current_activity(self.serial)
            if now_activity != current_activity:
                # 不在当前页了，尝试回退
                if not self._go_back_to_activity(current_activity):
                    print(f"  [DFS] depth={depth} 无法回到 {current_activity.split('/')[-1]}，结束本层")
                    return

            # 点击
            if not self._click(action):
                continue

            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            # 判断是否跳转（用 Activity）
            new_activity = metadata.get_current_activity(self.serial)
            if not new_activity or PACKAGE_NAME not in new_activity:
                # 跳出了App，回来
                self._go_back_to_activity(current_activity)
                continue

            if new_activity == current_activity:
                # 没跳转（点赞/展开等页面内操作），继续下一个
                continue

            # 跳过功能页
            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                self._go_back_to_activity(current_activity)
                continue

            # 检查目标Activity是否已经进入过太多次（同一种页面不用反复进）
            visit_count = self.activity_visit_count.get(new_activity, 0)
            if visit_count >= MAX_VISITS_PER_ACTIVITY:
                # 这个Activity已经看过够多次了，回退
                self._go_back_to_activity(current_activity)
                continue

            # 记录进入次数
            self.activity_visit_count[new_activity] = visit_count + 1

            # 真的跳转了 → 递归探索新页面
            print(f"  [DFS] depth={depth} 节点{idx} -> {new_activity.split('/')[-1]} (第{visit_count+1}次)")
            self._dfs(depth + 1)

            # 递归返回后，回退到当前页
            if not self._go_back_to_activity(current_activity):
                print(f"  [DFS] depth={depth} 回退失败，结束本层")
                return

    # ------------------------------------------------------------------
    # 截图与去重
    # ------------------------------------------------------------------

    def _try_screenshot(self, activity: str, depth: int) -> bool:
        """
        尝试截图当前页面。用指纹判断是否已截过。
        返回 True = 新页面已截图，False = 已知页面跳过。
        """
        hierarchy = self._dump_hierarchy()
        if not hierarchy:
            return False

        # 隐私页跳过
        if SKIP_PERSONAL_PAGES and privacy.is_personal_page(hierarchy, activity):
            print(f"  [SKIP] 个人页: {activity.split('/')[-1]} (depth={depth})")
            return False

        fp = fingerprint.generate(hierarchy, activity)

        # 精确指纹已访问
        if fp in self.visited_fingerprints:
            self.consecutive_known += 1
            return False

        # 粗粒度检查（同模板已截太多次）
        coarse_fp = fingerprint.generate_coarse(hierarchy, activity)
        coarse_count = self.visited_coarse_fps.get(coarse_fp, 0)
        if coarse_count >= MAX_SAME_ACTIVITY_SCREENSHOTS:
            self.visited_fingerprints.add(fp)
            self.consecutive_known += 1
            print(f"  [SKIP] 同模板已截{coarse_count}次: {activity.split('/')[-1]} (depth={depth})")
            return False

        # 新页面，截图
        self.visited_fingerprints.add(fp)
        self.visited_coarse_fps[coarse_fp] = coarse_count + 1
        self.consecutive_known = 0

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

            # 确认没跳转
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
                print(f"  [{self.screenshots_taken}] 分段截图 s{i}: {activity.split('/')[-1]} (depth={depth})")

        self._scroll_to_top()

    # ------------------------------------------------------------------
    # 导航：回退判断用 Activity（稳定）
    # ------------------------------------------------------------------

    def _go_back_to_activity(self, target_activity: str) -> bool:
        """
        按返回键回到目标 Activity。
        用 Activity 名判断（而非指纹），因为动态页面指纹会漂移。
        """
        for _ in range(5):
            now = metadata.get_current_activity(self.serial)
            if now == target_activity:
                return True
            if not now or PACKAGE_NAME not in now:
                # 退出了App，重启
                self._restart_app()
                now = metadata.get_current_activity(self.serial)
                return now == target_activity

            self._go_back()
            time.sleep(BACK_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=1)

        # 最后检查一次
        now = metadata.get_current_activity(self.serial)
        if now == target_activity:
            return True

        # 还不在目标页，但还在App内 → 不重启，让上层处理
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

                # 列表条目限制
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
        name_lower = name.lower()
        return any(kw in name_lower for kw in TAB_ID_KEYWORDS)

    def _click(self, action: dict) -> bool:
        try:
            action["node"].click()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # ADB 操作
    # ------------------------------------------------------------------

    def _go_back(self):
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "input", "keyevent", "4"],
            capture_output=True, timeout=5
        )

    def _in_app(self) -> bool:
        activity = metadata.get_current_activity(self.serial)
        return bool(activity) and PACKAGE_NAME in activity

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
    # 辅助方法
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
        entry_keywords = ["设置", "我的", "个人", "更多", "全部", "频道", "搜索", "分类", "详情"]
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
