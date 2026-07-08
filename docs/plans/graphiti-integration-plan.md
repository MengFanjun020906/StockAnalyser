# Graphiti 时序知识图谱集成计划

> 实施状态：最小可用路径已接入。当前实现包含 Graphiti/Neo4j 配置、LiteLLM LLM/Embedding 适配器、Graphiti 服务封装、普通/Agent 分析结果入图、新闻信号卡片 Graphiti episode 投影、新闻卡片确定性边表与 Neo4j 显式关系投影、Agent `search_knowledge_graph` 检索工具、Docker Neo4j profile、`test_env.py --graph` 连通性检查和基础单元测试。选股 Prompt 直接注入图谱证据、用户画像图谱仍按后续阶段推进。

## 1. 背景与目标

当前系统的知识是"一次性"的：每次分析独立运行，Agent 不知道上周对同一只股票说了什么，也无法追踪事件的演化链条。现有 `AgentMemory`（`src/agent/memory.py`）只做了扁平的历史准确率统计，没有实体关系和时序维度。

Graphiti 是一个时序感知的知识图谱框架，核心能力：
- **Episode 驱动**：每次数据写入都是一个 episode，自动抽取实体和关系
- **时序事实管理**：边带 `valid_at` / `invalid_at`，追踪事实变化
- **实体去重与演化**：同一实体的多次提及自动合并，summary 随时间更新
- **混合检索**：语义 + 关键词 + 图遍历，低延迟高精度
- **自定义本体**：通过 Pydantic 模型定义实体和边类型

**集成目标**：让分析系统具备跨时间、跨股票的关联记忆能力，Agent 分析时能引用历史分析、关联事件和实体关系网络。

## 1.1 为什么要做：选股链路的核心痛点

当前系统的产品路径是**先选股，再对选中的股票做单股深度分析**。选股链路（参见 `docs/plans/agent-stock-selection-prompts.md` 的 5 阶段 prompt 设计）对跨股票、跨事件的关联推理有刚性需求，而这恰好是扁平记忆做不到、图谱能做的。

### 候选发现（Prompt 1）需要结构化的关联链

Prompt 1 明确要求"不得只因为板块涨幅高就直接推荐个股，必须写明候选来源和后续必查证据"。现在这个"候选来源"完全靠 LLM 临场发挥，没有可追溯的数据支撑。

有了图谱后，Agent 可以先查询"当前活跃事件 → 受益板块 → 成分股"的关系链路。候选来源变成了图谱中可复核的结构化路径（例如："美联储降息预期 → 利好黄金板块 → 山东黄金/紫金矿业"），而不是 LLM 编造的理由。

### 候选初筛（Prompt 2）需要历史证据

初筛要对候选股票做多维度评分。现在评分依据只有当次工具调用返回的实时数据，缺少历史纵深。

有了图谱后，初筛可以查询：
- 这只股票过去一个月被系统分析了几次？信号是什么？准确率如何？
- 这只股票关联的事件是在升温还是降温？
- 同板块的其他股票最近的分析结论是什么？

### 反方审查（Prompt 5）需要真实数据来质疑

Prompt 5 要求反方"找出会导致用户亏损或错误执行的薄弱点"。现在反方没有真实历史数据支撑，只能靠 LLM 推理来"唱反调"，说服力有限。

有了图谱后，反方可以查询：
- "这只股票上次出现类似技术形态时，系统给了什么信号，实际走势如何？"
- "这个板块上次被标记为'强势'后，持续了多久就回调了？"
- "这个事件驱动的上涨，历史上类似事件的影响周期是多长？"

用历史事实来质疑当前结论，比纯推理的反方论点有力得多。

### 事件去重与演化追踪

选股时经常遇到同一个政策/事件被多篇新闻重复报道（例如"美联储降息"在不同日期的新闻里反复出现）。现在系统无法识别它们是同一事件的演化，每次都当新信息处理。

图谱的实体去重机制会自动把这些新闻合并到同一个事件节点，事件的 summary 随时间更新（从"预期降息"演化为"确认降息 25bp"再到"市场消化降息影响"）。Agent 看到的是一条清晰的事件演化链，而不是一堆重复的新闻标题。

### 与现有 AgentMemory 的关系

现有 `AgentMemory`（`src/agent/memory.py`）做的是**扁平的统计记忆**：历史准确率、置信度校准、skill 权重。这些对单股分析够用，但对选股链路不够——选股需要的是**关系记忆**（股票-板块-事件-机构之间的关联）和**时序记忆**（事实如何随时间变化）。

两者是互补关系：AgentMemory 继续负责统计校准，图谱负责关联推理。不替换，只增量。

### 成本与 ROI 判断

**成本**：
- 多一个 Neo4j 服务（Docker profile 隔离，不启用时零开销）
- 每次入图 3-5 次 LLM 调用（用 `gpt-4.1-nano` 做小任务可控制成本）
- 初期只写分析结论入图，新闻入图后续按需开启

**收益**：
- 选股候选发现从"LLM 猜测"变为"图谱关联链 + LLM 判断"
- 反方审查从"纯推理"变为"历史事实 + 推理"
- 事件去重减少 LLM 处理重复信息的 token 浪费
- 分析质量可追溯，天然形成分析日志图谱

**结论**：如果选股链路是近期重点，图谱的 ROI 是正的。如果只做单股分析，现有 AgentMemory 够用，图谱可以后置。

## 1.2 新闻消息卡片化图谱设计

本轮恢复 Graphiti 消息面能力时，新闻不直接以孤立列表展示，而是设计为“两层结构”：

1. `RawNewsEpisode`：原始新闻层。保持不可变，记录来源、URL、发布时间、标题、原始摘要、抓取时间、去重键、正文可用性和 source chain。它是审计与回溯的事实来源，不直接承担投资含义判断。
2. `NewsSignalCard`：信号卡片层。面向页面展示和 Agent 检索，由一篇或多篇语义相近、事件相近或产业链指向相近的原始新闻归并生成，记录日期、轻量 LLM 缩略、情绪、主产业、次产业、影响周期、影响层级、相关公司、证据来源、置信度和更新时间。

页面默认展示 `NewsSignalCard`，点开卡片后展示背后的 `RawNewsEpisode` 列表。这样可以避免同一事件被重复新闻刷屏，同时保留可追溯来源；图谱里的语义边也优先连接信号卡片，原始新闻作为证据挂载在卡片下面。

### 1.2.1 日期与交易时段语义

信号卡片需要同时保留三个日期字段，避免“新闻发布时间”“系统入库时间”和“交易复盘归属日”混在一起：

| 字段 | 含义 | 用途 |
| --- | --- | --- |
| `published_at` | 原始新闻发布时间 | 来源审计、排序辅助、判断新闻滞后性 |
| `ingested_at` | 系统抓取/入库时间 | 排查抓取延迟、重跑幂等、数据血缘 |
| `signal_date` | 信号归属日 | 页面主筛选、日内复盘、选股链路按日取消息池 |

页面主日期使用 `signal_date + session`，例如 `2026-07-01 盘后`。原始新闻展开列表再展示每条 `published_at`。

A 股默认归属规则：

| 时间窗口（北京时间） | `signal_date` | `session` |
| --- | --- | --- |
| 09:00 前 | 前一交易日或盘前池归属日 | `pre_open` |
| 09:00-15:00 | 当日 | `intraday` |
| 15:00 后 | 当日 | `post_close` |

这张表只定义业务语义。实现时不在新闻模块独立计算交易日归属，必须复用 `src/core/trading_calendar.py` 的 `get_effective_trading_date()` 和现有交易日历能力。

后续如果支持港股/美股，按市场本地时区和对应交易日历扩展同一套字段，不复用服务器自然日。

### 1.2.2 消息层级：产业层 / 公司层 / 宏观层

第一版卡片已经落地 `signal_layer` 字段，用于把消息分成三类：

| `signal_layer` | 含义 | 典型例子 | 第一版规则 |
| --- | --- | --- | --- |
| `industry` | 产业、主题、品类或供应链扰动 | DRAM 涨价、机器人量产、PCB 景气、日韩材料供应受限 | 无明确公司映射且非宏观关键词时归产业层 |
| `company` | 已映射到具体公司或可交易标的 | 财联社显式关联股票、主题词典映射到 A 股公司、公司公告/订单/产能 | 有 `company_impacts` 或来源显式股票时归公司层 |
| `macro` | 宏观经济、货币政策、流动性、海外利率和就业数据 | 美国非农、CPI/PPI/PMI、美联储利率、央行逆回购、MLF/LPR、降准降息 | 宏观关键词优先，覆盖公司/产业映射，防止把流动性消息误归产业 |

页面列表和 API 均支持按 `signal_layer` 筛选。宏观层第一版先用关键词规则，不做宏观二级分类；后续可以扩展为 `overseas_macro`、`domestic_liquidity`、`policy`、`commodity_macro` 等二级标签。

### 1.2.3 情绪与影响方向分离

新闻卡片不把“文本情绪”和“投资影响方向”混成一个字段。一条新闻可能文本语气偏负面，但对某条国产替代链或某家公司构成正面催化；也可能文本语气积极，但对下游成本形成压力。

核心字段拆分如下：

| 字段 | 枚举 | 含义 |
| --- | --- | --- |
| `news_tone` | `positive` / `negative` / `neutral` / `mixed` | 新闻文本自身语气 |
| `market_impact` | `positive` / `negative` / `neutral` / `mixed` / `unknown` | 对相关市场整体的方向判断 |
| `industry_impacts[]` | 每项含 `industry`, `direction`, `strength`, `rationale` | 对主产业/次产业分别判断受益、受损或不确定 |
| `company_impacts[]` | 每项含 `symbol`, `name`, `direction`, `confidence`, `rationale` | 对具体公司的受益/受损/不确定判断 |

`industry_impacts[].direction` 使用 `benefit` / `harm` / `neutral` / `uncertain`。
`company_impacts[].direction` 使用 `benefit` / `harm` / `neutral` / `uncertain`。

页面主 badge 优先展示“产业/公司影响方向”，而不是只展示 `news_tone`。`news_tone` 作为辅助字段保留，用于筛选新闻语气和模型质量审计。

### 1.2.4 明示事实与多跳产业链推理分层

新闻信号卡片允许做多跳产业链推理，但必须把“原文明确说了什么”和“系统推理出了什么”分开记录，避免弱推理在图谱中被误当成强事实。

典型例子：新闻原文是“日韩某材料供应受限”，原文只明示国家、材料和供给状态；系统进一步推理“国内替代材料公司可能受益”“先进封装/PCB 上游可能被重估”，这些属于产业链传导推理。

字段设计：

