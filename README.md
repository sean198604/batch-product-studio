# 场景图生成工具（批量白底产品图 → 场景图，v2）

企业内部 FastAPI 服务：批量上传白底产品图，调用 Google AI Studio（Gemini /
`gemini-2.5-flash-image`）生成场景图。**内置严格防 429 流控**，并配套完整的
用户鉴权（角色）、数据统计分析后台与文件物理生命周期管理。

---

## 功能一览

- **防 429 限流（核心）**
  - 全局并发信号量 `asyncio.Semaphore(1)`（单工作通道）。
  - 每次成功生图后强制 `asyncio.sleep(3.5s)`，实测频率 ~17/min，远低于 10 RPM 上限。
  - HTTP `429` / `5xx` / 网络错误：指数退避重试 **5/10/20s，最多 3 次**；耗尽则单图标记失败，不影响队列后续。
- **持久化（SQLite + SQLModel）**：User / GenerationTask / TaskItem 三张表；应用启动自动建表；进程重启后自动恢复未完成任务。
- **用户与鉴权**：JWT；首个注册账号自动成为 `admin`，其余为 `staff`；每成功一次 API 调用**原子递增**该用户的 `total_api_calls` 与 `total_images_generated`。
- **文件生命周期管理**：`DELETE /api/items/{id}` 与 `DELETE /api/tasks/{id}` 物理删除磁盘文件并同步删除数据库记录（员工本人或管理员可删）。
- **管理员后台**：统计看板（员工数 / 总调用 / 总生成图）、员工列表、全公司任务分页查看与强制删除。
- **本地存储**：原图 `storage/uploads/{task_id}/`，结果 `storage/outputs/{task_id}/`，经 `/storage` 静态路由预览。
- **前端 SPA**：登录/注册弹窗、员工批量生成控制台、个人历史（分页/删除）、管理员专属统计后台 Tab。
- **「保真锁」后端 Prompt 智能拼装（核心）**：后端在把提示词发给 AI Studio **之前**自动包裹两层——① 保真约束（严格保留原产品主体 / 形状 / 材质 / Logo / 纹理，仅替换背景与环境光影）；② 高品质商业摄影修饰词（`ultra-detailed, photorealistic, 8k commercial photography, realistic contact shadows`）。员工无论怎么写提示词都不会破坏主体一致性。常量与拼装逻辑集中在 `app/prompts.py`。
- **前端场景预设 / 光影风格**：控制台提示词框上方提供 5 个电商常用「场景快捷预设」按钮（极简原木家居 / 奢华大理石 / 自然户外绿植 / 现代办公桌面 / 艺术光影棚拍），点击即把高质量英文场景描述填入框内，可继续微调；另有 4 个「光影风格」标签（柔和晨光 / 专业摄影棚侧光 / 高级冷调 / 自然背光）点击追加或取消到提示词末尾。
- **生图模型选择器**：控制台下拉选择本次任务的模型（默认 `gemini-2.5-flash-image`，备选 `imagen-3.0-generate-002`），**按任务粒度覆盖**后台默认模型，无需改动全局配置。

---

## 目录结构

```
batch-product-studio/
├── main.py                # FastAPI 入口（lifespan / 路由 / 静态挂载）
├── app/
│   ├── config.py          # 全部可调配置（限流、Key、JWT、路径）
│   ├── db.py              # 异步引擎 + 会话 + 自动建表
│   ├── models.py          # SQLModel 表 + Pydantic 响应模型
│   ├── security.py        # 密码哈希 + JWT
│   ├── deps.py            # get_session / get_current_user / require_admin
│   ├── gemini.py          # AI Studio 调用 + 退避重试（自动读取代理环境变量）
│   ├── prompts.py         # 保真锁常量 + Prompt 自动拼装（assemble_prompt）
│   ├── worker.py          # asyncio.Queue + Semaphore(1) + 3.5s 节奏 + DB 更新 + 重启恢复
│   └── routes/{auth,tasks,admin}.py
├── static/index.html      # 单页前端
├── storage/               # 运行时：uploads/ outputs/（挂载持久化）
├── data/                  # 运行时：app.db（SQLite，挂载持久化）
├── requirements.txt · .env.example · Dockerfile · docker-compose.yml
```

