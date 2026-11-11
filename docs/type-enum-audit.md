# 枚举与类型用途审查

审查覆盖 Python 枚举、Literal、请求模型与主要调用方，以及 Web 的 Zod schema、筛选类型和显示逻辑。
判断依据是同一类型中的值是否回答同一个问题，而不是名字是否相似。固定协议、生命周期、业务词典和运行时来源不能一律做成枚举。

## 已修正的问题

| 位置 | 原问题 | 修正 |
| --- | --- | --- |
| 磁力检查 `library_state` | 把资料存在性、媒体登记情况、离线运行和完成状态放在一列；下载完成不能证明媒体已登记 | 新增独立 `presence` 与 `offline_status`；旧复合列标为兼容字段。`completed` 只在离线状态适配时转换为 `done`，未知状态保持 `unknown` |
| 作品查询 `status` | “想看”是偏好，“有关联文件 / Emby / 仅资料”是可用性；单选预设不能表达“想看且已有文件” | 新增 `wanted` 与 `availability`，查询、动态片单和 Web 可组合；旧 `want/downloaded/files` 等预设继续输入兼容 |
| 通用任务状态 | 多处手写终态集合，遗漏中断和暂停；迟到的进度可能把已结束任务改回运行中 | 使用 `TaskStatus` 及共享终态集合；终态不接受进度覆盖；结束任务须提供该领域的合法终态 |
| 后台任务返回状态 | 未识别的返回状态被默认视为成功 | 非法或未结束状态进入失败处理，不产生假成功 |
| 机构形态与作品职责 | 两套字符串列表分别维护，返回模型与 Web 接口还使用任意字符串 | 使用独立 `OrganizationKind`、`OrganizationRole`；发布公司可承担制作或发行职责，职责不等于组织形态；旧系列筛选值仅在兼容入口允许 |
| 榜单 | `rankings_weekly_fc2` 同时编码周期和来源分区，解析返回无具体类型的字符串二元组；HTTP / Skill 接收任意榜单名 | 通过 `RankingSpec(zone, period)` 表达独立轴，使用独立 `RankingZone` / `RankingPeriod`；旧榜单名保留为编码，非法名字在入口返回 422 |
| 来源体系到库分区映射 | 识别、入库和整理各维护一份映射 | 统一使用 `category_for_family`，不改变历史分区值或推定制作地区 |
| Web 同义类型 | 分区枚举多处重复；旧筛选辅助函数不认识标题排序 | `catalogTypes.ts` 集中定义分区、排序、预设、可用性、机构形态与职责；旧辅助函数共用定义并支持标题排序；旧本地网格缺少 Emby 绑定数据，明确保持其受支持的预设子集 |
| 旧检索面板的题材常量 | 业务标签名称作为源码中的“常用枚举”维护，且从混合展示标签生成题材 | 删除固定名称表，从正式标签引用和作品标签派生选项，按实际计数排序；地区、机构等展示信息不再冒充题材 |

### 存在性与流程结果

`presence=absent` 表示本地没有正式作品；远程查到标题和封面仍可保持 absent。
`catalog_only` 表示已有正式资料、未登记媒体；`in_library` 表示已有本地资产或已登记的 STRM。
它描述登记事实，不保证文件仍在线或已经成功播放。`offline_status=done` 只代表离线流程状态，两者可以同时是 done 与 catalog_only。
没有离线任务时 `offline_status=null`，未知历史任务状态为 unknown，不能归为成功或“没有任务”。

### 查询兼容

新查询可使用 `{"wanted":true,"availability":"files"}`，也可用 `wanted=false` 排除想看。
`availability` 的值为 all、files、emby、metadata、undownloaded；后者只表示没有登记文件，不证明远端没有下载。
旧 `status=downloaded` 与 `status=files` 在入口都适配为 files；`status=want` 适配为 wanted=true。
显式 `wanted` 优先于旧想看预设，非 all 的 `availability` 优先于旧可用性预设；Web 修改筛选时会清除旧预设，避免残留约束。
URL 中的 wanted=1/0 保留真与假，未传表示不筛选。保存的动态片单使用相同模型，无需改写已有规则。

## 用法合理，保留边界的类型