| 字段 | 含义 |
| --- | --- |
| `explicit_entities` | 原文明示的国家、地区、公司、产品、材料、产业、政策、机构 |
| `primary_industries` | 原文直接相关产业 |
| `secondary_industries` | 通过产业链传导推理出来的相关产业 |
| `transmission_paths[]` | 产业链传导路径，按 `source -> mechanism -> target` 表达 |
| `inference_level` | 卡片整体推理层级：`explicit` / `first_order` / `second_order` |
| `evidence_grade` | 证据等级：`confirmed` / `plausible` / `speculative` |

`transmission_paths[]` 每项建议结构：

```json
{
  "source": "日本材料供应受限",
  "mechanism": "国产替代预期升温",
  "target": "国内先进封装材料供应商",
  "affected_industries": ["先进封装", "电子材料"],
  "affected_symbols": ["示例股票代码"],
  "inference_level": "second_order",
  "evidence_grade": "plausible",
  "rationale": "原文未点名公司，但产业链映射显示该材料存在国产替代环节"
}
```

页面必须能区分“新闻明示”和“系统推理”。卡片主列表可展示传导路径摘要，详情页展示完整路径、证据等级和原始新闻来源。

### 1.2.5 影响周期、有效期与衰减

信号卡片不能只标“长期/中期/短期”，还必须记录有效期和衰减规则。否则短期快讯会长期污染图谱，长期产业趋势也会被误当成日内交易信号。

核心字段：

| 字段 | 含义 |
| --- | --- |
| `impact_horizon` | 影响周期：`short` / `medium` / `long` |
| `valid_from` | 信号开始生效时间 |
| `valid_until` | 预计有效截止时间，可为空 |
| `decay_rule` | 衰减规则：`intraday` / `3d` / `2w` / `quarterly` / `structural` |
| `refresh_trigger` | 刷新触发器，例如订单落地、政策细则、涨价函、财报验证、公司澄清 |
| `staleness_score` | 陈旧度评分，0-100，越高越陈旧 |

默认周期规则：

| `impact_horizon` | 默认有效期 | 适用消息 |
| --- | --- | --- |
| `short` | 1-3 个交易日 | 突发消息、涨价、制裁、临时供需扰动、异动澄清 |
| `medium` | 2-8 周 | 订单、行业景气、政策细则、产能变化、客户验证 |
| `long` | 1-4 个季度以上 | 产业趋势、国产替代、技术路线变化、供给格局重塑 |

页面不仅展示影响周期，还要展示“是否过期”“是否需要新证据续命”。进入选股链路时，短期信号优先要求价格/资金验证，中长期信号优先要求产业证据和业绩验证。

### 1.2.6 图谱边类型：语义相似边与业务类型边分离

新闻卡片之间的连接分为两类，不能混用：

1. `semantic_similarity`：只连接 `NewsSignalCard <-> NewsSignalCard`。表示两张卡片在语义、主题、产业扰动或事件演化上相近，权重主要来自 embedding 相似度、关键词重叠和轻量 LLM 判别。
2. `typed_relation`：连接 `NewsSignalCard -> Industry / Company / Event / Material / Country / Policy`。表示明确的业务语义关系，例如影响产业、受益公司、受损公司、涉及材料、发生地区、政策来源。

分离原因：

- 语义相似边回答“这两张卡片像不像、是否可能属于同一事件簇”。
- 业务类型边回答“这张卡片具体影响了什么产业、公司、材料或国家”。
- 如果混成一种边，后续图谱检索会把“像不像”和“是什么关系”混读，导致 Agent 把弱相似当成强因果。

统一边字段：

| 字段 | 含义 |
| --- | --- |
| `edge_type` | `semantic_similarity` 或具体业务关系类型 |
| `weight` | 边权重，0-1 |
| `method` | 建边方法：`embedding` / `llm` / `rule` / `hybrid` |
| `rationale` | 建边理由，保留轻量可读解释 |
| `created_at` | 建边时间 |
| `decay_rule` | 边权重随时间衰减规则 |

`semantic_similarity` 建议附加字段：

| 字段 | 含义 |
| --- | --- |
| `embedding_model` | 使用的 embedding 模型，如本地 Ollama embedding 模型 |
| `cosine_similarity` | 原始余弦相似度 |
| `same_event_probability` | 是否属于同一事件或事件演化链的概率 |
| `same_industry_chain_probability` | 是否属于同一产业链扰动的概率 |

`typed_relation` 建议关系枚举：

| 关系 | 含义 |
| --- | --- |
| `MENTIONS` | 新闻明示提到某实体 |
| `AFFECTS_INDUSTRY` | 影响某产业 |
| `BENEFITS_COMPANY` | 可能利好某公司 |
| `HARMS_COMPANY` | 可能利空某公司 |
| `INVOLVES_MATERIAL` | 涉及某材料/产品 |
| `LOCATED_IN` | 事件发生地区或国家 |
| `POLICY_FROM` | 政策来源 |
| `SUPPLY_CHAIN_PATH` | 产业链传导路径 |

### 1.2.7 原始新闻归并为信号卡片的规则

`RawNewsEpisode` 归并为 `NewsSignalCard` 不只依赖 embedding 相似度，采用三段式规则：

1. **硬去重**：URL、标题 hash、来源原文 ID、正文 hash 高度一致，直接归为重复来源。重复来源不生成新卡，只挂到已有卡片的来源列表。
2. **同事件归并**：embedding 相似度高，同时满足时间窗口接近、明示实体重叠、事件动作一致，归并到同一张 `NewsSignalCard`。例如同一政策发布被不同媒体转述。
3. **同主题/同产业链连边**：embedding 相似度中等，或产业链传导路径相似，但不是同一事件，不合并卡片，只建立 `semantic_similarity` 边。这样保留“主题簇”关系，但避免把不同事件混成一张卡。

默认阈值：

| 阈值 | 建议值 | 动作 |
| --- | --- | --- |
| `duplicate_threshold` | `>= 0.96` | 判定重复新闻 |
| `same_event_threshold` | `>= 0.88` | 归并为同一事件卡片 |
| `same_theme_threshold` | `>= 0.76` | 建语义相似边，不合并 |

阈值只能作为第一层候选规则，最终归并还要结合：

- 时间窗口：同事件默认要求发布时间相隔不超过 72 小时，重大政策/产业趋势可放宽。
- 明示实体重叠：国家、公司、材料、政策、机构至少有一类重叠。
- 事件动作一致：如“涨价”“禁运”“扩产”“订单”“澄清”“事故”等动作不能冲突。
- 产业链路径一致：只作为同主题连边的强证据，不单独作为同事件合并依据。

设计目标是避免把“都属于国产替代”的不同新闻粗暴合并；同一事件可以合并，同一主题只连边。

### 1.2.8 新闻卡片页面第一版信息架构

第一版页面以 `NewsSignalCard` 列表为主，局部图谱为辅助详情，不直接做全库网络大图。

原因：

- 日常选股动作需要快速扫描、筛选、排序、点开验证，卡片列表的信息密度更高。
- 全库图谱网络容易变成展示型视图，节点过多后不利于发现可交易线索。
- 局部图谱更适合解释“这条消息为什么和这些产业/公司/历史事件相关”。

页面结构建议：

| 区域 | 功能 |
| --- | --- |
| 顶部筛选栏 | `signal_date`、`session`、影响周期、主产业、影响方向、证据等级、是否过期、是否有相关公司 |
| 主区域 | `NewsSignalCard` 列表，支持按信号强度、更新时间、相似簇热度、影响周期排序 |
| 右侧详情面板 | 原始新闻列表、传导路径、相关产业/公司、相似卡片、证据等级、刷新触发器 |
| 局部图谱视图 | 点开单卡后展示当前卡片的 ego graph：当前卡片、相似卡片、产业、公司、材料、国家、政策 |

卡片列表的主目标是帮助用户发现“普通新闻中的隐含信号”，而不是展示所有图谱节点。图谱视图默认只展示 1-hop，必要时允许展开 2-hop，并限制节点数量，避免页面失控。

### 1.2.9 轻量 LLM 与规则/数据库的职责边界

新闻卡片生成采用“LLM 抽取 + 规则/数据库裁决”的架构。轻量 LLM 负责语义理解和可读解释，但不能单独决定股票映射、标准产业归属和最终强度。

轻量 LLM 可以负责：

| 任务 | 输出 |
| --- | --- |
| 新闻短缩略 | `summary_short` |
| 文本情绪判断 | `news_tone` |
| 明示实体抽取 | `explicit_entities` 草案 |
| 产业链传导草案 | `transmission_paths` 草案 |
| 影响周期初判 | `impact_horizon` 初判 |
| 可读解释 | `rationale` |

规则、embedding 或数据库必须参与的任务：

| 任务 | 约束 |
| --- | --- |
| 公司代码映射 | 必须查股票索引、同义词表、产业链词典或公司业务标签，不能由 LLM 编代码 |
| 主产业/次产业标准化 | 必须映射到本地行业/概念 taxonomy |
| 语义相似召回 | embedding 先召回候选，LLM 只做二次判别 |
| 证据等级 | 根据原文明示、公告/财报/订单证据、产业链推理层级等规则计算 |
| 卡片强度分 | 由规则综合计算，LLM 不直接给最终分 |

最终信号强度建议由以下因素综合：

- 消息新鲜度与 `staleness_score`
- 影响周期和衰减规则
- 证据等级
- 产业链传导层级
- 是否映射到可交易公司
- 是否有多来源互证
- 是否已有价格、成交量、资金流或板块热度验证

这样可以保留 LLM 的语义理解能力，同时让结果可追溯、可调参、可回测。

### 1.2.10 新闻卡片 embedding 与 Ollama 降级策略

新闻卡片和语义相似边会产生大量 embedding 调用，默认优先使用本地 Ollama embedding，以降低成本并提升可控性；但 embedding 不应阻断原始新闻入库和信号卡片生成。

建议配置：

```env
NEWS_CARD_EMBEDDING_PROVIDER=ollama       # ollama | litellm | disabled
NEWS_CARD_OLLAMA_BASE_URL=http://localhost:11434
NEWS_CARD_OLLAMA_EMBEDDING_MODEL=nomic-embed-text
NEWS_CARD_EMBEDDING_DIM=
NEWS_CARD_EMBEDDING_FALLBACK_PROVIDER=litellm  # litellm | none
```

运行逻辑：

| 场景 | 行为 |
| --- | --- |
| Ollama 可用 | 使用本地 embedding 生成向量并建立 `semantic_similarity` 边 |
| Ollama 不可用且配置 fallback | 使用远端 LiteLLM embedding 降级 |
| Ollama 和 fallback 都不可用 | 继续生成 `NewsSignalCard`，但不建立语义相似边 |

