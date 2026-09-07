"""Unified scene library: the SINGLE source of truth for the console UI.

Why this module exists
----------------------
Before this module, the console hard-coded its generic presets, lens/light
modifiers and ratio phrases inside ``static/index.html``, while only the
festival themes (ghost / spring / autumn) came from the backend. The same
knowledge therefore lived in two places: adding a scene meant editing both
the frontend and the backend, and "前端不硬编码提示词" was only half true.

Everything the console needs to render the scene picker now comes from
``/api/config``:

    scene_categories -> the filter tabs (电商白底 / 室内生活 / 户外自然 / 节日季节)
    scenes           -> every pickable scene (name, emoji, summary, keywords, prompt)
    modifiers        -> lens / lighting add-on chips
    ratios           -> output aspect-ratio options + their prompt guidance

Festival scenes are generated from :mod:`app.scenes` so the themed packs
(composition lock, random variants) stay in one place.

Adding a scene = appending one entry here. No frontend change, restart only.
"""
from __future__ import annotations

from dataclasses import dataclass

from app import scenes


@dataclass(frozen=True)
class ScenePreset:
    """One pickable scene in the console's scene library."""

    key: str
    name: str            # 中文名（不含 emoji，emoji 单独字段便于大图标排版）
    emoji: str
    category: str        # white | indoor | outdoor | festival
    summary: str         # 卡片上一行短说明（人话，讲清出图长什么样）
    keywords: str        # 搜索关键词（中英文，空格分隔）
    tooltip: str = ""    # 卡片 hover 浮层长说明（一句话讲清效果/用途/适用品类）
    prompt: str = ""     # 通用场景的提示词；主题场景留空由 scenes 模块生成
    white_bg: bool = False   # 纯白底模式（后端据此切换 Layer2/Layer4）
    theme: str = ""          # 节日/季节主题 key（ghost | spring | autumn）
    random: bool = False     # True = 逐张随机组合变体（seed 可复现）


# --------------------------------------------------------------------------- #
# 分类（控制台顶部的筛选 Tab）
# --------------------------------------------------------------------------- #
CATEGORIES: tuple[dict, ...] = (
    {
        "key": "white",
        "label": "电商白底",
        "emoji": "⚪",
        "description": "纯白底主图 / 抠图底，平台规范与详情页首图首选",
    },
    {
        "key": "indoor",
        "label": "室内生活",
        "emoji": "🏠",
        "description": "家居、办公、大理石台面等室内场景，适合生活化种草",
    },
    {
        "key": "outdoor",
        "label": "户外自然",
        "emoji": "🌿",
        "description": "林间、海边、露营、街头，适合户外与运动品类",
    },
    {
        "key": "festival",
        "label": "节日季节",
        "emoji": "🎃",
        "description": "鬼节 / 春 / 秋，自动注入居中构图锁，主题鲜明",
    },
)


