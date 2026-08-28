# 统一场景生成提示词 · 生成规范（供校验）

> 用途：让 Gemini 校验本系统「图生图（场景生成）」的提示词拼装规则是否完整、自洽、无遗漏。
> 范围：后端自动拼装引擎 + 前端预设库 + 前端修饰标签 + 输出比例处理。

---

## 一、整体流程（一句话）

无论前端选了哪个预设、填了什么词、追加了哪些标签，后端在向 Google AI Studio（Gemini / Imagen）发起请求前，都会把提示词**严格拼装成固定四层结构**。其中第 1、2、4 层由后端强制注入（用户不可删），第 3 层为用户在文本框中看到并提交的原文（已含预设 / 比例 / 修饰标签）。

---

## 二、四层拼装引擎（后端强制，顺序固定）

层与层之间用**两个换行符**（`\n\n`）分隔。

### Layer 1 · 主体保真与形状锁定（强制，必须置顶）

- **单图批量模式（默认 `single`）**：
```
Strictly preserve the exact product, shapes, contours, materials, colors, brand logos, and textures from the provided reference image. Do not warp, distort, stretch, or alter the product in any way.
```
- **多角度合成模式（`multi_angle_fusion`）**：Layer 1 切换为「融合锁」，强调多张参考图是同一产品的不同角度，需理解其完整 3D 结构与材质，再一次性生成单张统一场景图（见第十一节）。
```
The provided multiple reference images show the SAME product from different angles and perspectives. Understand the 3D structure, authentic materials, and complete details of the product from all reference angles. Generate a single coherent commercial scene featuring this product naturally placed in the environment, strictly preserving its genuine form and branding.
```

> **模式分支规则**：`multi_angle_fusion` 模式下 `white_bg` 分支被强制关闭（融合锁与纯白底互斥），始终采用普通 Layer 2 / Layer 4。

### Layer 2 · 物理接触、真实比例与透视纠正（强制，防畸变核心）
```
The product must be firmly grounded and physically resting on the contact surface with realistic ambient occlusion and accurate contact shadows directly beneath the base to completely eliminate any floating appearance. Maintain true-to-life real-world physical scale, natural proportional balance, and a standard commercial product perspective (simulating an 85mm lens, zero wide-angle distortion).
```

### Layer 3 · 场景主体描述（用户原文，唯一可由用户控制层）
> 内容为前端文本框提交值，至少包含以下之一或全部：
> - 用户手动输入的画面描述
> - 从下方「十大预设」一键填入的场景文本
> - 输出比例预填短语（见第六节）
> - 镜头视角 / 光影氛围修饰标签（见第五节，追加到末尾）
>
> 若用户未填任何内容，Layer 3 整层省略（但不影响 Layer 1/2/4 生效）。

### Layer 4 · 商业级画质渲染与光影融合（强制，必须置尾）
- 普通场景模式：
```
Professional commercial advertising photography, balanced natural reflections matching the surrounding environment, hyper-realistic textures, clean optical depth of field, 8k resolution.
```
- 纯白底模式（与 Layer 2 同步切换）：
```
High-contrast studio lighting, razor-sharp clean edges, pristine e-commerce product catalog standard, 8k resolution.
```

> **智能分支规则**：后端依据前端传入的 `is_white_bg=true` 标识，或自动识别 Layer 3 是否命中 A 组纯白预设短语（如 "pure solid white background" / "#FFFFFF" / "white background"），决定 Layer 2 / Layer 4 采用「普通场景」还是「纯白底」版本，从而防止纯白底生成多余杂色背景。

### 拼装伪代码
```
white_bg = is_white_bg_flag if provided else detect_white_bg(user_prompt)
white_bg = white_bg and not multi_angle          # 融合模式强制关闭白底分支
LAYER_1 = FUSION_LAYER1 if multi_angle else FIDELITY_LOCK
LAYER_2 = PHYSICS_LOCK_WHITE if white_bg else PHYSICS_LOCK
LAYER_4 = QUALITY_RENDER_WHITE if white_bg else QUALITY_RENDER
parts = [LAYER_1, LAYER_2]
if user_prompt.strip() not empty:
    parts.append(user_prompt)   # Layer 3
parts.append(LAYER_4)
final_prompt = "\n\n".join(parts)
```