当无法建立语义边时，卡片需要标记：

```json
{
  "semantic_graph_status": "disabled",
  "semantic_graph_reason": "ollama_unavailable"
}
```

这保证消息面系统的关键路径是“先沉淀新闻事实和信号卡片”，语义图谱增强可降级，不拖垮每日流程。

### 1.2.11 新闻卡片进入选股链路的 gate

新闻卡片可以作为选股候选来源之一，但不能绕过证据 gate 直接进入最终 seed pool。第一阶段定位为 `candidate_evidence`：提供候选线索、产业链传导和证据来源，是否进入 seed pool 由规则和市场验证共同决定。

进入 seed pool 的最低条件：

| 条件 | 要求 |
| --- | --- |
| 证据等级 | `evidence_grade != speculative` |
| 公司映射 | 有明确 `company_impacts`，或可验证的产业链映射 |
| 时效性 | `staleness_score` 未过期，且 `valid_until` 未失效 |
| 周期匹配 | `impact_horizon` 与当前交易场景匹配 |
| 市场验证 | 至少命中一个验证信号：板块热度、资金流、量价异动、公告、订单、公司资料 |

公司新闻中“直接点名公司”默认不直接产 seed，只作为诊断证据；需要额外验证是否存在营销稿、蹭热点、澄清公告、股价已充分反应等风险。

选股链路使用方式：

- `NewsSignalCard` 先进入候选证据池。
- gate 通过后才可作为 seed pool 的候选来源。
- Judge / 反方审查必须能看到原始新闻、推理层级、证据等级和市场验证状态。
- 对 `second_order` 或 `plausible` 级别信号，默认降权，除非有价格/资金/公告互证。

### 1.2.12 信号强度分与排序

新闻卡片页面默认按可解释的规则型 `signal_score` 排序，而不是让 LLM 直接给最终分。时间倒序仍作为可切换排序。

建议综合公式：

```text
signal_score =
  novelty_score
  + evidence_score
  + transmission_score
  + tradability_score
  + confirmation_score
  - staleness_penalty
  - crowding_penalty
```

分项字段：

| 字段 | 含义 |
| --- | --- |
| `novelty_score` | 新鲜度、是否首次出现、是否来自普通新闻里的隐含信号 |
| `evidence_score` | 证据等级、多来源互证、是否有公告/财报/订单支撑 |
| `transmission_score` | 产业链传导是否清晰，是否有明确受益/受损环节 |
| `tradability_score` | 是否映射到 A 股可交易标的，流动性是否足够 |
| `confirmation_score` | 板块热度、资金流、量价异动、公告或订单是否已有验证 |
| `staleness_penalty` | 陈旧度惩罚 |
| `crowding_penalty` | 共识拥挤惩罚；已经被热榜、涨停、媒体反复报道的线索降权 |

页面排序模式：

| 模式 | 排序 |
| --- | --- |
| `最强` | `signal_score desc` |
| `最新` | `signal_date desc, updated_at desc` |
| `最隐蔽` | `novelty_score desc, crowding_penalty asc, evidence_score desc` |
| `需验证` | `evidence_grade in (plausible, speculative)` 且有明确 `refresh_trigger` |

卡片列表默认使用 `最强`，但页面必须保留切换，避免高分旧卡长期压住新消息。

### 1.2.13 存储策略：关系型卡片库 + Graphiti 图谱双写

新闻卡片不只存在 Graphiti/Neo4j。页面列表、筛选、分页和排序优先使用关系型表；Graphiti/Neo4j 负责实体关系、语义相似边、事件演化和局部图谱检索。

原因：

- 页面需要高频列表查询、组合筛选、分页和排序，关系型表更稳定。
- Graphiti 更适合关系检索、语义召回和图遍历，不适合直接承载复杂表格分页。
- 双写能让消息面页面在 Graphiti 暂不可用时仍能展示卡片。

建议关系型表：

| 表 | 职责 |
| --- | --- |
| `news_raw_episodes` | 原始新闻，不可变事实来源 |
| `news_signal_cards` | 信号卡片主表，承载页面列表字段和评分字段 |
| `news_signal_sources` | 卡片与原始新闻的关联 |
| `news_signal_impacts` | 产业/公司影响明细 |
| `news_signal_edges` | `semantic_similarity` / `event_clue` / `typed_relation` 边缓存，便于页面快速读取和重放到 Neo4j |

Graphiti/Neo4j 侧职责：

- 存储 `NewsSignalCard`、`Industry`、`Company`、`Event`、`Material`、`Country`、`Policy` 等实体。
- 存储 `semantic_similarity` 与 `typed_relation`。
- 支持局部 ego graph 查询。
- 支持 Agent 的跨时间、跨产业链关系检索。

第一版实现已将 `NewsSignalCard` 作为 Graphiti episode 投影写入：

- `NewsSignalService.rebuild()` 默认只保存关系型真源；页面“重建卡片”不等待 Graphiti/Embedding，避免慢外部依赖拖垮卡片生成。
- 页面“同步图谱”或 `sync_graphiti=true` 才会 best-effort 调用 Graphiti 同步。
- `POST /api/v1/news-signals/graph-sync` 可按日期或全量 pending/failed 卡片补同步。
- `POST /api/v1/news-signals/edges/rebuild` 可重建确定性边；`include_semantic=true` 且 embedding 配置可用时额外生成 `semantic_similarity`。
- `GET /api/v1/news-signals/edges` 与 `GET /api/v1/news-signals/{card_id}/graph` 从关系型真源返回边和局部图。
- Graphiti 同步会 best-effort 将 `news_signal_edges` 投影为 Neo4j 中的 `NewsSignalCard` / `NewsSignalTarget` 显式关系，便于 Neo4j Browser 直接查看事件线索。
- Graphiti 不可用时保持 `graph_sync_status=pending`，不阻断卡片生成。
- Graphiti 写入失败时记录 `graph_sync_status=failed`、`graph_retry_count`、`graph_last_error`，后续可从关系型表重放。
- episode body 同时包含结构化 JSON 和 `semantic_text`，便于 Graphiti 抽取新闻、产业、公司、宏观变量和传导路径之间的关系。

读取路径：

- 卡片列表：优先查关系型表。
- 卡片详情：关系型表返回主数据和来源，Graphiti 返回局部图谱。
- Agent 检索：优先走 Graphiti；Graphiti 不可用时可降级查询 `news_signal_cards`。

### 1.2.14 新闻卡片页面 API 第一版

第一版 API 按页面消费设计，不暴露底层图谱复杂性。

| 方法 | 路径 | 职责 |
| --- | --- | --- |
| `GET` | `/api/v1/news-signals` | 卡片列表，支持日期、产业、影响方向、周期、证据等级、排序、分页 |
| `GET` | `/api/v1/news-signals/{card_id}` | 单卡详情，包括来源新闻、影响明细、传导路径、评分拆解 |
| `GET` | `/api/v1/news-signals/{card_id}/graph` | 单卡局部图谱，返回 `nodes` / `edges`，默认 1-hop，可选 2-hop |
| `GET` | `/api/v1/news-signals/edges` | 边列表，支持按日期、卡片和边类型过滤 |
| `POST` | `/api/v1/news-signals/rebuild` | 手动重建或补算卡片，第一版支持 `target_date`、来源开关和可选 `sync_graphiti`；默认不等待 Graphiti |
| `POST` | `/api/v1/news-signals/edges/rebuild` | 重建关系型边；默认规则边，显式 `include_semantic=true` 时尝试 embedding 语义边 |
| `POST` | `/api/v1/news-signals/graph-sync` | 从关系型真源重放 pending/failed 新闻卡片到 Graphiti，用于历史补同步和失败修复 |

列表接口建议查询参数：

| 参数 | 含义 |
| --- | --- |
| `date_from` / `date_to` | `signal_date` 范围 |
| `session` | `pre_open` / `intraday` / `post_close` |
| `signal_layer` | `industry` / `company` / `macro` |
| `industry` | 主产业或次产业 |
| `impact_direction` | `benefit` / `harm` / `neutral` / `uncertain` |
| `impact_horizon` | `short` / `medium` / `long` |
| `evidence_grade` | `confirmed` / `plausible` / `speculative` |
| `has_company` | 是否存在公司映射 |
| `expired` | 是否过期 |
| `sort` | `strongest` / `latest` / `hidden` / `needs_validation` |
| `page` / `page_size` | 分页 |

图谱检索式 API 后置；第一版只服务卡片列表、详情和单卡局部图谱。

### 1.2.15 前端页面入口

新闻信号卡片做独立页面，不塞进 Agent Trace 或选股页。

| 项目 | 设计 |
| --- | --- |
| 路由 | `/news-signals` |
| 导航名 | `消息面` |
| 页面定位 | 每日新闻信号工作台 |
| 与选股 Trace 的关系 | Trace 只展示本次使用了哪些 `NewsSignalCard` 作为候选证据 |
| 与 Seed Pool 质量页的关系 | 后续可反向关联：某个 seed 来自哪些消息卡片，T+1 表现如何 |

独立页面的原因：

- 消息面是每天可主动扫描的工作台，不依附于某次 Agent 运行。
- 用户需要在没有启动选股 Trace 的情况下，先浏览和筛选新闻信号。
- 选股链路消费消息卡片，但不承担完整的消息面探索功能。

### 1.2.16 卡片首屏字段

卡片列表第一屏只展示能帮助用户快速判断“是否值得点开”的字段，长解释和完整来源放到详情抽屉。

卡片首屏字段：

| 字段 | 展示方式 |
| --- | --- |
| `signal_date + session` | 日期与交易时段 badge |
| `signal_layer` | 产业层 / 公司层 / 宏观层 badge |
| `summary_short` | 一句话缩略 |
| `primary_industries` | 最多展示 2 个主产业 |
| `impact_horizon` | 短/中/长 badge |
| `top_company_impacts` | 最多 3 个公司，显示 `benefit` / `harm` / `uncertain` |
| `signal_score` | 总分 + 主要加分原因 |
| `evidence_grade` | `confirmed` / `plausible` / `speculative` |
| `inference_level` | `explicit` / `first_order` / `second_order` |
| `source_count` | 来源新闻数量 |
| `staleness / expired` | 陈旧或过期状态 |

首屏不展示：

- 长 `rationale`
- 完整 `transmission_paths`
- 全部相似卡片
- 原始新闻正文

这些内容统一进入右侧详情抽屉，避免卡片列表变成长文报告。

### 1.2.17 人工反馈与纠错 overlay

