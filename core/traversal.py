"""BFS遍历引擎 — 进入即探索

策略:
  - 每个Tab收集所有可点击节点, 逐个点击
  - 发现新页面: 截图 + 立即在子页面上做一轮广度探索 + 返回
  - 子页面的探索只做1层(只截图不再递归)
  - 回退只按1次返回键, 回不来则跳过继续下一个
  - dHash指纹去重, 同模板不重复截图也不重复进入
"""

import json
import subprocess
import time
from datetime import datetime

from core import fingerprint, popup_handler, screenshot, metadata, privacy
from core.adb_bin import ADB
from config import (
    PACKAGE_NAME,
    MAIN_ACTIVITY_KEYWORD,
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

# 同一Activity连续命中已知模板后不再等待加载直接返回
MAX_CONSECUTIVE_KNOWN_SAME = 3

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

                # 每个Tab: 广度探索(子页面进入即探索)
                for i in range(tab_count):
                    if self.screenshots_taken >= MAX_SCREENSHOTS:
                        break
                    if i in self.completed_tabs:
                        continue
                    self._run_tab(i)
                    self.completed_tabs.add(i)
                    self._save_state()

                # 滚动后再探索一轮
                if self.screenshots_taken < MAX_SCREENSHOTS:
                    print(f"\n[INFO] 滚动探索\n")
                    for i in range(tab_count):
                        if self.screenshots_taken >= MAX_SCREENSHOTS:
                            break
                        self._scroll_tab(i)
                    self._save_state()
            else:
                self._ensure_on_main_page()
                self._explore_page(can_recurse=True)
        except KeyboardInterrupt:
            print("\n[INFO] 中断, 保存状态...")
        finally:
            self._save_state()
            elapsed = time.monotonic() - self.run_started
            m, s = divmod(elapsed, 60)
            print(f"\n[DONE] 截图 {self.screenshots_taken} 张, 用时 {int(m)}分{s:.0f}秒")

        return self.screenshots_taken

    # ------------------------------------------------------------------
    # Tab
    # ------------------------------------------------------------------

    def _run_tab(self, order):
        try:
            self._ensure_on_main_page()
            time.sleep(1.0)
            if not self._switch_tab(order):
                return
            print(f"[TAB {order}] 开始")
            self._explore_page(can_recurse=True)
        except Exception as e:
            print(f"[TAB {order}] 异常: {e}")

    def _scroll_tab(self, order):
        try:
            self._ensure_on_main_page()
            time.sleep(0.5)
            if not self._switch_tab(order):
                return
            for _ in range(3):
                if self.screenshots_taken >= MAX_SCREENSHOTS:
                    return
                self._swipe(0.7, 0.3)
                time.sleep(0.6)
                activity = metadata.get_current_activity(self.serial)
                if not activity or PACKAGE_NAME not in activity:
                    break
                self._explore_page(can_recurse=True)
        except Exception:
            pass

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
    # 核心: 探索当前页面
    # ------------------------------------------------------------------

    def _explore_page(self, can_recurse=False):
        """
        探索当前页面的所有可点击节点.

        can_recurse=True:  发现新页面后进入子页面做一轮探索(不再递归)
        can_recurse=False: 发现新页面只截图, 不进入探索(防止无限递归)
        """
        activity = metadata.get_current_activity(self.serial)
        if not activity or PACKAGE_NAME not in activity:
            return
        if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return

        # 截图当前页
        self._try_screenshot(activity)

        # 收集所有可点击节点
        actions = self._get_actions()
        if not actions:
            return

        # 按Activity记录连续已知次数
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

            # 该Activity连续多次已知, 快速跳过不等加载
            if known_counts.get(new_activity, 0) >= MAX_CONSECUTIVE_KNOWN_SAME:
                self._go_back()
                time.sleep(BACK_WAIT)
                continue

            # 等待页面加载
            time.sleep(PAGE_LOAD_WAIT - 0.4)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)

            # dHash判重
            if self._is_known_page(new_activity):
                known_counts[new_activity] = known_counts.get(new_activity, 0) + 1
                self._go_back()
                time.sleep(BACK_WAIT)
                continue

            # 新模板! 截图
            known_counts[new_activity] = 0
            self._try_screenshot(new_activity)

            # 如果允许递归, 在子页面上做一轮广度探索(不再递归)
            if can_recurse:
                self._explore_page(can_recurse=False)

            # 返回父页面
            self._go_back()
            time.sleep(BACK_WAIT)
            now = metadata.get_current_activity(self.serial)
            if now != activity:
                if not self._back_to(activity):
                    # 回不来, 跳过剩余节点
                    break

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

            fp = fingerprint.generate(hierarchy, activity)
            if fingerprint.find_similar(fp, self.visited_fingerprints):
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

            fp = fingerprint.generate(hierarchy, activity)
            if fingerprint.find_similar(fp, self.visited_fingerprints):
                return False

            # 新模板
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
                print(f"  [{self.screenshots_taken}] {activity.split('/')[-1]} ({elapsed:.0f}s)")
                return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _back_to(self, target):
        for _ in range(3):
            now = metadata.get_current_activity(self.serial)
            if now == target:
                return True
            if not now or PACKAGE_NAME not in now:
                self._ensure_on_main_page()
                return metadata.get_current_activity(self.serial) == target
            self._go_back()
            time.sleep(BACK_WAIT)
        return metadata.get_current_activity(self.serial) == target

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
        return bool(activity) and MAIN_ACTIVITY_KEYWORD in activity and PACKAGE_NAME in activity

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
                       ["+", "发布", "拍摄", "publish", "create", "CenterPlus", "投稿", "post"])
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 控件收集
    # ------------------------------------------------------------------

    def _get_actions(self):
        raw = []
        seen = set()

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

                    aid = self._action_id(ntype, name, text, pos)
                    if aid in seen:
                        continue
                    seen.add(aid)

                    priority = self._priority(ntype, name, text)
                    raw.append({
                        "id": aid,
                        "node": node,
                        "priority": priority,
                        "x": pos[0],
                        "y": pos[1],
                    })
                except Exception:
                    continue
        except Exception:
            return []

        raw.sort(key=lambda a: a["priority"], reverse=True)

        # 卡片合并: 只对低优先级节点(列表项)做位置去重
        # 高优先级节点(导航/搜索/频道入口)不合并, 全部保留
        result = []
        used = []
        for a in raw:
            if a["priority"] < 60:
                dup = False
                for ux, uy in used:
                    if abs(a["y"] - uy) < 0.02 and abs(a["x"] - ux) < 0.1:
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

    def _swipe(self, y1, y2):
        cx = self.screen_w // 2
        try:
            subprocess.run(
                [ADB, "-s", self.serial, "shell", "input", "swipe",
                 str(cx), str(int(self.screen_h * y1)),
                 str(cx), str(int(self.screen_h * y2)), "400"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

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
