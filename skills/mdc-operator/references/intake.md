# 入库流程

先用 `scripts/mdc describe <能力名>` 读取实际参数。用户给出番号或磁力并明确要求入库时，直接执行对应的
`submit`；已说明要执行的请求不重复确认。只问资料、解析磁力或看看入库效果时，使用查询或 `preview`。
下面输入中的 ID、番号和磁力均为占位符，不能据此提交真实任务。

## 番号入库

- `intake.status {"code":"REPLACE_CODE"}`：读取是否建档、是否已有媒体及关联任务。
- `intake.code.preview {"code":"REPLACE_CODE"}`：预览元数据和候选磁力；不建档、不下载。
- `intake.code.submit {"code":"REPLACE_CODE"}`：建档、挑选磁力并提交现有 115/OpenList 入库流程。
  也可传 `magnet` 指定用户给出的磁力或下载链接。不要从返回文本自行选择无关下载地址。

同一作品已有媒体或正在执行时复用现有结果。没有可用磁力时，`action=hunting` 表示已建档并加入想看，
既有盯磁流程会继续找资源；这不是下载成功。服务未连接或目标未配置时按实际错误报告。

## 磁力入库

`intake.magnet.inspect` 解析信息并核对已存在条目；`intake.magnet.preview` 预览后续处理；
`intake.magnet.submit` 执行入库。三者都接受：

```json
{"magnet":"magnet:?xt=urn:btih:REPLACE_INFO_HASH","title":"REPLACE_OPTIONAL_TITLE"}
```

可提供用户明确给出的 `code`。有番号时沿用番号建档；无番号时按 info_hash 去重，保留无番号身份，
不猜造 JAV 番号或来源。重复的磁力复用已有任务。画质、字幕等从名称解析出的内容属于线索，不能当作已验证的媒体事实。
图片仅在真实元数据可用时展示。

## 只建档或重新刮削

`works.lookup` 仅刮削并保存元数据，不提交下载，输入必须且只能提供一种身份：

```json
{"code":"REPLACE_CODE"}
```

也可用 `{"source_url":"https://example.com/detail"}` 或 `{"external_ids":{"REPLACE_PROVIDER":"REPLACE_ID"}}`。
核对 `result.work`；为空说明没有可接受结果，不能报告已建档。`works.refresh {"work_id":"REPLACE_WORK_ID"}`
重刮已有作品，沿用字段锁和服务配额；核对 `accepted_work_id`，没有命中时说明未更新。

## 本地媒体扫描和识别

1. `libraries.list {}` 取得已配置媒体库的真实 ID。这里不创建库、不修改根目录。
2. `libraries.scan {"library_id":"REPLACE_LIBRARY_ID"}` 提交扫描，默认 `only_new=true`。
   只有明确要求重扫已有文件时设为 false。按返回的任务 `id` 用 `tasks.get` 等待实际结束。
3. `libraries.assets {"library_id":"REPLACE_LIBRARY_ID","offset":0,"limit":48}`
   分页读取资产（`items/total/offset/limit`），可按 `state` 筛选；默认不列已忽略资产。
4. `libraries.identify {"library_id":"REPLACE_LIBRARY_ID","limit":20}` 提交批量识别，默认跳过已识别条目；
   或用 `assets.identify {"asset_id":"REPLACE_ASSET_ID"}` 识别单项，可附用户提供的 `title/source_url/external_ids`。
5. 用 `assets.get` 核对资产的 `work_id`；低置信结果用 `assets.candidates` 展示候选及证据。
   只有用户选定某项后，才用 `candidates.accept {"candidate_id":"REPLACE_CANDIDATE_ID"}` 接受。

无番号资产在线没有可接受结果时，现有服务可能根据文件名与目录线索形成并关联本地作品。
`accepted_work_id` 只能证明已有作品关联；报告在线刮削命中前，还要核对候选来源或批量任务的在线识别、本地优化计数。
扫描和识别沿用服务的可恢复任务、去重和自动接受门槛，不从远程低置信候选中擅自选择。此流程不执行原文件移动或删除。

## 核对执行结果

- 番号入库用 `intake.status`；有无番号的下载任务都可用 `intake.job {"job_id":"REPLACE_JOB_ID"}`。
  最近离线任务用 `intake.jobs {"limit":20}`，可按 `status` 筛选。
- `action=planned` 是预览，`submitted/running` 是已提交或进行中，`hunting` 是等待资源，`exists` 是复用已有媒体。
  `action=error` 或 `ok=false` 表示失败。不要把 HTTP 200 当成业务成功。
- `intake.job` 的 `stage`、`status`、`strm_path` 说明下载与导出进度；完成后由现有服务写 STRM、NFO、图片、字幕，
  并通知媒体服务器。通知成功不等于 Emby 已建立索引，不能凭任务提交回执声称已可播放。
- 本地扫描/识别属于 `tasks.get` 的任务，离线下载属于 `intake.job` 的任务，不混用两种 ID。
- 断线或超时后，先查询原番号、info_hash 或任务状态；不要盲目再次提交。CLI 的连接超时可在私有配置中调整，
  不把等待时间当作执行结果。用户要求查看进度时只查询，不恢复或重试旧任务。
