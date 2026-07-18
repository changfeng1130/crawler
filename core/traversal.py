"""BFS遍历引擎 + 动态深度

策略:
  - BFS广度优先: 先把当前页面所有节点点完, 再处理子页面
  - 动态深度: 有新模板就继续深入, 同一Activity连续碰到已知才停止
  - 回退只按1次返回键, 失败则重启App
  - 指纹去重: 结构key + dHash
  - 队列任务记录Tab+action, 可从首页重放到达子页面
"""

import json
import subprocess
import time
from collections import deque
from datetime import datetime

from core import fingerprint, popup_handler, screenshot, metadata, privacy
from core.adb_bin import ADB
from config import (
    PACKAGE_NAME,
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

# 同一Activity连续多少次已知后停止(不同Activity不累计)
MAX_CONSECUTIVE_KNOWN_SAME_ACTIVITY = 3

LIST_CONTAINERS = {"RecyclerView", "ListView", "GridView", "ViewPager2"}
TAB_ID_KEYWORDS = ("tab", "bottom_nav", "navigation")


class TraversalEngine:

    def __init__(self, poco, serial, device_info, app_info, resume=False):
        self.poco = poco
        self.serial = serial
        self.device_info = device_info
        self.app_info = app_info

        self.visited_fingerprints = {}
        self.visited_structure_keys = set()
        self.completed_tabs = set()
        self.screenshots_taken = 0
        self.run_started = time.monotonic()

        if resume:
            self._load_state()

        self.screen_w, self.screen_h = self._parse_resolution(
            device_info.get("screen_resolution", "")
        )

    @staticmethod
    def _parse_resolution(res):
        try:
            w, h = res.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 1080, 2400

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.visited_fingerprints = state.get("visited_fingerprints", {})
            self.visited_structure_keys = set(state.get("visited_structure_keys", []))
            self.completed_tabs = set(state.get("completed_tabs", []))
            self.screenshots_taken = state.get("screenshots_taken", 0)
            fp_count = sum(len(v) for v in self.visited_fingerprints.values())
            print(f"[INFO] 恢复: {self.screenshots_taken}张, "
                  f"{len(self.completed_tabs)}Tab完成, {fp_count}指纹")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_state(self):
        state = {
            "visited_fingerprints": self.visited_fingerprints,
            "visited_structure_keys": list(self.visited_structure_keys),
            "completed_tabs": list(self.completed_tabs),
            "screenshots_taken": self.screenshots_taken,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self):
        print("[INFO] 开始遍历...")
        try:
            popup_handler.dismiss_popups(self.poco)
            self._ensure_on_main_page()

            tab_count = self._count_valid_tabs()
            if tab_count > 0:
                print(f"[INFO] 有效Tab: {tab_count}\n")
                # 第一轮: 每个Tab广度探索, 发现的子页面放入全局队列
                queue = deque()
                for i in range(tab_count):
                    if self.screenshots_taken >= MAX_SCREENSHOTS:
                        break
                    if i in self.completed_tabs:
                        continue
                    self._run_tab(i, queue)
                    self.completed_tabs.add(i)
                    self._save_state()

                # 第二轮: BFS处理队列中的子页面
                if queue:
                    print(f"\n[INFO] 第二轮: 探索 {len(queue)} 个子页面\n")
                self._process_queue(queue)
            else:
                self._ensure_on_main_page()
                queue = deque()
                self._explore_current(None, queue)
                self._process_queue(queue)
        except KeyboardInterrupt:
            print("\n[INFO] 中断, 保存状态...")
        finally:
            self._save_state()
            elapsed = time.monotonic() - self.run_started
            m, s = divmod(elapsed, 60)
            print(f"\n[DONE] 截图 {self.screenshots_taken} 张, "
                  f"用时 {int(m)}分{s:.0f}秒")

        return self.screenshots_taken

    # ------------------------------------------------------------------
    # Tab
    # ------------------------------------------------------------------

    def _run_tab(self, order, queue):
        try:
            self._ensure_on_main_page()
            time.sleep(1.0)
            if not self._switch_tab(order):
                return
            print(f"[TAB {order}] 开始")
            self._explore_current(order, queue)
        except Exception as e:
            print(f"[TAB {order}] 异常: {e}")

    def _switch_tab(self, order):
        tabs = self._find_tabs()
        valid = [t for t in tabs if not self._is_publish_button(t)]
        if order >= len(valid):
            return False
        try:
            valid[order].click()
            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)
            activity = metadata.get_current_activity(self.serial)
            if not activity or PACKAGE_NAME not in activity:
                return False
            if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
                return False
            if SKIP_PERSONAL_PAGES:
                h = self._dump_hierarchy()
                if h and privacy.is_personal_page(h, activity):
                    return False
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # BFS核心
    # ------------------------------------------------------------------

    def _explore_current(self, tab_order, queue):
        """
        探索当前页面所有节点.
        新模板截图并加入queue, 已知模板跳过.
        同一Activity连续3次已知则停止该类Activity的后续节点.
        """
        activity = metadata.get_current_activity(self.serial)
        if not activity or PACKAGE_NAME not in activity:
            return
        if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return

        # 截图当前页
        self._try_screenshot(activity)

        actions = self._get_actions()
        if not actions:
            return

        # 记录每个目标Activity连续已知次数
        known_counts = {}

        for action in actions:
            if self.screenshots_taken >= MAX_SCREENSHOTS:
                break

            if not self._click(action):
                continue

            time.sleep(0.4)
            new_activity = metadata.get_current_activity(self.serial)

            # 没跳转
            if not new_activity or new_activity == activity:
                continue

            # 跳出App
            if PACKAGE_NAME not in new_activity:
                self._handle_left_app()
                now = metadata.get_current_activity(self.serial)
                if now != activity:
                    if not self._back_to(activity):
                        break
                continue

            # 功能页/首页
            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS) or \
               self._is_main_activity(new_activity):
                self._go_back()
                time.sleep(BACK_WAIT)
                continue

            # 该Activity已连续多次跳到已知模板, 不再等待加载直接返回
            if known_counts.get(new_activity, 0) >= MAX_CONSECUTIVE_KNOWN_SAME_ACTIVITY:
                self._go_back()
                time.sleep(BACK_WAIT)
                continue

            # 等待加载
            time.sleep(PAGE_LOAD_WAIT - 0.4)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            # 判重
            if self._is_known_page(new_activity):
                known_counts[new_activity] = known_counts.get(new_activity, 0) + 1
                self._go_back()
                time.sleep(BACK_WAIT)
                continue

            # 新模板! 截图
            known_counts[new_activity] = 0
            self._try_screenshot(new_activity)
            # 加入队列(记录tab和action用于后续重放)
            queue.append({"tab": tab_order, "action": action})

            # 返回当前页
            self._go_back()
            time.sleep(BACK_WAIT)
            now = metadata.get_current_activity(self.serial)
            if now != activity:
                if not self._back_to(activity):
                    break

    def _process_queue(self, queue):
        """BFS处理队列: 逐个进入子页面探索"""
        while queue and self.screenshots_taken < MAX_SCREENSHOTS:
            task = queue.popleft()
            tab_order = task["tab"]
            action = task["action"]

            # 导航: 回首页 -> 切Tab -> 点击节点进入子页面
            if not self._navigate_to_child(tab_order, action):
                continue

            # 在子页面上继续广度探索
            self._explore_current(tab_order, queue)

            # 返回
            self._go_back()
            time.sleep(BACK_WAIT)

    def _navigate_to_child(self, tab_order, action):
        """导航到子页面: 回首页 -> 切Tab -> 点击action"""
        try:
            self._ensure_on_main_page()
            time.sleep(0.5)

            if tab_order is not None:
                if not self._switch_tab(tab_order):
                    return False

            # 重新点击
            return self._replay_click(action)
        except Exception:
            return False

    def _replay_click(self, action):
        """重新点击节点(先尝试直接引用, 再用id查找)"""
        try:
            if self._click(action):
                time.sleep(PAGE_LOAD_WAIT)
                popup_handler.dismiss_popups(self.poco, max_attempts=2)
                return True
        except Exception:
            pass

        # 引用失效, 用id重新查找
        try:
            action_id = action.get("id", "")
            actions = self._get_actions()
            for a in actions:
                if a["id"] == action_id:
                    if self._click(a):
                        time.sleep(PAGE_LOAD_WAIT)
                        popup_handler.dismiss_popups(self.poco, max_attempts=2)
                        return True
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------
    # 判重与截图
    # ------------------------------------------------------------------

    def _is_known_page(self, activity):
        try:
            hierarchy = self._dump_hierarchy()
            if not hierarchy:
                return True

            if SKIP_PERSONAL_PAGES and not self._is_main_activity(activity):
                if privacy.is_personal_page(hierarchy, activity):
                    return True

            struct_key = fingerprint.quick_structure_key(hierarchy, activity)
            if struct_key and struct_key in self.visited_structure_keys:
                return True

            fp = fingerprint.generate(hierarchy, activity)
            if fingerprint.find_similar(fp, self.visited_fingerprints):
                if struct_key:
                    self.visited_structure_keys.add(struct_key)
                return True

            return False
        except Exception:
            return True

    def _try_screenshot(self, activity):
        try:
            hierarchy = self._dump_hierarchy()
            if not hierarchy:
                return False

            if SKIP_PERSONAL_PAGES and not self._is_main_activity(activity):
                if privacy.is_personal_page(hierarchy, activity):
                    return False

            struct_key = fingerprint.quick_structure_key(hierarchy, activity)
            if struct_key and struct_key in self.visited_structure_keys:
                return False

            fp = fingerprint.generate(hierarchy, activity)
            if fingerprint.find_similar(fp, self.visited_fingerprints):
                if struct_key:
                    self.visited_structure_keys.add(struct_key)
                return False

            # 新模板
            if struct_key:
                self.visited_structure_keys.add(struct_key)
            fingerprint.add_fingerprint(fp, self.visited_fingerprints)

            if SKIP_BLOCKED_POPUPS and popup_handler.has_blocking_popup(self.poco):
                return False

            path = screenshot.capture(self.serial, activity, fp, segment_index=0)
            if path:
                record = metadata.build_record(
                    path, activity, fp, 0, self.device_info, self.app_info,
                    segment_index=0,
                )
                metadata.append_record(record)
                self.screenshots_taken += 1
                elapsed = time.monotonic() - self.run_started
                print(f"  [{self.screenshots_taken}] {activity.split('/')[-1]} "
                      f"({elapsed:.0f}s)")
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _back_to(self, target_activity):
        """尝试按返回键回到目标Activity"""
        for _ in range(3):
            now = metadata.get_current_activity(self.serial)
            if now == target_activity:
                return True
            if not now or PACKAGE_NAME not in now:
                self._ensure_on_main_page()
                return metadata.get_current_activity(self.serial) == target_activity
            self._go_back()
            time.sleep(BACK_WAIT)
        return metadata.get_current_activity(self.serial) == target_activity

    def _handle_left_app(self):
        try:
            time.sleep(1.5)
            now = metadata.get_current_activity(self.serial)
            if now and PACKAGE_NAME in now:
                return
            for _ in range(3):
                self._go_back()
                time.sleep(BACK_WAIT)
                now = metadata.get_current_activity(self.serial)
                if now and PACKAGE_NAME in now:
                    return
            self._restart_app()
        except Exception:
            self._restart_app()

    def _ensure_on_main_page(self):
        try:
            activity = metadata.get_current_activity(self.serial)
            if self._is_main_activity(activity):
                return
            if activity and PACKAGE_NAME in activity:
                for _ in range(5):
                    self._go_back()
                    time.sleep(BACK_WAIT)
                    activity = metadata.get_current_activity(self.serial)
                    if self._is_main_activity(activity):
                        return
                    if not activity or PACKAGE_NAME not in activity:
                        break
        except Exception:
            pass
        self._restart_app()
        time.sleep(1)

    def _is_main_activity(self, activity):
        return bool(activity) and "MainActivityV2" in activity and PACKAGE_NAME in activity

    # ------------------------------------------------------------------
    # Tab查找
    # ------------------------------------------------------------------

    def _count_valid_tabs(self):
        tabs = self._find_tabs()
        return sum(1 for t in tabs if not self._is_publish_button(t))

    def _find_tabs(self):
        patterns = ["main_tab", "tab_bar", "bottom_nav", "navigation_bar",
                    "BottomNavigationView", "RadioGroup"]
        for p in patterns:
            try:
                node = self.poco(nameMatches=f".*{p}.*")
                if node.exists():
                    children = list(node.children())
                    if len(children) >= 3:
                        return children
            except Exception:
                continue
        return self._find_bottom_nodes()

    def _find_bottom_nodes(self):
        try:
            nodes = self.poco(touchable=True)
            bottom = []
            for node in nodes:
                try:
                    pos = node.attr("pos")
                    if pos and pos[1] > 0.88:
                        bottom.append((pos[0], node))
                except Exception:
                    continue
            if len(bottom) < 3:
                return []
            bottom.sort(key=lambda t: t[0])
            return [t[1] for t in bottom]
        except Exception:
            return []

    def _is_publish_button(self, node):
        try:
            text = node.attr("text") or ""
            desc = node.attr("desc") or ""
            name = node.attr("name") or ""
            content = text + desc + name
            return any(kw in content for kw in
                       ["+", "发布", "拍摄", "publish", "create", "CenterPlus", "投稿"])
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 控件收集
    # ------------------------------------------------------------------

    def _get_actions(self):
        raw = []
        seen = set()
        list_counts = {}

        try:
            nodes = self.poco(touchable=True)
            for node in nodes:
                try:
                    text = node.attr("text") or ""
                    desc = node.attr("desc") or ""
                    ntype = node.attr("type") or ""
                    name = node.attr("name") or ""
                    pos = node.attr("pos")

                    if not pos:
                        continue
                    if any(kw in (text + desc) for kw in SKIP_TEXTS):
                        continue
                    if self._is_tab_node(name):
                        continue

                    ptype = self._parent_type(node)
                    if ptype in LIST_CONTAINERS:
                        pid = self._parent_id(node)
                        c = list_counts.get(pid, 0)
                        if c >= LIST_ITEM_MAX_CLICK:
                            continue
                        list_counts[pid] = c + 1

                    aid = self._action_id(ntype, name, text, pos)
                    if aid in seen:
                        continue
                    seen.add(aid)

                    raw.append({
                        "id": aid,
                        "node": node,
                        "priority": self._priority(ntype, name, text),
                        "x": pos[0],
                        "y": pos[1],
                    })
                except Exception:
                    continue
        except Exception:
            return []

        raw.sort(key=lambda a: a["priority"], reverse=True)

        # 同卡片合并
        result = []
        used = []
        for a in raw:
            dup = False
            for ux, uy in used:
                if abs(a["y"] - uy) < 0.025 and abs(a["x"] - ux) < 0.15:
                    dup = True
                    break
            if dup:
                continue
            used.append((a["x"], a["y"]))
            result.append(a)

        return result

    def _is_tab_node(self, name):
        if not name:
            return False
        return any(kw in name.lower() for kw in TAB_ID_KEYWORDS)

    def _click(self, action):
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

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _action_id(self, ntype, name, text, pos):
        if name and name != "None":
            return f"{ntype}:{name}"
        if text:
            return f"{ntype}:{text[:20]}"
        return f"{ntype}:@{round(pos[0]*10)/10},{round(pos[1]*10)/10}"

    def _priority(self, ntype, name, text):
        score = 0
        if any(kw in name.lower() for kw in ["menu", "drawer", "more", "setting", "search"]):
            score += 100
        if any(kw in text for kw in ["设置", "更多", "全部", "频道", "搜索", "分类"]):
            score += 80
        if ntype in ["TextView", "Button", "ImageButton"]:
            score += 40
        return score

    def _parent_type(self, node):
        try:
            return node.parent().attr("type") or ""
        except Exception:
            return ""

    def _parent_id(self, node):
        try:
            return node.parent().attr("name") or ""
        except Exception:
            return ""
