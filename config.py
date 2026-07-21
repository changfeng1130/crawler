"""小红书 配置"""

import os

# App 配置
PACKAGE_NAME = "com.xingin.xhs"
MAIN_ACTIVITY_KEYWORD = "IndexActivity"  # 判断是否在首页的关键词

# 连接方式: "usb" / "wifi" / "emulator"
CONNECTION_MODE = "usb"

# WiFi ADB 配置（CONNECTION_MODE="wifi" 时生效）
WIFI_ADB_HOST = "192.168.43.1"
WIFI_ADB_PORT = 5555

# 模拟器配置（CONNECTION_MODE="emulator" 时生效）
EMULATOR_HOST = "127.0.0.1"
EMULATOR_PORT = 5555  # MuMu 开启了默认ADB端口

# 遍历参数
MAX_DEPTH = 8
LIST_ITEM_MAX_CLICK = 2
MAX_SCREENSHOTS = 200
SCROLL_MAX_TIMES = 3
MAX_SAME_TEMPLATE_COUNT = 5
PAGE_LOAD_WAIT = 0.8
BACK_WAIT = 0.5

# 列表页滚动分段截图
SCROLL_SEGMENT_WAIT = 1.0
SCROLLABLE_TYPES = {
    "RecyclerView", "ListView", "ScrollView",
    "NestedScrollView", "HorizontalScrollView",
}

# 弹窗遮挡判定：顶层节点可见面积 >= 此比例视为遮挡弹窗
POPUP_OCCLUSION_RATIO = 0.5

# 隐私页跳过（个人详情页不截图）
SKIP_PERSONAL_PAGES = True
# 遮挡弹窗跳过截图 —— 先关闭以保证遍历覆盖（弹窗仍会尝试关闭，只是不因此跳过截图）
SKIP_BLOCKED_POPUPS = False
PRIVACY_ACTIVITY_KEYWORDS = ["ProfileActivity", "MineActivity", "UserInfoActivity", "PersonalInfoActivity"]
PRIVACY_ID_KEYWORDS = ["user_card", "personal_info", "mine_header", "mine_layout", "profile_header"]
PRIVACY_TEXT_KEYWORDS = ["编辑资料", "获赞与收藏", "我的钱包", "我的订单", "浏览记录"]
PRIVACY_TEXT_MIN_HITS = 2  # 至少命中2个才判定为个人页

# 不可点击的控件文本（危险操作或无效操作）
SKIP_TEXTS = [
    "发布", "拍摄", "直播", "充值", "开通",
    "分享", "收藏", "转发",
    "删除", "举报", "拉黑", "+",
]

# 需要跳过的Activity关键词（进入这些页面不截图不递归）
SKIP_ACTIVITY_KEYWORDS = ["Publish", "Upload", "Editor", "Camera", "Shoot", "Record", "Live", "ImageCrop"]

# 弹窗关闭关键词（含冷启动引导：跳过广告/同意隐私协议/青少年模式）
POPUP_CLOSE_KEYWORDS = [
    "我知道了", "取消", "跳过", "以后再说",
    "暂不", "关闭", "不再提示", "稍后",
    "允许", "下次再说", "暂不更新",
    "skip", "Skip", "同意", "同意并继续", "知道了", "继续",
    "关闭广告", "青少年", "以后再说", "暂不升级",
]

# 输出目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")
METADATA_CSV = os.path.join(OUTPUT_DIR, "metadata.csv")
STATE_FILE = os.path.join(OUTPUT_DIR, "state.json")

os.makedirs(SCREENSHOT_DIR, exist_ok=True)
