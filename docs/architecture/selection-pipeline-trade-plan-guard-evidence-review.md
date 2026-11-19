# 交易计划生成端的结构校验：外部依据核查与改动裁定

> 文档性质：依据审查（advisory），**不含任何代码改动**。
> 审查对象：2026-11-18 落地的 `src/agent/trade_plan_guard.py`、`stock_selection_prompts.py` 新增的第 10/11/20/21 条，以及同批回测口径修正。
> 核查日期：2026-11-19。
>
> **结论先行**：回测口径修正属于测量修正，有外部依据，应保留；提示词与 guard 中“由校验层强制改写点位数值”的部分，属于生成体制变更，缺少外部依据，**应回退**。校验层可以保留，但只能做合法性判定与拒绝，不应替模型决定价格。
>
> **执行状态（2026-11-19）**：上述回退已完成。提示词第 10/11/20/21 条已删除；guard 已改为纯校验器，只输出 `ok / needs_review / invalid` 与建议值，不再有任何写回路径；回测 B1/B2 保持不变。落地细节见第 6 节。

---

## 0. 证据分级与结论摘要

本文把依据强度分三档，和用户要求的“有外部依据 / 仅样本内对照 / 无依据”对应：

| 档位 | 含义 | 示例 |
| --- | --- | --- |
| A | 同行评议论文、监管规则、交易所规则 | Review of Finance、SEC 15c3-5、MiFID II RTS 6 |
| B | 工作论文 / 预印本 / 交易所或券商技术文档 | arXiv、Nasdaq PRM、FIX 风险网关文档 |
| C | 行业博客、课程、厂商科普 | ATR 倍数“经验区间”、区间宽度经验值 |

只有 A 档可以直接支撑“这是通行做法”；B 档可支撑“工程上有人这么做”；C 档只能说明“存在这种说法”，不能当作规范。

### 0.1 四个问题的结论

| 问题 | 结论 |
| --- | --- |
| 1. 入场点位给单点还是区间 | **两种都合法**。机构执行算法普遍用单一限价或价格带；区间是主观交易/零售实践。**没有“入场区必须非零宽”的公开规范**。区间宽度按波动定的经验值（0.5~1×ATR）只到 C 档。 |
| 2. 止损方法与“必须低于下沿” | 固定百分比、ATR 倍数、结构位、时间止损都是主流方法，但**没有普适参数**；ATR 倍数取决于策略周期（Turtle 2N、Chandelier 3×ATR）。Kaminski-Lo 证明止损是否增值取决于收益过程，随机游走下止损只会降低期望收益。**“必须低于下沿”只有“必须低于实际成交价”这一层有订单语义依据，具体距离没有公开规范。** |
| 3. 结构校验放哪一层 | 成熟做法是**独立的 pre-trade risk check 层**：确定性、可审计、只做拒绝/限额，不改写策略意图。LLM 系统里则把“算术与硬约束”放代码，“止损是否太紧”留给人。 |
| 4. 量化给点位、LLM 裁量 | 主流评价是“LLM 负责理解与解释，数学模型负责数值与约束，人负责授权与参数校准”。主要风险是不可逆执行、提示注入/对抗信号、自修订漂移、评测泄漏与乐观成交。 |

### 0.2 改动裁定摘要

