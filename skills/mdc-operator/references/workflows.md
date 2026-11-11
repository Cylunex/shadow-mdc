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

作品时长筛选可用 `works.query` / `works.facets` 的 `duration_min`、`duration_max`（秒，左闭右开）或 `unknown_duration`。
作品语言表示原始对白语言；题材用标签维护。标签词条支持 `group` 分组，更新省略保持、空字符串清除。
