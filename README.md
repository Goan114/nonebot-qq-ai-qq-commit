# NoneBot 游戏反馈与 GitHub 更新中枢

面向 QQ 游戏群的两个联动插件，使用 NoneBot2、OneBot v11、SQLite、FastAPI 和兼容 Chat Completions 的 AI 服务。可连接 NapCat。一个机器人进程、一个共享问题库，多个仓库/分支独立监视。

## 功能

- 默认不把目标群每条消息送给 AI；可用 `FEEDBACK_LISTEN_ALL=true` 显式开启全量 AI 阅读，也可使用 `/反馈`、`反馈:` 临时会话或管理员回溯记录提交上下文。AI 识别游戏问题、提取设备与浏览器、保留完整文本和 OneBot 消息段、概括与归类。
- 设备类型、浏览器和有效异常描述缺一不可；缺失时提示重新完整提交。可选视觉 LLM 分析与问题关联的截图，识别错误文字及可见异常；设备和浏览器仍需文字填写。
- 相同根因、触发条件和适用环境的反馈合并；按 `COUNT(DISTINCT QQ)` 降序排列。同一 QQ 多次反馈会保留原文，但不增加优先级。
- 管理员维护结构化 FAQ。只有明确匹配解决方法才拦截，返回可自定义提示模板；FAQ 原网页不会自动抓取，需把有效问答录入 WebUI。
- 明确 AI 闲聊/提示注入的高置信度滥用自动永久拉黑；不完整反馈和正常求助不属于滥用。只有 bot 超级用户能在 QQ 里管理，群主/群管理员无额外权限。WebUI 令牌等价于机器人管理权限。
- 全局消息预处理器阻止黑名单 QQ 与此 NoneBot 进程内各插件交互，覆盖群聊和私聊；不会控制其它机器人进程或插件主动定时发送的消息。
- 拉黑/解封、按 QQ 删除指定反馈或全部反馈、问题评论和暂缓、重开、人工确认修复、管理审计。
- 各仓库/分支自定义轮询间隔，带 HMAC-SHA256 签名的 GitHub Push Webhook。交付 ID、仓库+分支+SHA、消息 ID 去重；AI 分析按仓库+SHA 复用，持久化队列及失败重试。
- 提交标题与代码 diff 一起交给 AI。只有值得玩家知道的更新才发送到群；纯内部生命周期、重构、测试等无用户感知改动不公告。摘要优先复用现有反馈的表象描述，保持一句话。所有仓库共享问题库，直接代码证据、较高置信度和完整 diff 才自动确认修复；其它建议进入人工审核。
- 按问题原始反馈群逐个 @ 反馈人，明确表述“上游代码已修复，尚未确认部署上线”。被拉黑或已删除反馈的用户不会收到修复通知。
- 带身份校验的 WebUI：问题人数/条数/状态、原文、FAQ、黑名单、修复证据、仓库状态、任务重试、操作记录。

## 快速启动

需要 Python 3.11+。建议在独立虚拟环境安装。在本目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
# 编辑 .env 后启动
.venv\Scripts\python bot.py
```

Linux/macOS 对应使用 `.venv/bin/python` 和 `cp .env.example .env`。

至少填写 `SUPERUSERS`、`FEEDBACK_GROUPS`、AI 的三个参数、`FEEDBACK_ADMIN_TOKEN` 和真实 `FEEDBACK_REPOS`。密钥可分别运行以下命令生成（不要使用同一个密钥）：

```shell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

在 NapCat 中启用 **OneBot v11 反向 WebSocket**，地址默认 `ws://127.0.0.1:8080/onebot/v11/ws`，填写相同 `ONEBOT_ACCESS_TOKEN`。将机器人加入目标群。

浏览器打开 `http://127.0.0.1:8080/feedback/`，使用管理令牌登录。令牌仅在页面内存中，刷新页面需重新登录。先在“常见问题”录入实际 FAQ。