| ID | 改动 | 性质 | 依据强度 | 结论 |
| --- | --- | --- | --- | --- |
| P1 | pricing 提示词第 10 条：entry_zone 必须非零宽 | 生成体制变更 | 无依据（单点限价合法） | **已回退** |
| P2 | pricing 提示词第 11 条：止损 0.5~2.5×ATR20 且 1%~8% | 生成体制变更 | 方法有 C 档、具体参数无依据 | **已回退** |
| P3 | portfolio 提示词第 20 条：entry_condition 必须非零宽 | 生成体制变更 | 无依据 | **已回退** |
| P4 | portfolio 提示词第 21 条：止损 ATR 规则 | 生成体制变更 | 同 P2 | **已回退** |
| G1 | 零宽/过窄区间只抬高上沿，最小宽度 0.20×ATR20 / 0.5%，上限 3% | 生成体制变更 | 仅 C 档 | **已回退**（改为 warning） |
| G2 | 止损高于入场区上沿 → 拒绝 | 合法性校验 | A 档（订单语义 + 盘前风控） | **保留**（`status=invalid`） |
| G3 | 止损 ≥ 下沿（含等于成交价）→ 按 1% / 0.5×ATR 重算 | 方向属合法性，取值属生成体制变更 | 方向 A 档，取值无依据 | **已改为仅判定 + 建议值** |
| G4 | 止损距离夹在 [0.5, 2.5]×ATR20 ∩ [1%, 8%] | 生成体制变更 | 无依据（仓库口径 + 单样本） | **已回退**（改为 warning） |
| G5 | 审计字段 | 可观测性 | A 档（审计产物） | **保留**（改为 `trade_plan_validation`） |
| G6 | ATR20 只读本地缓存 | 数据管道 | A 档（point-in-time） | **保留**（诊断用途） |
| B1 | 严格入场改为限价：`low <= entry_low` 触发，成交价 `min(entry_low, open)` | 测量修正 | A 档 | **保留** |
| B2 | `entry_rule` 如实映射 `conditional_open → conditional` 等 | 测量修正 | A 档（字段语义） | **保留** |
| B3 | `_build_trade_plan` 优先数值字段；被拒绝计划 `parse_status=failed` | 测量/兼容修正 | A/B 档 | **保留** |
| B4 | 回测复用 guard 的价格解析函数 | 重构 | 无行为变化 | **保留** |

---

## 1. 外部证据调研

### 1.1 入场区间：单点还是区间？宽度怎么定？

**结论：单点限价与价格区间都合法，业界没有“必须给区间”的规范。**

- 机构执行侧的标准输入是**单一限价或价格带**，而不是主观区间。Nasdaq 的执行算法用户指南写明交易者需要指定限价，限价用于防止成交偏离；算法以参考价为基础运行 [S13]。Standard Chartered 的执行算法文档同样以 limit price 作为被动执行的约束参数 [S14]。
- 监管侧的价格约束也是“相对参考价的偏离带”，不是“入场区间”。MiFID II RTS 6 第 13 条要求对订单设置 price collars（相对参考价的最大偏离）、最大订单金额/数量、每秒最大报文数等盘前控制 [S12]。
- “支撑/阻力是区间不是线”以及“区间宽度 = 0.5~1.0×ATR”属于**从业者经验**，来源是交易教育类站点，只能算 C 档 [S21]。公开学术文献没有给出“入场区间宽度应为多少”的标准。

**对“零宽区间”的判断**：零宽区间在交易语义上就是一个单点限价单，本身不是非法输入。把它判为缺陷，是把“主观交易习惯”当成了“数据合法性”。因此 P1/P3/G1 缺少外部依据。

**宽度与成交率的关系**：

- 限价单的成交概率随挂单价格远离盘口而下降，随价差扩大和波动率上升而上升（Cho & Nelling 对 NYSE TORQ 数据的研究）[S2]。也就是说，**高波动时同样的价格距离更容易被触及**，这为“按波动定宽”提供的是机制解释，不是参数标准。
- 挂单时间越长，成交概率越低；卖单成交概率高于买单 [S2]。
- 成交率量级：美国零售限价单约 65% 完全成交，可成交单接近 100% [S1]；早期 NYSE 市场层面约 44%（Harris & Hasbrouck, 1996，转引自 [S1]）；机构订单约 43%（Jeria & Sofianos, 2008，转引自 [S1]）。

**未成交标的是否系统性更弱？**

- **没有公开证据支持“未成交的标系统性更弱”这一通用结论。**
- 相反，被动成交存在逆向选择：订单在价格即将朝不利方向运动时更容易成交，因此“成交概率”与“成交后短期收益”可能负相关（Binance BTC 永续的实盘实验，[S5]）。这说明“能成交”不等于“选到了更好的标的”。
- 本项目诊断里“未成交组 +20 日中位 -11.01%”是**单一样本、单一区间、偏下跌行情**的观察，不能外推为“挂低导致错过强势股”。

### 1.2 止损方法谱系与参数