# --------------------------------------------------------------------------- #
# 通用场景（原前端 A/B/C 三组预设，原样搬迁）
# --------------------------------------------------------------------------- #
_GENERIC_SCENES: tuple[ScenePreset, ...] = (
    # ---- A · 电商规范主图（纯白系列）----
    ScenePreset(
        key="white_studio",
        name="电商纯白底",
        emoji="⚪",
        category="white",
        summary="纯白背景 + 真实接地投影，平台主图首选",
        keywords="白底 纯白 主图 电商 抠图 white ecommerce catalog 地影",
        tooltip=(
            "纯白底有投影：背景完全纯白（RGB 255,255,255），产品底部带柔和的接地阴影，"
            "显得真实有立体感。适合平台主图 / 详情页首图 / 亚马逊 / 京东主图位。"
        ),
        white_bg=True,
        prompt=(
            "Isolate the product on a completely pure solid white background "
            "(RGB 255, 255, 255), seamless studio backdrop, subtle and soft "
            "realistic contact shadow underneath the base, professional "
            "commercial studio lighting, high-contrast, razor-sharp clean "
            "edges, zero background artifacts, crisp ultra-high-resolution e-commerce listing "
            "photography."
        ),
    ),
    ScenePreset(
        key="white_cutout",
        name="纯白无影抠图底",
        emoji="✂️",
        category="white",
        summary="绝对纯白、无影无反光，抠图级干净边缘",
        tooltip=(
            "纯白底无投影：只抠出产品轮廓，背景全白、无影、无反光、无边缘晕染，"
            "像素级干净，方便二次合成到任意营销海报/详情页背景。"
            "适合做后续合成的去背素材，区别于上面那条「有阴影的电商主图」。"
        ),
        keywords="白底 抠图 无影 白底图 cutout 白色 去背",
        white_bg=True,
        prompt=(
            "Extract and isolate the exact product onto an absolute pure blank "
            "white background (hex #FFFFFF), perfectly flat solid background, "
            "no shadows, no reflections, pin-sharp contour edges, clean "
            "isolation cutout style."
        ),
    ),
    # ---- B · 室内生活与家居 ----
    ScenePreset(
        key="indoor_cozy",
        name="温馨现代室内",
        emoji="🏠",
        category="indoor",
        summary="现代家居桌面，窗边暖阳，背景虚化",
        keywords="室内 家居 客厅 温馨 现代 indoor home living room cozy 窗边",
        tooltip="现代家居感的桌面或边几，窗边柔和暖阳、背景轻度虚化。家居 / 厨房小家电 / 日用品种种草常用。",
        prompt=(
            "placed naturally on a clean neutral-toned tabletop inside a modern "
            "cozy interior, soft ambient warm sunlight coming from a nearby "
            "window, blurred contemporary home background with subtle aesthetic "
            "decor, realistic contact shadows, f/2.8 shallow depth of field."
        ),
    ),
    ScenePreset(
        key="indoor_oak",
        name="极简原木家居",
        emoji="🪵",
        category="indoor",
        summary="浅橡木桌 + 晨光，日式极简质感",
        keywords="原木 木质 极简 日式 木桌 家居 wood oak minimal 晨光",
        tooltip="浅橡木桌面 + 晨光斜射，背景是日式极简风格的暖色木系空间。手作 / 茶器 / 文创 / 咖啡器具常见。",
        prompt=(
            "placed on a natural light-oak wooden table, soft morning sunlight "
            "casting gentle shadows, blurred minimalist warm living room "
            "background, clean organic texture."
        ),
    ),
    ScenePreset(
        key="indoor_marble",
        name="奢华大理石台面",
        emoji="🏛️",
        category="indoor",
        summary="卡拉拉白大理石，高端美妆 / 珠宝感",
        keywords="大理石 奢华 高端 台面 美妆 珠宝 marble luxury 高级感",
        tooltip="白色卡拉拉大理石台面，柔和的漫射摄影光 + 隐约水波纹，高端广告质感。香水 / 美妆 / 珠宝 / 护肤常用。",
        prompt=(
            "placed on a smooth white Carrara marble pedestal, luxury bathroom "
            "countertop, soft diffused studio lighting, subtle water ripples, "
            "elegant high-end commercial ad."
        ),
    ),
    ScenePreset(
        key="indoor_desk",
        name="现代办公桌面",
        emoji="💼",
        category="indoor",
        summary="深灰哑光办公桌，数码 / 文具场景",
        keywords="办公 桌面 工位 数码 文具 office desk 商务 简洁",
        tooltip="深灰哑光办公桌 + 笔记本局部 + 小绿植点缀，干净明亮的商务工位氛围。数码外设 / 文具 / 办公电器常用。",
        prompt=(
            "placed on a clean dark grey matte office desk, next to a subtle "
            "laptop edge and small succulent plant, modern bright office "
            "ambient lighting, crisp focus."
        ),
    ),
    # ---- C · 室外全维度自然与街头 ----
    ScenePreset(
        key="outdoor_forest",
        name="自然林间草木",
        emoji="🌲",
        category="outdoor",
        summary="乡野石台 + 绿植，黄金时刻透光",
        keywords="户外 森林 林间 草木 绿植 forest outdoor 自然 草地",
        tooltip="乡野粗粝石台上放着产品，黄金时段柔光透过背景树林。户外露营 / 户外装备 / 园艺工具 / 宠物用品质感。",
        prompt=(
            "placed securely on a rustic natural stone platform outdoors, "
            "surrounded by fresh lush greenery and subtle wild grass, soft "
            "golden hour sunlight filtering naturally through the background, "
            "authentic outdoor environment with rich atmospheric depth."
        ),
    ),
    ScenePreset(
        key="outdoor_beach",
        name="海边沙滩阳光",
        emoji="🏖️",
        category="outdoor",
        summary="金色细沙 + 海浪远景，夏日清爽",
        keywords="海边 沙滩 海浪 夏日 阳光 beach sand sea summer 度假",
        tooltip="金色细沙上一颗圆润海石，远景是蓝绿海浪和晴天。泳装 / 防晒 / 沙滩玩具 / 夏日茶饮。",
        prompt=(
            "placed firmly on dry fine golden sand next to a smooth ocean "
            "pebble, crisp natural outdoor sunlight from a 45-degree angle "
            "casting realistic contact shadows beneath the base, subtle "
            "turquoise sea waves and sunny sky gently blurred in the far "
            "background."
        ),
    ),
    ScenePreset(
        key="outdoor_camp",
        name="户外露营木台",
        emoji="🏕️",
        category="outdoor",
        summary="雪松野餐桌 + 松林漏光，露营风",
        keywords="露营 户外 野餐 木台 松林 camping outdoor 徒步",
        tooltip="风化雪松野餐桌，松林间斑驳日光 + 松果点缀。露营装备 / 保温杯 / 户外炊具 / 徒步用品。",
        prompt=(
            "resting naturally on a weathered rustic cedar picnic table, "
            "accurate physical scale with realistic wood grain texture, soft "
            "ambient daylight filtering through pine trees, subtle pine cones "
            "creating scale reference, razor-sharp contact shadows."
        ),
    ),
    ScenePreset(
        key="outdoor_street",
        name="现代街头水泥台",
        emoji="🏙️",
        category="outdoor",
        summary="工业水泥台 + 玻璃建筑街景，都市潮感",
        tooltip="工业风水泥台 + 阴天柔和日光，远景是玻璃幕墙和街边绿树。潮牌 / 球鞋 / 数码新品 / 街头生活方式。",
        keywords="街头 城市 水泥 工业 都市 street urban concrete 潮流",
        prompt=(
            "placed securely on a clean industrial architectural concrete ledge "
            "outdoors, bright overcast daylight providing balanced natural "
            "reflections, blurred modern glass architecture and street trees in "
            "the background, sharp grounded shadows."
        ),
    ),
)


