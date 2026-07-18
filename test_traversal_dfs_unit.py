"""TraversalEngine DFS 顺序的离线回归测试。"""

import unittest
from unittest.mock import Mock, patch

from core.traversal import TraversalEngine


class TraversalDfsTests(unittest.TestCase):
    def test_new_child_is_explored_before_parent_sibling(self):
        engine = TraversalEngine.__new__(TraversalEngine)
        engine.poco = object()
        engine.serial = "test-serial"
        engine.screenshots_taken = 0
        engine.visited_structure_keys = set()
        engine.visited_fingerprints = {}
        engine.activity_visit_count = {}
        engine.max_visits_per_activity = 3

        activity_a = "tv.danmaku.bili/ActivityA"
        activity_b = "tv.danmaku.bili/ActivityB"
        activity_c = "tv.danmaku.bili/ActivityC"
        activity_d = "tv.danmaku.bili/ActivityD"
        current = {"activity": activity_a}
        events = []

        actions = {
            activity_a: [
                {"id": "to-b", "target": activity_b},
                {"id": "to-c", "target": activity_c},
            ],
            activity_b: [{"id": "to-d", "target": activity_d}],
            activity_c: [],
            activity_d: [],
        }

        engine._try_screenshot = lambda _activity, _depth: False
        engine._capture_scroll_segments = Mock()
        engine._collect_all_actions = Mock(
            side_effect=AssertionError("普通遍历不应收集滚动多屏动作")
        )
        engine._get_actions = lambda: list(actions[current["activity"]])
        engine._dump_hierarchy = lambda: {"payload": {}, "children": []}

        def click(action):
            target = action["target"]
            events.append(f"click:{target}")
            current["activity"] = target
            return True

        def capture(activity, _depth, _hierarchy, _fp):
            events.append(f"shot:{activity}")
            engine.screenshots_taken += 1
            return True

        def back_to(activity):
            events.append(f"back:{activity}")
            current["activity"] = activity
            return True

        engine._click = click
        engine._do_screenshot = capture
        engine._go_back_to_activity = back_to

        with (
            patch(
                "core.traversal.metadata.get_current_activity",
                side_effect=lambda _serial: current["activity"],
            ),
            patch("core.traversal.time.sleep"),
            patch("core.traversal.popup_handler.dismiss_popups"),
            patch(
                "core.traversal.fingerprint.quick_structure_key",
                side_effect=lambda _hierarchy, activity: f"structure:{activity}",
            ),
            patch(
                "core.traversal.fingerprint.generate",
                side_effect=lambda _hierarchy, activity: f"{activity}|hash",
            ),
            patch("core.traversal.fingerprint.find_similar", return_value=False),
        ):
            engine._explore_page(depth=0)

        # 严格DFS：先完成 B -> D 分支，才能回到 A 点击兄弟 C。
        self.assertLess(
            events.index(f"click:{activity_d}"),
            events.index(f"click:{activity_c}"),
        )
        self.assertLess(
            events.index(f"back:{activity_a}"),
            events.index(f"click:{activity_c}"),
        )
        engine._capture_scroll_segments.assert_not_called()
        engine._collect_all_actions.assert_not_called()

    def test_run_finishes_normal_tabs_before_scroll_tabs(self):
        engine = TraversalEngine.__new__(TraversalEngine)
        engine.poco = object()
        engine.screenshots_taken = 0
        engine.completed_tabs = set()
        engine.completed_scroll_tabs = set()
        engine.scroll_explored_fingerprints = {}
        events = []

        engine._ensure_on_main_page = Mock()
        engine._count_valid_tabs = Mock(return_value=2)
        engine._save_state = Mock()
        engine._explore_tab_by_order = Mock(
            side_effect=lambda order, scroll_mode=False: (
                events.append(("scroll" if scroll_mode else "normal", order))
                or True
            )
        )

        with patch("core.traversal.popup_handler.dismiss_popups", return_value=0):
            engine.run()

        self.assertEqual(
            [
                ("normal", 0),
                ("normal", 1),
                ("scroll", 0),
                ("scroll", 1),
            ],
            events,
        )

    def test_scroll_phase_enables_segments_and_multi_screen_actions(self):
        engine = TraversalEngine.__new__(TraversalEngine)
        engine.serial = "test-serial"
        engine.screenshots_taken = 0
        engine.scroll_explored_fingerprints = {}
        engine._dump_hierarchy = Mock(
            return_value={"payload": {}, "children": []}
        )
        engine._try_screenshot = Mock(return_value=False)
        engine._capture_scroll_segments = Mock()
        engine._collect_all_actions = Mock(return_value=[])
        engine._get_actions = Mock(
            side_effect=AssertionError("滚动阶段应收集多屏动作")
        )

        with patch(
            "core.traversal.metadata.get_current_activity",
            return_value="tv.danmaku.bili/ActivityA",
        ):
            engine._explore_page(depth=0, scroll_mode=True)

        engine._capture_scroll_segments.assert_called_once()
        engine._collect_all_actions.assert_called_once()
        engine._get_actions.assert_not_called()

    def test_incomplete_normal_tab_defers_scroll_phase(self):
        engine = TraversalEngine.__new__(TraversalEngine)
        engine.poco = object()
        engine.screenshots_taken = 0
        engine.completed_tabs = set()
        engine.completed_scroll_tabs = set()
        engine.scroll_explored_fingerprints = {}
        events = []

        engine._ensure_on_main_page = Mock()
        engine._count_valid_tabs = Mock(return_value=2)
        engine._save_state = Mock()

        def explore(order, scroll_mode=False):
            events.append(("scroll" if scroll_mode else "normal", order))
            return order == 0

        engine._explore_tab_by_order = Mock(side_effect=explore)

        with patch("core.traversal.popup_handler.dismiss_popups", return_value=0):
            engine.run()

        self.assertEqual([("normal", 0), ("normal", 1)], events)
        self.assertEqual({0}, engine.completed_tabs)
        self.assertEqual(set(), engine.completed_scroll_tabs)


if __name__ == "__main__":
    unittest.main()