| 方法 | 做法 | 适用条件 | 常见参数 | 依据 |
| --- | --- | --- | --- | --- |
| 固定百分比 | 入场价下方固定 x% | 简单、跨标的统一 | 零售常见 5%~10%，无学术最优值 | C 档从业惯例；无 A 档参数标准 |
| ATR 倍数 | 入场价/最高价下方 k×ATR | 需要随波动自适应 | Turtle：2N（N=20 日 ATR）[S19]；Chandelier：3×ATR，回看约 22 日 [S20]；从业经验按周期分 1.5~2 / 2~3 / 3~5 | A/C 档混合：方法有公开来源，倍数无统一标准 |
| 结构位 | 放在前低/支撑下方，加缓冲 | 有清晰结构、主观交易 | 缓冲 0.25~0.5×ATR（C 档） | C 档 |
| 时间止损 | 持有 N 日未按预期走则退出 | 事件驱动、均值回归 | 常见 2~3× 盈利交易的中位持有期（C 档） | C 档，缺 A 档证据 |

**关键学术结论**：Kaminski & Lo 证明，止损规则是否增加期望收益取决于收益过程——在随机游走假设下，全有或全无的止损**总是降低**策略期望收益；只有存在价格动量时，止损才可能增值 [S4]。这意味着：

- 不存在“正确”的止损宽度；
- 止损宽窄必须和策略的收益来源（趋势 / 均值回归）匹配；
- 用单一样本（尤其偏下跌样本）反推“应该多宽”在方法论上不成立。

**“止损必须低于入场区下沿”有没有公开依据？**

- 有依据的只有更弱的一条：**多头止损必须低于实际成交价**，否则开仓即触发，这在订单语义上是恒真的，也属于盘前风控会直接拒绝的“方向错误” [S10][S12]。
- 由于本项目把 `entry_zone_low` 当作限价成交价，“止损低于下沿”与“止损低于成交价”在多数情况下等价，所以它是**实现上的保守代理**，不是行业规范。
- 跳空低开时会以 `open` 成交，成交价可能低于下沿；此时止损若落在 `(open, entry_low)` 区间内，仍然高于实际成交价。**正确的不变量是 `stop < 实际成交价`，而不是 `stop < 下沿`。**
- 因此：G3 的“方向判定”可以保留；“低于下沿多少”以及“自动重算到 1%/0.5 ATR”没有公开依据。

### 1.3 生成端结构校验应放在哪一层？

**结论：放在独立的、确定性的 pre-trade risk check 层；它只拒绝或限额，不改写策略给出的价格。**

监管与交易所的成熟做法：

- SEC Rule 15c3-5（Market Access Rule，2010）要求有市场准入的经纪商建立并记录盘前风险控制，系统性限制金融暴露 [S10][S11]。
- MiFID II RTS 6 第 13 条列出盘前控制：价格偏离带、最大订单金额与数量、每秒报文上限、最大持仓等 [S12]。
- 交易所层面，Nasdaq 的 PRM 在订单进入撮合引擎前做校验，不通过则拒绝 [S15]；FIX 生态里有独立的 Pre-Trade Risk Check 模块，未通过检查的订单被退回 [S16]。

工程架构上的共识是分层：

```
策略意图 -> 盘前风控（确定性、可审计、拒绝/限额） -> OMS 状态 -> FIX 网关 -> 交易所
```

风控层位于策略与 OMS 之间，策略不需要知道风控存在，风控也不实现策略 [S17][S18]。关键特征是：**检查是确定性的、毫秒级的、只做通过/拒绝，不重新设计策略参数。**

LLM 交易系统里的对应实践：

- AllPath 把验证拆成三层：代码验证“不能依赖判断的东西”（风险限额、策略修订完整性），代理在固定边界内自检，人验证判断。原文明确写道：“订单是否超过风险上限是算术，属于代码；**止损是否太紧是判断，属于人**。”[S9]
- Steward 把“订单算术、风险规则、纪律提示词”做成不可被自我修改触及的 invariant core，组合经理设目标仓位，独立的风险经理审计 [S7]。
- OpenPM 把自然语言风险指令转成**类型化约束**并对执行后的组合强制执行，同时输出污染证明、成本敏感性曲线和约束遵循报告 [S8]。