第一版必须支持轻量人工反馈/纠错。新闻信号卡片依赖 LLM 抽取、embedding 归并和产业链推理，早期必然会出现错误映射、错误合并或弱推理过强的问题；如果不能纠错，错误会进入图谱并污染后续检索。

原则：人工修正不直接覆盖原始 LLM 输出，而是写入 overlay，保留原始抽取结果和人工修正结果的差异。

建议新增表：

| 表 | 职责 |
| --- | --- |
| `news_signal_feedback` | 用户对卡片的反馈、纠错和标注 |

第一版支持的操作：

| 操作 | 含义 |
| --- | --- |
| 标记 `useful` | 有价值信号 |
| 标记 `wrong` | 判断错误 |
| 标记 `noisy` | 噪音或低质量 |
| 标记 `duplicate` | 重复卡片 |
| 调整 `primary_industries` | 修正主产业 |
| 移除错误公司映射 | 删除不成立的 `company_impacts` |
| 补充备注 | 记录人工判断理由 |

暂不在第一版页面做复杂合并/拆分操作。卡片合并、拆分可以先通过 rebuild 参数或后台脚本处理；反馈表先沉淀人工判断，为后续规则调参、归并阈值修正和 signal_score 评估提供数据。

### 1.2.18 第一版新闻来源

第一版复用现有新闻、搜索和公告来源，不先建设大型全网新闻采集系统。目标是先把已有消息源结构化为信号卡片，再逐步扩展实时源。

优先来源：

| 来源 | 用途 |
| --- | --- |
| `news_theme_daily` | 东方财富财经早餐/主题日报，作为每日盘前主题源 |
| `search_stock_news` / 现有搜索服务 | 按公司、产业、关键词补充新闻 |
| 公告 / 投资者关系记录 | 高证据等级来源 |
| 财联社电报 | 实时快讯来源，补充日内突发消息 |

财联社电报入口为 `https://www.cls.cn/telegraph`。页面本身可能存在 WAF 和前端渲染限制，实现时优先使用更稳定的聚合接口 `https://orz.ai/api/v1/dailynews/?platform=cls`；若接口不可用，工具应返回结构化降级原因，不能阻断新闻卡片生成。

第一版已经明确复用现有 Agent 新闻工具链路，新增 `get_cls_telegraph_news` 作为财联社电报实时快讯工具：

- 页面入口：`https://www.cls.cn/telegraph`
- 第一版数据接口：`https://orz.ai/api/v1/dailynews/?platform=cls`
- 返回字段：标题、内容、发布时间、热度分数、排名、来源链接、`source_chain` 和 `errors`
- 支持轻量过滤：`keyword`、`important_only`、`last_time`
- 降级原则：接口失败、WAF 或结构变化时返回 `status=error` 和结构化错误，不阻断其它新闻来源生成卡片

雪球热榜作为第二个消息面工具接入 `get_xueqiu_hot_news`，数据接口为 `https://orz.ai/api/v1/dailynews/?platform=xueqiu`。它只作为市场关注度、主题扩散和情绪确认的辅助证据，不能单独替代公司披露、业务归属或政策/产业新闻证据。

宏观财经作为第三个消息面工具接入 `get_macro_finance_news`，第一版复用 orz dailynews 的 `sina_finance`、`eastmoney` 平台，并在本地按宏观关键词过滤。为避免美国非农、央行逆回购等重要事件滚出热榜后丢失，工具默认追加 `SearchService.search_general_news()` 的固定宏观查询 fallback；未配置搜索 key 时只返回 `disabled` 诊断，不阻断 dailynews 来源。它用于补齐美国非农、CPI/PPI/PMI、美联储利率、央行公开市场操作、逆回购、MLF/LPR、流动性、汇率和大宗商品等宏观层消息。宏观消息默认只生成 `signal_layer=macro` 的市场/风格约束证据，不直接臆造公司受益。

当前第一版重建链路默认抓取四类来源：

| 工具 | 来源 | 用途 | 当前验证 |
| --- | --- | --- | --- |
| `get_eastmoney_cjzc_daily` | 东方财富财经早餐 | 盘前主题、产业链映射、候选主题来源 | 2026-07-04 smoke 通过，回退到 2026-07-03，返回 5 个主题 |
| `get_cls_telegraph_news` | orz dailynews `platform=cls` | 财联社消息热榜/电报，捕捉日内催化 | 2026-07-04 smoke 通过，返回 `status=ok` |
| `get_xueqiu_hot_news` | orz dailynews `platform=xueqiu` | 雪球热榜，观察讨论热度和主题扩散 | 2026-07-04 smoke 通过，返回 `status=ok` |
| `get_macro_finance_news` | orz dailynews `platform=sina_finance,eastmoney` + `SearchService.search_general_news` fallback | 宏观层消息，补齐非农、逆回购、利率、流动性等市场约束 | 2026-07-04 smoke 通过，两个平台均返回 `status=ok`；无搜索 key 时 fallback 降级为 `disabled` |

### 1.2.19 与现有资产的边界

NewsSignalCard 不能成为第二套平行的“新闻证据”或“新闻选股”系统。第一版的边界如下：

| 现有资产 | 复用方式 | NewsSignalCard 边界 |
| --- | --- | --- |
| `src/agent/evidence/` 的 `EvidenceCard` / `EvidenceSignal` | Agent 运行态仍使用现有证据卡协议，`dimension` 继续使用 `news_event` | NewsSignalCard 是持久化消息信号层，不替代 EvidenceCard；进入 Agent 前必须适配为 EvidenceCard |
| `candidate_experts_v2/resources/news_theme_daily/concept_mapping.json` | 作为第一版产业链词典和公司映射真源，复用 `aliases`、`related_boards`、`mapped_stocks` | 不另建手工平行映射；覆盖不到的主题先降级为产业级证据 |
| `theme_catalyst_desk` / Seed Pool | 新闻卡片作为主题催化席和 seed pool 的补充证据输入 | 不新增独立的“新闻卡片 -> 股票候选”直通链路 |
| `SelectionSeedPoolEvaluation` | 作为卡片信号效果回测和评分校准的主要数据源 | 不单独发明一套脱离 seed pool 的收益评估表 |
| `AgentMemory` | 继续负责置信度校准和历史命中率统计 | 图谱只补关系记忆；校准结果要回流 AgentMemory 或其后续校准接口 |

NewsSignalCard 到 EvidenceCard 的适配规则：

- `valid_from` / `valid_until` / `refresh_trigger` 映射为 `EvidenceExpiry`。
- `industry_impacts`、`company_impacts`、`transmission_paths` 映射为 `EvidenceSignal`，其中 `direction`、`strength`、`score_delta` 只使用现有字段表达。
- `evidence_grade`、`inference_level`、`mapping_confidence` 共同影响 `EvidenceImpact.confidence` 和 `counter_evidence`，不能绕过现有证据置信度口径。
- `card_id`、`raw_episode_ids` 和 `source_chain` 进入 `raw_ref`，便于 Trace 回溯到新闻卡片和原始新闻。

公司映射第一版直接复用 `concept_mapping.json`。当主题无法命中词典、简称存在歧义、或映射置信度不足时，只允许生成产业级证据，不允许把不确定股票注入 seed pool。人工反馈可以沉淀扩充建议，但扩充后的主题、别名和 mapped stocks 仍应回写到统一词典或其后续结构化替代物，避免多处维护。

### 1.2.20 反馈闭环与评分校准

`signal_score = novelty + evidence + transmission + tradability + confirmation - staleness - crowding` 只能作为第一版先验排序，不能长期固定权重。NewsSignalCard 的评分必须进入“卡片 -> seed -> T+1 表现 -> 权重校准”的闭环。

建议新增两类关联记录：

| 表/记录 | 职责 |
| --- | --- |
| `news_signal_seed_links` | 记录 `card_id`、`seed_item_id`、来源席位、gate 结果、当时的 `signal_score` 快照、映射置信度和证据等级 |
| `news_signal_outcomes` | 从 `selection_seed_pool_evaluations` 汇总卡片带来的 T+1 结果，包含 `alpha_return_pct`、`mfe_pct`、`mae_pct`、成交状态和样本窗口 |

第一版校准读路径：

- 每次 seed pool 质量评估完成后，按 `news_signal_seed_links` 回填卡片结果。
- 按来源、产业、`inference_level`、`evidence_grade`、`mapping_confidence`、`impact_horizon` 分桶统计命中率和超额收益。
- 统计结果用于调整后续 `signal_score` 的分项权重、归并阈值和 seed gate 的最低证据要求。
- 样本不足时只展示统计，不自动调权；达到最小样本数后再启用 AgentMemory 式置信度校准。

人工反馈 overlay 也必须有读路径：

| 反馈 | 后续影响 |
| --- | --- |
| `wrong` | 降低相同来源/主题/推理层级组合的排序权重；若涉及公司映射错误，阻止该映射直接进 seed |
| `noisy` | 降低卡片展示优先级和 seed gate 权重 |
| `duplicate` | 进入后续归并阈值校准；rebuild 时优先合并 |
| `useful` | 只作为人工质量标签；需要 T+1/T+N 结果验证后才提高模型权重 |
| 调整产业或移除公司 | 覆盖展示层和 EvidenceCard 适配层，但保留原始 LLM 输出用于审计 |

没有读路径的反馈数据不算闭环，第一版实现时必须至少让 `wrong/noisy/duplicate` 影响展示排序、候选证据权重和 rebuild 归并结果。

### 1.2.21 调度、成本预算与降级

新闻卡片第一版采用“盘后批处理为主、财联社电报增量为辅”的触发模型：

- `daily_run.sh` 或对应后端任务在每日数据更新后生成当日 NewsSignalCard，作为稳定主链路。
- 财联社电报用于日内增量入库，先进入 `RawNewsEpisode`，按 5-10 分钟小批量归并，不对每条快讯立即触发完整 LLM/Graphiti 流程。
- `signal_date` 和 `session` 复用 `src/core/trading_calendar.py` 的 `get_effective_trading_date()` 及现有交易日历语义，不在新闻模块重造盘前/盘中/盘后判断。

第一版建议预算上限：

| 配置 | 建议默认值 | 降级策略 |
| --- | --- | --- |
| `NEWS_SIGNAL_MAX_RAW_PER_RUN` | 200 | 超过后只存原始新闻，卡片生成延后 |
| `NEWS_SIGNAL_MAX_CARDS_PER_DAY` | 80 | 低分卡片进入 `pending`，不进入展示首屏 |
| `NEWS_SIGNAL_LLM_MAX_ITEMS_PER_RUN` | 80 | 超额新闻只做规则摘要和待处理标记 |
| `NEWS_SIGNAL_EMBED_BATCH_SIZE` | 64 | 分批失败时缩小批次重试 |
| `NEWS_SIGNAL_EMBED_MAX_PER_DAY` | 1000 | 超额后停止新增语义相似边 |
| `NEWS_SIGNAL_TOTAL_TIMEOUT_SECONDS` | 300 | 超时保留已完成卡片，剩余项标记 `pending` |