### 接入已有项目

在已有 NoneBot 虚拟环境 `pip install /path/to/nonebot-feedback-suite`，确保使用 FastAPI 驱动和 OneBot v11 适配器，然后加载：

```python
nonebot.load_plugin("nonebot_plugin_feedback_manager")
nonebot.load_plugin("nonebot_plugin_commit_monitor")
```

也可在 NoneBot 项目的 `pyproject.toml` 中把这两个名字加入 `[tool.nonebot].plugins`。只加载反馈插件不会启动仓库监视；只加载监视插件会自动加载反馈插件。

## 使用示例

```text
/反馈 设备：苹果 iPhone 15；浏览器：Safari；内容：打开游戏进入第三关后黑屏，刷新后仍能复现。
反馈: 设备苹果，浏览器 Safari，第三关黑屏
记录@某人
/反馈管理 拉黑 123456789 多次尝试无关 AI 聊天
/反馈管理 解封 123456789
/反馈管理 删除 123456789 12,15
/反馈管理 删除 123456789 all
/反馈管理 标注 3 暂时不支持此浏览器，请换 Edge；兼容性修复正在排期。
/反馈管理 重开 3
/反馈管理 确认修复 7
/反馈管理 列表
```

“删除”的数字是**反馈记录 ID**；“标注/重开”的数字是**问题 ID**；“确认修复”的数字是**修复建议 ID**，可在 WebUI 中查到。默认命令前缀为 `/`。

`反馈:` 不需要命令前缀。发送后机器人记录本群接下来 1 分钟的讨论，期间所有人的发言都会进入这一条反馈上下文；只有发起人发送 `ok`、`结束反馈`、`停止反馈` 或 `反馈结束` 可以提前提交。机器人会提示继续补充设备、浏览器、复现步骤和异常现象。

`记录@某人` 仅 `SUPERUSERS` 可用，语义是**回溯命令发出前 5 分钟**，不是开启未来 5 分钟监听。只取当前管理员与被 @ 用户在本群的消息，并把反馈归属到被 @ 用户。为了支持回溯，目标群普通文本会在本地 SQLite 保留最多约 10 分钟的滚动缓冲；`FEEDBACK_LISTEN_ALL=false` 时这些缓冲消息不会因此自动发送给 AI。

## GitHub 配置

### 指定分支与多个分支推送（0.1.1）

不限于主分支。`branch` 可以填写 `develop`、`release/1.0`、`feature/new-ui` 等实际分支名，支持包含 `/` 的分支。使用精确匹配，不支持通配符；无需写 `refs/heads/` 前缀。同一个仓库监视多个分支时，在 `FEEDBACK_REPOS` 中写多条。例如 `.env` 使用一行 JSON：

```dotenv
FEEDBACK_REPOS=[{"name":"owner/game","branch":"main","poll_seconds":300,"announce_groups":["123456789"]},{"name":"owner/game","branch":"develop","poll_seconds":120,"announce_groups":["987654321"]},{"name":"owner/game","branch":"release/1.0","poll_seconds":180,"announce_groups":["123456789","987654321"]}]
FEEDBACK_GROUPS=["123456789","987654321"]
```

每个分支有独立的轮询间隔、断点和推送群。同一 SHA 从 develop 合入 main 后，两个分支各自推送一次，重复轮询/Webhook 不会重复入队。AI 摘要及修复判断按仓库和 SHA 复用，同一问题不会因分支合并再次 @ 通知。首次建立某分支基线不补发历史提交。

群内更新格式保持极简：

```text
【th06 @ eagler 更新】
【https://github.com/YomotsuHisami/th06/commit/<sha>】
修复了使用 thprac 进行练习重开时录像录制状态未重置的问题。
```

如果 commit 只有内部生命周期、重构、测试、构建等玩家无法感知的变化，则不发群公告。玩家可直接使用的文档内容可以公告，但只能客观写成“更新了多人游戏说明文档，补充……”，不能包装成游戏功能变化。

