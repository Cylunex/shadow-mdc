# 片库整理与收口

## 标签和展示名

先读详情。`works.tags` 按增量操作，保留其它标签，支持注册表的别名归一：

```json
{"work_id":"REPLACE_WORK_ID","add":["LTX","经典","forum"],"remove":["REPLACE_TAG"]}
```

这里的标签仅为输入示例，按用户真实要求取子集。查询时使用 `tags.list` 返回的正式 ID。
`works.update {"work_id":"REPLACE_WORK_ID","title":"REPLACE_DISPLAY_TITLE"}` 修改展示名，
也支持明确给出的演员、剧情、制作商、系列或整组标签，默认 `lock_edited=true`。
整组 `tags` 是替换，普通补库追加标记应使用 `works.tags`。编辑后读 `works.detail` 核对。

## 维护标签和分类词条

作品标签增删与词条管理是两件事。`tags.dictionary {"q":"REPLACE_QUERY","offset":0,"limit":100}`
分页查询词条，包含来源标签、隐藏状态、别名和 revision。新增用 `tags.create`：

```json
{"name":"REPLACE_NAME","aliases":["REPLACE_ALIAS"],"hidden":false}
```

改名或显隐用 `tags.update`，保留真实 ID，并提交当前 revision：

```json
{"tag_id":"REPLACE_TAG_ID","name":"REPLACE_NEW_NAME","revision":1}
```

省略 aliases/hidden 保持原值，旧名称会自动保留为别名。修改后重新查询并用作品筛选核对。
已隐藏的来源标记仍保存在作品原始标签中，不批量删库或重写历史标签。
作品分类词条用 `classifications.create {"dimension":"genres","name":"REPLACE_NAME"}`，
或 `classifications.update {"term_id":"REPLACE_TERM_ID","name":"REPLACE_NEW_NAME","revision":1}`。
分类更新省略 aliases 保留原值；冲突回读，不自动采用较大修订号覆盖他人修改。

`tags.rules` 返回展示顺序、常用筛选和噪声规则以及字典 revision；用户明确要求修改全局规则时，
将读取结果中的 rules 按要求最小修改，再用 `tags.rules.update` 提交。普通标签改名不修改全局规则。
这些数据保存于运行词典/数据库，不通过改源码、改 seed 或重启服务完成。

## 只补空

```json
{"work_id":"REPLACE_WORK_ID","fields":["actors","artwork","translation"]}
```

调用 `works.fill`。可选 `title/plot/actors/artwork/translation`：

- 元数据只查询和写入被请求且为空的字段，不改其它元数据，也不覆盖人工锁。
- 只接受达到既有匹配门槛的来源记录；没有编号时不自动套用远程演员，改用有依据的 `works.update`。
- translation 跳过已有翻译与被锁的片名/剧情，沿用当前翻译配置和缓存；翻译关闭或无需翻译会跳过。
- artwork 补空图片资料，并沿用图片缓存下载缺失文件；已有缓存复用，不整部重刮。
- 返回 `changed/errors/outcome`。缺少可信资料或下载失败会报告失败，不能把部分更新说成全部补齐。

演员档案头像可用 `actors.images.fill` 补齐，默认不强制刷新；执行前看 schema，按范围设置 limit。
这与作品缺演员名单是两种操作。批量使用 `prepare works.fill`，见 [批量工作](batches.md)。
确实需要替换已有未锁元数据时才用 `works.refresh`，沿用重刮配额。

## LTX 选择

自动入库、盯磁和失败换磁共用磁力排序：番号匹配优先，再优先名称中明确标识的完整 LTX 发行，
随后按现有字幕、画质与体积排序。错误番号、sample/trailer 和过小片段不会靠 LTX 标识胜出。
用户指定的磁力优先于自动选择。名称标识是候选线索，不能当作实测画质。

## 离线失败、OpenList 已存在、缺 STRM

先用 `intake.job`、`intake.status` 和 `intake.pipeline` 读取原任务和作品：

- OpenList“任务已存在”优先由现有提交服务接管同任务/结果目录；已有本地任务用
  `intake.recover {"task_id":"REPLACE_OFFLINE_TASK_ID"}` 重新交给轮询器核对结果并补导出。
  它不提交第二次下载，返回 `rechecking` 仍是等待核对。源文件找不到或权限/配置有误时按实际错误收口。
- 已完成却缺导出、或导出失败，也用 `intake.recover`，保留原下载身份和远端任务 ID。
  已取消任务不能这样复活；已有实际 STRM 文件会跳过。
- 确认需要换磁后用 `intake.next`；需要停止时用 `intake.cancel`。既有恢复控制器先确认撤销，再提交替代磁力。
- `strm.status` 看导出配置和 Emby 待通知；`strm.rewrite` 按当前配置重写已管理 STRM，
  `strm.notify` 重试待通知。重写不能替代远端源文件恢复，也不保证 Emby 已完成索引。
- skill 不开放删除对账、迁移导出根目录、删除源文件、账号登录或凭据变更。

执行后持续读原 `intake.job`，报告 `status/stage/error/strm_path`；必要时用播放解析核对媒体服务器状态。
不能以 HTTP 200、重新进入 running 或发送了通知来认定收口完成。

## ed2k 和其它链接

`intake.magnet.*` 只接受 BT 磁力。来源页面可用 `works.lookup` 或 `intake.item` 建档，
已有作品的明确下载链接用 `intake.link.submit {"work_id":"REPLACE_WORK_ID","url":"REPLACE_DOWNLOAD_URL"}`。
直连 115 的 skill 链路不支持 ed2k，会明确拒绝；OpenList 仅转交给其实际支持的下载后端，
结果仍需核对。不能把 ed2k 建档成功、链接保存成功或 OpenList 接收当成 115 下载成功。
