# LLM 决策保守性的外部解法调研

> 范围：只做外部实践调研与选项罗列，不改代码、不调阈值、不做 git 操作。
> 本地数据口径：`data/agent_traces/` 中 `candidate_discovery_mode=thesis_desk_committee` 的 43 个 trace，131 条深度结果（日期 2026-05-30 ~ 2026-11-16）。
> 检索日期：2026-11-19。
> 标注约定：**【数据事实】**=可复核的数字或原文；**【分析判断】**=基于资料的推理，不冒充事实；**【查不到】**=公开检索未找到可靠出处。

---

## 0. 先界定本地现象（数据事实）

| 现象 | 本地数字 |
| --- | --- |
| 深度分析总数 | 131 条 |
| 非执行动作 | 129 条（wait 109 / monitor 17 / reject 3；另有 conditional_open 1） |
| 执行动作 open | 1 条（贵州茅台 600519，且带 1 维 `market_sector` 缺失） |
| `strength=strong` | 2 条，对应 wait 1 / reject 1，**0 条 open** |
| 唯一 `open` 的强度 | `weak` |
| 非 open 的理由 | 23/24 条零缺失样本的 `main_risks` 非空，理由是超买、趋势向下、主力净流出、乖离过高，**不是"没数据"** |
| 分数天花板 | 232 条初筛分 p50=46、p90=64、max=83；≥85=0、≥88=0 |

【分析判断】这不是"信息不足所以不表态"，而是"看到了具体风险，主动选择等待"。要解决的是**风险偏好/决策阈值**问题，不是数据覆盖问题。外部调研也应按这个方向筛。

---

## 1. 这类行为叫什么（便于后续检索的术语）

