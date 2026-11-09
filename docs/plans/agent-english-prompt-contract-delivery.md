# Agent 英文 Prompt 生产契约补齐开发、测试与验收规格

本文面向执行 AI，要求 `REPORT_LANGUAGE=en` 下的 planning Agent 具备与中文生产 Prompt 同等的约束、阶段协议和可验证输出。目标是语义一致，不要求逐字翻译或让英文规则另成一套。

## 当前缺口

`src/agent/planning_prompts.py` 的 `DEFAULT_ZH_PROMPT_SECTIONS` 汇总角色、任务分类、候选池、Planning/Execute、Debate、工具、账户约束及报告格式；`EN_SYSTEM_PROMPT` 明确写着 lightweight placeholder。`build_planning_system_prompt()` 选英文时仅返回该 placeholder。现有 `tests/test_planning_prompts.py` 应作为扩展点。

## 契约原则

- 中文与英文必须共享同一业务语义：任务类型、工具调用/失败降级、证据账本、候选来源限制、账户画像优先级、Risk Gate 不可绕过、反方 Debate/Judge 分离、缺数据时禁止强结论、禁止编造和不泄露隐藏思维链。
- 英文所有 JSON 字段名、枚举、状态值、工具名和 schema 标识保持机器协议原样；只翻译自然语言说明、展示标题和文案。
- 英文报告结构应覆盖 position review、watchlist scan、entry analysis；不可用“concise actionable guidance”取代中文中的阶段约束/字段合同。
- Prompt 语言选择应只改变自然语言，不改变工具清单、风险规则、字段结构或状态语义。`extra_instructions` 的语言行为要明确且测试。
- 不把中文块直接拼到英文 Prompt。维护英文段落并用契约测试检测关键约束；避免机械逐句翻译造成巨大重复和语义漂移。

## 实施方案

1. 把 Prompt 内容整理为清晰分层：语言无关的机器协议/字段名、ZH 自然语言段落、EN 自然语言段落。若不做代码级抽象，至少维护同构的 `DEFAULT_EN_PROMPT_SECTIONS`，并保持 build options 的工具策略/事件策略开关一致。
2. 将英文 placeholder 替换为生产版本，按中文结构至少含：角色与安全边界、任务分类、证据/工具策略、候选发现和来源限制、Planning -> Execute/停止条件、账户上下文、Risk Gate、强制反方和 Judge、数据时效/失败降级、三类报告格式及缺失信息处理。
3. 保留工具名、JSON key、枚举 literal、路径/artifact 文件名；所有英文 schema 示例须能由现有 Pydantic/解析器接受。不要在 Prompt 中承诺当前工具不支持的市场/数据/自动下单能力。
4. 明确 `PromptBuildOptions(include_tool_policy=False, include_event_policy=False)` 对 en 与 zh 等效；如英文段落拆分不同，测试开关对应片段实际被移除。
5. 为关键规则维护中英同构检查表（测试中的 marker 集合），而非测试完整 Prompt 文本快照。每项 marker 应绑定具体可观察约束，方便后续演进。
6. 检查 prompt token 体积和重复段落；只做本文件所需整理，不借机重写 Agent runtime。

## 测试验证

单测必须检查英文生产 Prompt 不含 `placeholder`、所有必需协议章节/marker 存在、工具和字段字面值保留、语言切换下 machine contract 一致、事件/工具策略开关生效、追加指令正确追加且不覆盖原约束。

增加 schema 兼容性测试：从英文 Prompt 中挑选固定 JSON 示例，经现有 schema/parser 解析；若 Prompt 使用动态占位符，构造 fixture 替换后再解析。测试中文已有规则不回退。

回归：`rtk pytest tests/test_planning_prompts.py tests/test_agent_models_api.py tests/test_agent_trace_target_integrity.py`（按实际依赖选择完整必要集合）、`rtk python -m py_compile src/agent/planning_prompts.py`、`rtk ./scripts/ci_gate.sh`。若无真实 LLM 凭证，不得声称完成在线质量评估；可补人工双语契约审阅清单作为交付证据。

## 验收条件

- 英文 Prompt 中没有 placeholder 声明，具有所有生产阶段约束。
- en/zh 对同一输入应遵守相同 machine-readable schema、工具能力和硬风控边界。
- 候选池、Execute、Debate、Judge、失败降级和至少三种报告意图均有英文明确规则。
- Prompt build options 在两种语言下行为一致；JSON 示例可解析。
- 不扩大多市场、真实交易或其他当前未支持能力。

## 交付与回滚

修改线上 Agent prompt 属于用户可见质量变化，更新 CHANGELOG 与 Agent Prompt 专题文档；若文档有英文版，同步该章节。回滚可恢复旧 `EN_SYSTEM_PROMPT`，但旧 placeholder 不应作为长期 fallback；若英文 Prompt 构造异常，应明确报错/降级至已验证中文契约并记录语言，而非静默返回空 prompt。
