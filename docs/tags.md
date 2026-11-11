# 标签词典数据与展示

标签词条、名称、别名、隐藏状态和展示规则属于运行数据，不在 Python 源码中维护。

- 运行词典保存在 `<data_dir>/tag-dictionary.json`，文件权限 0600，随片库数据一起备份。
- `<reference_data_dir>/tag-dictionary.seed.json` 是仓库外提供的可选首次初始化数据；只在运行词典不存在时导入。
  重启和升级不会覆盖已修改的词条或规则。以后日常新增、改名都通过接口维护运行数据。
- 未提供种子时创建空词典，来源标签仍可发现与维护。作品分类首次数据在外部 `<reference_data_dir>/classifications.seed.json`，运行词条继续保存于已有分类数据库表。
- Python 的 `tag_models.py` 仅保留模型、身份解析和加载校验，`services/tag_dictionary.py` 负责持久化。
  原 `tag_registry.py` 已删除。

每个标签有稳定 `id`、展示 `name`、搜索 `aliases`、自定义 `group`、`hidden` 与 `revision`。
分组只组织标签浏览，不改变匹配规则。作品题材统一在标签中维护，旧题材迁移到「题材」组；
词条编辑省略 group 保持原分组，空字符串移出分组。
内置标签保留原 `tag:` ID；未登记来源标签保留 `raw:` 身份。把已有来源标签纳入管理、改名时仍保留原 ID，
旧名称自动进入别名，不改写作品原始 `Work.tags`、封面关联或片单中的 ID。

来源标记可以隐藏于分类与筛选，但仍保存在作品数据中。给作品增删标签只处理明确的增量，
不会借展示过滤删除 `forum`、seed 等原始来源标记。对既有词条设定的显隐优先于通用噪声规则。

## 管理入口

### 文件名中的 LTX

扫描和关联媒体文件时，文件名中的 `-LTX`、`_LTX`、`.LTX`（大小写不敏感）会提供一个
`LTX` 标签；后面必须是结尾或点、横线、下划线、空白分隔符。`LTX-123`、`-LTXA`、`-LTX2`、
`-L`、`-LC` 和仅目录中出现的 LTX 不会触发。实体视频与 STRM 采用相同规则，不访问流地址。

标签沿用运行词典中的身份、别名和展示名称，没有在代码中新增标签词表。文件名证据保留在
资产路径与本地候选中；在线来源快照不被改写。接受在线候选、关联已有作品以及覆盖式重刮后
都会保留该标签，手动锁定标签字段时跳过自动添加。同一作品有任一关联文件带此标记即可添加，
文件后来改名不会自动删除作品标签，也不会据此猜测其它分类。

已有片库执行常规扫描可补齐；“只扫新增”仍跳过大小和修改时间未变的文件，不承担标签回填。

### 字典接口

作品库 → 片单与资料工具 → 标签字典，支持搜索、分页、新增、改名、别名与显隐修改。
作品分类词条继续在同处的分类字典管理，均支持通过 skill 操作：

| HTTP 接口 | Skill 能力 | 用途 |
| --- | --- | --- |
| `GET /api/catalog/tags` | `tags.dictionary` | 词条、别名、显隐、作品计数及修订号，q/offset/limit 分页 |
| `POST /api/catalog/tags` | `tags.create` | 新增词条 |
| `PUT /api/catalog/tags` | `tags.update` | 请求体携带 tag_id、name、revision，按需修改 aliases/hidden |
| `GET /api/catalog/tags/rules` | `tags.rules` | 读取展示优先级、常用筛选、主题封面顺序和噪声规则 |
| `PUT /api/catalog/tags/rules` | `tags.rules.update` | 用当前字典 revision 更新规则数据 |
| `POST /api/catalog/classifications/terms` | `classifications.create` | 作品分类新增词条 |
| `PUT /api/catalog/classifications/terms/{id}` | `classifications.update` | 作品分类词条改名、别名维护 |

标签更新把 ID 放 JSON 请求体，避免旧 raw ID 中编码的空格或斜杠被 URL 路由二次解码。
省略 aliases/hidden 保持原值；修改携带当前 revision，冲突返回 409 后回读，不覆盖其它会话的新修改。
同一名称或别名不能指向多个词条。写入先验证完整词典，再加锁原子替换文件，不写入半份数据。

## 读取与兼容

`tag_refs` 返回 id/name；`/api/works/tags`、分类图卡、facets、作品详情和移动目录使用同一运行词典。
改名和显隐修改成功后清理响应缓存；请求使用独立词典快照，多片库数据目录互不影响。
来源词保留原文，展示和筛选接受 ID、当前名称及历史别名；未知标签仍能被发现与管理。
`display_tags` 还包含地区、片商等信息，不能整体作为题材标签写回作品。

词典、隐藏规则、演员映射和种子片库均不进入 Git 或 Python 发布包；仓库保留模型、加载实现与必要测试夹具。路径与迁移方式见[运行数据边界](data-storage.md)。
