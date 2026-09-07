"""Festival / seasonal theme packs for the image-to-image scene pipeline.

Why this module exists
----------------------
The generic preset chips (living room, beach, forest ...) are *neutral* scenes.
Seasonal campaigns (Halloween / spring / autumn) need a much stronger, instantly
readable mood, and -- crucially for e-commerce -- the product must stay the
undisputed, centered hero of the frame instead of being buried under decoration.

The composition follows the classic festival-prompt paradigm:

    [product placement] + [festival scene / background]
    + [lighting & colour mood] + [material & art style]

Because this is **image-to-image**, the reference image already supplies the
product itself, so the theme text deliberately describes only the *background
reconstruction, atmosphere and festival props* -- never the product. That is
exactly what the reference guide recommends ("提示词的重点应该放在背景重构、
氛围渲染和节日元素的融合上，而不是过度描述产品本身").

Two deliberate improvements over the reference guide
----------------------------------------------------
1. **Spring and Autumn are split into two independent packs.** The guide mixes
   cherry blossoms and golden wheat in one "春秋" bank; random sampling from a
   merged bank produces incoherent scenes (wheat field + cherry petals, amber
   tones + pastel spring). Separate, internally consistent banks keep each
   theme distinct and unmistakable (主题鲜明有特色).

2. **``--seed`` is NOT appended to the prompt.** Neither Agnes nor Gemini
   documents a ``--seed`` generation parameter, so a literal ``--seed 123456``
   would just be junk text that risks leaking into the rendered image. The seed
   is instead used to make the *variant selection* deterministic, which gives
   the same reproducibility benefit with zero prompt pollution.
"""
from __future__ import annotations

import random
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# Composition lock (shared by every theme): 图片要居中
# --------------------------------------------------------------------------- #
CENTER_COMPOSITION: str = (
    "Centered hero composition: the product MUST sit in the exact visual center "
    "of the frame, horizontally and vertically, as the single dominant focal "
    "point occupying roughly the middle third of the image. Arrange the themed "
    "environment, props and atmosphere symmetrically around it with balanced "
    "breathing room on all sides. Keep the product fully inside the frame with "
    "no cropping or clipping at any edge, and never let scene elements, props "
    "or effects occlude, overlap or cover the product."
)

# --------------------------------------------------------------------------- #
# Shared style bank (画质 / 材质 / 艺术风格)
# --------------------------------------------------------------------------- #
_SHARED_STYLES: tuple[str, ...] = (
    "commercial product photography",
    "high-end editorial style",
    "octane render clean details",
    "photorealistic, ultra-high resolution",
)


@dataclass(frozen=True)
class ThemePack:
    """A coherent festival / seasonal vocabulary pack.

    Every field is internally consistent so that *any* random combination of
    background + element + light still reads as the same recognizable theme.
    """

    key: str
    label: str            # 前端分组标题用的短标签（含 emoji）
    description: str      # 前端展示的主题说明
    backgrounds: tuple[str, ...]
    elements: tuple[str, ...]
    lights: tuple[str, ...]
    styles: tuple[str, ...] = _SHARED_STYLES
    accent: str = ""      # 主题专属氛围 / 禁忌约束，作为独立层注入
    # 精选固定组合（所见即所得 chip 使用）：(background, element, light) 下标
    signature: tuple[int, int, int] = (0, 0, 0)
    signature_label: str = ""
    variant_label: str = ""


# --------------------------------------------------------------------------- #
# 👻 鬼节 / Halloween
# --------------------------------------------------------------------------- #
GHOST = ThemePack(
    key="ghost",
    label="👻 鬼节主题",
    description="暗调、神秘、电影感。适合 Halloween 与大促节日氛围款，恐怖感保持高级不血腥。",
    backgrounds=(
        "in a misty eerie forest at midnight",
        "inside a haunted gothic mansion hall",
        "on a mysterious old stone altar",
        "in an ancient foggy cemetery",
        "in an abandoned midnight theme park",
    ),
    elements=(
        "surrounded by floating eerie lanterns drifting in the dark air",
        "framed by glowing carved jack-o-lanterns",
        "wrapped in wisps of spectral ghost fire",
        "with whispering dark shadows lingering at the edges",
        "flanked by withered spooky trees",
    ),
    lights=(
        "low-key moody lighting with deep cinematic blacks",
        "mysterious purple and neon green glow",
        "chilling moonlight piercing through thick fog",
        "dramatic cinematic shadows with strong rim light",
    ),
    accent=(
        "The Halloween mood must read instantly and unmistakably. Keep the "
        "styling cinematic, premium and atmospheric -- never gory, bloody, "
        "grotesque, frightening-religious or disturbing. Do not add any text, "
        "letters, numbers, watermark or logo anywhere in the scene."
    ),
    signature=(1, 1, 1),
    signature_label="🎃 哥特古宅 · 南瓜灯",
    variant_label="🎲 鬼节随机变体（每张不同）",
)