Webhook 在仓库中配置一次即可，插件按 payload 的 `refs/heads/具体分支` 路由至对应监视配置；未配置的分支不会推送。所有已监视分支都参与共享问题库的修复判断，通知标明最初确认修复的分支，并不意味着已合入主分支或上线。

从 0.1.0 升级可直接安装新包，保留原 SQLite 数据，无需重建数据库。旧队列中未带分支字段的任务仍可处理，其通知不显示分支；新任务使用分支级去重键。

每个 `FEEDBACK_REPOS` 条目包含 `name: owner/repo`、`branch`、`poll_seconds`（至少 30 秒）、`announce_groups`。多个条目可使用同一仓库的不同分支。`announce_groups` 必须同时处于 `FEEDBACK_GROUPS` 才发送摘要；修复通知自动发回反馈原群。私有仓库需要拥有 Contents 只读权限的 GitHub token；公共仓库也建议配置 token 以提高限额。

首次轮询只记录当前 HEAD，不发送历史更新。之后停机期间的新提交会按分页补录。Webhook 只起唤醒作用，真实提交和 diff 仍通过 GitHub API 读取，不信任 payload 中的提交说明。第一次由 webhook 初始化时，会使用 payload 的 `before` 尝试补录本次推送；新建分支无有效 before 时建立当前基线。

仓库 Settings → Webhooks → Add webhook：

- Payload URL：`https://your-bot.example/feedback/github/webhook`
- Content type：`application/json`
- Secret：与 `FEEDBACK_WEBHOOK_SECRET` 相同
- Events：仅 Push

公网入口需要反向代理与 HTTPS；本地默认只监听 `127.0.0.1`。可只公开 webhook 路径，把 WebUI 留在内网。不配置 webhook 密钥就仅使用轮询。签名不正确返回 403，未配置仓库/分支的推送被忽略。大于 2MB 的 webhook 返回 413，轮询可补偿。

force-push 后旧游标不可达，或超过 `FEEDBACK_HISTORY_PAGES`（默认 100 页，每页 100）时，监视器保留旧游标并显示错误。管理员确认历史后可从 WebUI 重置基线；重置意味着跳过尚未入队的旧历史。

## AI 与自动化边界

### 可选截图视觉分析（0.2.0）

默认关闭。支持兼容 `/chat/completions` 的多模态视觉 LLM，需支持 `image_url` 的 Base64 图片输入及 JSON object 输出。视觉模型可与文字模型不同，视觉服务也可单独配置：

```dotenv
FEEDBACK_VISION_ENABLED=false
FEEDBACK_VISION_BASE_URL=https://your-vision-provider.example/v1
FEEDBACK_VISION_KEY=填写视觉服务密钥
FEEDBACK_VISION_MODEL=填写实际视觉模型ID
FEEDBACK_VISION_MODELS=["填写实际视觉模型ID","另一个支持图像的模型ID"]
```

视觉地址/密钥留空时分别复用 `FEEDBACK_AI_BASE_URL` / `FEEDBACK_AI_KEY`。模型不自动沿用文字模型，避免把截图送给不支持图像的服务。可选模型列表中的模型使用同一个视觉服务地址和密钥。配置服务或增加候选模型后需要重启。

机器人管理员可以随时执行下列命令，WebUI 顶部也提供模型选择与开关：

```text
/反馈管理 视觉 开启
/反馈管理 视觉 关闭
/反馈管理 视觉 状态
/反馈管理 视觉模型 实际模型ID
```

开关和选择持久化到数据库，重启保留，优先于 `.env` 的初始开关和模型。关闭后不再发起新的视觉请求；已经向服务商发出的请求无法撤回，但检测到关闭后不会接收其视觉结果。关闭不会删除历史分析。

两种明确关联方式：

