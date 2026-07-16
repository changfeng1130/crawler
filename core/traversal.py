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
    PAGE_LOAD_WAIT,
    BACK_WAIT,
)


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

        self.visited_fingerprints = set()
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

        print(f"[INFO] 发现 {len(tabs)} 个 Tab")

        for i, tab in enumerate(tabs):
            self._lost = False  # 每个 Tab 独立探索，重置迷路状态
            try:
                tab.click()
                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)
                self._dfs(depth=1)
            except Exception as e:
                print(f"[WARN] Tab {i} 遍历异常: {e}")
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
        # B站特殊：尝试找底部区域的可点击元素集合
        try:
            node = self.poco(nameMatches=".*tv.danmaku.bili:id/tab.*")
            if node.exists():
                return node.parent()
        except Exception:
            pass
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
                # 非导航型点击（点赞/关注/同一页内切换）—— 不递归、不回退
                continue

            # 进入新页面，递归
            print(f"  [DFS] depth={depth} 节点{idx} 跳转 -> 新页面，递归")
            self._dfs(depth + 1)

            # 递归返回后，若已退出App则一路向上退出
            if self._lost:
                return

            # 回退到当前页（只按返回键；退出App才重启）
            if not self._return_to_page(current_fp):
                print(f"  [DFS] depth={depth} 回不到当前页，放弃剩余动作")
                return

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
        fp = fingerprint.generate(hierarchy, activity)

        if fp in self.visited_fingerprints:
            self.consecutive_known += 1
            return ("KNOWN", fp)

        # 新页面
        self.visited_fingerprints.add(fp)
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
        尝试返回目标页：只按返回键 1-2 次。
        - 已在目标页 -> True
        - 退出了 App -> 重启回首页，置 _lost，返回 False（让 DFS 一路向上退出）
        - 在 App 内但回不到目标页 -> 返回 False（放弃本页剩余动作，向上退出让父页处理）
        不会因回退失败而连环重启。
        """
        if self._is_on_page(target_fp):
            return True

        for _ in range(2):
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
