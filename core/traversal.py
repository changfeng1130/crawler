"""DFS 遍历引擎——深度优先

流程:
  1. 第一阶段依次遍历每个 Tab：只截当前屏，不滚动
  2. 所有 Tab 普通遍历完成后，从 Tab 0 开始第二阶段滚动遍历
  3. 收集当前页面可点击节点，逐个点击
     - 跳转到新Activity → 截图 → 立即递归探索该页面 → 返回父页面
     - 没跳转 → 继续下一个
  4. 子页面分支完成后，再继续父页面的下一个节点
  5. Activity访问次数、结构key和dHash共同防止重复遍历
"""

import json
import subprocess
import time
from datetime import datetime

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
    STATE_FILE,
)

LIST_CONTAINERS = {"RecyclerView", "ListView", "GridView", "ViewPager2"}
TAB_ID_KEYWORDS = ("tab", "bottom_nav", "navigation")


class TraversalEngine:
    """DFS 遍历引擎"""

    def __init__(self, poco, serial: str, device_info: dict, app_info: dict, resume: bool = False):
        self.poco = poco
        self.serial = serial
        self.device_info = device_info
        self.app_info = app_info

        self.visited_fingerprints = {}     # {activity: [dhash1, dhash2, ...]}
        self.visited_structure_keys = set()  # 快速结构key集合（Activity+第1层TypeName）
        self.activity_visit_count = {}    # {activity: 进入次数}
        self.completed_tabs = set()
        self.completed_scroll_tabs = set()
        # 只用于当前进程的滚动阶段，防止已知页面之间循环递归。
        self.scroll_explored_fingerprints = {}
        self.screenshots_taken = 0

        # 同一Activity最多进入次数（允许共用Activity的不同功能页被发现）
        self.max_visits_per_activity = 3

        # 如果是resume模式，从state.json恢复状态
        if resume:
            self._load_state()

        self.screen_w, self.screen_h = self._parse_resolution(device_info.get("screen_resolution", ""))

    @staticmethod
    def _parse_resolution(res: str):
        try:
            w, h = res.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 1080, 2400

    def _load_state(self):
        """从 state.json 恢复遍历状态"""
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.visited_fingerprints = state.get("visited_fingerprints", {})
            self.visited_structure_keys = set(state.get("visited_structure_keys", []))
            self.activity_visit_count = state.get("activity_visit_count", {})
            self.completed_tabs = set(state.get("completed_tabs", []))
            self.completed_scroll_tabs = set(state.get("completed_scroll_tabs", []))
            self.screenshots_taken = state.get("screenshots_taken", 0)
            fp_count = sum(len(v) for v in self.visited_fingerprints.values())
            print(f"[INFO] 恢复状态: 已截图 {self.screenshots_taken} 张, "
                  f"普通遍历已完成 {len(self.completed_tabs)} 个Tab, "
                  f"滚动遍历已完成 {len(self.completed_scroll_tabs)} 个Tab, "
                  f"已知 {fp_count} 个指纹")
        except (FileNotFoundError, json.JSONDecodeError):
            print("[INFO] 无可恢复状态，从头开始")

    def _save_state(self):
        """保存当前遍历状态到 state.json"""
        state = {
            "visited_fingerprints": self.visited_fingerprints,
            "visited_structure_keys": list(self.visited_structure_keys),
            "activity_visit_count": self.activity_visit_count,
            "completed_tabs": list(self.completed_tabs),
            "completed_scroll_tabs": list(self.completed_scroll_tabs),
            "screenshots_taken": self.screenshots_taken,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[WARN] 状态保存失败: {e}")

    def run(self) -> int:
        """执行 DFS 遍历"""
        print("[INFO] 开始 DFS 遍历...")

        try:
            dismissed = popup_handler.dismiss_popups(self.poco)
            if dismissed:
                print(f"[INFO] 关闭了 {dismissed} 个弹窗")

            self._ensure_on_main_page()

            tab_count = self._count_valid_tabs()

            if tab_count > 0:
                print(f"[INFO] 发现 {tab_count} 个有效 Tab\n")

                print("[INFO] === 第一阶段: 普通DFS遍历（不滚动） ===\n")
                for i in range(tab_count):
                    if self.screenshots_taken >= MAX_SCREENSHOTS:
                        break
                    if i in self.completed_tabs:
                        print(f"[INFO] Tab {i} 普通遍历已完成，跳过")
                        continue
                    completed = self._explore_tab_by_order(i, scroll_mode=False)
                    if completed:
                        self.completed_tabs.add(i)
                    else:
                        print(f"[WARN] Tab {i} 普通遍历未完成，保留为待重试")
                    self._save_state()

                normal_complete = all(i in self.completed_tabs for i in range(tab_count))
                if not normal_complete:
                    pending = [i for i in range(tab_count) if i not in self.completed_tabs]
                    print(
                        f"\n[WARN] 普通遍历尚未全部完成，暂不启动滚动阶段: "
                        f"pending_tabs={pending}"
                    )
                elif self.screenshots_taken < MAX_SCREENSHOTS:
                    print("\n[INFO] === 第二阶段: 从Tab 0开始滚动DFS遍历 ===\n")
                    self.scroll_explored_fingerprints = {}
                    for i in range(tab_count):
                        if self.screenshots_taken >= MAX_SCREENSHOTS:
                            break
                        if i in self.completed_scroll_tabs:
                            print(f"[INFO] Tab {i} 滚动遍历已完成，跳过")
                            continue
                        completed = self._explore_tab_by_order(i, scroll_mode=True)
                        if completed:
                            self.completed_scroll_tabs.add(i)
                        else:
                            print(f"[WARN] Tab {i} 滚动遍历未完成，保留为待重试")
                        self._save_state()
            else:
                print("[INFO] 未找到 Tab，先普通遍历当前页\n")
                self._ensure_on_main_page()
                self._explore_page(depth=0, scroll_mode=False)
                if self.screenshots_taken < MAX_SCREENSHOTS:
                    print("\n[INFO] === 普通遍历完成，重新回到首页开始滚动 ===\n")
                    self._ensure_on_main_page()
                    self.scroll_explored_fingerprints = {}
                    self._explore_page(depth=0, scroll_mode=True)
        except KeyboardInterrupt:
            print("\n[INFO] 用户中断，保存状态...")
        finally:
            self._save_state()

        print(f"\n[DONE] DFS 遍历完成，共截图 {self.screenshots_taken} 张")
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

    def _explore_tab_by_order(self, order: int, scroll_mode: bool = False):
        """
        按顺序点击第 order 个有效Tab（跳过发布按钮）。
        每次从首页重新查找Tab列表，避免引用失效。
        返回该Tab是否完成或按规则跳过。
        """
        self._ensure_on_main_page()
        time.sleep(1.5)  # 等待首页Tab栏完全加载

        tabs = self._find_tabs()
        if not tabs:
            # 再等一次
            time.sleep(1.5)
            tabs = self._find_tabs()
        if not tabs:
            print(f"[WARN] 找不到Tab栏，跳过")
            return False

        # 找到第 order 个非发布按钮的Tab
        valid_idx = -1
        for i, tab in enumerate(tabs):
            if self._is_publish_button(tab):
                continue
            valid_idx += 1
            if valid_idx == order:
                # 点击这个Tab
                phase = "滚动" if scroll_mode else "普通"
                print(
                    f"[INFO] === {phase}遍历 Tab {order} "
                    f"(原始位置{i}) 开始 ==="
                )
                try:
                    tab.click()
                except Exception:
                    print(f"[WARN] Tab点击失败，跳过")
                    return False

                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)

                # 检查是否为功能页或个人页
                activity = metadata.get_current_activity(self.serial)
                if not activity or PACKAGE_NAME not in activity:
                    return False
                if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
                    print(f"  [SKIP] 功能页: {activity.split('/')[-1]}")
                    return True
                if SKIP_PERSONAL_PAGES:
                    hierarchy = self._dump_hierarchy()
                    if hierarchy and privacy.is_personal_page(hierarchy, activity):
                        print(f"  [SKIP] 个人页: {activity.split('/')[-1]}")
                        return True

                # 探索这个Tab页面
                self._explore_page(depth=0, scroll_mode=scroll_mode)
                return True

        print(f"[WARN] 未找到顺序为 {order} 的有效Tab")
        return False

    # ------------------------------------------------------------------
    # 深度探索核心
    # ------------------------------------------------------------------

    def _explore_page(self, depth: int, scroll_mode: bool = False):
        """
        深度探索当前页面:
        1. 截图当前页
        2. 普通阶段只收集当前屏节点；滚动阶段截取分段图并收集多屏节点
        3. 逐个点击: 新Activity → 截图 → 立即递归探索 → 返回父页面
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

        if scroll_mode:
            # 滚动阶段会重新进入普通阶段已知页面，单独的指纹集用来防止循环。
            scroll_hierarchy = self._dump_hierarchy()
            if not scroll_hierarchy:
                return
            scroll_fp = fingerprint.generate(scroll_hierarchy, current_activity)
            if fingerprint.find_similar(scroll_fp, self.scroll_explored_fingerprints):
                return
            fingerprint.add_fingerprint(scroll_fp, self.scroll_explored_fingerprints)

        # 截图当前页
        self._try_screenshot(current_activity, depth)

        if scroll_mode:
            # 只有第二阶段才执行滚动分段截图和多屏动作收集。
            self._capture_scroll_segments(current_activity, depth)
            actions = self._collect_all_actions(current_activity)
        else:
            actions = self._get_actions()
        if not actions:
            return

        phase = "SCROLL" if scroll_mode else "NORMAL"
        print(
            f"  [DFS-{phase}] depth={depth} "
            f"{current_activity.split('/')[-1]} 节点={len(actions)}"
        )

        skipped = 0

        for idx, action in enumerate(actions):
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                break

            if not self._click(action):
                continue

            # 快速检查是否跳转（短等待）
            time.sleep(0.4)
            new_activity = metadata.get_current_activity(self.serial)

            # 没跳转 → 继续下一个（最快路径，不做任何额外操作）
            if not new_activity or new_activity == current_activity:
                continue

            # 跳出App（广告等） → 等待回来或强制拉回
            if PACKAGE_NAME not in new_activity:
                # 等一下看是否自动回来
                time.sleep(1.5)
                now = metadata.get_current_activity(self.serial)
                if not now or PACKAGE_NAME not in now:
                    # 还在外部App，按返回键尝试回来
                    for _ in range(3):
                        self._go_back()
                        time.sleep(BACK_WAIT)
                        now = metadata.get_current_activity(self.serial)
                        if now and PACKAGE_NAME in now:
                            break
                # 如果还是在外面，强制拉回
                if not now or PACKAGE_NAME not in now:
                    self._restart_app()
                    time.sleep(1)
                # 回到App了，但可能不在原页面，尝试回到current_activity
                if not self._go_back_to_activity(current_activity):
                    # 回不到原页面，但还在App内，继续点下一个节点
                    continue
                continue

            # 跳到功能页 → 快速返回
            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                skipped += 1
                self._go_back()
                time.sleep(BACK_WAIT)
                if metadata.get_current_activity(self.serial) != current_activity:
                    if not self._go_back_to_activity(current_activity):
                        return
                continue

            # 跳回首页Activity → 不处理（防止深入探索时无限循环）
            if self._is_main_activity(new_activity):
                skipped += 1
                self._go_back()
                time.sleep(BACK_WAIT)
                continue

            # 该Activity已访问过太多次 → 快速返回（不等待、不dump）
            visit_count = self.activity_visit_count.get(new_activity, 0)
            if not scroll_mode and visit_count >= self.max_visits_per_activity:
                skipped += 1
                self._go_back()
                time.sleep(BACK_WAIT)
                if metadata.get_current_activity(self.serial) != current_activity:
                    if not self._go_back_to_activity(current_activity):
                        return
                continue

            # === 到了新页面 ===
            # 计数+1
            if not scroll_mode:
                self.activity_visit_count[new_activity] = visit_count + 1

            time.sleep(PAGE_LOAD_WAIT - 0.4)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            hierarchy = self._dump_hierarchy()
            if not hierarchy:
                self._go_back_to_activity(current_activity)
                continue

            # 快速预判：同Activity+同第1层结构 → 一定是同模板，跳过
            struct_key = fingerprint.quick_structure_key(hierarchy, new_activity)
            if struct_key and struct_key in self.visited_structure_keys:
                skipped += 1
                if scroll_mode and depth + 1 <= MAX_DEPTH:
                    self._explore_page(depth + 1, scroll_mode=True)
                self._go_back_to_activity(current_activity)
                continue

            # dHash精确判断
            fp = fingerprint.generate(hierarchy, new_activity)
            if fingerprint.find_similar(fp, self.visited_fingerprints):
                skipped += 1
                self.visited_structure_keys.add(struct_key)
                if scroll_mode and depth + 1 <= MAX_DEPTH:
                    self._explore_page(depth + 1, scroll_mode=True)
                self._go_back_to_activity(current_activity)
                continue

            # 真正的新页面！
            self.visited_structure_keys.add(struct_key)
            print(
                f"  [DFS] depth={depth} 节点{idx} -> "
                f"{new_activity.split('/')[-1]}"
            )

            # 截图（指纹已经算过了，直接用）
            took = self._do_screenshot(new_activity, depth + 1, hierarchy, fp)
            if took and depth + 1 <= MAX_DEPTH and self.screenshots_taken < MAX_SCREENSHOTS:
                print(
                    f"  [DFS] 深入: depth={depth + 1} "
                    f"{new_activity.split('/')[-1]}"
                )
                self._explore_page(depth + 1, scroll_mode=scroll_mode)

            # 子分支完成后必须恢复父页面，才能继续下一个动作。
            if not self._go_back_to_activity(current_activity):
                print(
                    f"[WARN] DFS无法恢复父页面，终止当前分支: "
                    f"depth={depth}, target={current_activity}"
                )
                return

        if skipped > 0:
            print(f"  [DFS] 快速跳过 {skipped} 个已知节点")

    # ------------------------------------------------------------------
    # 截图
    # ------------------------------------------------------------------

    def _is_main_activity(self, activity: str) -> bool:
        """判断是否为App首页Activity"""
        if not activity:
            return False
        return "MainActivityV2" in activity and PACKAGE_NAME in activity

    def _main_activity(self) -> str:
        """返回App的主Activity全名（用于精确比较）"""
        return f"{PACKAGE_NAME}/.MainActivityV2"

    def _try_screenshot(self, activity: str, depth: int) -> bool:
        """截图当前页面，布局指纹去重。"""
        hierarchy = self._dump_hierarchy()
        if not hierarchy:
            return False

        if SKIP_PERSONAL_PAGES and privacy.is_personal_page(hierarchy, activity):
            return False

        fp = fingerprint.generate(hierarchy, activity)
        if fingerprint.find_similar(fp, self.visited_fingerprints):
            return False

        return self._do_screenshot(activity, depth, hierarchy, fp)

    def _do_screenshot(self, activity: str, depth: int, hierarchy: dict, fp: str) -> bool:
        """执行截图（指纹已计算好）"""
        fingerprint.add_fingerprint(fp, self.visited_fingerprints)

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
        """确保在App首页（MainActivityV2），不是随便一个App内页面"""
        activity = metadata.get_current_activity(self.serial)
        if self._is_main_activity(activity):
            return

        # 不在首页：尝试按返回键回去（最多5次）
        if activity and PACKAGE_NAME in activity:
            for _ in range(5):
                self._go_back()
                time.sleep(BACK_WAIT)
                activity = metadata.get_current_activity(self.serial)
                if self._is_main_activity(activity):
                    return
                if not activity or PACKAGE_NAME not in activity:
                    break

        # 返回键搞不定，直接重启
        self._restart_app()
        time.sleep(1)

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

    def _collect_all_actions(self, current_activity: str) -> list:
        """
        滚动多屏收集所有可点击节点。
        每屏收集后向下滚动，最多收集3屏，最后回到顶部。
        用坐标去重（同位置节点不重复添加）。
        """
        all_action_ids = set()
        all_actions = []

        # 收集第一屏
        actions = self._get_actions()
        for a in actions:
            all_action_ids.add(a["id"])
            all_actions.append(a)

        # 滚动收集更多屏（最多再滚2次）
        for _ in range(2):
            self._swipe(0.7, 0.3)
            time.sleep(0.5)

            # 确认没跳转
            now = metadata.get_current_activity(self.serial)
            if now != current_activity:
                break

            new_actions = self._get_actions()
            for a in new_actions:
                if a["id"] not in all_action_ids:
                    all_action_ids.add(a["id"])
                    all_actions.append(a)

        # 回到顶部
        self._scroll_to_top()
        time.sleep(0.3)

        return all_actions

    def _get_actions(self) -> list:
        """
        获取当前页面所有可点击控件，按优先级排序。
        同一卡片内的多个子元素（y坐标相近）只保留1个，避免重复进出。
        """
        raw_actions = []
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
                raw_actions.append({
                    "id": action_id,
                    "node": node,
                    "priority": priority,
                    "x": pos[0],
                    "y": pos[1],
                })
            except Exception:
                continue

        raw_actions.sort(key=lambda x: x["priority"], reverse=True)

        # 按位置合并同卡片节点：
        # 两个节点的x和y坐标都很接近（同一块区域）时，只保留优先级最高的那个
        # 阈值: y差<0.025 且 x差<0.15 视为同一张卡片内的元素
        actions = []
        used_positions = []  # 已选中节点的(x, y)
        for action in raw_actions:
            y = action["y"]
            x = action.get("x", 0.5)
            is_duplicate = False
            for ux, uy in used_positions:
                if abs(y - uy) < 0.025 and abs(x - ux) < 0.15:
                    is_duplicate = True
                    break
            if is_duplicate:
                continue
            used_positions.append((x, y))
            actions.append(action)

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