**推论**：把“止损宽度取向”写进校验器并强制改写，等于把判断层塞进算术层。位置放错了，不是参数调错了。

### 1.4 “量化给点位、LLM 做买卖裁量”的公开评价

公开资料的主流分工是：

- **LLM 负责理解、归纳、解释与弃权；数学模型负责数值、约束与组合可行性；人负责授权与参数校准。** 行业课程把它压缩成一句：“LLM 推理，数学决策，人来否决” [S22]。
- Steward 的九代理结构里，分析师产出类型化报告，组合经理设目标仓位，**受保护的风险经理**做审计；自我演化只能改提示词学习，不能改订单算术与风险规则 [S7]。
- OpenPM 的参考代理是“类型化分析师打分 + 构造器 LLM 提权重 + 确定性 critic 保证可行”，并明确指出 LLM 交易代理的回报常因前视泄漏、乐观成交和“写了但没执行”的风险指令而虚高 [S8]。
- AllPath 的生产事故复盘显示，最危险的问题往往能通过所有代码门禁（例如策略静默失去监控），需要靠界面暴露给人 [S9]。

主要风险：

| 风险 | 表现 | 缓解 |
| --- | --- | --- |
| 不可逆执行 | LLM 直接下单，错误无法撤销 | 提案与执行分离、人工/代码双门禁 [S9] |
| 对抗信号 | 新闻/网页中的注入内容沿代理链传播到决策 | 角色隔离、来源标记、确定性校验 [S6] |
| 自修订漂移 | 代理自行改规则，短期看似合理、长期失控 | 不可变核心 + 可回放门禁 [S7] |
| 评测泄漏与乐观成交 | 回测高收益来自前视数据或上沿成交 | point-in-time 数据、限价成交口径 [S8][S1] |
| 把判断伪装成规则 | 用“校验”名义写入某个宽度/倍数 | 明确分层：合法性 vs 取向 |

**边界建议**：LLM 可以决定“买不买、等不等、是否弃权”；点位的**合法性**由代码判定；点位的**取向**（挂多低、止损多宽）要么由模型在明确授权下决定，要么由人设定 mandate，不能由校验器暗中替换。

---

## 2. 上一轮改动逐项裁定

### 2.1 提示词

| ID | 原文要点 | 位置 | 性质 | 依据强度 | 结论 |
| --- | --- | --- | --- | --- | --- |
| P1 | `entry_zone` 必须非零宽度，“下沿-上沿”，下沿不得为提高成交率而抬高 | [stock_selection_prompts.py](../../src/agent/stock_selection_prompts.py) pricing 第 10 条 | 生成体制变更：把“区间”从可选表达变成硬性格式 | 无依据：单点限价是合法且常见的执行输入 [S13][S14] | **已回退** |
| P2 | `stop_loss` 严格低于下沿，距离 0.5~2.5×ATR20 且 1%~8%，缺 ATR 也按 1%~8% | 同上第 11 条 | 生成体制变更：把策略参数写进提示词硬约束 | 方法 C 档（ATR 止损常见），具体倍数与夹取无 A 档依据；Kaminski-Lo 说明不存在普适宽度 [S4] | **已回退** |
| P3 | `entry_condition` 必须含非零宽区间，禁止单点 | portfolio 第 20 条 | 生成体制变更 | 同 P1 | **已回退** |
| P4 | `stop_loss_condition` 严格低于下沿，0.5~2.5×ATR20 且 1%~8% | portfolio 第 21 条 | 生成体制变更 | 同 P2 | **已回退** |

补充：P2/P4 还有一层隐性风险——提示词把“止损宽度”从策略判断变成了格式合规。模型会优先满足格式而不是表达真实风险观点，导致所有标的的止损被拉向同一分布，这与“按波动挂钩”的初衷相反。

### 2.2 guard 的四条规则

