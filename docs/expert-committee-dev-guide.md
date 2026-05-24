# 新增 LLM 专家维度：开发者指南

基于 `CapitalFlowExpert`（资金面专家）的实现经验，本文档描述如何为 `llm_expert_committee` 模式新增一个专家维度（如情绪面、基本面、技术面）。

---

## 整体架构

```
stock_selection.py
  └─ _run_candidate_discovery_tool()
       └─ run_committee_discovery()          ← 入口（committee.py）
            ├─ _build_seed_pool()            ← 确定性种子池（4路来源，无LLM）
            └─ CapitalFlowExpert.run(seeds)  ← LLM 专家（此处扩展）
                 └─ BaseExpert 有界工具调用循环
```

**现有文件布局**：

```
src/agent/candidate_experts_v2/
├── __init__.py                  # 只导出 schemas
├── schemas.py                   # SeedItem / ExpertCandidateV2 / ExpertPacketV2
├── committee.py                 # run_committee_discovery 入口、_build_seed_pool
├── experts/
│   ├── base.py                  # BaseExpert：有界工具调用循环（不动）
│   └── capital_flow.py          # CapitalFlowExpert：资金面具体实现
├── prompts/
│   ├── capital.py               # 资金面 prompt 模板
│   └── _renderer.py             # render_manifest_block 通用渲染
└── tools_manifest/
    └── capital.yaml             # 资金面工具语义清单
```

**新增一个专家维度需要创建 4 个文件，修改 2 个文件**（见下文）。

---

## 资金面专家输出数量（参考基准）

`CapitalFlowExpert` 的 prompt 约束分两步：

1. **初筛**：从种子池中挑选 **3-6 只**标的，调用工具核实资金面数据
2. **最终输出**：经工具核实后，输出 **2-5 只**候选（`prompts/capital.py` line 69）；若所有标的均无资金面支撑，返回空列表

> 新专家可自行调整这个数量约束，写在对应 prompt 模板里即可。建议参考上面的两步框架：先宽（3-6 只调研），再收（2-5 只输出），避免 LLM 一步压得太死。

---

## 核心契约

### SeedItem（输入）

```python
class SeedItem(BaseModel):
    code: str          # 股票代码，如 "600519"
    name: str = ""     # 股票名称
    market: str = "cn"
    source: SeedSource # 来源：user_watchlist / limit_up_pool / hot_rank / alphasift / sequoia
    hint: str = ""     # 给 LLM 的上下文提示，如 "涨停,连板=3"
```

### ExpertCandidateV2（输出）

```python
class ExpertCandidateV2(BaseModel):
    code: str
    name: str = ""
    score: float       # 0–100，维度得分（资金强度 / 情绪热度 等）
    confidence: float  # 0–1，专家信心
    stance: Literal["support", "watch", "neutral", "oppose", "invalid"]
    reason: str        # 必须引用工具证据，不能主观推断
    evidence: List[EvidenceItem]  # 工具调用结果（tool / summary / metrics）
    risks: List[RiskNote]         # 风险注释
    valid_until: str              # 如 "next_trading_day"
```

### ExpertPacketV2（expert 返回值）

```python
class ExpertPacketV2(BaseModel):
    expert: str        # e.g. "sentiment_expert"
    dimension: str     # e.g. "sentiment"
    status: Literal["ok", "partial", "empty", "failed", "timeout", "unavailable"]
    candidates: List[ExpertCandidateV2]
    rejected: List[Dict]
    tool_calls: List[Dict]   # 工具调用 trace
    diagnostics: List[Dict]  # 循环问题 trace（如 tool_not_whitelisted）
    errors: List[str]
    elapsed_ms: int
    cache_hit: bool
```

---

## 实施步骤：以「情绪面专家」为例

### Step 1：定义工具白名单（`experts/{dimension}.py`）

新建 `src/agent/candidate_experts_v2/experts/sentiment.py`：

```python
from src.agent.candidate_experts_v2.experts.base import BaseExpert
from src.agent.candidate_experts_v2.prompts.sentiment import build_sentiment_system_prompt
import yaml, pathlib, logging

logger = logging.getLogger(__name__)

SENTIMENT_TOOLS = (
    "get_tushare_hot_rank",
    "get_stockapi_popularity_rank",
    "search_comprehensive_intel",
    # 仅与情绪面相关的工具；不要贪大
)

def _load_manifest():
    path = pathlib.Path(__file__).parent.parent / "tools_manifest" / "sentiment.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)

class SentimentExpert(BaseExpert):
    def __init__(self, *, tool_registry, tool_decls, llm, prompt_variables=None,
                 max_llm_rounds=5, max_tool_calls=10):
        manifest = _load_manifest()
        system_prompt = build_sentiment_system_prompt(
            manifest=manifest, variables=prompt_variables or {}
        )
        super().__init__(
            allowed_tools=set(SENTIMENT_TOOLS),
            tool_registry=tool_registry,
            # 只传白名单工具的声明，防止 LLM 看到无关工具
            tool_decls=[d for d in tool_decls if d["function"]["name"] in SENTIMENT_TOOLS],
            llm=llm,
            system_prompt=system_prompt,
            max_llm_rounds=max_llm_rounds,
            max_tool_calls=max_tool_calls,
            freshness="intraday",
        )
        self.dimension = "sentiment"
        self.expert_name = "sentiment_expert"
```

