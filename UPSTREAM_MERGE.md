# 本地 4.4.1 选择性合并记录

## 版本与边界

2026-10-24 核对：官方最新为 [4.6.3，cafd4ea](<https://github.com/ptrel1/mais_art_journal/commit/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5>)，与本地 `88b6387` 的共同祖先为 `295382d`。本地保留 S1/S2 重构，只移植不冲突的修复；本版本不宣称包含上游全部新功能。

源码先复制到运行目录之外，逐文件摘要核对后修复及回归，避免每次编辑都触发线上全量插件重启。此次不修改实际模型连接、正式 Key、人设图路径、QQ 配置或长期自动调度开关。

## 已合并的上游修复

| 范围 | 合并结果 |
|---|---|
| ComfyUI 等待预算 | 未合入。独立复核发现，单独延长轮询不能覆盖上传、提交、下载及重试总时限，线程取消也不会停止同步 worker。保留原值，等待整体整改。 |
| ComfyUI 必需参考 | 工作流缺少 `${image}` 时抛出不可重试错误；上传、排队与提交前终止，不丢图转文生图。 |
| SDK 配置访问 | `ClientContext.get_config` 与代理配置读取不再吞异常。保留本地 `ctx/legacyctx` 调用兼容及禁环境代理行为。 |
| 外层执行预算 | 随长轮询集群一并暂缓；未改旧公开 API 和命令元数据。S2 普通 Tool 保持 600 秒，不搬入上游额度或整套流水线。 |
| 授权文本 | 合入新版代码沿用上游 AGPL-3.0-or-later；完整 [LICENSE](<LICENSE>) 与原 [MIT 通知](<LICENSE.MIT>) 同时保留。 |

## 本地修复

日程 API 异常、插件初始化错误、无效快照明确失败，仅有效快照的 `has_activity=false` 表示无活动。管理员页面保留 `activity/failed` 与可执行的检查方向，不将故障伪装成跳过。自动路径不生成、不发布；手动路径发生 API 故障时也明确失败，真正无活动时仍可用空日程段创作。

SDK 默认布尔发送回执只接受 `True`；失败字典、整数、字符串不计入成功数量。保持单次原生生成、不补图。

## 未合并范围

- 聊天模型偏好、按能力自动换模：与全局默认或本次显式模型的已确认规则冲突。
- 上游主动发图调度：保留 S2 等分窗口随机、真实日程缺失跳过和不补排规则。
- 额度系统及聊天设置持久化：未直接适配 S2 多输出计数，不随维护修复引入。
- NovelAI 新后端：本轮没有对应已确认使用配置，也未完成其有序参考和多输出契约，不作为已支持能力。
- 新版缓存身份、工厂迁移：需要成对改造，不能覆盖现有人设服务、原生多图协议及私有联调页。

## 保留的验收规则

GPT Image 2.5 统一默认、标准 multipart 编辑、火山原生 JSON、有序必需参考、原生多输出、部分已有结果交付、人设图必需、手动只回真实聊天、自动仅当前规划 API、单次 MaiTrace 发布与未知结果不重发，均保持原约定。真实火山请求、宿主聊天投递及 QQ 空间发布仍需分别验收。

上游自主规划未修复数组根解析矛盾，单独在其隔离副本修复完整根 Schema 与对象校验；没有覆盖它的本地 Hook、连续性和日程生成逻辑。部署前须完成最终差异审阅及离线回归。


## S4 真实合并父链与逐路径决策

本次从已冻结白名单源码形成S4提交，再以正常合并引擎合并官方cafd4ea。未使用 `-s ours` 或全局 `-X ours`。S4之后实际产生8个文本冲突，已逐路径解决；未以自动合并成功代替语义审阅。

第一父为 `020614d775d3ae6ff87c3a5234c6d3149eaa13bc`，第二父为 `cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5`。结果明确保留已验收S4生产源码，不宣称完整升级到4.6.3；新增上游产品静态图标，保留已移植授权与三个小兼容修复。正文及[机器决策表](<UPSTREAM_MERGE_RESOLUTION.json>)公开记录全部99个官方改动路径的取舍。

官方成为祖先仅表示该提交已逐项审阅。被排除的NAI、quota、proactive、聊天偏好、工厂迁移及其测试不会因此自动获得支持，后续启用必须另行适配和验收。独立600秒等待集群仍被排除。

预合并与最终合并树的生产源码没有额外变更；合并提交只增加产品图标和本节/机器审计记录。此处不隐藏这一点，也不把父链存在当作功能全部兼容。最终测试、critic准入与发布仍由Lead执行，当前未push或合入远端main。

| 官方改动路径 | 处理 | 具体原因 |
| --- | --- | --- |
| [.gitignore](<.gitignore>) | 保留已验收S4版本 | 保留本地忽略规则；不把运行配置、图片或缓存纳入发布。 |
| [LICENSE](<LICENSE>) | 已在S4含同一内容 | 已在S3移植官方AGPL授权，保留原MIT通知。 |
| [README.md](<README.md>) | 保留已验收S4版本 | 保留已验收S4能力与限制说明，不导入4.6.3尚未支持能力宣称。 |
| [_manifest.json](<_manifest.json>) | 保留已验收S4版本 | 保留S4插件身份、SDK/依赖边界和私有WebUI能力。 |
| [changelog.md](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/changelog.md>) | 明确不引入新增路径 | 上游版本记录包含本轮未启用的NAI、quota与proactive；避免误报完整升级。 |
| [core/__init__.py](<core/__init__.py>) | 保留已验收S4版本 | 保留S4导入表，不挂入已排除的NAI、quota、proactive或新状态存储。 |
| [core/api_clients/__init__.py](<core/api_clients/__init__.py>) | 保留已验收S4版本 | 保留S4导入表，不挂入已排除的NAI、quota、proactive或新状态存储。 |
| [core/api_clients/base_client.py](<core/api_clients/base_client.py>) | 保留已验收S4版本 | 保留已移植的配置错误传播修复，同时保留本地ctx/legacyctx兼容；不覆盖整个客户端基类。 |
| [core/api_clients/client_context.py](<core/api_clients/client_context.py>) | 保留已验收S4版本 | 保留已移植的配置错误传播修复，同时保留本地ctx/legacyctx兼容；不覆盖整个客户端基类。 |
| [core/api_clients/comfyui_client.py](<core/api_clients/comfyui_client.py>) | 保留已验收S4版本 | 保留必需参考图缺占位符的NonRetryableError与禁环境代理；明确排除独立600秒等待集群。 |
| [core/api_clients/novelai_client.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/api_clients/novelai_client.py>) | 明确不引入新增路径 | NAI后端扩展暂缓；尚无已确认配置及有序引用/原生多输出契约，不新增未验收支持。 |
| [core/commands/dispatcher.py](<core/commands/dispatcher.py>) | 保留已验收S4版本 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/commands/generate.py](<core/commands/generate.py>) | 保留已验收S4版本 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/commands/handlers/__init__.py](<core/commands/handlers/__init__.py>) | 保留已验收S4版本 | 保留S4导入表，不挂入已排除的NAI、quota、proactive或新状态存储。 |
| [core/commands/handlers/config_handlers.py](<core/commands/handlers/config_handlers.py>) | 保留已验收S4版本 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/commands/handlers/quota_handlers.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/commands/handlers/quota_handlers.py>) | 明确不引入新增路径 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/commands/handlers/selfie_handlers.py](<core/commands/handlers/selfie_handlers.py>) | 保留已验收S4版本 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/commands/handlers/toggle_handlers.py](<core/commands/handlers/toggle_handlers.py>) | 保留已验收S4版本 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/commands/help_renderer.py](<core/commands/help_renderer.py>) | 保留已验收S4版本 | 保留已审阅命令行为；不引入聊天默认模型偏好、额度管理及对应提示文案。显式中文编号改进未纳入本次已测source。 |
| [core/config/__init__.py](<core/config/__init__.py>) | 保留已验收S4版本 | 保留S4导入表，不挂入已排除的NAI、quota、proactive或新状态存储。 |
| [core/config/backup.py](<core/config/backup.py>) | 保留已验收S4版本 | 保留S4唯一全局默认、显式引用/输出字段与现有配置兼容；不引入会清理S2字段的工厂迁移或新的默认链。 |
| [core/config/migration.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/config/migration.py>) | 明确不引入新增路径 | 保留S4唯一全局默认、显式引用/输出字段与现有配置兼容；不引入会清理S2字段的工厂迁移或新的默认链。 |
| [core/config/model_registry.py](<core/config/model_registry.py>) | 保留已验收S4版本 | 保留S4唯一全局默认、显式引用/输出字段与现有配置兼容；不引入会清理S2字段的工厂迁移或新的默认链。 |
| [core/config/models.py](<core/config/models.py>) | 保留已验收S4版本 | 保留S4唯一全局默认、显式引用/输出字段与现有配置兼容；不引入会清理S2字段的工厂迁移或新的默认链。 |
| [core/config/novelai.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/config/novelai.py>) | 明确不引入新增路径 | NAI后端扩展暂缓；尚无已确认配置及有序引用/原生多输出契约，不新增未验收支持。 |
| [core/config/quota.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/config/quota.py>) | 明确不引入新增路径 | 未授权新增额度产品规则；上游按单request计量也未适配S2原生多输出。 |
| [core/config/style_registry.py](<core/config/style_registry.py>) | 保留已验收S4版本 | 保留S4唯一全局默认、显式引用/输出字段与现有配置兼容；不引入会清理S2字段的工厂迁移或新的默认链。 |
| [core/pipeline/factories.py](<core/pipeline/factories.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/pipeline.py](<core/pipeline/pipeline.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/request.py](<core/pipeline/request.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/build_selfie_prompt.py](<core/pipeline/steps/build_selfie_prompt.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/check_cache.py](<core/pipeline/steps/check_cache.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/detect_img2img_fallback.py](<core/pipeline/steps/detect_img2img_fallback.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/merge_negative_prompt.py](<core/pipeline/steps/merge_negative_prompt.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/optimize_prompt.py](<core/pipeline/steps/optimize_prompt.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/resolve_model.py](<core/pipeline/steps/resolve_model.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/resolve_size.py](<core/pipeline/steps/resolve_size.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/schedule_recall.py](<core/pipeline/steps/schedule_recall.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/send_image.py](<core/pipeline/steps/send_image.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/pipeline/steps/update_cache.py](<core/pipeline/steps/update_cache.py>) | 保留已验收S4版本 | 保持已验收S4/旧公开API边界；拒绝quota流水线、参考图降级、按场景换默认与新超时集群。 |
| [core/prompts/novelai_prompt.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/novelai_prompt.py>) | 明确不引入新增路径 | NAI后端扩展暂缓；尚无已确认配置及有序引用/原生多输出契约，不新增未验收支持。 |
| [core/prompts/prompt_templates.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/prompt_templates.py>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/scene_action_llm.py](<core/prompts/scene_action_llm.py>) | 保留已验收S4版本 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/selfie_prompt_builder.py](<core/prompts/selfie_prompt_builder.py>) | 保留已验收S4版本 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/group_image_decision_en.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/group_image_decision_en.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/group_image_decision_ja.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/group_image_decision_ja.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/group_image_decision_zh.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/group_image_decision_zh.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/prompt_optimizer_en.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/prompt_optimizer_en.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/prompt_optimizer_ja.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/prompt_optimizer_ja.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/prompt_optimizer_zh.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/prompt_optimizer_zh.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/scene_en.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/scene_en.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/scene_ja.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/scene_ja.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/scene_zh.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/scene_zh.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/selfie_caption_en.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/selfie_caption_en.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/selfie_caption_ja.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/selfie_caption_ja.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/selfie_caption_zh.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/selfie_caption_zh.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/selfie_publication_en.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/selfie_publication_en.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/selfie_publication_ja.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/selfie_publication_ja.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/prompts/templates/selfie_publication_zh.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/prompts/templates/selfie_publication_zh.txt>) | 明确不引入新增路径 | 保持当前已验收身份/场景提示词，不新增上游LLM优化/主动决策及其三语模板行为。 |
| [core/quota/__init__.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/quota/__init__.py>) | 明确不引入新增路径 | 未授权新增额度产品规则；上游按单request计量也未适配S2原生多输出。 |
| [core/quota/service.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/quota/service.py>) | 明确不引入新增路径 | 未授权新增额度产品规则；上游按单request计量也未适配S2原生多输出。 |
| [core/quota/store.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/quota/store.py>) | 明确不引入新增路径 | 未授权新增额度产品规则；上游按单request计量也未适配S2原生多输出。 |
| [core/selfie/activity_rules.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/selfie/activity_rules.py>) | 明确不引入新增路径 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/auto_selfie_task.py](<core/selfie/auto_selfie_task.py>) | 保留已验收S4版本 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/caption_generator.py](<core/selfie/caption_generator.py>) | 保留已验收S4版本 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/group_image_task.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/selfie/group_image_task.py>) | 明确不引入新增路径 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/proactive_context.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/selfie/proactive_context.py>) | 明确不引入新增路径 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/publication_decision.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/selfie/publication_decision.py>) | 明确不引入新增路径 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/reference_image.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/selfie/reference_image.py>) | 明确不引入新增路径 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/selfie/schedule_provider.py](<core/selfie/schedule_provider.py>) | 保留已验收S4版本 | 保持S4人设图必需、当前规划API、手动仅真实聊天、分段随机不补排及单次发布；不引入新群主动图/渠道决策或可选参考图。 |
| [core/state/__init__.py](<core/state/__init__.py>) | 保留已验收S4版本 | 保留S4导入表，不挂入已排除的NAI、quota、proactive或新状态存储。 |
| [core/state/migration.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/state/migration.py>) | 明确不引入新增路径 | 不引入聊天默认模型持久化、quota迁移及proactive发布存储；保留现有S4状态边界。 |
| [core/state/proactive_state.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/state/proactive_state.py>) | 明确不引入新增路径 | 不引入聊天默认模型持久化、quota迁移及proactive发布存储；保留现有S4状态边界。 |
| [core/state/runtime_state.py](<core/state/runtime_state.py>) | 保留已验收S4版本 | 不引入聊天默认模型持久化、quota迁移及proactive发布存储；保留现有S4状态边界。 |
| [core/utils/__init__.py](<core/utils/__init__.py>) | 保留已验收S4版本 | 保留S4导入表，不挂入已排除的NAI、quota、proactive或新状态存储。 |
| [core/utils/cache_manager.py](<core/utils/cache_manager.py>) | 保留已验收S4版本 | 新版缓存身份需与读写step成对改造，本次不扩展已验收缓存行为。 |
| [core/utils/image_utils.py](<core/utils/image_utils.py>) | 保留已验收S4版本 | S2有序必需引用由专用resolve_image链路处理，不用上游单图提取替代。 |
| [core/utils/prompt_optimizer.py](<core/utils/prompt_optimizer.py>) | 保留已验收S4版本 | 不为维护合并新增LLM优化和语法切换行为，保留S4已验收提示词路径。 |
| [core/utils/prompt_policy.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/core/utils/prompt_policy.py>) | 明确不引入新增路径 | 不为维护合并新增LLM优化和语法切换行为，保留S4已验收提示词路径。 |
| [core/utils/recall_utils.py](<core/utils/recall_utils.py>) | 保留已验收S4版本 | 新撤回流程需要真实回执、provider归属与卸载任务整套接线，本次不引入未经验证的平台推断。 |
| [core/utils/request_context.py](<core/utils/request_context.py>) | 保留已验收S4版本 | 保持现有请求上下文与尺寸策略，不连带新增上游参数/超时行为。 |
| [core/utils/shared_constants.py](<core/utils/shared_constants.py>) | 保留已验收S4版本 | 保持现有请求上下文与尺寸策略，不连带新增上游参数/超时行为。 |
| [core/utils/size_utils.py](<core/utils/size_utils.py>) | 保留已验收S4版本 | 保持现有请求上下文与尺寸策略，不连带新增上游参数/超时行为。 |
| [icon.png](<icon.png>) | 接纳官方静态资源 | 经Lead确认允许按上游许可纳入产品静态图标，不属于运行生成或私有人设图片。 |
| [model_presets.toml](<model_presets.toml>) | 保留已验收S4版本 | 保留已确认stage中的历史模型预设模板，不因上游删除改变现有兼容面。 |
| [plugin.py](<plugin.py>) | 保留已验收S4版本 | 保持S4有序引用、原生集合、人设服务、私有管理员预览/已备图发布API及现有公开调用契约。 |
| [pyproject.toml](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/pyproject.toml>) | 明确不引入新增路径 | 上游4.6.3版本、SDK范围及test_*收集策略不等于当前S4；继续使用已审阅manifest依赖声明。 |
| [requirements.txt](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/requirements.txt>) | 明确不引入新增路径 | 上游4.6.3版本、SDK范围及test_*收集策略不等于当前S4；继续使用已审阅manifest依赖声明。 |
| [tests/conftest.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/conftest.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/smoke_test.py](<tests/smoke_test.py>) | 保留已验收S4版本 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_adapter_compatibility.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_adapter_compatibility.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_command_model_selection.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_command_model_selection.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_novelai.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_novelai.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_prompt_optimization.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_prompt_optimization.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_quota_integration.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_quota_integration.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_quota_store.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_quota_store.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_scene_and_tasks.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_scene_and_tasks.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_sdk_upgrade.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_sdk_upgrade.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
| [tests/test_state_persistence.py](<https://github.com/ptrel1/mais_art_journal/blob/cafd4eae857a5dbb7412e5611ce1f3e2a8a32ed5/tests/test_state_persistence.py>) | 明确不引入新增路径 | 保留已验收S4测试；新上游测试强依赖本次未启用的NAI/quota/proactive/迁移或相反的重试行为，不作为本轮支持声明。 |
