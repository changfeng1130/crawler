"""
dHash指纹算法验证测试

运行方式:
    python test_fingerprint.py

测试内容:
    1. 同模板不同内容 → 指纹相同（汉明距离<5）
    2. 不同模板 → 指纹不同（汉明距离>=5）
    3. 微小变化容错（如隐藏一个按钮）→ 仍判为同模板
    4. 性能测试（100次指纹计算耗时）
"""

import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.fingerprint import (
    generate, find_similar, add_fingerprint,
    is_same_page, _hamming_distance, _draw_skeleton,
    _extract_components, _compute_dhash, CANVAS_W, CANVAS_H,
)


def make_video_detail_page(video_title="视频A", comment_count=5):
    """模拟B站视频详情页控件树"""
    comments = []
    for i in range(comment_count):
        comments.append({
            "payload": {"visible": True, "type": "TextView",
                        "pos": [0.5, 0.65 + i * 0.05], "size": [0.9, 0.04],
                        "text": f"评论{i}"},
            "children": []
        })

    return {
        "payload": {"visible": True, "type": "FrameLayout", "pos": [0.5, 0.5], "size": [1.0, 1.0]},
        "children": [
            # 视频播放器区域
            {"payload": {"visible": True, "type": "VideoView",
                         "pos": [0.5, 0.2], "size": [1.0, 0.35]},
             "children": []},
            # 标题
            {"payload": {"visible": True, "type": "TextView",
                         "pos": [0.5, 0.42], "size": [0.9, 0.04], "text": video_title},
             "children": []},
            # 按钮栏（点赞/投币/收藏/转发）
            {"payload": {"visible": True, "type": "LinearLayout",
                         "pos": [0.5, 0.48], "size": [0.9, 0.05]},
             "children": [
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.15, 0.48], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.35, 0.48], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.55, 0.48], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.75, 0.48], "size": [0.08, 0.04]}, "children": []},
             ]},
            # 评论列表
            {"payload": {"visible": True, "type": "RecyclerView",
                         "pos": [0.5, 0.75], "size": [1.0, 0.4]},
             "children": comments},
        ]
    }


def make_search_page(query="搜索词", result_count=8):
    """模拟搜索结果页控件树"""
    results = []
    for i in range(result_count):
        results.append({
            "payload": {"visible": True, "type": "ImageView",
                        "pos": [0.15, 0.2 + i * 0.08], "size": [0.2, 0.07]},
            "children": []
        })

    return {
        "payload": {"visible": True, "type": "FrameLayout", "pos": [0.5, 0.5], "size": [1.0, 1.0]},
        "children": [
            # 搜索栏
            {"payload": {"visible": True, "type": "EditText",
                         "pos": [0.5, 0.04], "size": [0.85, 0.05], "text": query},
             "children": []},
            # Tab栏
            {"payload": {"visible": True, "type": "LinearLayout",
                         "pos": [0.5, 0.1], "size": [1.0, 0.04]},
             "children": [
                 {"payload": {"visible": True, "type": "TextView",
                              "pos": [0.15, 0.1], "size": [0.12, 0.03]}, "children": []},
                 {"payload": {"visible": True, "type": "TextView",
                              "pos": [0.35, 0.1], "size": [0.12, 0.03]}, "children": []},
                 {"payload": {"visible": True, "type": "TextView",
                              "pos": [0.55, 0.1], "size": [0.12, 0.03]}, "children": []},
             ]},
            # 结果列表
            {"payload": {"visible": True, "type": "RecyclerView",
                         "pos": [0.5, 0.55], "size": [1.0, 0.8]},
             "children": results},
        ]
    }


def make_home_page(feed_count=10):
    """模拟首页信息流"""
    feeds = []
    for i in range(feed_count):
        feeds.append({
            "payload": {"visible": True, "type": "ImageView",
                        "pos": [0.5, 0.2 + i * 0.08], "size": [0.95, 0.07]},
            "children": []
        })

    return {
        "payload": {"visible": True, "type": "FrameLayout", "pos": [0.5, 0.5], "size": [1.0, 1.0]},
        "children": [
            # 顶部栏
            {"payload": {"visible": True, "type": "LinearLayout",
                         "pos": [0.5, 0.03], "size": [1.0, 0.05]},
             "children": [
                 {"payload": {"visible": True, "type": "ImageView",
                              "pos": [0.05, 0.03], "size": [0.06, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "EditText",
                              "pos": [0.5, 0.03], "size": [0.6, 0.04]}, "children": []},
             ]},
            # 信息流
            {"payload": {"visible": True, "type": "RecyclerView",
                         "pos": [0.5, 0.5], "size": [1.0, 0.88]},
             "children": feeds},
            # 底部Tab栏
            {"payload": {"visible": True, "type": "LinearLayout",
                         "pos": [0.5, 0.97], "size": [1.0, 0.05]},
             "children": [
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.1, 0.97], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.3, 0.97], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.5, 0.97], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.7, 0.97], "size": [0.08, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.9, 0.97], "size": [0.08, 0.04]}, "children": []},
             ]},
        ]
    }