# --------------------------------------------------------------------------- #
# 🌸 春 / Spring
# --------------------------------------------------------------------------- #
SPRING = ThemePack(
    key="spring",
    label="🌸 春季主题",
    description="清新、明亮、复苏感。粉樱与新绿为主，柔和漫射光，适合春夏上新与礼品线。",
    backgrounds=(
        "in a misty spring mountain covered in blooming blossoms",
        "in a serene countryside garden at golden hour",
        "in a traditional oriental courtyard with fresh spring greenery",
        "on a sunlit riverside bank lined with young willow",
    ),
    elements=(
        "with blooming pink cherry blossom petals drifting through the air",
        "with fresh spring flowers and tender new buds scattered around",
        "with morning dew glistening on fresh green grass",
        "with soft petals resting on a light spring breeze",
    ),
    lights=(
        "soft diffused natural spring daylight",
        "warm gentle golden hour sunlight",
        "vibrant spring pastel colors with a clean bright palette",
        "gentle backlit glow filtering through fresh foliage",
    ),
    accent=(
        "The mood must feel fresh, bright and full of renewal. Keep a clean "
        "premium commercial look with an airy, light palette. Do not add any "
        "text, letters, numbers, watermark or logo anywhere in the scene."
    ),
    signature=(0, 0, 0),
    signature_label="🌸 迷雾春山 · 飘落樱花",
    variant_label="🎲 春季随机变体（每张不同）",
)

# --------------------------------------------------------------------------- #
# 🍁 秋 / Autumn
# --------------------------------------------------------------------------- #
AUTUMN = ThemePack(
    key="autumn",
    label="🍁 秋季主题",
    description="温暖、丰盈、丰收感。琥珀与陶土色调，长影暖光，适合秋冬 Main Fig 与家居线。",
    backgrounds=(
        "in a golden wheat field under a low autumn sunset",
        "in a traditional oriental courtyard carpeted with fallen leaves",
        "on a rustic wooden harvest table in an autumn orchard",
        "on a quiet forest path lined with amber foliage",
    ),
    elements=(
        "with swirling golden maple leaves caught in the wind",
        "with ripe harvest fruits and wheat sheaves arranged around",
        "with scattered russet leaves and dried autumn grasses",
        "with warm bokeh of falling amber leaves",
    ),
    lights=(
        "warm golden hour sunlight with long soft shadows",
        "rich amber and terracotta tones",
        "low warm autumn sun casting a cozy hazy glow",
        "soft diffused overcast harvest light",
    ),
    accent=(
        "The mood must feel warm, abundant and cozy like the harvest season. "
        "Commit fully to a rich amber and terracotta palette. Do not add any "
        "text, letters, numbers, watermark or logo anywhere in the scene."
    ),
    signature=(0, 0, 0),
    signature_label="🍁 金色麦田 · 枫叶旋舞",
    variant_label="🎲 秋季随机变体（每张不同）",
)


THEME_PACKS: dict[str, ThemePack] = {
    pack.key: pack for pack in (GHOST, SPRING, AUTUMN)
}


# --------------------------------------------------------------------------- #
# Public helpers
# --------------------------------------------------------------------------- #
def is_valid_theme(theme: str | None) -> bool:
    """True when ``theme`` names a known pack."""
    return bool(theme) and theme in THEME_PACKS


def theme_layer(theme: str | None) -> str:
    """Composition lock + theme accent, injected as one hard constraint layer.

    Returns an empty string for unknown / absent themes so callers can append
    unconditionally.
    """
    pack = THEME_PACKS.get((theme or "").strip())
    if pack is None:
        return ""
    return f"{CENTER_COMPOSITION}\n\n{pack.accent}".strip()


def render_theme_prompt(theme: str, seed: int | None = None) -> tuple[str, dict]:
    """Compose one themed scene sentence using the pack vocabularies.

    The returned text deliberately describes only the *background, props,
    lighting and style* -- the product itself comes from the reference image.

    ``seed`` makes the combination **deterministic and reproducible**: the same
    seed always yields the same background / element / light / style. When
    ``seed`` is None a fresh one is drawn and reported back in the metadata, so
    any generated image can be re-created later from its recorded seed.

    Returns ``(prompt_text, meta)``; raises ``KeyError`` for an unknown theme.
    """
    pack = THEME_PACKS[theme]
    if seed is None:
        seed = random.randint(100000, 999999)
    rng = random.Random(seed)
    background = rng.choice(pack.backgrounds)
    element = rng.choice(pack.elements)
    light = rng.choice(pack.lights)
    style = rng.choice(pack.styles)

    text = (
        f"Place the product in the exact center of the frame {background}, "
        f"{element}, {light}, {style}."
    )
    meta = {
        "theme": pack.key,
        "theme_label": pack.label,
        "seed": seed,
        "background": background,
        "element": element,
        "light": light,
        "style": style,
    }
    return text, meta


def signature_prompt(theme: str) -> str:
    """The curated, fixed combination for a theme (WYSIWYG chip text)."""
    pack = THEME_PACKS[theme]
    bi, ei, li = pack.signature
    return (
        f"Place the product in the exact center of the frame "
        f"{pack.backgrounds[bi]}, {pack.elements[ei]}, {pack.lights[li]}, "
        f"{pack.styles[0]}."
    )


def theme_catalog() -> list[dict]:
    """Theme metadata for the frontend (no secrets, no prompt internals)."""
    out: list[dict] = []
    for pack in THEME_PACKS.values():
        out.append(
            {
                "key": pack.key,
                "label": pack.label,
                "description": pack.description,
                "signature_label": pack.signature_label,
                "signature_text": signature_prompt(pack.key),
                "variant_label": pack.variant_label,
                "combinations": len(pack.backgrounds)
                * len(pack.elements)
                * len(pack.lights)
                * len(pack.styles),
                "counts": {
                    "backgrounds": len(pack.backgrounds),
                    "elements": len(pack.elements),
                    "lights": len(pack.lights),
                    "styles": len(pack.styles),
                },
            }
        )
    return out