# --------------------------------------------------------------------------- #
# 节日 / 季节场景（由 app.scenes 的主题词库动态生成）
# --------------------------------------------------------------------------- #
def _festival_scenes() -> tuple[ScenePreset, ...]:
    out: list[ScenePreset] = []
    for pack in scenes.THEME_PACKS.values():
        # 主题标签去掉 emoji 前缀作为名称，emoji 单独取首字符簇
        raw = pack.label.strip()                     # e.g. "👻 鬼节主题"
        parts = raw.split(" ", 1)
        emoji = parts[0] if len(parts[0]) <= 4 else "🎉"
        name = (parts[1] if len(parts) > 1 else raw).replace("主题", "")
        sub = pack.signature_label.split(" ", 1)
        sig_name = sub[1] if len(sub) > 1 else pack.signature_label
        out.append(
            ScenePreset(
                key=f"theme_{pack.key}",
                name=sig_name,
                emoji=emoji,
                category="festival",
                summary=pack.description,
                tooltip=(
                    f"{name}主题场景：从该主题词库中精选一组（背景+道具+光影+风格）"
                    f"作为全套批次统一出图。整批画面协调一致，适合店铺专题活动主图。"
                ),
                keywords=f"{name} 节日 季节 {pack.key} festival seasonal 主题",
                theme=pack.key,
                random=False,
            )
        )
        out.append(
            ScenePreset(
                key=f"theme_{pack.key}_random",
                name=f"{name} · 随机变体",
                emoji="🎲",
                category="festival",
                summary=(
                    f"每张图独立随机组合（{len(pack.backgrounds)}×{len(pack.elements)}"
                    f"×{len(pack.lights)}×{len(pack.styles)} 种），批量不重样"
                ),
                tooltip=(
                    f"{name}主题随机变体：每张图从{len(pack.backgrounds)}×{len(pack.elements)}"
                    f"×{len(pack.lights)}×{len(pack.styles)} 共{len(pack.backgrounds)*len(pack.elements)*len(pack.lights)*len(pack.styles)}种组合中独立抽取，"
                    f"保证整批主题鲜明但不重样。批量详情页/活动专题图常用。"
                ),
                keywords=f"{name} 随机 变体 批量 {pack.key} random 不重样",
                theme=pack.key,
                random=True,
            )
        )
    return tuple(out)