---

## 三、十大高转化电商预设场景库（前端分组，一键填入 Layer 3）

点击预设 = 将对应英文文本整段写入文本框（替换原文）。分组如下：

### A 组 · 电商规范主图（纯白系列）
1. ⚪ 电商纯白底（带真实地影）
   `Isolate the product on a completely pure solid white background (RGB 255, 255, 255), seamless studio backdrop, subtle and soft realistic contact shadow underneath the base, professional commercial studio lighting, high-contrast, razor-sharp clean edges, zero background artifacts, crisp 8k e-commerce listing photography.`
2. ✂️ 纯白无影抠图底
   `Extract and isolate the exact product onto an absolute pure blank white background (hex #FFFFFF), perfectly flat solid background, no shadows, no reflections, pin-sharp contour edges, clean isolation cutout style.`

### B 组 · 室内生活与家居
3. 🏠 温馨现代室内
   `placed naturally on a clean neutral-toned tabletop inside a modern cozy interior, soft ambient warm sunlight coming from a nearby window, blurred contemporary home background with subtle aesthetic decor, realistic contact shadows, f/2.8 shallow depth of field.`
4. 🪵 极简原木家居
   `placed on a natural light-oak wooden table, soft morning sunlight casting gentle shadows, blurred minimalist warm living room background, clean organic texture.`
5. 🏛️ 奢华大理石台面
   `placed on a smooth white Carrara marble pedestal, luxury bathroom countertop, soft diffused studio lighting, subtle water ripples, elegant high-end commercial ad.`
6. 💼 现代办公桌面
   `placed on a clean dark grey matte office desk, next to a subtle laptop edge and small succulent plant, modern bright office ambient lighting, crisp focus.`

### C 组 · 室外全维度自然与街头
7. 🌲 自然林间草木
   `placed securely on a rustic natural stone platform outdoors, surrounded by fresh lush greenery and subtle wild grass, soft golden hour sunlight filtering naturally through the background, authentic outdoor environment with rich atmospheric depth.`
8. 🏖️ 海边沙滩阳光
   `placed firmly on dry fine golden sand next to a smooth ocean pebble, crisp natural outdoor sunlight from a 45-degree angle casting realistic contact shadows beneath the base, subtle turquoise sea waves and sunny sky gently blurred in the far background.`
9. 🏕️ 户外露营木台
   `resting naturally on a weathered rustic cedar picnic table, accurate physical scale with realistic wood grain texture, soft ambient daylight filtering through pine trees, subtle pine cones creating scale reference, razor-sharp contact shadows.`
10. 🏙️ 现代街头水泥台
    `placed securely on a clean industrial architectural concrete ledge outdoors, bright overcast daylight providing balanced natural reflections, blurred modern glass architecture and street trees in the background, sharp grounded shadows.`

---

## 四、一键扣白底图（独立预设，已包含在 A 组第 1 条）

即「⚪ 电商纯白底（带真实地影）」预设（见上），其完整英文文本即为扣白底图提示词，与 Layer 1/2/4 共同生效。

---

## 五、快捷修饰标签器（前端，点击追加 / 取消到 Layer 3 末尾）

### 镜头视角修饰器
- 👁️ 经典平视 → `, eye-level front view`
- 📐 35°微俯视 → `, 35-degree high-angle commercial product shot`
- 🔍 微距特写 → `, close-up macro product lens`

### 光影氛围修饰器
- ☀️ 柔和自然晨光 → `, soft diffused morning sunlight`
- 💡 摄影棚专业侧光 → `, professional 45-degree studio side lighting`
- 🌇 温暖黄昏光影 → `, warm golden hour sunset lighting`
- ⚡ 戏剧感硬光高反差 → `, dramatic high-contrast studio lighting with crisp geometric shadows`

> 规则：再次点击同一标签即从 Layer 3 中移除；可多选叠加。

---

## 六、输出比例处理