| 术语 | 出处 | 与本地现象的对应 |
| --- | --- | --- |
| **Over-cautiousness as dominant failure mode** | 立场感知 Agent 基准论文，明确写 "exposing agents over-cautiousness as the dominant failure mode"（[arXiv:2610.03356](https://arxiv.org/html/2610.03356)） | 最贴近："该出手时不出手"是主要失效模式，而非能力不足 |
| **Decision paralysis / Multi-Objective Oscillation** | Agent Patterns Catalog 反模式条目（[链接](https://www.agentpatternscatalog.org/patterns/decision-paralysis/)） | 多目标冲突、无优先级时，Agent 卡住或来回震荡 |
| **Skepticism Trap / Scaling Paradox（defaulting to paralysis）** | ACL 2026 findings 论文（[PDF](https://aclanthology.org/2026.findings-acl.427.pdf)） | 安全对齐后的模型"高特异度、低敏感度"，在模糊情境下默认瘫痪 |
| **Overly Cautious Behaviors of LRMs / overthinking** | 高效推理综述（[arXiv:2503.24377](https://arxiv.org/html/2503.24377v2)） | 反复验证、冗余推理后仍不给结论 |
| **Over-refusal / false refusal / exaggerated safety** | OR-Bench（[arXiv:2405.20947](https://arxiv.org/html/2405.20947)）、FalseReject（[arXiv:2505.08054](https://arxiv.org/html/2505.08054)）、XSB（[arXiv:2510.08158](https://arxiv.org/pdf/2510.08158?)） | 拒绝不是因为有害，而是因为触发词/保守倾向 |
| **Safety–Pragmatism trade-off** | ManagerBench（[arXiv:2510.00857](https://arxiv.org/html/2510.00857)） | 过度安全导致任务完成度下降，与"风险提示到位但不出手"同构 |
| **Selective prediction / reject option / abstention** | SelectiveNet（[arXiv:1901.09192](https://arxiv.org/html/1901.09192v2)）、Know When to Abstain（[arXiv:2505.15008](https://arxiv.org/pdf/2505.15008v1)） | 学术上把"弃权"当作可选动作，用 risk–coverage 曲线评估，而不是默认正确 |

【查不到】公开文献里**没有**一个统一术语专指"LLM 选股 Agent 出手率过低 / low action rate / under-trading"。最接近的是 `over-cautiousness`、`decision paralysis`、`over-refusal` 三个方向，检索时应组合使用，而不是只搜一个词。

---

## 2. 二元/序贯决策的已知偏差与校准方法

| 方法 | 实际做法 | 效果证据 | 出处 |
| --- | --- | --- | --- |
| **自洽性采样（Self-Consistency）** | 同一输入采样多条推理路径，对最终答案做多数投票，替代贪心解码 | 论文报告在多类算术/常识推理基准上稳定提升，且无需额外训练；未针对交易决策验证 | [Wang et al., arXiv:2203.11171](https://arxiv.org/html/2203.11171v4) |
| **自洽投票率作为弃权信号** | 用一个标签是否获得 >50% 票数来决定"答/不答" | 论文把弃权决策本身作为研究对象，说明投票率可用于 selective classification | [MDPI Information 2026](https://www.mdpi.com/2078-2489/17/11/1115) |
| **置信度校准（verbalized / token log-prob / temperature & Platt scaling）** | 让模型给置信度并做后处理校准，或对 claim 级分数做 temperature/Platt scaling | 有论文指出"口头置信度更多反映承诺而非正确性"，token log-prob 的弃权信号更贴合正确性；claim 级校准可把 0.85 解释为约 15% 误差 | [Reported Confidence Tracks Commitment](https://arxiv.org/html/2606.29490)、[Claim-Level Calibration](https://arxiv.org/html/2608.22483)、[SteerConf](https://openreview.net/forum?id=5sgK63Zshg) |
| **强制选择（forced choice）** | 从选项里去掉"再想想/弃权"，迫使模型二选一 | 有研究提醒：强制二选一会把偏好强度、无差异、不确定性压成一个结果，测到的可能只是 elicitation artifact | [Mind the Gap, arXiv:2601.21975](https://arxiv.org/html/2601.21975) |
| **Base rate 锚定** | 先给历史基准率/先验，再让模型判断 | 人类实验中 base rate neglect 很普遍（仅 11% 接近贝叶斯后验）；LLM 也有 base rate fallacy 报告 | [NBER w28650](https://www.nber.org/system/files/working_papers/w28650/w28650.pdf)、[arXiv:2406.10999](https://arxiv.org/html/2406.10999v2) |
| **风险偏好本身可被对齐改变** | 对 LLM 做伦理/安全对齐后测风险偏好 | 论文报告"伦理度提高 10%，风险偏好下降 2%–8%"；LLM 普遍表现出 risk aversion / loss aversion / 小概率高估 | [AI as Decision-Maker, arXiv:2406.01168](https://arxiv.org/pdf/2406.01168)、[NeurIPS 2024 Decision-Making Behavior](https://papers.nips.cc/paper_files/paper/2024/file/cda04d7ea67ea1376bf8c6962d8541e0-Paper-Conference.pdf) |
| **风险控制式弃权（conformal / likelihood-ratio）** | 用校准集控制错误率，再决定何时弃权 | "Learn then Test" 与 selective classification 系列给出可证的风险上界，而不是拍阈值 | [Learn then Test, arXiv:2110.01052](https://arxiv.org/html/2110.01052v1)、[SelectiveNet](https://arxiv.org/html/1901.09192v2) |

【分析判断】对本项目最有直接借鉴意义的是两条：

1. **自洽性采样**：不要求模型改性格，只把同一输入多跑几次、按多数动作聚合，能压低单次保守/单次激进带来的方差。代价是 k 倍调用。
2. **概率/风险控制式弃权**：把"wait"从"默认动作"改成"只有在校准后的风险预算被突破时才允许的动作"，而不是反过来。

【查不到】没有找到"把'再想想'从选项里去掉后，LLM 交易决策 open 率提升 X%"这类直接实验。强制选择的效果证据主要来自偏好测量领域，不能直接外推到交易。

---

## 3. 成熟架构做法

| 做法 | 真实系统/论文 | 具体机制 | 与本地项目的距离 |
| --- | --- | --- | --- |
| **分析角色与交易角色分离** | TradingAgents（[arXiv:2412.20138](https://arxiv.org/pdf/2412.20138v7.pdf)、[项目页](https://tradingagents-ai.github.io/)） | 四类 analyst 只产证据；bull/bear researcher 辩论；trader 必须综合后给交易；risk team 管暴露 | 本项目已有 `single_stock_deep_dive`（分析）与 `meta_orchestrator`/`portfolio_allocation`（决策），但决策层仍可输出 wait，且 wait 是默认 |
| **风险团队管暴露而非一票否决** | TradingAgents Risk Management Team | 监控波动/流动性/对手方风险，设止损、分散，不是"看到风险就禁止交易" | 本项目 `HARD_BEARISH_RISK_MARKERS` 是硬否决，性质更接近"一票否决" |
| **多资产对冲经理制** | HedgeAgents（[arXiv:2502.13165](https://arxiv.org/pdf/2502.13165)） | 三个 analyst 分管资产，hedge fund manager 做总体风险与配置 | 可借鉴"分析归分析、配置归配置"的边界 |
| **把观望计入成本（cash drag / opportunity cost）** | 行业词条（[Fincanva](https://fincanva.com/glossary/cash-drag)、[EquityAnalysisLab](https://equityanalysislab.com/en/portfolio-construction/portfolio-maintenance/cash-drag/)、[Cash Drag vs Deploying Capital](https://static1.squarespace.com/static/5f24c8ef6ddee906a804a935/t/6a10b9c9c388b2011ec09d42/1779481033749/Cash+Drag+vs+Deploying+Capital.pdf)） | 空仓的机会成本 = 未投入资金 × 市场涨幅；不是"没亏损"，而是"放弃了本可获得的收益" | 本项目报告未把 wait 的机会成本量化 |
| **最低出手率/最低换手约束** | 组合优化中的 turnover constraint（[arXiv:1910.11840](https://arxiv.org/pdf/1910.11840v1)、[Smart Rebalancing](https://www.tandfonline.com/doi/full/10.1080/0015198X.2024.2317323)） | 用换手约束/优先规则限制"不动"或"乱动"，本质是给交易频率设边界 | **注意**：这些是约束交易成本，不是强制出手；"最低出手率"在公开组合优化里不是标准做法 |
| **风险预算替代二元否决** | 仓位 sizing 实践（[QuaTick](https://quatick.com/resources/articles/position-sizing-for-algo-portfolios)、[波动率目标化回测](https://arxiv.org/html/2412.14361v1)） | 不确定时缩小仓位，而不是 0/1 拒绝；按波动率目标缩放权重 | 本项目 action 是离散枚举，没有"小仓位试错"档 |

【分析判断】"analyst 给证据、trader 必须给动作"在 TradingAgents 里是**角色分工**，不是"trader 永远看多"。它的 trader 仍然可以卖/不买，但必须给出明确动作与理由。本项目的差别在于：`wait` 被写成默认输出（prompt L673 的"本轮没有可直接入手标的"标准文案），而不是"经过判断后的主动选择"。

---

## 4. 量化投研的老问题：策略过度保守导致空仓

| 做法 | 说明 | 出处 | 是否有实验/实盘支撑 |
| --- | --- | --- | --- |
| **波动率目标化（vol targeting）** | 按目标波动率缩放总暴露，而不是"满足条件才满仓、否则空仓" | [arXiv:2412.14361](https://arxiv.org/html/2412.14361v1) | 有回测支撑 |
| **分数仓位（fractional sizing）** | 按信号强度/回撤承受力给比例仓位，避免 0/1 跳变 | [QuaTick](https://quatick.com/resources/articles/position-sizing-for-algo-portfolios) | 工程实践，非严格实验 |
| **换手约束** | 用 turnover penalty/constraint 控制成本；也间接防止"频繁空仓-满仓"抖动 | [arXiv:1910.11840](https://arxiv.org/pdf/1910.11840v1)、[Markowitz under Transaction Costs](https://www.econ.uzh.ch/apps/workingpapers/wp/econwp420.pdf) | 有回测/理论支撑 |
| **现金拖累（cash drag）度量** | 把未投资比例 × 基准收益作为显式成本 | [Fincanva](https://fincanva.com/glossary/cash-drag) | 行业口径，无统一公式 |
| **最低仓位下限** | 部分 mandates 要求最低股票暴露（如 ≥80%） | 【查不到】未找到权威公开文档给出统一数值；多为基金合同个体条款 | — |

【查不到】公开量化文献里**没有**"最低出手率必须 ≥N%"的通行标准；能查到的是换手约束、波动率目标、分数仓位。把"最低出手率"当成行业惯例是不准确的。

---

## 5. 阈值设计：绝对分数 vs 分位数 vs 校准概率

| 做法 | 核心思想 | 出处 | 关键结论 |
| --- | --- | --- | --- |
| **固定绝对阈值** | 直接拍一个分数（如 0.5、85、88） | 信用风险论文明确指出"固定 0.50 不能解决偏斜分布与漂移" | [ACM 2026](https://dl.acm.org/doi/10.1145/3838323.3838341) |
| **分位数/历史分布标定** | 用历史分数分布定阈值，保证每期有可交易样本 | 金融极端事件检测中的 threshold calibration；[arXiv:2607.17555](https://arxiv.org/html/2607.17555) | 阈值随分布自适应，而非拍数字 |
| **校准后概率 + 风险控制** | 先校准概率，再用 conformal / Learn-then-Test 控制错误率 | [Learn then Test, arXiv:2110.01052](https://arxiv.org/html/2110.01052v1)、[Conformal Risk Control](https://arxiv.org/html/2607.24562) | 阈值由风险预算反推，可给统计保证 |
| **代价敏感阈值** | 由漏判/误判代价决定阈值 | scikit-learn precision-recall / cost-sensitive 文档 | 阈值应由业务代价决定，不是拍分 |

【分析判断】本项目的 85/88 属于"固定绝对阈值"，且真实分布 max=83，说明阈值从未被分布标定过。外部通行做法支持改为**分位数/校准概率**，但前提是有足够历史样本；当前 232 条样本不足以稳定标定，需要先积累。

---

## 6. 明确查不到的部分

1. **没有一个统一的"LLM 低出手率"术语**；只能借用 `over-cautiousness`、`decision paralysis`、`over-refusal`。
2. **没有找到"LLM 选股 Agent 必须输出 open 的强制动作"公开实验**；TradingAgents 等是角色分工，不是强制看多。
3. **没有找到"把观望计入成本"的成熟 LLM 实现**；cash drag 是金融口径，未见用于 LLM prompt 约束的工程案例。
4. **没有找到"最低出手率 ≥N%"的行业标准**；能查到的是换手约束与仓位下限的个体条款。
5. **没有找到"去掉'再想想'选项后 open 率提升"的直接交易实验**；强制选择的证据主要在偏好测量领域，且可能引入 artifact。

以上均不得在后续文档中编造出处。

---

## 7. 可落到本项目的选项

> 以下只列选项，不替你做选择。每项标注：预期效果、成本、风险、出处支撑、**是否削弱"数学模型给点位、AI 做建议"的分工**。
> 本轮不改代码。

### 7.1 不碰点位体制就能做的

| 选项 | 预期效果 | 成本 | 风险 | 出处支撑 | 削弱分工？ |
| --- | --- | --- | --- | --- | --- |
| **A1. 把 wait 的机会成本显式化** | 让报告读者看到"等待也是有代价的"，不改变动作本身 | 低（仅报告层加字段） | 可能被误读为"催买"；不解决根因 | cash drag 口径（Fincanva 等） | 否 |
| **A2. 分析角色与决策角色输出分离** | analyst 只给证据/强弱；决策角色必须从 open/conditional/wait 中选一个并给触发条件 | 中（多一次 LLM 调用或拆分现有 prompt） | 决策角色仍可能保守；调用成本上升 | TradingAgents 角色分工 | 否（分工不变，只是更清晰） |
| **A3. 自洽性采样聚合动作** | 同一候选采样 k 次，按多数动作聚合，降低单次保守方差 | 中高（k 倍调用/延迟） | 若模型系统性保守，多数投票仍保守；不能解决偏差，只降方差 | Self-Consistency（2203.11171） | 否 |
| **A4. 阈值改分位标定** | 用历史分数分布（如 p90）替代固定 85/88，让阈值落在分布内 | 低 | 当前 max=83，分位阈值可能仍无样本；需先积累样本 | Learn then Test（2110.01052）、threshold calibration（2607.17555） | 否（阈值在决策层，不碰点位） |
| **A5. 减少"默认 wait"措辞** | 把"本轮没有可直接入手标的"从默认文案改为"必须给出触发条件" | 低 | 可能只是换措辞，不改变模型倾向；强制选择可能引入承诺偏差 | forced choice 的 artifact 警告（2601.21975） | 否 |
| **A6. 给 wait 设"必须写明触发条件与失效条件"** | 让观望变成可检验的条件单，而不是终止状态 | 低 | 模型可能编造触发条件 | 与现有 prompt L672 一致 | 否 |
| **A7. 引入风险预算而非硬否决（仅在决策层）** | 把"命中风险词就 reject"改为"降档/降置信" | 中 | 会改变现有 HARD_BEARISH 行为；需同步改测试 | TradingAgents risk team 管暴露 | **是**（见 7.2 说明，若只改决策层不动点位，可视为边界调整） |

### 7.2 必须改体制的

| 选项 | 预期效果 | 成本 | 风险 | 出处支撑 | 削弱分工？ |
| --- | --- | --- | --- | --- | --- |
| **B1. 小仓位试错（fractional position）** | 不确定时给 10%-20% 仓位，而不是 0 | 高（要改 action schema 与组合分配） | 直接改变"AI 只给建议"的边界，AI 开始决定仓位规模 | QuaTick、vol targeting（2412.14361） | **是，必须显式标注** |
| **B2. 最低出手率/最低换手约束** | 强制每期至少 N 个可执行动作 | 高（策略层约束） | 可能为了达标而买不该买的；无行业标准可依 | turnover constraint 系列（但那是限上限，不是强制下限） | **是，必须显式标注** |
| **B3. 连续信号→仓位映射** | 把 strength 连续化为仓位比例，消除 0/1 跳变 | 高（改 schema、回测、风控） | 数学模型给点位+仓位，AI 只给方向；分工被重新定义 | QuaTick、fractional Kelly（外部实践） | **是（但可能强化数学模型角色）** |
| **B4. 校准概率替代离散动作** | 输出 open 概率而非枚举，由阈值决定动作 | 高（需标注数据与校准集） | 需要历史样本，当前 232 条不足 | Learn then Test（2110.01052） | **是，必须显式标注** |

【分析判断】A 类里真正可能改变"0 open"的是 A4（阈值标定）和 A2（角色分离）；A1/A5/A6 更偏可读性与流程规范，不应期待它们显著提升 open 率。B 类都会改变"数学模型给点位、AI 做建议"的边界，其中 B1/B3 最直接。

---

## 8. 来源清单

- [Diagnosing and Mitigating Sycophancy and Skepticism in LLM Causal Judgment（ACL 2026 findings）](https://aclanthology.org/2026.findings-acl.427.pdf)
- [Diagnosing and Mitigating Sycophancy and Skepticism（arXiv:2601.08258）](https://arxiv.org/pdf/2601.08258v3)
- [Decision Paralysis — Agent Patterns Catalog](https://www.agentpatternscatalog.org/patterns/decision-paralysis/)
- [Harnessing the Reasoning Economy（arXiv:2503.24377）](https://arxiv.org/html/2503.24377v2)
- [OR-Bench（arXiv:2405.20947）](https://arxiv.org/html/2405.20947)
- [FalseReject（arXiv:2505.08054）](https://arxiv.org/html/2505.08054)
- [Beyond Over-Refusal / XSB（arXiv:2510.08158）](https://arxiv.org/pdf/2510.08158?)
- [ManagerBench（arXiv:2510.00857）](https://arxiv.org/html/2510.00857)
- [SelectiveNet（arXiv:1901.09192）](https://arxiv.org/html/1901.09192v2)
- [Know When to Abstain（arXiv:2505.15008）](https://arxiv.org/pdf/2505.15008v1)
- [Self-Consistency Improves Chain of Thought Reasoning（arXiv:2203.11171）](https://arxiv.org/html/2203.11171v4)
- [Self-Consistency Vote Share as Selective-Classification Signal（MDPI Information）](https://www.mdpi.com/2078-2489/17/11/1115)
- [Reported Confidence in LLMs Tracks Commitment More Than Correctness（arXiv:2606.29490）](https://arxiv.org/html/2606.29490)
- [Claim-Level Confidence Calibration（arXiv:2608.22483）](https://arxiv.org/html/2608.22483)
- [SteerConf（OpenReview）](https://openreview.net/forum?id=5sgK63Zshg)
- [Mind the Gap: Elicitation Protocols（arXiv:2601.21975）](https://arxiv.org/html/2601.21975)
- [Cognitive Biases: Mistakes or Missing Stakes?（NBER w28650）](https://www.nber.org/system/files/working_papers/w28650/w28650.pdf)
- [Balancing Rigor and Utility: Mitigating Cognitive Biases（arXiv:2406.10999）](https://arxiv.org/html/2406.10999v2)
- [AI as Decision-Maker: Ethics and Risk Preferences of LLMs（arXiv:2406.01168）](https://arxiv.org/pdf/2406.01168)
- [Decision-Making Behavior Evaluation Framework for LLMs（NeurIPS 2024）](https://papers.nips.cc/paper_files/paper/2024/file/cda04d7ea67ea1376bf8c6962d8541e0-Paper-Conference.pdf)
- [Learn then Test（arXiv:2110.01052）](https://arxiv.org/html/2110.01052v1)
- [Hierarchical Group-Conditional Conformal Risk Control（arXiv:2607.24562）](https://arxiv.org/html/2607.24562)
- [Volatility-Aware Extreme Event Detection / threshold calibration（arXiv:2607.17555）](https://arxiv.org/html/2607.17555)
- [Credit Risk Threshold Calibration（ACM 2026）](https://dl.acm.org/doi/10.1145/3838323.3838341)
- [TradingAgents（arXiv:2412.20138）](https://arxiv.org/pdf/2412.20138v7.pdf)
- [TradingAgents 项目页](https://tradingagents-ai.github.io/)
- [HedgeAgents（arXiv:2502.13165）](https://arxiv.org/pdf/2502.13165)
- [Cash Drag — Fincanva](https://fincanva.com/glossary/cash-drag)
- [Cash Drag — EquityAnalysisLab](https://equityanalysislab.com/en/portfolio-construction/portfolio-maintenance/cash-drag/)
- [Cash Drag vs. Deploying Capital](https://static1.squarespace.com/static/5f24c8ef6ddee906a804a935/t/6a10b9c9c388b2011ec09d42/1779481033749/Cash+Drag+vs+Deploying+Capital.pdf)
- [Sparsity and Stability for Minimum-Variance Portfolios（arXiv:1910.11840）](https://arxiv.org/pdf/1910.11840v1)
- [Portfolio Selection with Small Transaction Costs（arXiv:1205.4588）](https://arxiv.org/pdf/1205.4588v2)
- [Smart Rebalancing（Financial Analysts Journal）](https://www.tandfonline.com/doi/full/10.1080/0015198X.2024.2317323)
- [Markowitz Portfolios under Transaction Costs（UZH working paper）](https://www.econ.uzh.ch/apps/workingpapers/wp/econwp420.pdf)
- [Position Sizing for Algo Portfolios — QuaTick](https://quatick.com/resources/articles/position-sizing-for-algo-portfolios)
- [Refining and Robust Backtesting of A Century of Profitable Industry Trends（arXiv:2412.14361）](https://arxiv.org/html/2412.14361v1)