| ID | 规则 | 性质 | 依据强度 | 结论 |
| --- | --- | --- | --- | --- |
| G1 | 区间宽度 < `max(0.20×ATR20, 0.5%)` 时只抬高 `entry_zone_high`，上限 3% | 生成体制变更：替模型补一个它没给的价格 | 仅 C 档经验值 [S21]；零宽本身合法 | **已回退**：只留 `warning` |
| G2 | `stop > entry_zone_high` → `invalid` | 合法性校验：止损高于整个入场区，多头必然立即触发 | A 档：订单语义 + 盘前风控“方向错误直接拒绝” [S10][S12] | **保留**（不降级 action） |
| G3 | `stop >= entry_zone_low`（含止损等于成交价）→ 按 `min_pct` 重算 | 方向是合法性；取值是生成体制变更 | 方向 A 档；`min_pct`（1% 或 0.5×ATR）无依据 | **已改为 `invalid` + 建议值，不重算** |
| G4 | 止损距离夹到 `[0.5, 2.5]×ATR20` 再夹 `[1%, 8%]`，8% 优先 | 生成体制变更：直接改止损宽度取向 | 无 A 档依据；区间来自仓库既有口径 + 单样本对照 | **已回退**：只留 `warning` |

关于 G2/G3 的落地边界：旧实现在 `entry_low <= stop < entry_high` 时选择重算而不是拒绝，这仍然高于成交价、会立即触发。回退后已统一为：`stop > entry_high` 判 `stop_loss_above_entry_zone`（G2），`stop >= entry_low` 判 `stop_loss_at_or_above_entry_low`（G3）；两者都是 `invalid`，只判定、不重算、不回填。

关于 G3 的边界：由于跳空低开成交在 `open`，`stop < entry_low` 并不能保证 `stop < 实际成交价`。因此“低于下沿”只能当保守代理；真正的合法性判据应在执行层用实际成交价复核。

### 2.3 回测口径

| ID | 改动 | 性质 | 依据强度 | 结论 |
| --- | --- | --- | --- | --- |
| B1 | 严格入场：`low <= entry_zone_low` 触发，成交价 `min(entry_zone_low, open)`；不再用上沿成交 | 测量修正：让回测反映真实限价单 | A 档：限价单只在价格触及限价时成交；成交概率与挂价距离、波动、时间相关 [S1][S2] | **保留** |
| B2 | `entry_rule` 如实映射：`conditional_open → conditional`、`immediate_open → immediate`、`wait/plain_wait → unknown` | 测量修正：字段语义对齐 | A 档：字段名与执行语义一致，避免把等待态统计成即时入场 | **保留** |
| B3 | `_build_trade_plan` 优先读数值字段；`trade_plan_rejected` → `parse_status=failed` | 测量/兼容修正：避免二次解析歧义；拒绝计划不应进入绩效统计 | A/B 档：结构化字段优先于自然语言解析；被拒绝计划不可执行 | **保留** |
| B4 | 回测复用 guard 的 `extract_price_range` / `extract_single_price` | 重构：消除双份解析实现 | 无行为变化 | **保留** |

**为什么 B1 是测量修正而不是策略变更**：旧口径在“价格进入区间但未触及下沿”时按上沿成交，等于假设一个不存在的成交。修正后成交率下降（54 → 45），是把回测从“乐观成交”改回“可实现成交”，与是否看好入场策略无关。Cho & Nelling 也明确指出，限价单的收益必须扣除“未成交的机会成本”来评估 [S2]。

---

## 3. 分层边界：哪些能由工程约束，哪些必须留给模型或用户

### 3.1 判定准则

一条规则能不能放进校验层，用四个问题判断：

1. **机械可判定**：不依赖偏好就能判 true/false？
2. **可逆性**：判错时能否回滚且不改变原始意图？
3. **可审计性**：能否留下 before/after 与理由？
4. **影响面**：改写的是“合法性”还是“取向”？

四条都满足，才适合做**硬校验**；只满足 1/3/4 但涉及取向的，只能做**告警**。

### 3.2 分层表