降级原则：

- LLM 失败：保留 RawNewsEpisode，生成 `status=pending_llm`，不阻断其它来源。
- Embedding 失败：跳过 `semantic_similarity` 边，但保留 typed relation 和展示卡片。
- Graphiti 失败：关系型表仍为真源，记录 `graph_sync_status=failed`，后续 repair。
- 财联社接口失败：记录结构化错误，继续处理 `news_theme_daily`、搜索新闻和公告来源。

### 1.2.22 一致性、幂等与重建

NewsSignalCard 的一致性原则：关系型表是真源，Graphiti 是可重建投影。任何时候不能因为 Graphiti 写入失败而丢失原始新闻或卡片。

写入顺序：

1. 写入或 upsert `RawNewsEpisode`，使用来源 ID、URL、发布时间、标题/正文 hash 组成去重键。
2. 生成或更新 `NewsSignalCard`，使用稳定 `card_id` 和事件簇 key upsert。
3. 写入 `news_signal_seed_links`、feedback overlay 等关系型记录。
4. 第一版在 `rebuild()` 后 best-effort 同步 Graphiti episode，记录 `graph_sync_status`、`retry_count`、`last_error`；后续再升级为独立 outbox worker。

`POST /rebuild` 契约：

- 默认按日期区间从关系型表重算卡片、边和 Graphiti 投影。
- rebuild 必须幂等：相同 RawNewsEpisode 和相同归并规则生成相同 `card_id` 或更新同一条记录。
- 默认软失效旧边和旧卡片版本，再 upsert 新版本；只有显式 `force_delete=true` 才物理删除孤立投影。
- Graphiti rebuild 只从关系型真源重放，不从 Graphiti 反推关系型数据。
- 当前已支持 `/api/v1/news-signals/graph-sync` 对 pending/failed 卡片做关系型真源重放；完整 date range rebuild 和 outbox worker 仍属后续项。
- rebuild 输出必须包含新增、更新、合并、软失效、失败数量，便于对账。

### 1.2.23 阈值、映射歧义与可观测性

语义归并阈值必须绑定 embedding 模型，不能跨模型复用。`0.96/0.88/0.76` 这类余弦阈值只在指定模型、维度和文本拼接方式下有效。

第一版要求：

- `semantic_similarity` 边保存 `embedding_model`、`embedding_dimension`、`threshold_profile` 和 `cosine_similarity`。
- 每个 embedding provider 单独配置高/中/低阈值；切换到降级模型时，如果没有校准 profile，只生成向量检索结果，不自动合并卡片。
- rebuild 时按记录里的模型 profile 解释历史边，避免新模型重算后静默改变事件簇。

公司代码映射必须有置信度和失败模式：

| 映射状态 | 条件 | 允许行为 |
| --- | --- | --- |
| `mapped` | 股票代码明确或词典唯一命中，且业务描述一致 | 可进入 `company_impacts` 和 seed 证据 |
| `ambiguous` | 简称同名、A/H/美股混淆、多个 mapped stocks 同时匹配 | 只展示候选，不进入 seed；需要公告/业务上下文二次确认 |
| `industry_only` | 只命中产业或主题，未命中公司 | 只生成产业级 EvidenceSignal |
| `unmapped` | 无可信产业或公司映射 | 仅保留新闻卡片和 RawNewsEpisode |

关键观测指标：

- 原始新闻入库数、去重率、来源失败率。
- 卡片生成成功率、`pending_llm` 数量、LLM 超时率。
- 产业映射命中率、公司映射命中率、歧义率、人工移除公司次数。
- 语义归并率、重复反馈率、embedding 降级率。
- Graphiti 同步成功率、失败 backlog、repair 成功率。
- 卡片进入 seed pool 的比例、T+1 `alpha_return_pct`、`mfe_pct`、`mae_pct` 分桶表现。

## 2. 技术选型与约束

| 项目 | 选型 | 说明 |
|------|------|------|
| Graphiti 引用方式 | 本地源码 (`-e ./graphiti`) | 方便调试和定制 |
| 图数据库 | Neo4j 5.26+ | Graphiti 默认后端，Docker 部署 |
| LLM 适配 | 写 LiteLLM adapter | 复用项目现有的 LiteLLM 多 provider 路由 |
| Embedding 适配 | 写 LiteLLM embedder | 复用项目现有的 embedding 配置 |
| 开关控制 | `GRAPHITI_ENABLED=true/false` | 默认关闭，不影响现有流程 |
| 写入模式 | 异步，不阻塞分析主流程 | 图谱写入失败不影响报告生成 |
| 隔离策略 | `group_id` 按市场或用户分区 | cn / hk / us 各自独立子图 |

## 3. 适配点分析

### 3.1 LLM 适配（核心适配）

**Graphiti 侧接口**：`graphiti_core/llm_client/client.py` → `LLMClient` 抽象类

```python
# Graphiti 要求实现的核心方法
class LLMClient(ABC):
    async def _generate_response(
        self,
        messages: list[Message],           # Message(role, content)
        response_model: type[BaseModel] | None,  # 期望 JSON schema
        max_tokens: int,
        model_size: ModelSize,             # small / medium
    ) -> dict[str, Any]:
        ...
```

**项目侧现有能力**：`src/agent/llm_adapter.py` 通过 LiteLLM 统一调用多 provider。

**适配方案**：新建 `src/services/graphiti/litellm_client.py`，继承 `graphiti_core.llm_client.LLMClient`，内部用 `litellm.acompletion()` 实现 `_generate_response`。

关键映射：
- `Message(role, content)` → LiteLLM 的 `{"role": ..., "content": ...}`
- `response_model` → 在 prompt 末尾追加 JSON schema（Graphiti 已在 `generate_response` 中处理）
- `ModelSize.small` → 项目配置的轻量模型（如 `gpt-4.1-mini`）
- `ModelSize.medium` → 项目配置的主力模型（如 `gemini/gemini-2.0-flash`）
- `LLMConfig.model` / `LLMConfig.small_model` → 从项目 `get_config()` 读取

**注意事项**：
- Graphiti 的 `generate_response` 会在 messages 末尾追加 JSON schema，在 messages[0] 追加多语言指令——这些逻辑在基类中，adapter 只需实现 `_generate_response`
- Graphiti 内部大量使用 structured output（Pydantic response_model），LiteLLM 对此的支持因 provider 而异；建议 Graphiti 专用模型优先选 OpenAI 或 Gemini
- `LLMConfig` 的 `temperature` 默认为 1（Graphiti 侧），需要确认是否需要覆盖

### 3.2 Embedding 适配

**Graphiti 侧接口**：`graphiti_core/embedder/client.py` → `EmbedderClient` 抽象类

```python
class EmbedderClient(ABC):
    async def create(self, input_data: str | list[str] | ...) -> list[float]: ...
    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]: ...
```

**适配方案**：新建 `src/services/graphiti/litellm_embedder.py`，继承 `EmbedderClient`，内部用 `litellm.aembedding()` 实现。

关键点：
- `EMBEDDING_DIM` 默认 1024，需要和实际使用的 embedding 模型对齐（`text-embedding-3-small` 默认 1536，需要截断或配置 `dimensions` 参数）
- 如果项目没有统一的 embedding 配置，可以先直接用 Graphiti 默认的 `OpenAIEmbedder`，后续再统一

### 3.3 Graph Service 封装

**新建文件**：`src/services/graph_service.py`

职责：
- 初始化和管理 Graphiti 客户端实例（单例）
- 封装 `add_episode` / `search` / `close` 等常用操作
- 处理 `GRAPHITI_ENABLED` 开关，关闭时所有方法返回空/跳过
- 异常兜底，图谱操作失败不影响主流程

```python
# 伪代码
class GraphService:
    def __init__(self):
        if not config.graphiti_enabled:
            self._client = None
            return
        self._client = Graphiti(
            uri=config.neo4j_uri,
            user=config.neo4j_user,
            password=config.neo4j_password,
            llm_client=LiteLLMGraphitiClient(...),
            embedder=LiteLLMGraphitiEmbedder(...),
        )

    async def ingest_analysis(self, result: AnalysisResult, context: dict) -> None:
        """将分析结果作为 episode 写入图谱"""
        ...

    async def ingest_news(self, news_items: list, stock_code: str) -> None:
        """将新闻批量写入图谱"""
        ...

    async def search(self, query: str, group_ids: list[str], ...) -> SearchResults:
        """混合检索"""
        ...
```

### 3.4 Pipeline 接入点

**文件**：`src/core/pipeline.py` → `StockAnalysisPipeline`

**接入位置 1：分析结果入图**

在 `analyze_stock()` 的 Step 8（保存分析历史）之后，追加异步入图：

```
# pipeline.py:497-515 附近
# Step 8: 保存分析历史记录 (现有)
if result and result.success:
    self.db.save_analysis_history(...)

# Step 9: 写入知识图谱 (新增)
if result and result.success:
    await self._ingest_to_graph(result, enhanced_context, news_context)
```

同样在 `_analyze_with_agent()` 的保存历史之后（约 line 906-918）追加。

**接入位置 2：新闻入图**

在 `analyze_stock()` 的 Step 4（情报搜索完成后，约 line 393-414），保存新闻到 DB 的同时写入图谱：

```
# 现有：self.db.save_news_intel(...)
# 新增：await self._ingest_news_to_graph(code, stock_name, intel_results)
```

**注意**：Pipeline 当前是同步的（ThreadPoolExecutor），图谱写入是 async。需要用 `asyncio.run()` 或 `loop.run_until_complete()` 桥接，或者把入图操作放到后台线程/队列。

### 3.5 Agent 工具注册

**文件**：`src/agent/tools/registry.py` → `ToolRegistry`

新增一个 `search_knowledge_graph` 工具，注册到现有的 ToolRegistry：

```python
@tool(
    name="search_knowledge_graph",
    category="search",
    description="从知识图谱中检索股票、事件、板块、机构之间的关联关系和历史分析记忆。"
                "支持语义搜索、关键词搜索和图遍历。"
                "适用于：查询某只股票的历史分析结论、关联事件演化、板块关系链、机构动向等。",
)
def search_knowledge_graph(
    query: str,
    market: str = "cn",
    limit: int = 10,
) -> dict:
    ...
```

**文件位置**：建议放在 `src/agent/tools/graph_tools.py`，和现有的 `data_tools.py`、`search_tools.py` 平级。