### Step 2：写工具清单 YAML（`tools_manifest/{dimension}.yaml`）

新建 `src/agent/candidate_experts_v2/tools_manifest/sentiment.yaml`：

```yaml
tools:
  - name: get_tushare_hot_rank
    description: 东方财富热榜，反映散户情绪热度
    when_to_use: 排名 TOP20 内的股票通常有情绪共鸣，用于发现或验证热点
    typical_args:
      source: "ths"
      trade_date: "{today}"
      limit: 20
    combo_hints:
      - 配合 get_stockapi_popularity_rank 双源交叉验证
    failure_modes:
      - 非交易日返回空

  - name: get_stockapi_popularity_rank
    description: 第三方人气榜，补充东财数据盲区
    when_to_use: 与热榜交叉验证，两榜同时出现代表情绪共鸣
    typical_args:
      limit: 20
    combo_hints:
      - 两榜均在前20且资金同向流入为最强信号
    failure_modes:
      - 网络超时需降级

  - name: search_comprehensive_intel
    description: 情报搜索，获取近期新闻/公告/机构分析
    when_to_use: 验证热度是否有事件驱动（公告/政策/行业催化）
    typical_args:
      stock_code: "{code}"
      stock_name: "{name}"
    combo_hints:
      - 有事件驱动的热度信号更可靠
    failure_modes:
      - 搜索无结果时不代表无新闻，仅代表搜索未覆盖
```

清单的作用：把每个工具的语义注入 prompt，让 LLM 知道「什么时候用什么工具」。

### Step 3：写 prompt 模板（`prompts/{dimension}.py`）

新建 `src/agent/candidate_experts_v2/prompts/sentiment.py`：

```python
SENTIMENT_SYSTEM_PROMPT_TEMPLATE = """
你是 A股市场情绪面专家（sentiment_expert），负责从情绪热度维度筛选候选股票。

## 工作模式
- 种子池非空：从种子池中筛选 3-6 支情绪共鸣最强的股票
- 种子池为空：主动调用工具从热榜/情报搜索中发现候选

## 可用工具
{tool_manifest_block}

## 硬性规则
- 每支候选至少有 1 条工具支持的 evidence
- 禁止无根据猜测；reason 必须引用工具返回的数据
- 返回 2-5 支候选；不满条件可返回空列表
- 只输出 JSON，不加任何其他文字

## 输出格式（JSON）
{{
  "data_quality": {{"freshness": "intraday", "as_of": "", "warnings": []}},
  "candidates": [
    {{
      "code": "600519",
      "name": "贵州茅台",
      "market": "cn",
      "score": 75,
      "confidence": 0.7,
      "stance": "support",
      "reason": "热榜排名第3（rank=3），人气榜同时进入前20，双源共振",
      "evidence": [
        {{"tool": "get_tushare_hot_rank", "summary": "热榜第3", "metrics": {{"rank": 3}}}}
      ],
      "risks": [{{"type": "emotion_fading", "summary": "情绪退潮后无基本面支撑"}}],
      "valid_until": "next_trading_day"
    }}
  ],
  "rejected": []
}}
""".strip()

def build_sentiment_system_prompt(manifest, variables=None):
    from src.agent.candidate_experts_v2.prompts._renderer import render_manifest_block
    block = render_manifest_block(manifest, variables=variables or {})
    return SENTIMENT_SYSTEM_PROMPT_TEMPLATE.format(tool_manifest_block=block)
```

### Step 4：在 committee.py 中注册新专家

在 `run_committee_discovery()` 里，紧接资金面专家调用段之后添加（约 line 427 之后）：