| 层 | 可以由工程强制约束 | 不应由工程强制 | 理由 |
| --- | --- | --- | --- |
| 数据层 | point-in-time、缺失标记、单位与复权一致、禁止未来数据 | 用未来数据“补”缺失值 | 前视泄漏会让所有下游结论失真 [S8] |
| 点位生成 | 不干预 | 入场价、区间宽度、止损距离、止盈目标 | 这些是策略观点；不存在普适参数 [S4][S2] |
| 合法性校验 | 价格 > 0、有限值、`low <= high`、多头 `stop < 实际成交价`、代码存在、字段可解析 | 把 `stop >= low` 自动改写成某个“合理”距离 | 方向错误可机械判定；改写到哪是判断 [S9] |
| 组合/风险 | 仓位上限、现金约束、单票集中度、最大回撤熔断 | 用风控层反向修改模型给的点位 | 风控管暴露，不管选价 [S12][S17] |
| 执行 | 限价单语义、T+1、涨跌停/最小报价单位、成交回报 | 用回测口径反推实盘应挂多低 | 执行层只保证“按计划可执行” [S1] |
| 裁量与授权 | 记录决策、保留弃权、人工审批 | 让 LLM 无门禁直接下单；让校验器替人调参 | 不可逆动作需要提案/执行分离 [S9] |

### 3.3 对本项目 guard 的具体定位建议

**已落地为“校验器”，而不是“修正器”**：

- 输入：模型给出的 `entry_zone` / `entry_condition`、`stop_loss` / `stop_loss_condition`、ATR20（仅诊断用）。
- 输出：`status = ok / invalid / needs_review`，附 `issues`、`observed`、`suggestions`（`advisory_only=True`，只作参考，不写回）。
- 行为：
  - `stop > entry_high` → `invalid`（G2）；`stop >= entry_low` → `invalid`（G3），由调用方拒绝或回问模型；
  - 零宽区间 → 只记 `warning`，`status` 仍为 `ok`（单点限价合法）；
  - 止损距离与 ATR 明显脱钩 → 只记 `warning`，不改数值；
  - 判定写入 stage 级 `trade_plan_validation`，保留原始值；`invalid` 标记 `blocked=true`。
- **禁止**：在校验层把 `entry_zone_high`、`stop_loss` 改成模型没给过的数值，也不降级 `action` 或清零仓位。

这样既能在生成端拦住“买入即触发止损”这类机械错误，又不会把策略取向偷偷替换掉。

---

## 4. 样本内对照的正确读法

上一轮的 54 笔前后对照（完整表见 [入场/止损保守度诊断](selection-pipeline-entry-stop-conservatism-diagnosis.md) 第 9 节）摘要：

| 指标 | 基线 | 修复后 |
| --- | ---: | ---: |
| 成交笔数 | 54 | 45 |
| 止损距离与 ATR20 相关系数 | -0.20 | +0.15 |
| 胜率 | 16.7% | 13.3% |
| 已实现收益中位 | -2.55% | -2.95% |

**使用限制（必须同时引用）**：

- **单一样本**：同一批 54 条历史计划，不是独立样本外验证。
- **单一区间**：固定的一段历史窗口，未覆盖不同市场状态。
- **偏下跌行情**：未成交组 +20 日中位 -11.01%，成交组从成交价持有 20 日中位 -9.07%。
- **口径同时变化**：修复后同时改了下沿成交和止损宽度，无法把“胜率下降”单独归因于止损变宽。
- **无对照组**：没有同期随机抽样或基准对照。

因此这张表只能说明“机械结构缺陷被修正了、样本内结果没有变好”，**不能说明修复方向正确，也不能说明应把止损调窄**。把它当作通用结论，正是本次要纠正的问题。

---

## 5. 回退与保留清单（已执行）

### 5.1 应回退（无外部依据的生成体制变更）

1. **P1 / P3**：删除“入场区必须非零宽”的提示词硬约束；保留“下沿是限价触发价、不要为成交而抬高”的语义提示（这条没有改变点位，属于口径说明）。
2. **P2 / P4**：删除 `0.5~2.5×ATR20` 与 `1%~8%` 的硬约束；如需引导，改为“可用结构位或波动尺度描述止损依据”，不规定倍数。
3. **G1**：不再抬高 `entry_zone_high`；零宽/过窄只记 `warning`。
4. **G4**：不再夹取止损距离；脱钩只记 `warning`。
5. **G3 的自动重算**：`stop >= entry_low` 时不要自行按 1%/0.5ATR 生成新止损；改为 `invalid` + 拒绝/回问。

### 5.2 应保留

