"""Official Google Gemini image-generation pricing (USD) + billing helpers.

All prices are sourced from the Gemini Developer API pricing page
(https://ai.google.dev/gemini-api/docs/pricing), **Standard (paid) tier**,
fetched on 2026-08-17.

Billing model for THIS tool
----------------------------
Every task in this tool is image-to-image (图生图): each source product photo
is sent to Gemini together with a prompt, and ONE scene image is returned.
So each successful generation consumes:

  * 1 INPUT image  (billed at the model's input $/1M-token rate)
  * 1 OUTPUT image (billed at the model's output-image $/1M-token rate)

Google publishes the exact token count for a 1K (1024x1024) OUTPUT image per
model, but does NOT publish a fixed token count for INPUT images (they are
tokenized by resolution). As a transparent, conservative budget baseline we
assume the input image is also ~1K resolution -> uses the same token count as
the model's 1K output image. Input cost is <1% of the total for these models,
so the approximation error is negligible for budgeting.
"""
from __future__ import annotations

from dataclasses import dataclass

# 汇率（USD -> CNY），用于近似换算展示，可在此调整。
EXCHANGE_RATE_USD_TO_CNY = 7.2


@dataclass(frozen=True)
class ModelPrice:
    id: str
    label: str
    input_per_1m: float          # 输入（文本/图片）价格，USD / 1M tokens
    output_per_1m: float         # 输出图片价格，USD / 1M tokens
    output_tokens_1k: int        # 1K (1024x1024) 输出图片的 token 数
    tag: str = ""                # 业务定位（emoji + 短标题），用于前端展示
    description: str = ""        # 适用场景与推荐使用时机
    note: str = ""

    @property
    def unit_price_usd(self) -> float | None:
        """单价（USD/张，图生图：1 输入图 + 1 输出图）。无公开价格时返回 None。"""
        if self.output_tokens_1k <= 0:
            return None
        in_cost = (self.output_tokens_1k / 1_000_000) * self.input_per_1m
        out_cost = (self.output_tokens_1k / 1_000_000) * self.output_per_1m
        return round(in_cost + out_cost, 6)

    @property
    def unit_price_cny(self) -> float | None:
        if self.unit_price_usd is None:
            return None
        return round(self.unit_price_usd * EXCHANGE_RATE_USD_TO_CNY, 4)


# 官方定价（Standard 付费档，2026-08-17 取自 ai.google.dev/gemini-api/docs/pricing）
MODEL_PRICING: dict[str, ModelPrice] = {
    "gemini-3.1-flash-lite-image": ModelPrice(
        id="gemini-3.1-flash-lite-image",
        label="Gemini 3.1 Flash Lite Image（Nano Banana 2 Lite · 最快最省）",
        input_per_1m=0.25,
        output_per_1m=30.00,
        output_tokens_1k=1120,
        tag="⚡ 极速预览 / 低消耗",
        description="大批量初筛构图、快速验证提示词效果，延迟极低，最省额度。",
    ),
    "gemini-2.5-flash-image": ModelPrice(
        id="gemini-2.5-flash-image",
        label="Gemini 2.5 Flash Image（Nano Banana）",
        input_per_1m=0.30,
        output_per_1m=30.00,
        output_tokens_1k=1290,
        tag="⚖️ 稳定基准（默认推荐）",
        description="日常主力模型，兼顾产品保真度与出图速度，性价比均衡。",
    ),
    "gemini-3.1-flash-image": ModelPrice(
        id="gemini-3.1-flash-image",
        label="Gemini 3.1 Flash Image（Nano Banana 2 · 标准）",
        input_per_1m=0.50,
        output_per_1m=60.00,
        output_tokens_1k=1120,
        tag="🚀 高保真复杂场景",
        description="针对结构复杂、带有微小文字/Logo、或需要精细光线反射的多模态融合任务。",
    ),
    "imagen-3.0-generate-002": ModelPrice(
        id="imagen-3.0-generate-002",
        label="Imagen 3.0 Generate（旧版 · 官网已无定价）",
        input_per_1m=0.0,
        output_per_1m=0.0,
        output_tokens_1k=0,
        tag="💎 商业广告级渲染",
        description="电商大促主图、宣传海报渲染，画质与物理光影质感最顶，生成耗时相对较长。",
        note="已弃用，官方定价页已无此模型，按 $0 计（不计费）。",
    ),
}


def get_price(model_id: str | None) -> ModelPrice | None:
    if not model_id:
        return None
    return MODEL_PRICING.get(model_id)


def unit_price_usd(model_id: str | None) -> float | None:
    p = get_price(model_id)
    return p.unit_price_usd if p else None


def compute_cost_usd(model_id: str | None) -> float:
    """单张（图生图）生成成本，单位 USD。无定价模型返回 0.0。"""
    up = unit_price_usd(model_id)
    return up if up is not None else 0.0


def catalog() -> list[dict]:
    """供前端构建模型下拉与单价展示的目录（不含任何密钥）。"""
    out = []
    for p in MODEL_PRICING.values():
        out.append(
            {
                "id": p.id,
                "label": p.label,
                "tag": p.tag,
                "description": p.description,
                "unit_price_usd": p.unit_price_usd,
                "unit_price_cny": p.unit_price_cny,
                "note": p.note,
            }
        )
    return out