1. 在同一条消息中发送“设备：苹果，浏览器：Safari，进入第三关黑屏，如图”并附截图（也可用 `/反馈`）。先判断文字是否与游戏问题相关，再读取同条截图；无关闲聊或纯图片不会调用视觉模型。
2. 已收到问题编号后，用 `/反馈补图 3 [补充说明]` 并在同条消息附截图。只能给本人在当前群提交过的未解决问题补图。补图附在原反馈下，不增加反馈人数或条数。

不自动把单独图片、引用消息或相邻聊天猜测为某个问题。图片中与问题无关或置信度不足的内容会标记为“关联不明确”，不参与分类和 commit 修复分析。图片内的聊天/指令不会导致自动拉黑。静态截图不能证明真实帧率或代码根因。

WebUI 问题详情展示截图分析所用模型、识别文字、观察结果、限制及关联反馈。commit 分析会参考已关联的视觉结果；补图期间问题版本会更新，避免旧的修复分析直接覆盖新证据。设备、浏览器仍要求文字原文证据，不能靠截图猜测。未通过完整性检查或已命中 FAQ 的反馈不会收录截图记录。

每次默认最多 3 张，每张最多 5 MB，支持 PNG/JPEG/WebP，可通过 `FEEDBACK_VISION_MAX_IMAGES` 和 `FEEDBACK_VISION_MAX_BYTES` 调整。图片仅从 `FEEDBACK_VISION_IMAGE_HOSTS` 中精确匹配的公网 QQ 图片域名下载，禁止本地文件、内网地址及重定向。如果 OneBot 返回其它图片域名，管理员需核实后加入列表；仅返回本地文件路径的协议端需要配置为提供可访问的 QQ 图片 URL。

启用后截图会下载到内存，并以 Base64 发送到所选视觉服务；不会把带临时参数的 QQ 图片链接直接提交给模型。数据库保存消息段、补图原文及分析结果，不长期保存截图二进制文件，QQ 原始图片链接可能过期。视觉服务失败、图片过期或不合规格时会提示，本次文字反馈仍可正常处理，之后可重新补图。

从旧版本升级会自动将数据库迁移至版本 3，保留原记录，并增加临时聊天缓冲、上下文会话和 commit 公告标记。升级前请备份数据库，升级后不要用旧程序直接打开已迁移的数据。

默认开启 `FEEDBACK_AUTO_RESOLVE=true`（阈值 0.95）与 `FEEDBACK_AUTO_BAN=true`（阈值 0.98）。AI 分数不是经过校准的概率，仍可能误判。初次上线可将两者设为 `false` 观察；关闭自动修复后建议仍进入人工审核，关闭自动拉黑后不自动封禁。

匹配每 30 条问题/FAQ 一批，修复分析每 20 个未解决问题一批，遍历全部候选，避免仅高优先级问题被分析。问题很多时会增加费用和延迟。`FEEDBACK_LISTEN_ALL` 默认 `false`：普通群消息只有命中 `FEEDBACK_KEYWORDS` 时才单独送 AI；设为 `true` 才会逐条分析目标群普通文本。`/反馈`、`反馈:` 会话和 `记录@某人` 不受此开关限制。每个 QQ 默认 30 秒普通提交冷却，可配置为 0。

一分钟会话和管理员回溯记录都是“先收集上下文，结束后一次性送 AI”，不会逐句调用模型。上下文包含多名参与者时不会根据第三方的闲聊、提示注入或其它内容自动拉黑反馈归属用户。

设备和浏览器必须附带原文中存在的逐字证据。FAQ 不匹配时不胡乱推荐。commit 自动修复必须包含输入 patch 中真实增删行的引用；缺失 patch、二进制文件、超过 100 个文件或 45000 字符等情况会关闭该提交的自动修复，保留人工审核。语义是否足以证明修复仍由模型判断，人工可拒绝、确认或重开。