1. **B1 / B2 / B3 / B4**：回测限价口径、`entry_rule` 如实映射、数值字段优先、解析函数复用。
2. **G2**：止损高于整个入场区的拒绝逻辑（可把阈值统一为“高于成交价即非法”）。
3. **G5**：审计字段与 stage 级汇总；即使 guard 缩成校验器，审计仍然需要。
4. **G6**：ATR20 的本地只读计算；改为诊断指标后仍然有用。

### 5.3 待验证（不要现在决定）

1. **是否要 ATR 止损**：这是策略层面的 mandate 变更，不是代码缺陷修复。若要做，应在提示词/策略文档中显式声明，并用**独立样本外 + 上行/下行分层**做影子对照。
2. **入场区间是否应成为产品规范**：如果产品希望报告更易读，可以要求“展示区间”，但应把它定义为**展示层格式**，不要影响执行触发价。
3. **拒绝后的回问流程**：拒绝比改写更安全，但会增加一次模型调用；需要评估时延与成本。

---

## 6. 落地结果（2026-11-19）

### 6.1 代码形态

- `src/agent/trade_plan_guard.py`：删除 `apply_guard_to_plan` 及文本/数值替换函数，只保留 `extract_price_range`、`extract_single_price`、`compute_atr20_pct`、`validate_price_plan`、`validate_plan_fields`。校验函数是纯函数，返回 `status` / `issues` / `suggestions` / `observed` / `atr20_pct`，不修改入参。
- `src/agent/stock_selection.py`：`_apply_trade_plan_guard` → `_apply_trade_plan_validation`；`_guard_plan_items` → `_validate_plan_items`。接入层只收集判定与建议，写入 stage 级 `trade_plan_validation`（`advisory_only=True`），不写入任何计划字段。
- `src/agent/stock_selection_prompts.py`：第 10/11/20/21 条已删除，恢复原编号与原内容。
- 回测 `src/services/agent_entry_execution_backtest_service.py`：B1/B2 未改动，仍按 `low <= entry_zone_low` 触发、`min(entry_zone_low, open)` 成交，`entry_rule` 如实映射。

### 6.2 判定语义

| status | 触发条件 | 含义 | 是否改计划 |
| --- | --- | --- | --- |
| `ok` | 无 `error`，无解析失败 | 可执行；可能带 `warning` | 否 |
| `needs_review` | 入场区或止损文本非空但无法解析 | 需要模型/人补充 | 否 |
| `invalid` | 价格非正、下沿 > 上沿、止损 > 上沿、止损 ≥ 下沿 | **不得执行**，由消费方拒绝或回问 | 否 |

- `suggestions` 同时带 `advisory_only=True`，仅作参考；调用方不得将其直接落盘。
- `invalid` 条目在 `trade_plan_validation.results` 中标记 `blocked=true`，但计划本身保持原样，`action` 与仓位字段不降级。

### 6.3 验证

- `py_compile` 覆盖上述四个 Python 文件。
- `tests/test_agent_trade_plan_guard.py`：10 例，覆盖零宽告警、止损等于下沿、止损高于上沿、ATR 脱钩告警、极端 ATR 上限、不可解析、非正/倒置区间、**入参不被修改**、ATR20 计算、价格解析。
- `tests/test_agent_stock_selection.py`：新增 2 例，断言校验前后计划完全一致、无 `trade_plan_rejected`、`action` 与仓位不变，且 `trade_plan_validation` 含判定与建议。
- `tests/test_agent_entry_execution_backtest_service.py`：回测限价与 `entry_rule` 映射用例保持通过。

---

## 7. 参考来源

### 学术

