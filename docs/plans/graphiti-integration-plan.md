# Graphiti 时序知识图谱集成计划

> 实施状态：最小可用路径已接入。当前实现包含 Graphiti/Neo4j 配置、LiteLLM LLM/Embedding 适配器、Graphiti 服务封装、普通/Agent 分析结果入图、Agent `search_knowledge_graph` 检索工具、Docker Neo4j profile、`test_env.py --graph` 连通性检查和基础单元测试。新闻批量入图、选股 Prompt 直接注入图谱证据、用户画像图谱仍按后续阶段推进。

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

### 阶段 0：基础设施

- [ ] `docker-compose.yml` 加 Neo4j 服务（profile: graphiti）
- [ ] `requirements.txt` 加 `-e ./graphiti`
- [ ] `.env.example` 加 Graphiti / Neo4j 配置项
- [ ] `src/config.py` 加配置读取
- [ ] `src/core/config_registry.py` 加 UI 元数据

### 阶段 1：LLM / Embedding 适配

- [ ] `src/services/graphiti/litellm_client.py`：实现 `LLMClient`，桥接 LiteLLM
- [ ] `src/services/graphiti/litellm_embedder.py`：实现 `EmbedderClient`，桥接 LiteLLM
- [ ] 单元测试：验证 adapter 能正确调用 LiteLLM 并返回 Graphiti 期望的格式

### 阶段 2：GraphService 封装

- [ ] `src/services/graphiti/graph_service.py`：客户端管理、同步/异步桥接、开关控制
- [ ] `src/services/graphiti/ontology.py`：自定义实体类型
- [ ] 集成测试：验证 `add_episode` + `search` 端到端可用

### 阶段 3：Pipeline 接入

- [ ] `src/core/pipeline.py`：`analyze_stock()` 末尾追加 `_ingest_to_graph()`
- [ ] `src/core/pipeline.py`：`_analyze_with_agent()` 末尾追加入图
- [ ] 验证：跑一次完整分析，确认 Neo4j 中有正确的实体和关系

### 阶段 4：Agent 工具

- [ ] `src/agent/tools/graph_tools.py`：实现 `search_knowledge_graph` 工具
- [ ] 注册到 ToolRegistry
- [ ] 验证：Agent 分析时能调用图谱检索并获得有意义的上下文

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
