# Shadow MDC 架构

## 核心实体

- **Work**：一个逻辑作品或场景。内部 UUID 是唯一身份；主番号可空。
- **MediaAsset**：本地视频文件。文件可未识别，也可多文件关联同一 Work。
- **ExternalIdentity**：Work 的多值身份，按 provider、kind、规范化 value 唯一。
- **MatchCandidate**：provider 返回但尚未被用户或阈值接受的候选。
- **SourceSnapshot**：某来源一次返回的原始结构化字段，用于追溯与重新聚合。

演员、片商、标签是 Work 的规范化关联实体，不承担作品身份。

## 识别流程

```text
文件扫描
  → 提取 code / title / URL / hash / duration 线索
  → 根据内容族与查询能力选择 providers
  → 并发获取候选
  → 确定性证据和相似度评分
  → 高置信自动接受 / 中置信待确认 / 低置信拒绝
  → 保存 Work、Identity、Snapshot
  → 生成 NFO、图片与整理计划
```

内容族只决定路由，不决定身份。国产内容不假定有稳定番号；欧美内容优先指纹、provider ID 和 URL；JAV 优先规范化番号。

## 来源边界

Provider 是无状态适配器，只接收 `IdentityHints`，返回 `ProviderRecord` 列表。它声明查询模式、内容族、认证和交互要求。Provider 不接触数据库和文件系统。

内置首批来源：

- JavDB：JAV 综合搜索与详情。
- JavBus：JAV 番号与图片补充。
- ThePornDB：需 Token 的场景/JAV/指纹联邦源。
- JSON-LD URL：用户指定详情页时提取标准结构化数据，服务非常见影片。

## 安全整理

整理器只生成 `OperationPlan`。计划包含来源、目标、冲突、动作和摘要哈希；执行端必须提交同一哈希，路径必须落在 Library 根目录内。默认不覆盖目标文件。

## 验收范围

- 规范 JAV 番号可自动识别并生成 NFO。
- 无番号文件可通过标题或 URL 产生候选并人工确认。
- 欧美场景可用 oshash 或 ThePornDB ID 匹配。
- 同片多号合并为一个 Work。
- 来源失败不会阻断其它来源；所有失败可诊断。
- 文件操作未经计划确认不会发生。