原文、QQ 和相关内容会保存到本地 SQLite，并随分析请求发往你配置的 AI 服务。请使用适合你社群数据要求的服务并告知成员。不要将数据库、`.env`、令牌上传到公共仓库。

## 运行与维护

### 发送冷静期（0.2.1）

`FEEDBACK_SEND_COOLDOWN_SECONDS=10` 设置机器人向同一个目标群发送消息的最小间隔，单位秒，默认 10 秒，设为 0 关闭额外冷静期。修改后重启生效。各群独立计时，多个仓库或机器人账号发往同一群共用冷静期；当前仍只支持单进程运行。

反馈回执、缺字段/FAQ 提示、截图补充回执、commit 摘要、修复 @ 通知，以及目标群内的管理员命令回复都经过发送队列。冷静期内保留消息等待发送，不会因等待消耗失败重试次数。同一成员在同一群反复触发的即时提示（如提交过于频繁）最多保留一条待发送记录。普通反馈结果、commit 摘要和修复通知不因冷静期被丢弃，集中更新较多时可能较晚送达。

最近发送时间保存在 SQLite 中，重启后继续生效；失败的发送尝试也遵守间隔和原有退避规则。此设置只影响本插件在 `FEEDBACK_GROUPS` 中的群消息，私聊管理回复、非目标群管理回复和其它插件不受影响。

原有 `FEEDBACK_COOLDOWN_SECONDS=30` 控制每个 QQ 提交反馈的间隔，与机器人发送冷静期是两个独立设置。

- **只运行一个 NoneBot 进程/一个 ASGI worker**。当前 SQLite 队列为单进程设计，不支持多个实例竞争任务。
- 网络或 AI 失败使用指数退避，最多 8 次，失败后留在 WebUI“任务队列”供手动重试。不会因模型不可用而直接判定成功/滥用。
- 通知使用持久化 outbox。在通常重试、重复 webhook 和正常重启时不会重复入队；QQ 发送成功但进程在写回状态前崩溃的极小窗口可能重复发送（OneBot 无幂等发送键），不承诺 exactly-once。
- 管理令牌是持有者凭据，只有机器人管理员应持有。所有 Web API 都要求 Authorization Bearer；页面无第三方脚本，不使用 `innerHTML` 渲染反馈，CQ 消息使用纯文本段构造。
- 配置变更需重启。FAQ 和管理员评论立即生效。数据库已有版本号；拒绝打开比当前程序更高版本的数据库。
- 备份时停止 bot 后复制 `data/`。数据库保存原文、AI 工作任务和审计记录，没有自动保留期清理；“删除反馈”删除有效反馈行并重新计算人数，**并非隐私擦除**，历史任务 payload 和审计仍可能包含原文或 QQ。
- 普通 `/反馈` 与关键词反馈仍只使用单条消息，不自动拼接前后文；`反馈:` 一分钟会话和管理员 `记录@某人` 是明确的上下文收集例外。关联截图在视觉功能启用后参与分析，音频不参与 AI 分析。
- 已解决问题不会与后来的新反馈合并，避免复发被隐藏；新问题会重新统计，也可手动重开原问题。

## 测试

```shell
python -m pip install -e ".[test]"
python -m pytest
python -m ruff check .
```

自动化测试使用假的 AI/GitHub/QQ 发送端，不需要真实 token，也不会向真实群发送消息。真实服务联调需要你配置账号后完成：提交完整/缺失字段反馈，验证 FAQ，提交测试修复，检查 @ 通知及 WebUI 状态。

实现参考：[NoneBot 生命周期钩子](https://nonebot.dev/docs/advanced/runtime-hook)、[OneBot 适配器](https://onebot.adapters.nonebot.dev/docs/guide/installation/)、[GitHub 提交 API](https://docs.github.com/en/rest/commits/commits)、[GitHub Webhook 签名校验](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)。