```python
# 在 try 块外，检查超时预算
if elapsed < overall_timeout_s:
    try:
        from src.agent.candidate_experts_v2.experts.sentiment import SentimentExpert
        sentiment_expert = SentimentExpert(
            tool_registry=committee_tool_registry,
            tool_decls=list(tool_decls or []),
            llm=llm_callable,
            prompt_variables=prompt_variables,
        )
        sentiment_packet = sentiment_expert.run(seeds, market=market_value, use_cache=True)
        _merge_expert_evidence(payload, sentiment_packet, dimension="sentiment")
    except Exception as exc:
        logger.warning("sentiment_expert failed: %s", exc)
        payload.setdefault("discovery_steps", []).append({
            "source": "llm_expert_committee",
            "dimension": "sentiment",
            "status": "failed",
            "error": str(exc),
        })
```

> `_merge_expert_evidence` 是通用合并函数（参考 `_merge_capital_evidence` 仿写，或者先直接仿写一个 `_merge_sentiment_evidence`）。

---

## 注意事项（坑）

### 1. LLM adapter 必须经过 `_coerce_llm_callable` 适配

外部 LLM adapter 的 `.chat()` 返回格式不一定是内部的 `LLMTurn`：

```python
# 正确（committee.py 里已有）
llm_callable = _coerce_llm_callable(llm_adapter)
SentimentExpert(llm=llm_callable, ...)

# 错误：直接传原始 adapter
SentimentExpert(llm=llm_adapter, ...)  # ← 可能 crash
```

### 2. 工具注册用 `committee_tool_registry`（dict），不是原始 `tool_registry`

- 原始 `tool_registry`：有 `.execute()` 方法，用于 `_build_seed_pool`（种子池构建）
- `BaseExpert` 内部：用 `registry.get(name)` 取 callable，要求 dict 形式

```python
# committee.py 里已有这个转换
committee_tool_registry = {
    name: (lambda n: lambda **kw: tool_registry.execute(n, **kw))(name)
    for name in tool_registry.list_names()
}
# 新专家传 committee_tool_registry，不传 tool_registry
```

### 3. `tool_decls` 必须在专家内部按白名单过滤

```python
# 必须做过滤，否则 LLM 看到全量工具但执行时被白名单拒绝（LLM 会困惑）
tool_decls=[d for d in tool_decls if d["function"]["name"] in SENTIMENT_TOOLS]
```

### 4. 超时预算是共享的，每个专家调用前检查

```python
elapsed = time.time() - start_time
if elapsed >= overall_timeout_s:
    logger.warning("sentiment_expert skipped: budget exhausted")
else:
    # 调用专家
```

### 5. `partial` 状态是正常的，不是错误

`status="partial"` 表示 LLM 没有调用工具就给出了候选（设计行为）。下游对 `partial` 和 `ok` 处理相同，不要报错。

### 6. 调试时用 `use_cache=False`

缓存 key 基于 seed_hash，种子没变就会命中缓存：

```python
expert.run(seeds, market="cn", use_cache=False)  # 强制刷新
```

### 7. 测试桩用 `tool_registry={}`

`CapitalFlowExpert` 对 `tool_registry={}` 做了防御处理（跳过 registry validation）。新专家仿照：

```python
# 在 __init__ 里
if tool_registry:  # 空 dict 时跳过校验，方便测试
    # 校验 manifest 中的工具在 registry 中存在
    ...
```

### 8. 合并逻辑：区分「已知种子」和「LLM 新发现」

`_merge_capital_evidence` 的策略：
- LLM 输出的 code **在种子池里** → 挂 `llm_expert_evidence.{dimension}` 字段
- LLM 输出的 code **不在种子池里** → 追加为新候选，`source="llm_{dimension}_expert"`

不要把新专家的结果替换掉已有维度的结果，要追加/合并。

---

## 文件清单汇总

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `experts/{dimension}.py` | 专家类，继承 BaseExpert |
| 新建 | `tools_manifest/{dimension}.yaml` | 工具语义清单（工具的 when_to_use / combo_hints / failure_modes） |
| 新建 | `prompts/{dimension}.py` | prompt 模板 + `build_{dimension}_system_prompt()` 渲染函数 |
| 新建 | `tests/test_expert_{dimension}.py` | 单元测试（stub LLM + 空 tool_registry） |
| 修改 | `committee.py` | 在 `run_committee_discovery` 里注册新专家 |
| 修改 | `docs/CHANGELOG.md` | `[新功能]` 记录 |

---

## 参考实现路径

| 新文件 | 对照参考 |
|--------|---------|
| `experts/sentiment.py` | `experts/capital_flow.py`（完整实现） |
| `tools_manifest/sentiment.yaml` | `tools_manifest/capital.yaml`（结构示例） |
| `prompts/sentiment.py` | `prompts/capital.py`（模板风格） |
| committee.py 注册段 | `committee.py` line 400–426（资金面专家调用） |
| `_merge_sentiment_evidence()` | `committee.py` line 260–334（资金面合并逻辑） |