1. 前端比例按钮：原图比例 / 1:1 / 4:3 / 3:4 / 16:9 / 9:16。
2. 选择非「原图」比例时，会把对应**比例指引短语**注入 Layer 3（由模型在生成场景时自然扩展环境以匹配该比例，**不再用 Canvas 物理塞白边**，避免硬留白与比例畸变）：
   - 1:1 → `composition framed in a 1:1 square aspect ratio`
   - 4:3 → `composition framed in a 4:3 landscape aspect ratio`
   - 3:4 → `composition framed in a 3:4 portrait aspect ratio`
   - 16:9 → `composition framed in a 16:9 wide landscape aspect ratio`
   - 9:16 → `composition framed in a 9:16 vertical aspect ratio`
3. 物理统一：原图以**原始分辨率**上传（不再经 Canvas 居中补齐 / 裁剪），Google 模型输出的图比例跟随输入图比例；比例一致性由第 2 步的指引短语在语义层约束，而非强制白边。（模型本身不接收 `aspectRatio` 参数。）

---

## 七、记录与复用

- 每条任务落库两条提示词：`prompt`（用户原文，即 Layer 3）与 `full_prompt`（四层完整版，可直接贴回 AI Studio 复用）。
- 任务卡 / 详情页提供「复制提示词」按钮，复制的是 `full_prompt` 四层完整文本。

---

## 八、最终拼装示例（供 Gemini 比对）

若用户选择「⚪ 电商纯白底（带真实地影）」+ 比例 1:1 + 修饰「👁️ 经典平视」，后端最终发给 Google 的提示词为：

```
Strictly preserve the exact product, shapes, contours, materials, colors, brand logos, and textures from the provided reference image. Do not warp, distort, stretch, or alter the product in any way.

The product must be firmly grounded and physically resting on the contact surface with realistic ambient occlusion and accurate contact shadows directly beneath the base to completely eliminate any floating appearance. Maintain true-to-life real-world physical scale, natural proportional balance, and a standard commercial product perspective (simulating an 85mm lens, zero wide-angle distortion).

Isolate the product on a completely pure solid white background (RGB 255, 255, 255), seamless studio backdrop, subtle and soft realistic contact shadow underneath the base, professional commercial studio lighting, high-contrast, razor-sharp clean edges, zero background artifacts, crisp 8k e-commerce listing photography. Output aspect ratio 1:1 (square composition)., eye-level front view

Professional commercial advertising photography, balanced natural reflections matching the surrounding environment, hyper-realistic textures, clean optical depth of field, 8k resolution.
```

---

## 十一、多角度合成模式（`multi_angle_fusion`）

### 触发与约束
- 前端「🅱️ 模式B · 多角度合成」：需上传 **2~4 张同一产品的不同角度图**（如正视 / 45° / 细节图）。
- 后端 `POST /api/tasks/create` 接收 `mode=multi_angle_fusion`：
  - `total_count = 1`（一次性融合生成 **1 张**场景图，而非 N 张）；
  - 仅写入 **1 个 TaskItem**，其 `original_paths` 字段以 JSON 数组记录全部参考图相对路径（前端历史卡据此渲染多张原图）；
  - `white_bg` 分支被强制关闭（融合锁与纯白底互斥）。

### 请求体构造（后端 → Google）
- 多个参考图作为**同一个 `contents[0].parts` 下的多个 `inlineData`** 发送（camelCase：`inlineData` / `mimeType`），而非多次请求；
- 提示词按第十节伪代码组装：`LAYER_1 = FUSION_LAYER1`，`LAYER_2 / LAYER_4` 采用普通（非白底）版本；
- 单任务、单图产出，对应 1 条计费记录（`cost_usd` 按所选模型单价计 1 张）。

### 与单图批量模式（模式A）的差异
| 维度 | 模式A `single` | 模式B `multi_angle_fusion` |
|---|---|---|
| 上传图数 | 1~N（可多选） | 2~4（同产品多角度） |
| 产出图数 | N 张（每图独立场景） | 1 张（融合场景） |
| 提示词 Layer 1 | FIDELITY_LOCK | FUSION_LAYER1 |
| 参考图发送 | 每张单独一次请求 | 多图同一次请求（多 inlineData） |
| TaskItem 数 | N | 1（`original_paths` 记全部图） |
| 白底分支 | 可开关 | 强制关闭 |
