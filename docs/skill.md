# MDC operator skill

`skills/mdc-operator/SKILL.md` 是片库操作入口，`scripts/mdc`（或已安装包的 `shadow-mdc-skill`）执行受支持 CLI/API。
旧 MCP 的入库、元数据查询、演员/番号盯磁、重刮和播放解析已合并到此入口；MCP 服务、SDK 可选依赖和旧 HTTP 路由已移除。
私有配置项 `mcp_token` 暂保留原名以复用现有凭据，名称兼容不代表继续运行 MCP。

## 日常能力

| 场景 | 能力 |
| --- | --- |
| 作品、演员、标签、作品分类筛选 | `works.query/facets/detail`、`actors.query/detail`、`tags.list`、`classifications.list/work` |
| 标签与展示名 | `works.tags` 增删标签，`works.update` 编辑作品明确字段并默认锁定；`tags.dictionary/create/update` 维护词条，`tags.rules` 读写规则 |
| 单条入库 | `intake.code.*`、`intake.magnet.*`、`intake.link.submit`、`intake.item` |
| 日榜、论坛和片单批量补库 | `discover.browse/search`、`rankings.seed`、CLI `prepare/batch` |
| 元数据、缺译缺图缺演员 | `metadata.lookup`、`works.lookup/refresh/fill`、`actors.images.fill` |
| 分类回填 | `classifications.create/update` 新增改名词条；`classifications.assign/fill`，配合 `prepare/batch` 处理大库 |
| 想看、片单和演员盯磁 | `works.want`、`playlists.list/works/create/update`、`subscriptions.*` |
| 下载与导出收口 | `intake.jobs/job/recover/next/cancel/pipeline`、`strm.status/rewrite/notify` |
| 播放 | `playback.code/work` 解析已配置的 Emby 与外站源 |
| 本地扫描和识别 | `libraries.list/assets/scan/identify`、`assets.get/identify/candidates`、`candidates.accept` |
| 通用执行任务 | `tasks.list/get/retry/resume/cancel` |

真实参数以服务 `catalog/describe` 为准；较旧部署不会因为本地 skill 更新就自动出现新能力。
详细日常步骤见 [入库](../skills/mdc-operator/references/intake.md)、
[批量操作](../skills/mdc-operator/references/batches.md) 和 [维护收口](../skills/mdc-operator/references/maintenance.md)。

## 批量执行

`prepare` 沿游标收集片库真实 ID 或读取指定数量的来源榜单页，在写入前固定清单；
`batch` 串行执行清单，按每项保存私有 JSONL 回执。新建、跳过、失败、待资源和已提交下载分别计数。
已完成项重跑跳过；明确失败需 `--retry-failed`，未知结果需核对原目标后才使用 `--retry-unknown`。
回执采用追加写入和文件锁，单批最多 50000 项；不创建待审核业务流程。

目标服务直接建档和维护，日常无需本地 `seed_* --sync-nas` 搬运结果；旧 seed 脚本作为专用来源/历史迁移工具保留，
不由 skill 直接操作生产 SQL 或复制数据库。批量下载完成仍需逐任务核对 STRM 和媒体服务器状态。

## 数据约束

- `works.tags` 保留无关标签；`works.update.tags` 才是整组替换。标签接受注册表别名并归一，查询使用固定 ID。
- `works.fill` 只补指定空字段、尚未翻译字段与缺失图片缓存，保留原值和人工锁；仅接受达到既有匹配门槛的来源记录。
- `classifications.fill` 仅填空或 unknown；已有明确维度保持原值。语言与制作地区分别取证，不从内容分区猜测；题材用 works.tags 维护。
- 自动入库与恢复共用 LTX 优先磁力排序，保留番号匹配和片段惩罚；用户指定磁力仍优先。
- `intake.recover` 只让既有远端任务/结果重新进入轮询核对，不新提下载；返回 rechecking 不代表恢复完成。
- ed2k 不走磁力解析，直连 115 的 skill 链路明确拒绝；OpenList 是否支持以实际后端结果为准。
- 片单 owner 固定来自配置；想看沿用个人片库共同列表。片单编辑使用 revision 检查。
- 不开放原媒体删除、清理对账、导出根迁移、账户/凭据变更和部署。

## 连接和接口

连接来自 `--config`、仅传路径的 `MDC_SKILL_CONFIG` 或 skill 下已忽略的 `skill.local.json`。
私有配置和 token 文件保持 0600，真实值不提交。HTTP 示例：

```json
{"transport":"http","base_url":"https://example.com","token_file":"REPLACE_TOKEN_FILE","owner":"REPLACE_CLIENT_ID","timeout_seconds":60}
```

可复用已有 SSH 别名和远端绝对启动器/配置路径，JSON 输入走 stdin，不拼入远端命令；本机无需复制服务令牌。
更多结构见 [常见用法](../skills/mdc-operator/references/workflows.md)。

- `GET /api/skill/v1/catalog` 返回固定能力目录、读写效果与 schema。
- `POST /api/skill/v1/call` 接收 `name/arguments`，返回 `ok/capability/status/result`。
- `/api/intake/v1` 是独立受支持的入库、元数据和播放 HTTP 路由；`/api/operator/v1` 提供增量标签、补空和重核操作。
- 三组入口均复用原机器凭据做 Bearer 校验，未配置为 503，匿名/错误凭据为 401。
- skill 仅转发固定域内端点，不接受任意 HTTP 路径、方法或目标；operator 直接路由也要求认证。
- CLI 不跟随重定向、不输出凭据或原始 SSH 错误、不自动重放未知写入。

## 验证

隔离测试覆盖原入库行为、旧 MCP 能力的 HTTP/CLI 替代、预览无写入、字段锁、标签保留、分类补空、
LTX 选择、缺 STRM 重核、ed2k 边界、万条分页清单、回执续跑和连接中断后的未知状态。
测试使用临时 SQLite 与模拟远端，不提交真实下载、不改线上片库。