---

## 本地运行

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # 填 GEMINI_API_KEY 与强随机 JWT_SECRET_KEY
uvicorn main:app --host 0.0.0.0 --port 7021 --workers 1
```

> ⚠️ **必须 `--workers 1`**：队列与任务状态在进程内存中（SQLite 是共享的，但队列不是）。

访问 http://localhost:7021 使用前端；首个注册者即为管理员。交互式文档：/docs

---

## Docker 部署

```bash
# 1) 在 .env 中填好 GEMINI_API_KEY 与 JWT_SECRET_KEY
#    （可选）如需代理访问 Google，设置 HTTP_PROXY / HTTPS_PROXY

# 2) 一键构建 + 启动
docker compose up -d --build

# 3) 停止
docker compose down
```

- 镜像基于 `python:3.11-slim`，设置 `PYTHONUNBUFFERED=1` 与清华 PyPI 镜像加速安装。
- `docker-compose.yml` 将 `./storage` 挂载至 `/app/storage`、`./data` 挂载至 `/app/data`，数据库与图片重启不丢。
- 通过 `env_file: .env` 自动注入所有环境变量；`httpx` 默认读取 `HTTP_PROXY` / `HTTPS_PROXY`，因此容器内对 Google API 的调用会自动走代理（前提是你在 `.env` 里填了代理）。

### ⚠️ 数据持久化与「单实例」铁律（务必遵守）

数据库文件始终落在 **宿主机的 `./data/app.db`**（容器内对应 `/app/data/app.db`，由 `./data:/app/data` 绑定挂载）。只要通过 `docker compose up` 启动，**注册账号、历史任务在容器 `restart` / `down`+`up` 后都不会丢**。

踩坑记录（已发生）：数据库被「清空」几乎都是下面两种情况之一 ——

1. **同一台机器同时跑了两个实例、抢同一个 7021 端口、用两个不同的库。** 例如一边用 `docker compose` 跑、另一边又用 `python main.py` / `uvicorn` 直接在宿主机起了个服务。两个实例各自的 `./data/app.db` 若路径解析不一致（或其中一个没挂卷），你在 A 上注册、过会儿连到 B，就会以为「数据没了」。
   - **务必只跑一个。** 跑 Docker 前先确认 7021 没被别的进程占用：`netstat -ano | findstr 7021`。
2. **用 `docker run`（没挂卷）或老的 compose 启动**，数据库写在容器可写层，容器一重建即清空。
   - 始终用本仓库的 `docker-compose.yml`（已含 `./data` 挂载），不要手写 `docker run` 省略 `-v`。

**验证持久化（推荐跑一次确认）：**
```bash
# 启动后注册一个账号，然后“重启容器”（不是重建镜像）再登录，账号应在
docker compose restart            # 模拟宕机/重启，数据来自宿主机 ./data/app.db
# 浏览器打开 http://localhost:7021 用刚才的账号登录 —— 仍可用即证明持久化 OK
```
启动日志里会打印一行 `SQLite database at: /app/data/app.db`，确认路径在挂载卷内即可放心。

> 注意：不要用 `docker compose down -v`（`-v` 会删除**命名卷**；本仓库用的是宿主机绑定挂载，理论上不受影响，但养成不用 `-v` 的习惯更稳）。`.dockerignore` 已排除 `data/`、`storage/`，不会把运行时数据打进镜像。

---

## 环境变量（.env）

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `GEMINI_API_KEY` | AI Studio Key | 必填 |
| `GEMINI_MODEL` | 生图模型 | `gemini-2.5-flash-image` |
| `REQUEST_INTERVAL_SECONDS` | 成功调用后强制间隔 | `3.5` |
| `SEMAPHORE_CONCURRENCY` | 并发通道（保持 1） | `1` |
| `MAX_RETRIES` | 429/5xx 最大重试 | `3` |
| `RETRY_BACKOFF_SECONDS` | 退避等待（逗号分隔） | `5,10,20` |
| `JWT_SECRET_KEY` | JWT 签名密钥（请改！） | 占位 |
| `JWT_ALGORITHM` | JWT 算法 | `HS256` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | Token 有效期 | `1440` |
| `STORAGE_DIR` / `UPLOADS_DIR` / `OUTPUTS_DIR` / `SQLITE_PATH` | 存储路径 | 见 `.env.example` |
| `HTTP_PROXY` / `HTTPS_PROXY` | 可选代理 | 空 |

> **配置 Gemini Key 的两种方式（推荐第二种）**
> 1. **文件方式**：在 `.env` 填 `GEMINI_API_KEY=你的key`（需重启服务生效）。
> 2. **后台可视化（推荐，无需重启）**：以管理员身份登录 → 进入「管理后台」→ 顶部「API 配置」卡片，粘贴 Key、可改 API 地址与生图模型，点「保存配置」立即生效；点「测试连接」会用 models 列表接口校验 Key 是否有效（不消耗生图额度）。配置保存在 `data/settings.json`，优先级高于 `.env`。
>
> 对应接口：`GET/PUT /api/admin/settings`、`POST /api/admin/settings/test`（均要求 admin 权限）。
>
> ⚠️ **关于 `gemini-2.5-flash-image`（生图模型）的额度**：该模型已从免费档移除，**免费额度为 0**。Key 鉴权有效（测试连接会显示成功、能列出模型）≠ 能生图——未给该 Key 所属 Google 账号**开通计费（绑定付费方案）**前，任何生图请求都会返回 `429 Quota exceeded ... limit: 0`。这是账号额度问题，不是代码或地址问题。开通计费后即按张计费生图。失败的任务会在 item 的报错里显示 Google 原文与中文提示（"该模型免费额度为 0，需开通计费"），便于排查。

---

## API 速览

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| `POST` | `/api/auth/register` | 公开（首个=admin） |
| `POST` | `/api/auth/login` | 公开 |
| `GET` | `/api/auth/me` | 登录 |
| `POST` | `/api/tasks/create` | 登录（表单支持 `model` 字段覆盖模型） |
| `GET` | `/api/config` | 公开（返回后台默认模型名，供前端预选） |
| `GET` | `/api/tasks/{task_id}` | 本人或 admin |
| `GET` | `/api/tasks/my-history` | 登录（分页） |
| `GET` | `/api/tasks/{task_id}/download-zip` | 本人或 admin |
| `DELETE` | `/api/items/{item_id}` | 本人或 admin |
| `DELETE` | `/api/tasks/{task_id}` | 本人或 admin |
| `GET` | `/api/admin/dashboard/stats` | admin |
| `GET` | `/api/admin/users` | admin |
| `GET` | `/api/admin/tasks/all` | admin（分页） |

---

## 设计要点

1. **限流发生在全局 Worker**：无论多少任务，同一时刻仅 1 个生图请求，且两次成功请求间隔 ≥ 3.5s。
2. **429 重试**：`gemini.py` 捕获 429/5xx/网络错误并按 5/10/20s 退避；耗尽才标记单图 `failed`。
3. **单图失败不阻塞**：队列继续消费，失败信息写入 `error_message`。
4. **计数器原子性**：成功生图时在同一 DB 事务内递增 `User.total_api_calls` 与 `total_images_generated`。
5. **重启恢复**：启动时 `worker.recover_pending()` 扫描 `pending/processing` 的 Item 重新入队，DB 为唯一可信源。
6. **删除即清理**：删除 Item/Task 会一并物理删除 `storage/` 下对应文件并移除空目录。
7. **保真锁（后端强制）**：`worker._process` 在调用 `generate_image` 前用 `assemble_prompt(task.prompt)` 包裹，强制前置保真约束、后置商业摄影修饰词；`model` 按任务粒度传入，覆盖后台默认模型。