| 类型 | 实际用途与保留理由 |
| --- | --- |
| `ContentFamily` | 兼容的识别 / 来源体系，包含 jav、chinese、western、animation 等，不是纯地区或作品类型。保留用于识别路由；正式类型、形式、语言、制作地区用独立分类维度 |
| `MediaCategory` | 历史库与识别分区。Europe 承载欧美来源体系，不能据此写作品的制作国家；不在本次修改持久化协议值 |
| `DimensionKey` | 固定分类维度是 API schema；维度里的分类词条、名称、别名、分组是外部运行数据，不继续堆进枚举 |
| `QueryMode` | 查询证据的入口方式，code / text / URL / fingerprint / external_id / catalogue 有不同必填约束，适合封闭枚举 |
| `RecognitionScope` | 识别范围策略 all / jav_only，不是内容分区，也不写成作品分类 |
| `IdentityKind` | 外部标识、URL、指纹等证据形态；与人物 / 机构名称别名分别处理，不能用名称替代实体 ID |
| `AssetState`、`CandidateState`、`MatchDecision` | 分别描述文件处理状态、候选处理状态、匹配建议。虽然存在近义词，但对象与生命周期不同，应保持独立 |
| `TaskStatus` | 通用执行尝试的生命周期。running、cancel_requested 不结束当前尝试；succeeded、partial、failed、cancelled、interrupted、paused 结束当前尝试。暂停 / 中断仍可通过新尝试继续业务 |
| 离线状态、checkpoint 状态 | 离线任务的 done 不等于 TaskRun 的 succeeded；checkpoint 的 paused / done 表示续跑游标状态。不能仅因同名就共享整个枚举 |
| CLI 批量 `BatchItem.state` | 描述调用回执，不描述下载；unknown 表示请求发出但结果未知，应先核对，不能自动重试或归为失败 |
| `OperationKind`、`OutputMode`、`NfoPolicy` | 分别是计划中的具体文件动作、用户选择的输出方式、已有 NFO 的冲突策略。sidecar 可产生多个具体动作，不应合并 |
| `ProviderRequirement` | 来源连接条件，允许一个来源同时需要多项，不是互斥内容类型 |
| `pan_backend`、`strm_mode` | 分别选择云盘接入与 STRM 输出 / 解析策略。同为 openlist 的取值不代表同一个问题 |
| Skill `effect`、HTTP method、transport | 固定执行协议边界，适合 Literal；不能替代操作能力、任务状态或作品分类 |
| 界面语言模式、刷片模式、刷新范围 | 是各自界面的局部选择，不属于作品或任务的领域状态，不应因为出现 all / none 等同名值而合并 |
| 时长选项 | 是查询区间，允许自定义上下界；不恢复为“长片 / 短片”这样的作品形式枚举 |

## 保持开放或兼容的字段

- Provider ID、任务 kind、来源字段名、匹配证据 kind、图片 kind 允许插件和来源扩展。读历史数据也应容忍未知值；强行全部改为封闭枚举会让新来源或旧记录使整页读取失败。已知值的写入和控制分支仍须按各自领域验证。
- `TaskRunOut.status` 暂保留可读的字符串协议；正式结束入口验证 `TaskStatus`。这使旧记录仍可展示，同时不允许新任务用离线 done 或任意拼写结束。
- `CollectionKind` 是旧兼容投影，不能重新用于把系列、机构、片单做成同类实体。机构与系列的边界见[实体与归属关系](catalog-entities.md)。
- `PlayKind` 的历史值同时含来源和传输提示。当前还要结合 provider、mime_type、playable_in_app 判断；来源名不应作为媒体格式。已有客户端依赖这些值，后续协议升级可以单独增加传输类型字段，不在本次直接改掉旧播放协议。

这次不重写真实词典或既有分类值，不进行线上迁移、服务部署或 APK 构建。新增查询字段、存在性字段与旧参数适配由同一组回归验证。

## 验证

- 后端全量 939 项、Web 全量 124 项通过；包含迟到进度、未知任务结果、离线完成但未登记媒体、组合筛选与旧参数回归。
- TypeScript 与 Web 生产构建通过，7 个相关 Python 模块的 mypy 检查通过。
- 改动 Python 文件没有新增 Ruff 问题；大型既有模块仍有历史问题，未将仓库全量 lint 记为通过。