def all_scenes() -> tuple[ScenePreset, ...]:
    return _GENERIC_SCENES + _festival_scenes()


def find_scene(key: str) -> ScenePreset | None:
    for s in all_scenes():
        if s.key == key:
            return s
    return None


# --------------------------------------------------------------------------- #
# 输出比例
# --------------------------------------------------------------------------- #
RATIOS: tuple[dict, ...] = (
    {"value": "", "label": "原图比例", "hint": "跟随上传原图，不额外扩展场景"},
    {"value": "4:3", "label": "4:3 横", "hint": "电商详情页通用横向构图"},
    {"value": "1:1", "label": "1:1 正方", "hint": "平台主图 / 社媒方形"},
    {"value": "3:4", "label": "3:4 竖", "hint": "详情页竖图 / 小红书"},
    {"value": "16:9", "label": "16:9 宽屏", "hint": " banner / 视频封面"},
    {"value": "9:16", "label": "9:16 竖屏", "hint": "短视频 / 竖版海报"},
)

# 比例 -> 注入提示词的英文指引（模型据此自然扩展场景以匹配比例，不再物理塞白边）
RATIO_PROMPTS: dict[str, str] = {
    "1:1": "composition framed in a 1:1 square aspect ratio",
    "4:3": "composition framed in a 4:3 landscape aspect ratio",
    "3:4": "composition framed in a 3:4 portrait aspect ratio",
    "16:9": "composition framed in a 16:9 wide landscape aspect ratio",
    "9:16": "composition framed in a 9:16 vertical aspect ratio",
}


def ratio_prompt(value: str | None) -> str:
    return RATIO_PROMPTS.get((value or "").strip(), "")


# --------------------------------------------------------------------------- #
# 微调修饰器（镜头 / 光影，追加到提示词末尾）
# --------------------------------------------------------------------------- #
LENS_MODIFIERS: tuple[dict, ...] = (
    {"label": "👁️ 经典平视", "text": ", eye-level front view"},
    {"label": "📐 35°微俯视", "text": ", 35-degree high-angle commercial product shot"},
    {"label": "🔍 微距特写", "text": ", close-up macro product lens"},
)

LIGHT_MODIFIERS: tuple[dict, ...] = (
    {"label": "☀️ 柔和自然晨光", "text": ", soft diffused morning sunlight"},
    {"label": "💡 摄影棚专业侧光", "text": ", professional 45-degree studio side lighting"},
    {"label": "🌇 温暖黄昏光影", "text": ", warm golden hour sunset lighting"},
    {"label": "⚡ 戏剧感硬光高反差", "text": ", dramatic high-contrast studio lighting with crisp geometric shadows"},
)


def modifiers() -> dict:
    return {"lens": list(LENS_MODIFIERS), "light": list(LIGHT_MODIFIERS)}


# --------------------------------------------------------------------------- #
# 前端载荷
# --------------------------------------------------------------------------- #
def categories() -> list[dict]:
    """Filter tabs for the scene picker."""
    return [dict(c) for c in CATEGORIES]


def library() -> list[dict]:
    """Every pickable scene, serialised for the frontend (no secrets)."""
    out: list[dict] = []
    for s in all_scenes():
        out.append(
            {
                "key": s.key,
                "name": s.name,
                "emoji": s.emoji,
                "category": s.category,
                "summary": s.summary,
                "tooltip": s.tooltip,
                "keywords": s.keywords,
                "prompt": s.prompt,
                "white_bg": s.white_bg,
                "theme": s.theme,
                "random": s.random,
            }
        )
    return out


def payload() -> dict:
    """Everything the console needs to build the scene picker."""
    return {
        "categories": categories(),
        "scenes": library(),
        "modifiers": modifiers(),
        "ratios": [dict(r) for r in RATIOS],
        # 比例 -> 注入提示词的英文短语（前端据此拼接，不再硬编码）
        "ratio_prompts": dict(RATIO_PROMPTS),
    }