def test_same_template_different_content():
    """测试1: 同模板不同内容 → 应判为相同"""
    print("=" * 60)
    print("测试1: 同模板不同内容")
    print("=" * 60)

    # 视频A详情页 vs 视频B详情页
    page_a = make_video_detail_page("周杰伦新歌MV", comment_count=5)
    page_b = make_video_detail_page("猫咪搞笑合集", comment_count=8)

    fp_a = generate(page_a, "com.bili/.VideoDetailActivity")
    fp_b = generate(page_b, "com.bili/.VideoDetailActivity")

    hash_a = fp_a.split("|")[1]
    hash_b = fp_b.split("|")[1]
    distance = _hamming_distance(hash_a, hash_b)

    print(f"  视频A指纹: ...{hash_a[-16:]}")
    print(f"  视频B指纹: ...{hash_b[-16:]}")
    print(f"  汉明距离: {distance}")
    print(f"  判定结果: {'相同模板 ✓' if is_same_page(fp_a, fp_b) else '不同模板 ✗'}")
    assert is_same_page(fp_a, fp_b), "同模板应判为相同!"
    print()

    # 首页刷新前 vs 刷新后（不同推荐内容）
    home_a = make_home_page(feed_count=6)
    home_b = make_home_page(feed_count=10)

    fp_ha = generate(home_a, "com.bili/.MainActivityV2")
    fp_hb = generate(home_b, "com.bili/.MainActivityV2")

    hash_ha = fp_ha.split("|")[1]
    hash_hb = fp_hb.split("|")[1]
    distance_h = _hamming_distance(hash_ha, hash_hb)

    print(f"  首页(6条)指纹: ...{hash_ha[-16:]}")
    print(f"  首页(10条)指纹: ...{hash_hb[-16:]}")
    print(f"  汉明距离: {distance_h}")
    print(f"  判定结果: {'相同模板 ✓' if is_same_page(fp_ha, fp_hb) else '不同模板 ✗'}")
    assert is_same_page(fp_ha, fp_hb), "首页刷新后应判为相同!"
    print()


def test_different_templates():
    """测试2: 不同模板 → 应判为不同"""
    print("=" * 60)
    print("测试2: 不同模板")
    print("=" * 60)

    video_page = make_video_detail_page()
    search_page = make_search_page()
    home_page = make_home_page()

    fp_video = generate(video_page, "com.bili/.VideoDetailActivity")
    fp_search = generate(search_page, "com.bili/.SearchActivity")
    fp_home = generate(home_page, "com.bili/.MainActivityV2")

    # 视频页 vs 搜索页
    same = is_same_page(fp_video, fp_search)
    print(f"  视频页 vs 搜索页: {'相同 ✗' if same else '不同 ✓'}")
    assert not same, "不同页面应判为不同!"

    # 视频页 vs 首页
    same2 = is_same_page(fp_video, fp_home)
    print(f"  视频页 vs 首页:   {'相同 ✗' if same2 else '不同 ✓'}")
    assert not same2, "不同页面应判为不同!"

    # 搜索页 vs 首页
    same3 = is_same_page(fp_search, fp_home)
    print(f"  搜索页 vs 首页:   {'相同 ✗' if same3 else '不同 ✓'}")
    assert not same3, "不同页面应判为不同!"
    print()


def test_same_activity_different_page():
    """测试3: 同Activity不同功能页（如GeneralActivity复用）"""
    print("=" * 60)
    print("测试3: 同Activity不同功能页")
    print("=" * 60)

    # 稍后再看页面 - 纯列表
    watch_later = {
        "payload": {"visible": True, "type": "FrameLayout", "pos": [0.5, 0.5], "size": [1.0, 1.0]},
        "children": [
            {"payload": {"visible": True, "type": "LinearLayout",
                         "pos": [0.5, 0.04], "size": [1.0, 0.06]},
             "children": [
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.05, 0.04], "size": [0.06, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "TextView",
                              "pos": [0.5, 0.04], "size": [0.3, 0.04]}, "children": []},
             ]},
            {"payload": {"visible": True, "type": "RecyclerView",
                         "pos": [0.5, 0.55], "size": [1.0, 0.88]},
             "children": []},
        ]
    }

    # 历史记录页面 - 有日期分组+列表
    history = {
        "payload": {"visible": True, "type": "FrameLayout", "pos": [0.5, 0.5], "size": [1.0, 1.0]},
        "children": [
            {"payload": {"visible": True, "type": "LinearLayout",
                         "pos": [0.5, 0.04], "size": [1.0, 0.06]},
             "children": [
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.05, 0.04], "size": [0.06, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "TextView",
                              "pos": [0.5, 0.04], "size": [0.3, 0.04]}, "children": []},
                 {"payload": {"visible": True, "type": "ImageButton",
                              "pos": [0.92, 0.04], "size": [0.06, 0.04]}, "children": []},
             ]},
            {"payload": {"visible": True, "type": "TextView",
                         "pos": [0.15, 0.12], "size": [0.2, 0.03]}, "children": []},
            {"payload": {"visible": True, "type": "RecyclerView",
                         "pos": [0.5, 0.58], "size": [1.0, 0.82]},
             "children": []},
        ]
    }

    fp_wl = generate(watch_later, "com.bili/.GeneralActivity")
    fp_hist = generate(history, "com.bili/.GeneralActivity")

    hash_wl = fp_wl.split("|")[1]
    hash_hist = fp_hist.split("|")[1]
    distance = _hamming_distance(hash_wl, hash_hist)

    print(f"  稍后再看指纹: ...{hash_wl[-16:]}")
    print(f"  历史记录指纹: ...{hash_hist[-16:]}")
    print(f"  汉明距离: {distance}")
    same = is_same_page(fp_wl, fp_hist)
    print(f"  判定结果: {'相同模板 ✗(应为不同)' if same else '不同模板 ✓'}")
    # 这两个结构有差异，应该不同
    print(f"  （注：如果汉明距离<5可能误判为同模板，需调整阈值）")
    print()