### 3.6 自定义本体（Ontology）

Graphiti 支持通过 Pydantic 模型定义实体类型（prescribed ontology），让抽取更精准。

**新建文件**：`src/services/graphiti/ontology.py`

```python
from pydantic import BaseModel, Field

class Stock(BaseModel):
    """上市公司股票"""
    code: str = Field(description="股票代码，如 600519、AAPL")
    name: str = Field(description="股票名称")
    market: str = Field(description="市场：cn / hk / us")

class Sector(BaseModel):
    """行业板块"""
    name: str = Field(description="板块名称")
    level: str = Field(description="板块层级：industry / concept / theme")

class MarketEvent(BaseModel):
    """市场事件"""
    title: str = Field(description="事件标题")
    event_type: str = Field(description="事件类型：policy / earnings / macro / corporate / geopolitical")

class Institution(BaseModel):
    """机构"""
    name: str = Field(description="机构名称")
    institution_type: str = Field(description="类型：fund / broker / insurance / foreign / individual")

class AnalysisConclusion(BaseModel):
    """分析结论"""
    signal: str = Field(description="信号：buy / sell / hold / strong_buy / strong_sell")
    sentiment_score: int = Field(description="情绪评分 0-100")
    confidence: str = Field(description="置信度：high / medium / low")
```

这些类型传给 `Graphiti.add_episode()` 的 `entity_types` 参数，让 LLM 抽取时优先识别这些实体。

### 3.7 配置项

**文件**：`src/config.py` + `.env.example`

新增配置项：

```env
# === Graphiti 知识图谱 ===
GRAPHITI_ENABLED=false
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=
GRAPHITI_LLM_MODEL=          # 留空则复用 LITELLM_MODEL
GRAPHITI_EMBEDDING_MODEL=    # 留空则用 text-embedding-3-small
GRAPHITI_EMBEDDING_BASE_URL= # 可选：embedding 专用 OpenAI-compatible 地址
GRAPHITI_EMBEDDING_API_KEY=  # 可选：embedding 专用 API Key
GRAPHITI_GROUP_STRATEGY=market  # market（按市场分区）| user（按用户分区）| single（单图）
```

**文件**：`src/core/config_registry.py`

在配置注册表中新增 `graphiti` 分类，提供 UI 元数据。

### 3.8 Docker 部署

**文件**：`docker-compose.yml`（或 `docker/docker-compose.yml`）

新增 Neo4j 服务：

```yaml
services:
  neo4j:
    image: neo4j:5.26-community
    ports:
      - "7474:7474"   # HTTP
      - "7687:7687"   # Bolt
    environment:
      NEO4J_AUTH: neo4j/${NEO4J_PASSWORD:-changeme}
      NEO4J_PLUGINS: '["apoc"]'
    volumes:
      - neo4j_data:/data
    profiles:
      - graphiti       # 只在启用 graphiti profile 时启动

volumes:
  neo4j_data:
```

使用 `docker compose --profile graphiti up` 启动。

## 4. 文件变更清单

### 新增文件

| 文件 | 职责 |
|------|------|
| `src/services/graphiti/__init__.py` | 包入口 |
| `src/services/graphiti/graph_service.py` | Graphiti 客户端封装，单例管理 |
| `src/services/graphiti/litellm_client.py` | LLMClient adapter，桥接 LiteLLM |
| `src/services/graphiti/litellm_embedder.py` | EmbedderClient adapter，桥接 LiteLLM |
| `src/services/graphiti/ontology.py` | 自定义实体类型定义 |
| `src/agent/tools/graph_tools.py` | Agent 图谱检索工具 |

### 修改文件

| 文件 | 改动 |
|------|------|
| `src/core/pipeline.py` | `analyze_stock()` 和 `_analyze_with_agent()` 末尾追加入图调用 |
| `src/agent/tools/__init__.py` 或工具注册入口 | 注册 `search_knowledge_graph` 工具 |
| `src/config.py` | 新增 Graphiti 相关配置项 |
| `src/core/config_registry.py` | 新增 graphiti 配置分类和 UI 元数据 |
| `.env.example` | 新增 Graphiti / Neo4j 配置项 |
| `requirements.txt` | 新增 `-e ./graphiti`（本地源码引用） |
| `docker-compose.yml` 或 `docker/docker-compose.yml` | 新增 Neo4j 服务 |

### 不改动的文件

| 文件 | 原因 |
|------|------|
| `graphiti/` 目录下所有文件 | 作为外部依赖引用，不修改源码 |
| `src/agent/memory.py` | 现有 AgentMemory 保持不变，图谱是增量能力 |
| `src/storage.py` | SQLite 存储层不变，图谱是独立存储 |

## 5. 适配难点与风险

### 5.1 同步/异步桥接

**问题**：Pipeline 是同步的（`ThreadPoolExecutor`），Graphiti 全部是 async API。

**方案**：
- 方案 A：在 `GraphService` 内部用 `asyncio.run()` 包装（简单但有嵌套 event loop 风险）
- 方案 B：用 `concurrent.futures` 把 async 调用提交到独立的 event loop 线程（推荐）
- 方案 C：把入图操作放到后台队列，完全异步化（最干净但改动最大）

**建议**：先用方案 B，在 `GraphService.__init__` 中启动一个后台 event loop 线程，所有 async 调用通过 `asyncio.run_coroutine_threadsafe()` 提交。

### 5.2 LLM structured output 兼容性

**问题**：Graphiti 大量使用 Pydantic `response_model` 做 structured output。不是所有 LLM provider 都支持。

**方案**：
- Graphiti 的 `generate_response` 基类已经把 `response_model` 转成 JSON schema 追加到 prompt 末尾，不依赖 provider 原生 structured output
- 但 OpenAI client 的 `_create_structured_completion` 用了 `client.beta.chat.completions.parse()`，这是 OpenAI 专有 API
- LiteLLM adapter 应该走基类的 `_generate_response` 路径（不走 structured completion），让 Graphiti 基类自己处理 schema 注入

**风险**：非 OpenAI 模型的 JSON 输出稳定性可能不如 OpenAI。建议 Graphiti 专用模型优先配 OpenAI 或 Gemini。

### 5.3 写入延迟与成本

**问题**：每次 `add_episode` 会触发多次 LLM 调用（实体抽取、去重、关系抽取、summary 更新），成本和延迟不低。

**方案**：
- 入图操作必须异步，不阻塞分析主流程
- 可以用 `add_episode_bulk` 批量写入，减少 LLM 调用次数
- Graphiti 的 `small_model` 用于简单任务（去重、分类），配一个便宜的模型（如 `gpt-4.1-nano`）
- 初期只写分析结论入图（数据量小），新闻入图后续按需开启

### 5.4 Neo4j 运维

**问题**：新增了一个有状态服务（Neo4j），增加运维复杂度。

**方案**：
- 用 Docker profile 隔离，不启用 graphiti 时完全不启动 Neo4j
- Neo4j Community Edition 免费，够用
- 数据量不大（每天几十到几百个 episode），单机 Neo4j 足够
- 备份：定期 `neo4j-admin dump`

## 6. 实施阶段

> 当前 Graphiti 最小可用路径已经完成，下面原阶段清单保留为历史实施拆解。后续恢复消息面能力时，应优先执行本节 6.0 的新闻卡片第一版任务，而不是重新实现已经接入的 Graphiti MVP。

### 6.0 当前状态与新闻卡片第一版拆分

已完成的 Graphiti MVP：

- [x] Graphiti/Neo4j 配置、`.env.example` 和配置注册。
- [x] LiteLLM LLM/Embedding 适配器。
- [x] Graphiti 服务封装和开关控制。
- [x] 普通/Agent 分析结果入图。
- [x] Agent `search_knowledge_graph` 检索工具。
- [x] Docker Neo4j profile、`test_env.py --graph` 连通性检查和基础单元测试。

新闻卡片第一版当前状态：

- [x] 新增 `RawNewsEpisode`、`NewsSignalCard`、`news_signal_seed_links`、`news_signal_feedback`、`news_signal_outcomes` 关系型真源表。
- [x] 实现 NewsSignalCard 到 `EvidenceCard(dimension="news_event")` 的适配层，复用现有 EvidenceCard/EvidenceSignal 协议。
- [x] 复用 `candidate_experts_v2/resources/news_theme_daily/concept_mapping.json` 做产业链与公司映射，补齐 `mapped/ambiguous/industry_only/unmapped` 失败模式。
- [x] 新增 `/api/v1/news-signals` 列表、详情、重建、反馈、指标、EvidenceCard 适配和 outcome 刷新接口。
- [x] 新增 Web 独立“消息”页面，支持卡片列表、详情抽屉式详情区、反馈 overlay 和关键指标展示。
- [x] 财联社工具切换到 `orz.ai dailynews platform=cls`，新增 `get_xueqiu_hot_news` 雪球热榜工具，并接入新闻卡片重建。
- [x] 新增 `signal_layer=industry/company/macro` 三层分类，API 和页面支持按层级筛选；宏观层先用非农、逆回购、CPI/PPI/PMI、利率、流动性等关键词规则。
- [x] 新增 `get_macro_finance_news` 宏观财经工具，默认抓取 `sina_finance`、`eastmoney` 并按宏观关键词过滤；当热榜覆盖不足时追加 `SearchService.search_general_news()` 宏观关键词 fallback，新闻卡片重建和主题催化席白名单已接入。
- [x] 2026-07-04 真实 smoke：`get_eastmoney_cjzc_daily`、`get_cls_telegraph_news`、`get_xueqiu_hot_news`、`get_macro_finance_news` 均返回可用；临时库 rebuild 生成卡片无错误。
- [x] 新闻卡片保存后默认 best-effort 写入 Graphiti episode；新增 `/api/v1/news-signals/graph-sync` 可从关系型真源补同步 pending/failed 卡片。
- [x] 新增 `news_signal_edges` 关系型真源表，支持 `typed_relation`、`event_clue` 和显式 `semantic_similarity` 三类边。
- [x] 新增 `/api/v1/news-signals/edges`、`/api/v1/news-signals/edges/rebuild` 和 `/api/v1/news-signals/{card_id}/graph`，用于页面读取边、重建边和展示单卡局部图。
- [x] Graphiti 同步会将 `news_signal_edges` best-effort 投影到 Neo4j，生成 `NEWS_SIGNAL_TYPED_RELATION`、`NEWS_SIGNAL_EVENT_CLUE`、`NEWS_SIGNAL_SEMANTIC_SIMILARITY` 显式关系。
- [ ] 让新闻卡片作为 `theme_catalyst_desk` / seed pool 的证据输入，不新增独立新闻选股直通链路。
- [ ] 将新闻卡片自动链接到 seed item，并让 `SelectionSeedPoolEvaluation` 结果稳定回流到 `news_signal_outcomes`。
- [ ] 实现 `daily_run.sh` 盘后批处理调度、财联社电报定时增量、预算配置和限流开关。
- [ ] 完整 Graphiti outbox worker、定时 repair 和幂等区间 rebuild。
- [ ] embedding 阈值 per-model 校准、跨模型重算审计和同事件归并；当前已支持显式 semantic edge rebuild，但不默认阻塞卡片生成。