- [S1] Anand, Samadi, Sokobin, Venkataraman, "Retail limit orders", *Review of Finance* 30(2), 2026. https://academic.oup.com/rof/article/30/2/459/8277182
- [S2] Cho, J.-W. & Nelling, E., "The Probability of Limit-Order Execution", *Financial Analysts Journal* 56(5), 2000, 28–33. https://www.tandfonline.com/doi/abs/10.2469/faj.v56.n5.2387
- [S3] Harris, L. & Hasbrouck, J. (1996); Jeria & Sofianos (2008)：成交率数据转引自 [S1]。
- [S4] Kaminski, K. & Lo, A., "When Do Stop-Loss Rules Stop Losses?", *Journal of Financial Markets* 18, 2014, 234–254；工作论文摘要：https://ideas.repec.org/p/hhs/sifrwp/0063.html
- [S5] "The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill Returns Trade-Off", arXiv:2502.18625. https://arxiv.org/html/2502.18625
- [S6] "Poisoning Agentic Alpha: Adversarial Vulnerabilities Across Roles and Architectures in Multi-Agent Trading Systems", arXiv:2608.24069. https://arxiv.org/html/2608.24069v1
- [S7] "Governed Self-Evolution for Autonomous Investing: A Multi-Agent LLM System with an Invariant Core", arXiv:2610.30557. https://arxiv.org/abs/2610.30557
- [S8] "OpenPM: Auditable Point-in-Time Evaluation for LLM Portfolio-Management Agents", arXiv:2608.09988. https://arxiv.org/abs/2608.09988
- [S9] "AllPath: Layered Verification for a Deployed, Self-Revising LLM Trading Agent under Human-Gated and Autonomous Regimes", arXiv:2610.26662. https://arxiv.org/html/2610.26662

### 监管与交易所

- [S10] SEC, Rule 15c3-5 FAQ（Market Access Rule 盘前风险控制）. https://www.sec.gov/divisions/marketreg/faq-15c-5-risk-management-controls-bd.htm
- [S11] SEC, "SEC Adopts New Rule Preventing Unfiltered Market Access", 2010. https://www.sec.gov/newsroom/press-releases/2010-210-sec-adopts-new-rule-preventing-unfiltered-market-access
- [S12] Commission Delegated Regulation (EU) 2017/589（MiFID II RTS 6）, Article 13 盘前控制. https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32017R0589
- [S15] Nasdaq, "Pre-Trade Risk Management"（PRM 在进入撮合引擎前校验）. https://www.nasdaq.com/docs/2269-Q19%20Pre-Trade%20Risk%20Management.pdf

### 工程与执行

- [S13] Nasdaq, *Execution Algorithms User Guide* v2.7（限价保护、参考价）. https://www.nasdaq.com/docs/2020/06/08/Nasdaq-Exchange-Clearing-Services-AB-Execution-Algorithms-User-Guide-2.7.pdf
- [S14] Standard Chartered, *Execution Algorithm 5.0*, 2025. https://www.sc.com/EN/uploads/sites/66/content/docs/execution-algorithms-standard-chartered.pdf
- [S16] B2BITS, "FIX-based Pre-Trade Risk Check Module". https://btobits.com/trading_solutions/pre-trade-risk-check-solutions
- [S17] Jebel Quant, "A Technology Vision for Quantitative Trading"（pre-trade risk checks 作为 middleware）. https://jebel-quant.github.io/platform/vision.pdf
- [S18] NexusFi Academy, "Automated Futures Trading Architecture: Production System Design for 7 Decoupled Layers"（Risk Engine 位于策略意图与 OMS 之间）. https://nexusfi.com/a/automation/automated-futures-trading-architecture

### 行业实践（C 档，仅作“存在这种说法”）

- [S19] Turtle Trading Rules（2N 止损，N = 20 日 ATR）. https://uptickflow.com/en/learn/turtle-trading
- [S20] Chandelier Exit（最高价 − 3×ATR，LeBeau）. https://bollingerbands.us/help-stops.php
- [S21] 支撑/阻力区间宽度经验值（0.5~1.0×ATR）. https://pomegra.io/wiki/resistance-zone-width/
- [S22] QuantArmy, "The LLM stack in asset management in 2026"（“LLM 推理、数学决策、人否决”）. https://quantarmy.com/langchain/en/modulo-01/
- [S23] Iron Mind, "How Do You Architect a Safe Autonomous LLM Trading Agent in 2026?"（移除参数而不是检查误用）. https://iron-mind.ai/blog/autonomous-llm-trading-agent-architecture-2026

---

## 附：本次审查未做的事

- 未修改任何代码、测试或配置。
- 未执行 git 操作。
- 未重新跑回测；第 4 节数据沿用上一轮已记录的 54 笔对照。
