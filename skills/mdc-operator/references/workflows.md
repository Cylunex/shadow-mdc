# 常见用法

先读取服务 schema，使用实际返回的作品、演员、标签、分类、片单与任务 ID。下面的 ID 是占位符。

```sh
scripts/mdc catalog
scripts/mdc describe works.query
scripts/mdc call works.query input.json
```

作品筛选输入：

```json
{"q":"REPLACE_QUERY","contents":["contents:jav"],"languages":["languages:ja"],"limit":20}
```

大标签查询：`tags.list` 使用 `{"only_with_works":true}`。将返回的标签 `id` 放入作品查询的 `tags`。
标签封面可能为空，不能根据标签名称虚构图片。

分类字典查询：`classifications.list` 使用 `{}`。作品分类读取用
`{"work_id":"REPLACE_WORK_ID"}`；分类设定示例：

```json
{"work_id":"REPLACE_WORK_ID","languages":["languages:ja"]}
```

这个例子只调整作品语言，不改变标签、形式、制作地区和人工发行年。
必须有用户提供的事实或实际资料支持原始对白语言设置；中文字幕不证明作品语言为中文。

想看设置：

```json
{"work_id":"REPLACE_WORK_ID","wanted":true}
```

`works.want` 返回的是偏好结果；执行后读取 `works.detail` 检查目标作品的 `want_list`。
这不会提交下载任务，也不证明影片已经在本地或 Emby 中。

片单查询：`playlists.list` 使用 `{}`，`playlists.works` 使用
`{"playlist_id":"REPLACE_PLAYLIST_ID"}`。动态片单由保存条件生成，查询时的成员不代表手工固定成员。

任务查询：`tasks.list` 使用 `{"limit":20}`；`tasks.get` 使用
`{"task_id":"REPLACE_TASK_ID"}`。按具体任务状态报告成功、失败或仍在进行，不把列表为空解释成从未执行。

# 私有配置结构

HTTP 配置只存于实际的私有文件；示例不含真实连接：

```json
{"transport":"http","base_url":"https://example.com","token_file":"REPLACE_TOKEN_FILE","owner":"REPLACE_CLIENT_ID","timeout_seconds":60}
```

复用已有 SSH 连接时，本机不复制服务 Token：

```json
{"transport":"ssh","ssh_host":"REPLACE_SSH_ALIAS","remote_launcher":"/REPLACE_ROOT/skills/mdc-operator/scripts/mdc","remote_config":"/REPLACE_PRIVATE_CONFIG.json","timeout_seconds":60}
```

SSH 端使用已有 HTTP 私有配置。CLI 不跟随 HTTP 重定向，不输出 SSH stderr 或凭据；失败时先核对配置与原操作结果。

查询资料缺项时，先用 `works.metadata_gaps` 读取当前范围的去重计数，再用 `works.query` 的
`missing_metadata` 传 `plot`、`cover`、`tags`、`year`、`duration` 或 `any` 分页取作品。
封面按登记记录统计。动态片单可保存同一筛选；修复后重新查询即可核对。

作品时长筛选可用 `works.query` / `works.facets` 的 `duration_min`、`duration_max`（秒，左闭右开）或 `unknown_duration`。
想看与媒体情况可组合：`{"wanted":true,"availability":"files"}` 查询想看且有关联文件的作品；
`wanted=false` 排除想看，省略不筛选。`availability` 支持 all/files/emby/metadata/undownloaded；
无关联文件不证明没有远端下载。磁力检查分别读取 `presence` 和 `offline_status`，不能用 done 断言媒体已入库。
作品语言表示原始对白语言；题材用标签树维护。`tags.tree` 读取节点，`tags.create/update` 用真实 `parent_id` 设置上级；
编辑时省略保持位置，null 移到顶层。分组也是高层节点；旧 group 只作输入兼容。
查询父节点会包含其全部后代，统计按作品去重；作品只需保存具体标签，维护父子关系不改写原标签或猜测下级。
明确同义但已有独立 ID 的词条用 `tags.merge`，传 source_id、target_id、source_revision 和 target_revision；
旧 ID 留作持久重定向，名字加入目标别名，作品原始标签及片单条件不改写。上位概念和具体子类不能按同义合并。

机构与系列分开：`organizations.list` 返回片商、工作室及所属品牌和渠道，`series.list` 返回独立系列。
`works.query` / `works.facets` 的 `organizations`、`labels`、`platforms`、`series` 使用实际 ID，跨维度同时匹配。
`organizations.create/update` 维护机构及所属关系，`organizations.link` 只维护制作、发行等职责；
`series.create/update` 维护系列及已知归属，`series.add` 只加入作品成员。更新带当前 `revision`。
不把系列 ID 当机构 ID，不把厂牌视为演员，不凭平台词或 `studio` 字段名推定组织形态、隶属关系。
调用前先确认当前服务 catalog 已包含这些能力；代码中的新增能力不代表当前已部署。
