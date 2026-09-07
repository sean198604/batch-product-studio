"""Prompt assembly for the batch image-to-image (scene generation) pipeline.

The backend auto-assembles every prompt into a strict 4-layer "physics realism
& anti-distortion engine" right before it is sent to Google AI Studio, so a
staff member can never accidentally forget the mandatory constraints that keep
generated scenes believable:

  Layer 1 -- Subject fidelity & shape lock:
      The product subject, shape, material, logo and texture from the
      reference image MUST be preserved and never warped / stretched.

  Layer 2 -- Physical contact, true scale & perspective correction (core):
      Normal mode: firmly grounded with contact shadows, true scale, 85mm
      commercial perspective (no floating, no wide-angle distortion).
      White-bg mode: placed cleanly on a pure flat ground with subtle contact
      ambient occlusion, avoiding any floating look (no extra colored backdrop).

  Layer 3 -- Scene subject description:
      The user-submitted scene text / selected preset (what the staff typed or
      picked). This is the only user-controlled layer.

  Layer 4 -- Commercial-grade rendering & light fusion:
      Normal mode: universal commercial-photography polish.
      White-bg mode: high-contrast studio lighting, razor-sharp clean edges,
      pristine e-commerce catalog standard.

When a festival / seasonal ``theme`` is selected (ghost / spring / autumn), an
extra **composition layer** is injected between Layer 2 and Layer 3: the shared
``CENTER_COMPOSITION`` lock (product dead-center, never cropped or occluded)
plus the theme's own atmosphere / taboo accent. This is what keeps seasonal
scenes both "居中" and "主题鲜明" while the existing physics engine stays intact.

``assemble_prompt`` composes those layers. The frontend writes the user's
scene text (preset + manual + ratio + modifier tags) into the visible box, so
what the staff sees is exactly Layer 3; Layers 1 / 2 / 4 and the theme layer
are injected here. The mode (normal vs white-bg) is selected by an explicit
``white_bg`` flag or, failing that, by detecting A-group "pure white
background" phrases in Layer 3.
"""
from __future__ import annotations

from app.scenes import theme_layer

# --- Layer 1：主体保真与形状锁定（强制保留原产品，绝不形变）---
FIDELITY_LOCK: str = (
    "Strictly preserve the exact product, shapes, contours, materials, colors, "
    "brand logos, and textures from the provided reference image. Do not warp, "
    "distort, stretch, or alter the product in any way."
)

# --- Layer 2：物理接触、真实比例与透视纠正（普通场景模式，防悬浮核心）---
PHYSICS_LOCK: str = (
    "The product must be firmly grounded and physically resting on the contact "
    "surface with realistic ambient occlusion and accurate contact shadows "
    "directly beneath the base to completely eliminate any floating appearance. "
    "Maintain true-to-life real-world physical scale, natural proportional "
    "balance, and a standard commercial product perspective (simulating an 85mm "
    "lens, zero wide-angle distortion)."
)

# --- Layer 2（纯白底模式）：避免多余杂色背景与悬浮观感 ---
PHYSICS_LOCK_WHITE: str = (
    "The product must be placed cleanly on a pure flat ground with realistic "
    "subtle contact ambient occlusion directly beneath the base, avoiding any "
    "floating look."
)

# --- Layer 4：商业级画质渲染与光影融合（普通场景模式，通用，自动追加）---
QUALITY_RENDER: str = (
    "Professional commercial advertising photography, balanced natural "
    "reflections matching the surrounding environment, hyper-realistic "
    "textures, clean optical depth of field, ultra-high resolution."
)

# --- Layer 4（纯白底模式）：高反差棚拍、锐利边缘、电商目录标准 ---
QUALITY_RENDER_WHITE: str = (
    "High-contrast studio lighting, razor-sharp clean edges, pristine "
    "e-commerce product catalog standard, 8k resolution."
)

# --- Layer 1（多角度合成专用）：多参考图理解 3D 结构、统一融合成单张场景 ---
FUSION_LAYER1: str = (
    "The provided multiple reference images show the SAME product from different "
    "angles and perspectives. Understand the 3D structure, authentic materials, "
    "and complete details of the product from all reference angles. Generate a "
    "single coherent commercial scene featuring this product naturally placed in "
    "the environment, strictly preserving its genuine form and branding."
)

# 用于后端自动识别「纯白底模式」的短语特征（当前端未显式传 is_white_bg 时兜底）。
_WHITE_BG_MARKERS = (
    "rgb 255, 255, 255",
    "pure solid white background",
    "blank white background",
    "#ffffff",
    "pure white background",
    "white background",
)


def is_white_bg_prompt(user_prompt: str) -> bool:
    """Heuristically detect a pure-white-background scene from the user text.

    Triggered when the Layer 3 text references an A-group (pure white catalog)
    preset. Used only as a fallback when the frontend does not pass an explicit
    ``is_white_bg`` flag.
    """
    text = (user_prompt or "").lower()
    return any(marker in text for marker in _WHITE_BG_MARKERS)


def assemble_prompt(
    user_prompt: str,
    white_bg: bool | None = None,
    multi_angle: bool = False,
    theme: str | None = None,
) -> str:
    """Wrap a user scene description into the strict 4-layer prompt.

    Layer 1 (subject lock) is chosen by *mode*:
    * single-image mode    -> FIDELITY_LOCK (preserve the one reference product).
    * multi-angle mode      -> FUSION_LAYER1 (understand the 3D product from
      several reference angles, then fuse into one coherent scene).

    Layer 2 (physics / anti-distortion) and Layer 4 (commercial quality) are
    chosen by the scene *mode*:
    * normal mode   -> PHYSICS_LOCK + QUALITY_RENDER (full grounding, env-aware
      reflections, 85mm perspective).
    * white-bg mode -> PHYSICS_LOCK_WHITE + QUALITY_RENDER_WHITE (no extra
      colored background, high-contrast catalog look). White-bg is ignored when
      ``multi_angle`` is set (fusion always produces a scene).

    ``white_bg`` is resolved as: the explicit boolean if provided, otherwise a
    heuristic detection of A-group white-background phrases in ``user_prompt``.

    Layer 3 is the user's own scene text (it may already contain a preset /
    environment / ratio / modifier phrases the frontend wrote into the box). An
    empty / whitespace-only user prompt still gets Layers 1 / 2 / 4 so
    generation is never left unconstrained.

    ``theme`` (ghost / spring / autumn) additionally injects the composition
    layer -- see ``app.scenes.theme_layer`` -- right after Layer 2, so the
    centering lock and the theme atmosphere are enforced as hard constraints
    rather than depending on what the staff typed.
    """
    if white_bg is None:
        white_bg = is_white_bg_prompt(user_prompt)
    white_bg = white_bg and not multi_angle
    user_prompt = (user_prompt or "").strip()
    layer1 = FUSION_LAYER1 if multi_angle else FIDELITY_LOCK
    layer2 = PHYSICS_LOCK_WHITE if white_bg else PHYSICS_LOCK
    layer4 = QUALITY_RENDER_WHITE if white_bg else QUALITY_RENDER
    parts = [layer1, layer2]
    # 主题构图层（居中锁 + 节日/季节氛围约束），仅在选定主题时注入
    tl = theme_layer(theme)
    if tl:
        parts.append(tl)
    if user_prompt:
        parts.append(user_prompt)
    parts.append(layer4)
    return "\n\n".join(parts)