### 6.1 2026-07-04 当前进度记录

本地 Graphiti / Ollama / Neo4j 验证状态：

- `ollama list` 已安装 `mxbai-embed-large:latest` 和 `qwen3:8b`。
- `.env` 当前使用 `GRAPHITI_EMBEDDING_MODEL=ollama/mxbai-embed-large`，`test_env.py --graph` 已验证 embedding 请求成功。
- Neo4j 通过 Docker profile `graphiti` 启动，`test_env.py --graph` 已验证 Neo4j 连通。
- 2026-07-04 本地库存在 52 张 active 新闻卡片。
- 已对 2026-07-04 卡片重建 embedding 语义边：共 1118 条边，其中 `typed_relation=461`、`event_clue=271`、`semantic_similarity=386`。
- 已将 52 个 `NewsSignalCard`、88 个 `NewsSignalTarget` 和 1118 条新闻关系投影到 Neo4j。
- Neo4j Browser 可直接查询 `NEWS_SIGNAL_TYPED_RELATION`、`NEWS_SIGNAL_EVENT_CLUE`、`NEWS_SIGNAL_SEMANTIC_SIMILARITY` 三类关系。
- Graphiti Core episode 同步使用本地 `qwen3:8b` 抽实体/关系时明显偏慢；小批量 3 张卡超过 90 秒未返回，当前不应把这条慢路径作为页面“同步图谱”的主依赖。

当前可用闭环：

1. 新闻工具抓取原始消息。
2. 关系型真源生成 `RawNewsEpisode` 和 `NewsSignalCard`。
3. 规则 + embedding 生成 `news_signal_edges`。
4. Web“消息面”页面展示卡片和单卡事件线索。
5. `news_signal_edges` 可重放到 Neo4j，Neo4j Browser 可查看显式节点和关系。

当前主要短板：

- 入库质量还偏“能入库”，不够“高质量入库”：原始新闻摘要、正文规范化、重要性过滤、宏观/产业/公司层判定和公司映射仍需要更严格的质量门。
- 边质量还偏“广覆盖”，不够“高信噪比”：同主题、同公司、语义相似边数量较多，需要区分强事件链、弱主题簇和噪声边。
- Graphiti Core LLM 抽取链路较慢，短期应把确定性边表 + Neo4j 显式投影作为主图谱路径，Graphiti episode 抽取作为异步增强。

### 6.2 下一阶段 TODO：提高入库质量和边质量

下一阶段不优先扩功能面，优先提高“进来的新闻是否值得进来”和“连出来的边是否可靠”。

入库质量 TODO：

- [x] 建立 `RawNewsEpisode` 入库质量评分：来源可信度、发布时间完整性、标题/正文可读性、是否重复、是否只有广告/盘中宝营销文案、是否有可验证实体。
- [x] 对新闻正文做规范化：清理 JSON 包装、HTML/多余空白、重复标题、截断异常、来源模板话术，生成更适合阅读和 embedding 的 `normalized_content` 或等价投影字段。
- 增加重要性过滤：宏观层优先保留非农、CPI/PPI/PMI、央行公开市场、MLF/LPR、降准降息、汇率/利率等；产业层优先保留价格、供需、政策、订单、技术路线、制裁/禁令、产能变化等。
- 改进 `signal_layer` 判定：避免人民银行行政处罚等普通公司新闻被误判为宏观流动性；宏观消息避免被概念词典错误映射到公司。
- 改进公司映射质量：显式股票 > 权威来源关联 > 主题词典映射；低置信度或同名歧义时只保留产业级证据，不注入具体股票。
- [x] 增加第一版低质量处理：低质量原始消息保留审计，但生成卡片时标记 `low_quality` 并显著降权，不进入 active 图谱主路径。
- [x] Web 卡片详情展示入库质量：展示来源质量分、质量等级、质量 flags 和规范化正文预览。
- 增加人工反馈读路径：`wrong/noisy/duplicate/remove_company` 不只降权，还要进入后续入库过滤、映射黑名单或规则修正。
- 建立入库指标：来源成功率、去重率、无正文率、宏观命中率、公司映射命中率、歧义率、被人工标噪比例。

### 6.3 2026-07-06 P1 入库质量门实施记录

已落地：

- `RawNewsEpisode` 新增 `normalized_content`、`quality_score`、`quality_grade`、`quality_flags_json`，并提供 SQLite 幂等迁移。
- 新闻入库时先做确定性规范化：解析 JSON 文本、去 HTML、去重复片段、合并标题/摘要/正文候选文本。
- 新闻入库时计算质量分：综合来源、发布时间、文本长度、正文可读性、营销话术、信号词、实体/股票/主题和 source errors。
- 低质量原始新闻不删除，`raw_news_episodes.status` 标记 `low_quality`；对应卡片 `status=low_quality`，`signal_score` 被压低，`evidence_grade` 降为 `speculative`。
- 卡片 `diagnostics.raw_quality` 和 `diagnostics.quality_gate` 记录质量分、等级、flags、阈值和入库门结果。
- Web“消息面”详情区新增“入库质量”面板，展示每个原始来源的质量分、等级、flags 和规范化正文预览。

仍需继续提高：

- 当前质量评分是规则版，缺少按来源/日期的统计分布校准。
- 当前低质量只影响分数和状态，还没有沉淀到反馈驱动的过滤规则、映射黑名单或来源权重自适应。
- 当前 `quality_flags` 已进入边质量评分第一版，但还没有结合人工反馈和回测结果做动态校准。
- 当前未新增入库质量聚合指标 API；后续应在 `/api/v1/news-signals/metrics` 或独立 metrics 中展示质量分布。

边质量 TODO：

- 将边分层展示和使用：`typed_relation` 是实体事实边，`event_clue` 是规则事件线索，`semantic_similarity` 是弱语义相似；Agent 不得把弱语义边直接当成因果。
- [x] 为边增加质量评分第一版：实体重叠、主题重叠、时间距离、来源质量、映射置信度、embedding 相似度共同决定 `edge_quality`、`quality_grade`、`quality_flags`。
- [x] 限制语义边密度第一版：语义边先过最低质量门，再按每张卡 top-k 保留，避免小样本日期生成过密弱连接网。
- 建立 per-model embedding 阈值校准：`mxbai-embed-large` 的 `0.76` 只作为当前实验阈值，后续需要用样本分布和人工反馈重新校准。
- 区分“同一事件”“同一主题”“同一产业链传导”：同一事件才可强绑定；同主题只做弱边；产业链传导需要可解释路径。
- [x] 增加边审计展示第一版：Web 单卡事件线索展示强/中/弱边、质量分、生成理由和质量 flags；Neo4j 显式关系同步 `edge_quality` 与 `quality_grade`。
- 建立边质量指标：平均每卡边数、强/弱边比例、人工否定率、语义边命中率、同事件误连率、孤立卡片比例。

### 6.4 2026-07-06 P2-1 边质量收敛实施记录

已落地：

- `NewsSignalEdge` 新增 `edge_quality`、`quality_grade`、`quality_flags_json`，并提供 SQLite 幂等迁移。
- 边生成统一通过 `_edge_payload` 计算质量：`typed_relation` 偏实体事实边，`event_clue` 偏规则事件线索，`semantic_similarity` 默认带 `semantic_not_causal` flag，防止被误读成因果。
- 语义边重建先按 `edge_quality >= 45` 过滤，再按每张卡 `top_k=4` 控制密度，避免 embedding 相似度把同日新闻连成高度密集弱网。
- 规则事件边补充共同公司、共同主/次主题、信号层级、时间距离、来源质量分等 evidence，页面可以解释“为什么相连”。
- Web“事件线索”展示强/中/弱边、质量分、权重和质量 flags；Neo4j 显式关系同步 `edge_quality`、`quality_grade`、`quality_flags_json`。
- `/api/v1/news-signals/edges`、`/api/v1/news-signals/{card_id}/graph` 和重建结果返回边质量分布摘要。

仍需继续提高：

- 质量评分仍是规则版，尚未使用人工 `wrong/noisy/duplicate` 反馈和 `news_signal_outcomes` 做权重校准。
- 语义阈值仍绑定当前实验值 `0.76`，尚未按 embedding 模型做 per-model 分布校准。
- Agent 检索和选股链路尚未读取 `quality_grade` 来约束弱语义边的使用语义。
- 还未新增全局边质量指标面板，例如平均每卡边数、强弱边比例、孤立卡片比例和人工否定率。

### 6.5 2026-07-07 P2-2 消息面有效链路实施记录

背景反馈：

- 参考 `docs/report/消息面传导路径报告.md` 的优点是“一环扣一环”：事件催化、客户/技术/供应链线索、公司落点和分数拆解都很凝练。
- 当前消息卡片的问题是“全而散”：`news_theme_daily` 把整篇财经早餐作为每个主题卡片的 raw content，导致单个“存储器”卡片展示 ST 新规、黄金、原油等无关内容；传导路径也只是“主题词典映射”，缺少事件链路。

已落地：

- `news_theme_daily` 从“整篇财经早餐一个 raw episode”改为“每个主题一个 raw episode”；每张卡片的入库质量只展示对应主题的 evidence、关键词和相关板块，避免聚合源刷屏。
- `transmission_paths` 新增事件链字段：`event_category`、`event_score`、`chain_steps`、`score_breakdown`、`evidence_snippets`、`conclusion`。
- 事件类型第一版覆盖价格/供需、大客户/订单、技术突破、供应链/替代、政策/宏观、业绩验证、产能变化等消息面主线。
- Web“传导路径”改为展示 `[事件类型] 事件 -> 分数`、传导机制、映射落点和结论，优先输出有效信息；入库质量中的正文预览强制截断，避免老数据或聚合源残留时刷屏。

仍需继续提高：

- 事件类型和分数仍是规则版，尚未接入搜索引擎二次核验和来源可信度打分。
- “国外供应链”和“国产替代”两条主线需要沉淀成专门规则：海外大客户/海外限制/海外扩产对应国内替代、二供、材料设备、模组封测等不同落点。
- 公司映射仍依赖显式股票和主题词典，后续要补“客户关系/供应链认证/产品相似性”的证据来源，避免仅因同主题而映射到公司。