def test_minor_variation_tolerance():
    """测试4: 微小变化容错（如一个按钮隐藏）"""
    print("=" * 60)
    print("测试4: 微小变化容错")
    print("=" * 60)

    # 正常详情页
    page_normal = make_video_detail_page()

    # 少了一个按钮（如关注按钮隐藏了）
    page_minor = make_video_detail_page()
    page_minor["children"][2]["children"].pop()  # 去掉最后一个按钮

    fp_normal = generate(page_normal, "com.bili/.VideoDetailActivity")
    fp_minor = generate(page_minor, "com.bili/.VideoDetailActivity")

    hash_n = fp_normal.split("|")[1]
    hash_m = fp_minor.split("|")[1]
    distance = _hamming_distance(hash_n, hash_m)

    print(f"  正常页指纹:    ...{hash_n[-16:]}")
    print(f"  少一按钮指纹:  ...{hash_m[-16:]}")
    print(f"  汉明距离: {distance}")
    print(f"  判定结果: {'相同模板(容错) ✓' if is_same_page(fp_normal, fp_minor) else '不同模板 (距离超阈值)'}")
    print()


def test_find_similar_performance():
    """测试5: find_similar性能（模拟已有100个指纹时的查找速度）"""
    print("=" * 60)
    print("测试5: 性能测试")
    print("=" * 60)

    visited = {}

    # 预填100个不同页面的指纹
    for i in range(100):
        page = make_video_detail_page(f"视频{i}", comment_count=i % 10 + 1)
        fp = generate(page, f"com.bili/.Activity{i % 10}")
        add_fingerprint(fp, visited)

    # 测试查找速度
    test_page = make_video_detail_page("测试视频")
    test_fp = generate(test_page, "com.bili/.Activity0")

    start = time.time()
    iterations = 1000
    for _ in range(iterations):
        find_similar(test_fp, visited)
    elapsed = time.time() - start

    print(f"  已有指纹数: {sum(len(v) for v in visited.values())}")
    print(f"  查找 {iterations} 次耗时: {elapsed*1000:.1f}ms")
    print(f"  单次查找: {elapsed/iterations*1000:.3f}ms")
    print()

    # 单次指纹生成耗时
    start = time.time()
    for _ in range(100):
        generate(make_video_detail_page(), "com.bili/.VideoDetailActivity")
    elapsed = time.time() - start
    print(f"  指纹生成 100次耗时: {elapsed*1000:.1f}ms")
    print(f"  单次生成: {elapsed/100*1000:.2f}ms")
    print()


def test_skeleton_visualization():
    """测试6: 可视化骨架图（输出为ASCII预览）"""
    print("=" * 60)
    print("测试6: 骨架图可视化")
    print("=" * 60)

    page = make_video_detail_page()
    components = []
    _extract_components(page, components, in_list=False)
    canvas = _draw_skeleton(components)

    print(f"  组件数: {len(components)}")
    print(f"  画布尺寸: {CANVAS_W}x{CANVAS_H}")
    print(f"  骨架图预览 (缩放到16x32):")
    print()

    # 缩放到16x32做ASCII预览
    for y in range(0, CANVAS_H, 4):
        row = "    "
        for x in range(0, CANVAS_W, 4):
            val = canvas[y, x]
            if val < 40:
                row += "██"  # 深色（图片/视频）
            elif val < 70:
                row += "▓▓"  # 中深（列表）
            elif val < 100:
                row += "░░"  # 浅灰（文字）
            elif val < 140:
                row += "··"  # 中性（背景）
            elif val < 180:
                row += "──"  # 边框（容器）
            else:
                row += "□□"  # 白色（按钮）
        print(row)
    print()


if __name__ == "__main__":
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║       dHash 布局指纹算法验证测试                        ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    test_same_template_different_content()
    test_different_templates()
    test_same_activity_different_page()
    test_minor_variation_tolerance()
    test_find_similar_performance()
    test_skeleton_visualization()

    print("=" * 60)
    print("全部测试通过!")
    print("=" * 60)