### 6.6 后续技术路线：减少硬关键词，转向事件抽取 + 证据核验

判断：

- 开放消息场景里，硬关键词匹配只能作为低成本召回、兜底和护栏，不应作为主判断方法。
- 当前第一版大量依赖关键词和规则，优点是稳定、可测试、可解释；缺点是容易漏掉隐含逻辑，也容易把“出现同一个词”误判成真实传导。
- 后续目标不是完全删除关键词，而是把关键词从“主推理引擎”降级为“辅助信号”。

推荐主链路：

1. 新闻切分：聚合新闻先按主题、段落、事件句切成更小 episode，避免整篇文章污染单张卡片。
2. 语义召回：使用 hybrid retrieval，结合 BM25/关键词、embedding、图谱邻居召回候选证据。
3. 结构化事件抽取：用轻量 LLM 或专用抽取器输出 `event_type`、`trigger`、`subject`、`object`、`time`、`metric/value`、`direction`、`evidence_sentence`、`source_url`。
4. 实体链接与消歧：将新闻实体规范化到公司、产业、海外公司、产品、材料、设备、客户、地区等图谱节点；不确定时只保留产业级或事件级证据。
5. 搜索核验：对高影响事件用搜索引擎或权威源二次核验，确认事实是否真实、是否最新、是否有多源交叉验证。
6. 图谱建边：根据结构化事件和实体链接生成关系边，区分事实边、事件线索边、语义相似边和推理边。
7. 传导路径生成：基于事件事实、供应链关系、公司暴露度和证据强度生成“一环扣一环”的链路，而不是模板化主题映射。
8. 反馈校准：人工 `useful/wrong/noisy/duplicate` 和 T+1/T+N 结果回流，校准事件类型、实体链接、边质量和公司映射权重。

关键词保留边界：

- 可保留：宏观事件初筛、低成本召回、危险词/否定词护栏、fallback、明确枚举型事件。
- 应减少：事件类型最终判定、公司受益判断、供应链传导、国产替代推理、强弱边质量判断。
- 不允许：仅凭关键词命中就把公司注入 seed pool；仅凭主题相似就声明因果受益。

下一步实现建议：

- [x] 新增 `NewsExtractedEvent` 结构，作为 `RawNewsEpisode -> NewsSignalCard` 之间的事件层。
- [x] 先对 `news_theme_daily`、财联社电报、宏观新闻做事件抽取 JSON schema，不急着替换全部逻辑。
- [x] 接入可选轻量 LLM JSON 抽取器，默认规则兜底，配置后可用 `deepseek/deepseek-v4-flash` 抽取事件事实。
- 对“国外供应链”和“国产替代”两条主线建立证据模板：海外限制/海外提价/海外大客户扩产/海外公司订单变化 -> 国内可替代环节 -> 公司产品和客户证据 -> 置信度。
- [x] Web 页面展示事件事实和推理分层：事实、推理、待核验，不把推理伪装成事实。

### 6.7 2026-07-08 P2-3 事件抽取层闭环实施记录

已落地：

- 新增 `NewsExtractedEvent` 关系型真源表，字段覆盖 `event_type`、`trigger`、`subject`、`object`、`event_time`、`metric_value`、`direction`、`evidence_sentence`、`source_url`、`verification_status`、`entity_links`、`confidence` 和 `extractor`。
- SQLite 幂等迁移创建 `news_extracted_events` 及索引，支持老库直接升级。
- `NewsSignalService.rebuild()` 在 raw 入库后、card 入库前保存事件层，并返回 `events_upserted`。
- 卡片生成先产出 `extracted_events`，再让 `transmission_paths` 引用事件 `event_id`、核验状态和置信度；关键词规则被标记为 `extractor=rule_fallback`，定位为 fallback，不再伪装成主推理引擎。
- API 详情返回 `extracted_events`，Web“消息面”详情新增“事件事实”区，先展示事件事实、核验状态、置信度、证据句和实体链接，再展示传导路径。
- 测试覆盖 `RawNewsEpisode -> NewsExtractedEvent -> NewsSignalCard -> Detail API/Web` 闭环。

仍需继续提高：

- 当前事件抽取仍是 deterministic fallback；后续要接轻量 LLM JSON schema，把规则降级成异常兜底。
- `verification_status` 当前主要是 source-level 核验；后续要接搜索引擎和权威源，区分 `source_only`、`multi_source_verified`、`conflicting`、`stale`。
- 实体链接仍使用主题、来源 subject 和 company_impacts；后续要接公司别名、海外实体、产品、客户、材料设备和供应链关系库。

验收标准：

- Web 页面里每张高分卡片的主要边能解释“为什么相连”，不只显示相似度。
- Neo4j Browser 中的新闻图谱不再是高度密集的弱连接网，而是能看出少数清晰事件线索簇。
- 人工标记 `noisy/wrong/duplicate` 后，后续重建能减少同类低质量入库或低质量连边。
- Graphiti Core 慢同步不影响页面重建、边重建和 Neo4j 显式关系投影。

### 6.8 2026-07-08 P2-4 轻量 LLM 事件抽取实施记录

已落地：

- 新增新闻事件抽取配置：`NEWS_EVENT_EXTRACTOR_MODE=fallback|llm|auto`、`NEWS_EVENT_EXTRACTOR_MODEL`、`NEWS_EVENT_EXTRACTOR_TIMEOUT_SECONDS`、`NEWS_EVENT_EXTRACTOR_MAX_TOKENS`、`NEWS_EVENT_EXTRACTOR_TEMPERATURE`。
- 默认 `fallback`，不触发外部 LLM 请求；开启 `llm` 后优先通过 LiteLLM JSON object 模式抽取 `NewsExtractedEvent`，失败自动降级到规则事件。
- 未显式指定模型且存在 `DEEPSEEK_API_KEY` 时，事件抽取器默认选择 `deepseek/deepseek-v4-flash`；也复用现有 `LLM_CHANNELS` / `LITELLM_CONFIG` Router 和 `extra_litellm_params()`。
- LLM prompt 只允许抽事件事实：`event_type`、`trigger`、`subject`、`object`、`metric_value`、`evidence_sentence`、`entity_links`、`confidence`、`verification_status`，禁止生成股票推荐或脑补未出现公司。
- LLM 成功时事件 `extractor=llm_json:<model>`，并把模型、token usage、fallback event id 写入 diagnostics；失败/关闭时规则事件保留 `diagnostics.llm_extraction`，用于审计为什么没有使用 LLM。
- 单元测试覆盖 LLM JSON 成功、LLM 异常降级和默认规则路径，确保消息卡片重建不会被单个模型调用拖垮。

仍需继续提高：

- 当前 LLM 只替换事件事实抽取，不负责搜索核验；高影响事件仍需接入搜索引擎或权威源二次确认。
- LLM 输出的实体链接还没有进入公司映射黑名单/消歧表；后续应把低置信度公司降级为产业级证据。
- 事件类型仍是一组固定枚举，后续要为“国外供应链”和“国产替代”补专门模板和验证字段。

### 阶段 0：基础设施

- [x] `docker/docker-compose.yml` 加 Neo4j 服务（profile: graphiti），`start_all.sh` / `stop_all.sh` 支持 `START_NEO4J` / `STOP_NEO4J` 控制
- [x] `requirements.txt` 加 `-e ./graphiti`
- [x] `.env.example` 加 Graphiti / Neo4j 配置项
- [x] `src/config.py` 加配置读取
- [x] `src/core/config_registry.py` 加 UI 元数据
- [x] `test_env.py --graph` 支持 Neo4j 与 Graphiti embedding 检查

### 阶段 1：LLM / Embedding 适配

- [x] `src/services/graphiti/litellm_client.py`：实现 `LLMClient`，桥接 LiteLLM
- [x] `src/services/graphiti/litellm_embedder.py`：实现 `EmbedderClient`，桥接 LiteLLM
- [x] 单元测试：`tests/test_graphiti_service.py` 覆盖 wrapper、序列化、初始化与离线检索路径
- [ ] 在线验证：真实 Neo4j + LLM / Embedding 配置下执行 `python test_env.py --graph`

### 阶段 2：GraphService 封装

- [x] `src/services/graphiti/graph_service.py`：客户端管理、同步/异步桥接、开关控制
- [x] `src/services/graphiti/ontology.py`：自定义实体类型
- [x] 单元测试：禁用态、Neo4j 不可达、ingest / search 初始化、ontology 校验
- [ ] 在线集成测试：真实 `add_episode` + `search` 端到端可用性

### 阶段 3：Pipeline 接入

- [x] `src/core/pipeline.py`：`analyze_stock()` 保存分析历史后追加 `_ingest_analysis_to_graphiti()`
- [x] `src/core/pipeline.py`：Agent 分析保存历史后追加入图
- [x] `api/v1/endpoints/agent.py`：Agent Trace finalize 后写入 Graphiti
- [x] 单元测试：`tests/test_agent_models_api.py::test_trace_finalize_ingests_graphiti_episode`
- [ ] 在线验证：跑一次完整分析，确认 Neo4j 中有正确实体和关系

### 阶段 4：Agent 工具

- [x] `src/agent/tools/graph_tools.py`：实现 `search_knowledge_graph` 工具
- [x] `src/agent/factory.py` 注册到 ToolRegistry
- [x] 工具 schema 纳入 Agent registry 测试
- [ ] 在线验证：Agent 分析时能调用图谱检索并获得有意义的上下文

### 阶段 5：新闻入图（可选）

- [ ] `src/core/pipeline.py`：情报搜索后批量写入图谱
- [ ] 验证：同一事件的多篇新闻被合并为一个事件节点

### 阶段 6：选股链路增强（可选）

- [ ] 选股 Prompt 1（候选发现）接入图谱查询
- [ ] 选股 Prompt 5（反方审查）接入历史分析记录
- [ ] 验证：选股结论引用了图谱中的关联证据

### 阶段 7：用户画像记忆（可选）

- [ ] Agent 对话写入图谱（按用户 group_id 隔离）
- [ ] 对话时先查用户 context graph 注入个性化上下文
- [ ] 验证：Agent 能记住用户的投资偏好变化

## 7. 最小可用路径

**阶段 0 → 1 → 2 → 3 → 4** 是最小可用路径。

完成后的效果：
- 每次分析结果自动写入知识图谱
- Agent 分析时可以调用 `search_knowledge_graph` 工具
- Agent 能回答"贵州茅台最近一个月的分析结论演变"、"半导体板块关联了哪些事件"等跨时间问题

阶段 5-7 根据实际使用体验决定优先级。
