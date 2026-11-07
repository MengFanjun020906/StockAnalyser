# Polymarket 实盘接入 — 踩坑与经验沉淀

> 目标：让下一个接手 live 模块（或接新 CLOB 协议）的人，在一小时内避开我们两周踩过的所有坑。
>
> 关键文件：
> - [`live/pm_trader.py`](../../live/pm_trader.py) — 下单核心
> - [`live/run_live.py`](../../live/run_live.py) — K 线订阅 + 调度
> - [`live/preprocessor.py`](../../live/preprocessor.py) — 推理输入构造
> - [`live/ensemble_adapter.py`](../../live/ensemble_adapter.py) — 多模型投票
> - [`live/eval_tracker.py`](../../live/eval_tracker.py) — K 线命中率统计
> - [`docs/analysis/polymarket_trading_strategy.md`](../analysis/polymarket_trading_strategy.md) — 策略白皮书（实测版）

---

## 一、最可能把人吃进去的 3 个坑（P0）

### 1. Slug / Gamma API 的所有直觉都是错的，必须实测

**症状**：`RuntimeError: event not found`、市场查不到、token_id 对不上 Up/Down。

**三个独立错点**：

1. **Slug 模板**：早期 `.env.example` 里放的是 `bitcoin-up-or-down-{YYYY-MM-DD-HH-MM}-utc`，照着跑永远查不到。实测正确的是 `btc-updown-5m-{unix_ts}`，其中 `unix_ts = int(predict_target.timestamp())`。
2. **查询端点**：`/markets?slug=...` 对 5min 市场经常返回空，正确的是 `/events?slug=...`，从 `event.markets[0]` 取。
3. **outcomes 类型**：Gamma 返回的 `clobTokenIds` 和 `outcomes` **有时是 JSON 字符串而不是数组**，必须 `isinstance(..., str)` 检查后 `json.loads`。

**修复位置**：[pm_trader.py:102-143](../../live/pm_trader.py#L102-L143)

**教训**：下游交易所的 API 约定不看文档，直接 `curl` 一个真实在售市场实测，比读 30 页 doc 更靠谱。新加 CLOB 接入时，第一步是把"从 slug 查到 token_id"的完整链路写成一个独立脚本 + 单元测试。

---

### 2. `predict_target` 就是市场开始时刻，不要自作聪明减 5 分钟

**症状**：slug 构造出来差一根 K 线，市场查不到；或者查到的市场是上一个。

**根因**：心智模型容易滑成"predict_target = 收到信号的时刻"，但约定上它是**模型要预测的那根 K 线的 open time**，也就是 Polymarket 市场的开始时刻本身。

对齐表（来自 `polymarket_trading_strategy.md` §三）：

| 步骤 | 值 |
|------|----|
| 刚收盘的 K 线 open_time | 16:30 |
| `predict_target` | **16:35**（下一根 K 线 open，= 市场开始时刻） |
| slug | `btc-updown-5m-{16:35.timestamp()}` |
| Polymarket 结算 | Chainlink 16:35 vs 16:40，`>=` 视为 Up |

**教训**：所有和交易所市场对齐的时间字段，必须在接入之初就写一份"时序对齐检查表"（`polymarket_trading_strategy.md` §三就是范例），永远用同一套命名。

---

### 3. Kronos 推理时 `amount` 必须无条件重算，不能读 raw CSV

**症状**：local 验证脚本 vs 远端 `predictions.csv` 的概率差最大 0.8（！）—— 方向都反。

**根因**：raw CSV 里的 `amount` 列是 Binance 上报的原始成交额，训练时预处理是用公式 `amount = volume × mean(o,h,l,c)` 重算的。两者在高振幅 K 线可以差一个数量级，经过模型一堆 zscore/token 化会放大到离谱。

**铁律**（已写入 [`CLAUDE.md`](../../CLAUDE.md)）：

```python
# 任何构造推理输入的代码（验证脚本 / live / 回测）必须这样写：
df["amount"] = df["volume"] * df[["open", "high", "low", "close"]].mean(axis=1)
# 不要读 df["amount"]
```

**配套的"推理一致性四要素"**：① `amount` 重算 ② per-window zscore `mean/std + 1e-5` ③ `clip=5` ④ 同一个 checkpoint。T / top_p / batch_size 不影响 classify 输出。

**修复位置**：[preprocessor.py:10-19](../../live/preprocessor.py#L10-L19)

**教训**：只要远端和本地预处理有任何一步写法不一致，先别怀疑模型、先 dump 出 `x_mean / x_std / x[0:5]` 逐项对比。90% 是预处理不对齐。

---

## 二、实盘必须解决的工程问题

### 4. 美国 IP 硬封禁 — 代码规避不了

**症状**：`POST /order` 返回 `403`，msg 里含 `geoblock` / `restricted` / `region`。

**根因**：Polymarket 协议层对美国出口 IP 有合规封禁。SageMaker 在 us-west-2，默认就是美国 IP。

**处理**：代码只能做**识别并优雅记录**（[pm_trader.py:305-321](../../live/pm_trader.py#L305-L321)），真实下单必须把 live 模块部署到非美节点（阿里云港/新/日）。

**教训**：接入任何 DeFi/CLOB 协议前，先测一次 `curl -X POST` 能不能过。把"出口 IP 合规性"当作部署前置条件，不是运行时问题。

---

### 5. 幂等：必须用 `pred_ts` 当键，必须在 dry-run 也生效

**症状**：WebSocket 断线重连后，同一根 K 线的 on_kline_closed 被触发第二次，同一个信号下了两次单。

**修复要点**（[pm_trader.py:54-65, 161-213](../../live/pm_trader.py#L54-L213)）：

1. 幂等键 = `int(predict_target.timestamp())`（Unix 秒），不是时间字符串（字符串 timezone 容易错）。
2. `_traded_ts: set[int]` 在内存里，但**进程启动时必须从 `pm_orders.csv` 恢复**，否则重启后白板。
3. 恢复时用 `int(float(row["pred_ts_utc"]))`—— float 兼容 `"123.0"` 这种格式。
4. **dry-run 也写 CSV 也查 set**。否则灰度期看起来一切正常，上真单立刻重复下。
5. `_traded_lock` + `_orders_csv_lock` 分开，避免 CSV 写慢了阻塞主逻辑。

**教训**：幂等不是"下单时的一个检查"，是"**整条链路的前置条件**"。恢复逻辑 + dry-run 一致行为，两件都做才叫幂等。

---

### 6. signature_type 和 funder 的配对

**症状**：签名报错、`create_or_derive_api_creds()` 失败。

**约定**（[pm_trader.py:72-92](../../live/pm_trader.py#L72-L92)）：

| 场景 | `signature_type` | `funder` |
|------|----|----|
| EOA（私钥直接签） | 0 | None |
| Polymarket Proxy / Gnosis Safe | 2 | Safe 合约地址 |

代码里是 `sig_type = 2 if funder else 0`，把这两个变量**绑成一个派生值**，不要允许外部同时设。

**教训**：任何两个必须联动的配置项，不要让用户分别填——派生一个，校验另一个。

---

### 7. 订单参数四件套

这几个参数第一次写的时候很容易拍脑袋，实测后结论：

| 参数 | 正确值 | 错了会怎样 |
|------|-------|-----------|
| `amount` | ≥ `_MIN_SIZE_USDC = 5.0` | Polymarket `orderMinSize=5` 是硬约束，低于这个值直接拒单。我们的 `refused_min_size` 状态码就是这个 |
| `OrderType` | `FAK`（不是 FOK） | FOK 深度不够时整单撤；FAK 有多少吃多少。流动性实测很好，但改一个字母换兜底，零成本 |
| `price` | 滑点上限，如 `0.80` | 不传 = 无上限，极端行情可能成交到离谱价。BUY 时 `price` 是"最坏可接受价" |
| implied_prob 预检 | midpoint > 0.80 直接 skip | 市场已经定价到头，下进去等于送人头。[pm_trader.py:252-262](../../live/pm_trader.py#L252-L262) |

**教训**：任何交易接入的"风控参数"都必须有**两道保险**——客户端预检 + 交易所层面的兜底。比如 implied>0.80 跳过（客户端） + price=0.80 上限（交易所撮合）。

---

## 三、模型与业务对齐的坑

### 8. 7 分类方向反转 Bug（P0，实际发生过）

**症状**：回测胜率正常，实盘方向全反，亏钱。

**根因**：7 分类标签 `0=strong_buy, 6=strong_sell`。`extreme_side_diff` 策略的 `score = p[0] - p[6]`，**score > 0 应该返回 UP**。但早期 signal_adapter.py 里写成了 `score > 0 → DOWN`。

**影响面**：`extreme_side_diff`、`prob_diff(7)`、`softmax_weighted` 都中招；`prob_diff(2)` 因为 2 分类 label 约定不同反而没事。

**修复参考**：以 [`lib/backtest/ensemble_pm.py`](../../lib/backtest/ensemble_pm.py) 为权威，里面 `sign = np.where((p0+p1) > (p5+p6), 1, -1)`（1=Up）是对的。

**教训**：
- **live signal adapter 和 backtest 必须共用同一个打分函数**，不要抄第二份实现。
- 上线前必须跑一次"同 checkpoint + 同 K 线 → 本地回测 vs live 推理 → 方向一致性"对账。

---

### 9. Chainlink vs Binance 基准差异 — 模型精度的天花板

**症状**：Binance 数据上 OOS 胜率 54%，上到 Polymarket 实测可能只有 53-53.5%。

**根因**：模型训练用 Binance BTCUSDT，Polymarket 用 Chainlink BTC/USD 数据流（多交易所聚合）。**正常偏差 <0.01%，但 5min 涨跌本身只有 0-0.3%**，偏差在幅度里占比不小。极端行情 0.1%+ 偏差很常见。

**处理**：
- 灰度期（PM_ENABLED=0）必须**同时记录 Chainlink 读数**，量化两个口径胜率差。
- 把"基准差异带来的胜率损失"当作不可消除的结构性成本，纳入盈亏平衡计算（Taker 51.8%，Maker 50%）。

**教训**：模型和结算所用的数据源不同，就是一条信息泄漏通道。接新市场时第一件事查"结算源是什么"，再决定训练数据要不要换。

---

### 10. Ensemble 投票里的 SKIP 归类

**症状**：评估指标异常高或异常低，找不到解释。

**根因**：SKIP 预测如果当作"错"算进分母，胜率会被拉低；如果当作"对"算，会被拉高。正确做法是**从分母里排除**。

**约定**（[eval_tracker.py:105-109](../../live/eval_tracker.py)）：`SKIP → None`（不计分），只对 UP/DOWN 算命中。

**下单策略**：
- `duo_v1/v2`：2 模型 consensus，任一 SKIP 即 final=SKIP，不下单。
- `trio_v1/v2/v3`：3 模型 majority，2/3 同向就下，1 个 SKIP 可接受。
- **2/3 同向 + 1 反对** 这档实际 sizing 是 `3.5 USDC × 1.0 = 5 USDC`（受 orderMinSize 约束）。

---

## 四、灰度与上线纪律

灰度顺序（写进 [`docs/analysis/polymarket_trading_strategy.md`](../analysis/polymarket_trading_strategy.md) §十三，务必遵守）：

| 阶段 | 配置 | 要做的验证 |
|------|------|----------|
| Week 1 | `PM_ENABLED=0` | dry-run 200+ 笔，对比 Chainlink vs Binance，算基准差异 |
| Week 2 | `PM_ENABLED=1 PM_SIZE_USDC=5` | 100 笔最小额，确认 fill rate + 成交价 vs 模拟一致 |
| Week 3+ | `PM_SIZE_USDC=5~10` + 投票强度分层 | 无异常后加码 |

**关键**：Week 1 的 dry-run 必须走**完整链路**（查市场 / 查 midpoint / 写 CSV），只差"签名+下单"两步。这是上面第 5 条的延伸 —— 否则灰度毫无意义。

---

## 五、上线前检查清单

- [ ] `POLYMARKET_PRIVATE_KEY` 有 `0x` 前缀，钱包有 USDC.e + POL gas
- [ ] `POLYMARKET_FUNDER` 与 `signature_type` 配对（有 funder=2，否则=0）
- [ ] `PM_SIZE_USDC ≥ 5.0`
- [ ] 出口 IP 非美国（`curl https://clob.polymarket.com/` 200 OK）
- [ ] `PM_SLUG_TEMPLATE` 用默认值 `btc-updown-5m-{ts}`（别自定义）
- [ ] 用一个历史 `predict_target` 跑通 `_find_market_tokens()` 返回合理的 Up/Down token_id
- [ ] 本地回测 vs live 推理方向一致（随机抽 20 根 K 线对账）
- [ ] `pm_orders.csv` 恢复逻辑测试过：写几条假数据，重启进程，`_traded_ts` 恢复
- [ ] Week 1 灰度计划写清楚 —— 多久、看什么指标、什么情况下停

---

## 六、一句话总结给接手人

> "Polymarket 的 API、合规、模型口径、投票方向，每一处直觉都错过一次。所有接入工作 = **先实测一个真实在售市场从 slug 到 token_id 到下单的完整链路，写进一个独立脚本，再往 live 里搬**。不要相信文档，不要相信我们之前写的代码，自己跑一遍。"

















# pm-autoresearch Skill — 迭代历史与踩坑沉淀

> 这套 skill 是"让 Claude 自己跑自主实验迭代"的系统：multi-agent debate 生假设 → 并行训练 → OOS 评估 → 归因 debate → 更新 leaderboard → 循环。
>
> 下面所有经验都是从**真实踩过的坑**里挤出来的。想让 skill 不出错，核心就是一件事：**把"人工约束"全部变成"自动化硬门禁"**。
>
> 关键位置：
> - [`.claude/skills/pm-autoresearch/SKILL.md`](../../.claude/skills/pm-autoresearch/SKILL.md)
> - [`.claude/skills/pm-autoresearch/phases/`](../../.claude/skills/pm-autoresearch/phases/)
> - [`lib/templates/VERSION_GUIDE.md`](../../lib/templates/VERSION_GUIDE.md) — "铁律"所在
> - [`docs/skill-autoresearch_errors_and_fixes.md`](../skill-autoresearch_errors_and_fixes.md) — 原始错误记录
> - [`docs/skill-kronos_autoresearch_ml_audit.md`](../skill-kronos_autoresearch_ml_audit.md) — ML 管线 audit

---

## 一、核心事故：为什么要有 Phase 1.9 Deep Review

B4–B6 这一轮，以及 bin_v2 / ml_lgb 系列，总共发生了下面这些"提交了训练才发现出错"的事故。每一次都是**训练跑完才知道白跑**。Phase 1.9 就是为了在"提交训练之前"拦下所有这类错误。

### 事故 1：`lib/` 公共框架被实验性代码污染

**发生了什么**：B4-B6 为了测 `cls_detach` / `cls_pool_size` / 多尺度分类头，直接在 [`lib/model/kronos_v3_0.py`](../../lib/model/kronos_v3_0.py) 和 [`lib/finetune/train_predictor.py`](../../lib/finetune/train_predictor.py) 里加了一堆 `[B4]/[B6]` 条件分支。

**为什么很糟**：
- `lib/` 是所有 version（bin_v1、bin_v2、7_v2、normal/v3_0…）共用的框架。
- 其他版本的训练行为被**隐式改变**，历史实验都复现不出来了。
- 实验失败后清理极难，死代码堆积。

**铁律**（[VERSION_GUIDE.md](../../lib/templates/VERSION_GUIDE.md) §一）：**"绝对禁止修改 `lib/`，任何代码变更 = 新建 version"**。

**现在怎么防**（Phase 1.9 Gate 1）：`git diff --name-only` 检查 `lib/` 和已有 `version/*/code/` 必须为空，有改动立刻 ABORT。

---

### 事故 2：mode=run 还是改了代码

**发生了什么**：用户启动参数 `mode=run`（明确说：只改 YAML），agent 在生成方案时还是往 `lib/` 写代码。

**根因**：Claude 对 "Run vs Version" 概念的拆分是文字约束，没有运行时拦截。

**现在怎么防**：
- [`phases/debate-design.md`](../../.claude/skills/pm-autoresearch/phases/debate-design.md) §13-31：debate 产出的**每个假设必须先分类为 Run 或 Version**，Lead agent 裁决时强制校验。
- Phase 1.9 Gate 1 兜底：mode=run 下任何代码 diff = ABORT。

---

### 事故 3：`cls_pool_size` 被 `from_pretrained` 静默覆盖

**发生了什么**：
1. YAML 里写 `cls_pool_size: 4`
2. `trainer.py` 读到 config，调 `ModelClass.from_pretrained(base_path)`
3. `from_pretrained` 从 base model 的 `config.json` 读取初始化参数（不含 `cls_pool_size`）→ 模型 `self.cls_pool_size = 0`
4. **训练正常完成，metrics 正常，但行为完全不对**

这是最隐蔽的一类 bug —— 没有报错，没有 NaN，只是安静地训了个错东西。

**修复方向**：建立 **model_kwargs 透传框架**。YAML 中所有非框架参数打包成 `config["model_kwargs"]`，trainer 用 `**config.get("model_kwargs", {})` 直接传给 `from_pretrained`。**禁止白名单模式**（`if config.get('cls_pool_size'): kwargs['cls_pool_size'] = ...`）—— 白名单必漏。

**现在怎么防**：
- Phase 1.9 Gate 2：用 `inspect.signature(ModelClass.__init__)` 检查 YAML 里的每个 `model.*` 参数都能被接收，遗漏立即 ABORT。
- Phase 1.9 Layer 2 R2（训练流审查）：审 `from_pretrained` 调用点是否用了 `**model_kwargs` 模式。

---

### 事故 4：`version/{path}/code/` overlay 没生效

**发生了什么**：新建 `version/bin_v2/code/finetune/trainer.py`，但运行时 `from finetune import trainer` 还是加载了 `lib/finetune/trainer.py` —— `PYTHONPATH` 里 `version/code` 没排在最前。

**结果**：代码改了，实际跑的是老代码。一个 batch 的实验全是假的。

**现在怎么防**：
- [`run_local.sh`](../../lib/run_local.sh)：`PYTHONPATH="{version}/code:$PYTHONPATH"` 写死。
- Phase 1.9 Gate 3：模拟 overlay，检查 `from finetune import trainer` 实际加载路径，必须来自 `version/{path}/code/`。

---

### 事故 5：eval_range 和 train_range 重叠（数据泄漏）

**发生了什么**：Batch 1 有 3 个实验的 `pred_ranges` 设成了验证集区间 `2025-11-01~28`，而不是真正 OOS `2025-11-29~12-31`。结果 OOS acc 虚高，归因 debate 基于假数据做了错误推论。

**根因**：没有校验脚本；config 写在 runs/ 下，随手能复制粘贴出错。

**现在怎么防**：
- `dataset.yaml` 是三个区间的**唯一 source of truth**（[memory: project_data_alignment](../../../..)）。ML config 不允许手写 range。
- Phase 1.9 Gate 4：train / val / eval range 三者不允许重叠，任何重叠 ABORT。

---

### 事故 6：ML 管线的三重时间穿越

ML 管线（LightGBM 等）独立实现，踩了一套自己的坑：

| 编号 | 症状 | 根因 | 修复 |
|------|------|------|------|
| P0-1 | 训练样本数比预期少 1 行，无报错 | `df["close"].shift(-1)` 标签最后一行 NaN，LightGBM 静默忽略 | `build_labels()` 后显式 `dropna()` + 跨区间边界断言 |
| P0-2 | 短 eval 区间准确率不稳定 | Rolling window 预热期（288 bars）的特征全 NaN，被 dropna 丢掉 | eval 数据前扩展 `max_window` 行用于预热，不参与指标计算 |
| NEW-P0-1 | ML 版 OOS acc 虚高 | `_target_encoded_time()` 在 eval 上**独立 fit**，用了 eval 自己的标签统计量 | fit-on-train, freeze-apply-to-eval，不允许 eval 上 fit |
| NEW-P0-3 | ML early stop 挑到过拟合的点 | 用 eval 前 20% 做 early stop | 用 val 尾段做 incremental validation |

**现在怎么防**：
- [`docs/skill-kronos_autoresearch_ml_audit.md`](../skill-kronos_autoresearch_ml_audit.md) 列出所有 P0/P1/P2 条目。
- Phase 1.9 Gate 4 扩展：dataset 完整性检查 + train/val/eval 分离断言。
- [SKILL.md 约束 14]：**ML version 的 `code/` 必须完全自包含**，不允许 `import lib.*`（AST parse 可检测）。

---

## 二、Phase 1.9 Deep Review 的设计逻辑

两层防线，一层 100% 确定性，一层补语义。

### Layer 1：自动化硬门禁（4 gates）

| Gate | 检查 | 防的事故 |
|------|------|---------|
| 1 代码隔离 | `git diff lib/ version/{path}/code/` 必须为空 | 事故 1, 2, 4 |
| 2 模型参数签名 | YAML `model.*` 每项都在 `ModelClass.__init__` 签名里 | 事故 3 |
| 3 Trainer 加载路径 | 模拟 `from finetune import trainer`，来源必须是 `version/{path}/code/` | 事故 4 |
| 4 Config 完整性 | dataset 存在、exp_no 唯一、train/val/eval 不重叠 | 事故 5, 6 |

**任一 FAIL 立即 ABORT**，没有人工干预空间。

### Layer 2：3 个并行 Agent 审查

补 Layer 1 抓不到的语义问题：

| 审查者 | 聚焦 | 来自教训 |
|--------|------|---------|
| R1 假设-代码一致性 | debate 提的每个假设是否精确对应到生成的 config / code | 避免 agent "知行不一"（嘴上改 A 实际改 B） |
| R2 训练流 | forward 签名、loss 逻辑、梯度流、`from_pretrained` kwargs | 事故 3 的语义兜底 |
| R3 推理一致性 | classify 路径、checkpoint 参数持久化、本地与远端对齐 | Polymarket 实盘的 `amount` 对齐问题 |

**纯超参 Run 简化**：Gate 3 和 R2/R3 中"代码相关"的项可 SKIP（因为没改代码），Gate 2 仍必跑（防 YAML 拼写错）。

---

## 三、其他重要经验

### 并行训练调度：2×4 优于 1×8

早期是 8 卡 DDP 跑一个实验，串行 3 个 → wall-clock 50 min，GPU 利用率 100% 但吞吐只有 3/h。

评估过 4 种方案后选了 **2 卡 DDP × 4 组并行**（[`phases/train-and-monitor.md`](../../.claude/skills/pm-autoresearch/phases/train-and-monitor.md) §2b-2d）：

- 每 epoch ~11 min，8 epoch 总 88 min
- 4 组同时出结果 → 一次性做批量归因（给 debate 提供横向对比信息）
- `MASTER_PORT = 29500 + GPU_START` 隔离 DDP，防冲突
- `wait -n` 事件驱动：某组完成立即补队列（队列非空时）

**经验**：多实验并行做出来的价值不仅是节省时间，更是**让归因 debate 有横向对比的对照组**，质量比串行高一档。

### Multi-Agent Debate 质量保证

从单 agent 列清单 → 3 专家独立提案 → 交叉 critique → Lead 裁决，主要抓的是"正交性"和"信息增益"：

3 个固定视角（防止 agent 重复）：
1. 正则化 & 泛化专家（dropout / weight_decay / label_smoothing）
2. 优化器 & 训练动态专家（lr / schedule / batch_size / gradient）
3. 数据 & 领域专家（lookback_window / class imbalance / BTC 非稳态）

Lead 的自检清单（[`phases/debate-design.md`](../../.claude/skills/pm-autoresearch/phases/debate-design.md)）：
- ☐ 每个假设引用至少 1 个历史实验作依据
- ☐ 每个假设与已验证规律不矛盾
- ☐ 是否有验证性实验（复核归因假说）
- ☐ 是否有探索性实验（与历史正交的新方向）
- ☐ 连续 2 批无提升时，必须有反直觉实验

### 归因 debate：失败样本比成功样本更值钱

早期归因总结只做浅层对比表，B4 batch "2/3 实验 neutral" 后只有"无效"结论。改进后：
- 每个 neutral/negative 结果必须有**因果链假说**（为什么无效）
- 超参之间的**交互效应**是首要关注点（不是单变量分析）
- 统计显著性必须附 CI / effect size（没到显著就别当结论）

### Debate 记录的文件规范

**每次 debate 独立一个文件，不追加**。命名：
```
debates/batch_{NNN}__{version_short}__design.md
debates/batch_{NNN}__{version_short}__attribution.md
```

**经验**：追加模式在长期迭代下不可读，独立文件 + grep 便于复盘某一轮 debate 的原始论证。

---

## 四、用户反馈反复出现的模式

这些都写进了 auto-memory（`~/.claude/projects/-home-sagemaker-user-tough-code/memory/`）。**真正的问题不是单次事件，是 Claude 反复犯同一类错**：

| Memory 条目 | 反复犯的错 |
|-------------|----------|
| `feedback_code_isolation.md` | 想在 `version/xxx/code/` 之外"小改"一下 lib/ |
| `feedback_rename_cleanup.md` | 重命名后不清理旧文件 |
| `feedback_session_stop_not_delete.md` | agent-deck `stop` 错当成 `rm`（毁掉会话历史） |
| `feedback_skill_edit.md` / `skill_no_approval.md` | 改 skill 反复找用户确认，没必要 |
| `project_model_kwargs_fix.md` | 用白名单模式透传参数（事故 3 根因） |
| `project_data_alignment.md` | `dataset.yaml` 是唯一 source of truth，ML config 不写 range |

**教训**：**文字约束不够，得加硬门禁**。这些 feedback 里的所有项，最终都体现在 Phase 1.9 的 gate 和 R1/R2/R3 清单里。

---

## 五、已知局限（还没完全解决）

1. **Debate 产垃圾假设**：Layer 2 审代码不审假设的物理意义。一个"在逻辑上无效但代码写对的"假设会通过所有 gate。
   - 缓解：多 agent + Lead 自检清单，但没法 100% 防。

2. **异常 batch_size 的脚本生成**：模板假设 `batch_size=4`，8 或 12 时需要手改。

3. **GPU OOM 自动降速**：单实验 OOM → 无自动 `batch_size ↓ / grad_accum ↑` 机制。

4. **Template 和实际代码漂移**：[`templates/ml_train_skeleton.py`](../../.claude/skills/pm-autoresearch/templates/ml_train_skeleton.py) 与 `ml_lgb_v3/code/train.py` 可能不一致，新建 version 从旧 template 启动，老 bug 会被复制。

5. **Session 跨批次上下文**：debate 现在用的是"最近 3 批归因摘要 + 超参 sensitivity 矩阵"，但长周期规律（10+ batch 后）没有专门沉淀机制。

---

## 六、一句话给下一个维护者

> "**Skill 的可靠性 = Gate 的覆盖率**。任何一次事故的正确反应不是改 prompt，是问'这个错 Phase 1.9 为什么没拦下来，再加个什么 gate'。"

后续加固清单见 [`skill-hardening-checklist.md`](./skill-hardening-checklist.md)。











# pm-autoresearch Skill 设计分析与可借鉴点

> 阅读对象：想理解这个 skill 为什么这么设计、或打算写类似「自主实验迭代」skill 的人。
> 本文不是 how-to，是 **why & what-to-learn**。

---

## 目录

1. [一句话定位](#一句话定位)
2. [整体架构图](#整体架构图)
3. [七大核心价值点（值得学习）](#七大核心价值点值得学习)
4. [关键机制拆解](#关键机制拆解)
5. [可迁移到其他 skill 的设计范式](#可迁移到其他-skill-的设计范式)
6. [当前设计的权衡与隐忧](#当前设计的权衡与隐忧)
7. [一页纸总结](#一页纸总结)

---

## 一句话定位

> **用「多 Agent Debate」替代「硬编码实验列表」，把超参/架构搜索变成一个「假设—验证—归因」的永动机，OOS accuracy 是唯一裁判。**

区别于传统 AutoML / grid search：
- **传统**：在预先定义的参数格子里穷举，靠「覆盖」取胜。
- **本 skill**：靠 LLM 专家视角的正交组合产出「少而精」的假设，靠归因 debate 产出因果链驱动下一批方向，靠「永不停止」的循环逼近 OOS 上限。

---

## 整体架构图

```
               ┌──────────────────────────────────────────────┐
               │   Phase 0  初始化 / 环境 / 数据集 / 历史加载  │
               └───────────────────┬──────────────────────────┘
                                   │
                                   ▼
       ┌─────────────────────────────────────────────────────────┐
       │  Phase 1: Multi-Agent Debate 「设计」                   │
       │  ─────────────────────────────────                      │
       │  Agent 正则化 ┐                                          │
       │  Agent 优化器 ├─ R1 独立提案 → R2 交叉 critique → R3 Lead 裁决 │
       │  Agent 数据   ┘                                          │
       │  产出：8~12 个候选假设（带因果链 + 信息增益评估）       │
       └──────────────────────┬──────────────────────────────────┘
                              │ 落盘 debates/batch_NNN__version__design.md
                              ▼
       ┌─────────────────────────────────────────────────────────┐
       │  Phase 1.9 Deep Review（两层防线）                      │
       │  Layer 1 硬门禁：代码隔离 / 参数签名 / 加载路径 / config │
       │  Layer 2 Agent 审查：R1 假设-代码映射 / R2 训练流 / R3 推理一致性 │
       │  任一 FAIL → ABORT（不烧 GPU）                           │
       └──────────────────────┬──────────────────────────────────┘
                              │
                              ▼
       ┌─────────────────────────────────────────────────────────┐
       │  Phase 2-3: 事件驱动并行训练（2卡DDP × 4组 = 8卡）       │
       │  BATCH_DONE → S3 同步 → 早期无效检测主动 kill            │
       └──────────────────────┬──────────────────────────────────┘
                              │
                              ▼
       ┌─────────────────────────────────────────────────────────┐
       │  Phase 4: 下载 + 评估 + 月度分解 + 双向阈值胜率分析       │
       │  落盘 report.md（纯事实，无归因）                         │
       └──────────────────────┬──────────────────────────────────┘
                              │
                              ▼
       ┌─────────────────────────────────────────────────────────┐
       │  Phase 5: Multi-Agent Debate 「归因」                   │
       │  统计视角 / ML 视角 / 领域视角 × 3                       │
       │  产出：每个实验的因果链、交互效应、下一批方向推荐         │
       └──────────────────────┬──────────────────────────────────┘
                              │ 落盘 debates/batch_NNN__version__attribution.md
                              ▼
       ┌─────────────────────────────────────────────────────────┐
       │  Phase 6-7: 更新 research_log / leaderboard → 决策下一批 │
       │  队列 < 阈值时触发新 debate 补充候选 → 永不停止           │
       └─────────────────────────────────────────────────────────┘
```

---

## 七大核心价值点（值得学习）

### ① Debate 驱动假设生成（Phase 1）

**核心创新**：把「想下一批做什么实验」这件事本身，变成一个 **3 专家并行独立思考 + 交叉 critique + Lead 裁决** 的多轮过程，而不是从硬编码列表里捞。

为什么强：

| 传统做法的问题 | Debate 解法 |
|---|---|
| 清单容易遗漏冷门方向 | 3 个正交视角（正则化 / 优化器 / 数据）各自独立思考 |
| 假设质量参差不齐 | 每个假设必须说「解决哪个已知问题、与已有实验有何本质区别」 |
| 容易重复做本质相同的实验 | Round 2 交叉 critique 显式要求标记「本质重复」 |
| 无法处理超参交互效应 | Round 3 Lead 裁决以「信息增益 + 正交性」为标准 |

**可迁移**：凡是涉及「从无限可能中选 N 个最值得做的」场景（Bug 根因候选、架构候选、用户测试问题），都可以套这个 R1→R2→R3 三段式。

---

### ② 失败与成功同等重要（Phase 5 归因 debate）

**核心理念**：「negative 实验不是浪费 GPU，是信息」。Phase 5 强制对每个 negative/neutral 结果产出 **因果链** 而非结论标签。

```
不接受：这个实验无效 ❌
要求：  lr ↓ → 收敛到 sharp minimum → val gap 扩大 → OOS 降 0.8pp ✅
```

归因 debate 的自检清单：
1. 每个 negative 实验是否有「为什么无效」的因果链？
2. 因果链是否有具体数据支撑（而非抽象理论）？
3. 下一批方向是否**直接来源于**归因结论（而非拍脑袋）？
4. 是否有**可证伪的预测**（下一批结果能验证/推翻假说）？
5. 交互效应是否被考虑？

**可迁移**：任何迭代优化场景（产品 A/B test、性能调优、CTR 优化），默认的「记录结果」升级成「记录因果链」，长期来看经验资产会指数级累积。

---

### ③ Phase 1.9 Deep Review：提交前的「两层防线」

**痛点来源**：历史上四次血的教训写在 deep-review.md 开头——
- SWA overlay bypass：代码其实没生效，烧了 8 卡 × N 小时
- cls_pool_size 静默丢失：`from_pretrained()` 悄悄忽略未识别 kwargs
- lib/ 污染：改错层影响所有 version
- NIP_OC 假阳性：校准问题导致 OOS 虚高

**防线设计**：

| 层 | 特点 | 示例 |
|---|---|---|
| Layer 1 Hard Gate | bash/python 脚本，100% 确定性，能检出就绝不漏 | `git diff lib/` 非空 → 直接 ABORT |
| Layer 2 Agent Review | 3 个审查 agent 并行，覆盖语义层面 | R1 假设-代码一致性、R2 训练流、R3 推理一致性 |

**关键设计哲学**：**能用硬门禁做的事不要交给 agent**。硬门禁是"烂好人防御"，agent 审查是"专家复核"，两者互补。

**可迁移**：所有「执行前无法撤销」的 skill（部署、删数据、发消息）都应该有硬门禁层。agent 审查是锦上添花，硬门禁是保命。

---

### ④ 代码隔离的「铁律」（Version vs Run）

```
Version = 代码（frozen code/） 
Run    = 配置（只有 config.yaml）
lib/   = 所有版本共用（任何版本都不能改）
改代码 → 必须新建 version，不能改已有 version/code/
```

为什么这是铁律：
- **可复现性**：老 run 的 config 永远能用它所属 version 的 code 复现
- **归因清晰**：指标差异只会源于 config，不会源于「某天偷偷改了 trainer.py」
- **防污染**：一个 version 的 bug 不会传染其他 version

**model_kwargs 透传**也是同一个理念的延伸：
- 禁止白名单：`model_cls(lr=config['lr'], wd=config['wd'], ...)`
- 强制透传：`model_cls(**config.get('model_kwargs', {}))`
- 新增模型参数只需改 YAML + `__init__`，trainer **零改动**

**可迁移**：任何有"共享组件 + 定制实验"的工程（feature flag 框架、插件系统、CI pipeline 模板），这套 Version/Run/lib 三分格局都能搬。

---

### ⑤ Debate 落盘的强制性 + 跨批次信息传递

**设计**：每次 debate 独立一个 md 文件（`debates/batch_NNN__version_short__{design|attribution}.md`），不追加。

好处：
- **可读**：每次 debate 独立文件，不会演化成 10000 行巨型日志
- **可追溯**：`ls debates/batch_*_attribution.md | tail -3` 就能拿到最近 3 批的 Lead 裁决
- **强制执行**：每次写入后都有 `test -f` 验证，"不可跳过"

**跨批次传递的信息链**：

```
Batch N Phase 5 归因 debate
    ↓ （Round 3 下一批方向推荐）
Batch N Phase 7 决策
    ↓ （baseline 可能更新）
Batch N+1 Phase 0 加载历史
    ↓
Batch N+1 Phase 1 debate：上下文 = baseline + 已验证规律 + 最近 2~3 批归因
    ↓
Agents 可以挑战、修改、扩展上一批的方向（不是被动执行）
```

**可迁移**：「有记忆的 agent workflow」——不是靠长 context，而是靠 **结构化落盘 + 选择性加载**。

---

### ⑥ 两个管线共存的优雅扩展（Kronos vs ML）

skill 一开始只做 Kronos（深度 Transformer），后来加了 ML 管线（LightGBM/CatBoost/MLP/Stacking）。扩展方式值得学习：

| 维度 | Kronos | ML | 共享 |
|---|---|---|---|
| Phase 指令 | `phases/*.md` | `phases_ml/*.md` | - |
| 远程目录 | `/home/sagemaker-user/kronos/` | `/home/sagemaker-user/kronos_ml/` | - |
| 版本目录 | 依赖 `lib/` | **完全自包含** `code/` | - |
| 模板 | `config.yaml`, `run_batch.sh` | `ml_config.yaml`, `run_ml_batch.sh`, `ml_*_skeleton.py` | - |
| 只读资源 | - | - | `data/raw/` CSV, `leaderboard.md` |

**设计点**：
1. **自动检测**：没显式指定 `pipeline=ml` 时，从 `version.yaml` 或 `code/` 结构推断
2. **指令文件对称复制 + 路径替换**：phases/ 和 phases_ml/ 同名文件，内容因管线不同而变化，主 skill 只需一个路由表
3. **严格目录隔离**：不共享目录就不会相互污染，问题定位简单

**可迁移**：skill 的「多种策略分支」可以靠「平行的指令目录 + 路由」优雅扩展，而不是在主文件里写 if/else 嵌套到地狱。

---

### ⑦ 事件驱动的并行训练调度

不是"一次开 4 个训练，等全部结束再开下一批"（浪费 GPU），而是：
- **GPU 分组**：`0,1 | 2,3 | 4,5 | 6,7` 四组，每组 2 卡 DDP，`MASTER_PORT` 隔离
- **事件驱动补位**（SKILL.md Phase 2-3）：`wait -n` 检测任一实验完成立刻补下一个，队列不空不停
- **早期无效检测 + 主动 kill**：val_acc 停滞 / class 坍缩 / loss 爆炸时主动 pkill，不空耗 GPU
- **队列低于阈值触发新 debate**：剩余 ≤ 2 时发起新 debate 补充 8~12 候选 → 永远不空转

**可迁移**：任何有"固定资源池 + 可拆解任务"的工作流（CI 并发构建、爬虫、批处理 ETL），这套 GPU 分组 + 事件驱动 + 主动淘汰的模式都适用。

---

## 关键机制拆解

### A. Debate 上下文的「分层披露」

Phase 1 debate 在 mode=run vs mode=version 时，**agents 能看到的信息范围不同**：

| 信息 | mode=run | mode=version |
|---|---|---|
| 当前 version research_log + config | ✅ | ✅ |
| 最近 2~3 批归因 debate | ✅ | ✅ |
| 当前 version 的 code/ 源码 | ❌ | ✅ |
| 跨版本 leaderboard | ❌ | ✅ |
| 其他 version 的 research_log | ❌ | ✅ |
| 模型 __init__ 签名 | ❌ | ✅ |

**为什么重要**：给 agent 更多信息 ≠ 更好决策。mode=run 只需要调超参，给 agent 看代码反而诱导它超出权限提"改 forward()"的建议。**渐进式披露减少决策噪声**，这是 prompt 设计的核心技巧。

---

### B. 纯事实 report.md vs 归因 debate 的分离

Phase 4 写 `runs/{exp_no}/report.md` → **纯事实**（指标、月度分解、阈值胜率），**无 verdict/归因**。
Phase 5 归因写到 `debates/batch_*_attribution.md` → **因果链 + 交互效应 + 方向推荐**。

**好处**：
- report.md 是实验本身的客观档案，后续任何人翻看都不会被"当时的归因结论"误导（归因可能错）
- 归因可以随着后续批次的发现被修正，但事实不会
- 报告格式天然对齐 → 后续批次归因 debate 可以批量加载多个 report.md 做横向对比

**可迁移**：任何"事实 vs 解读"分离的文档场景（监控告警 vs 复盘报告、测试结果 vs 分析结论）都值得学。

---

### C. 「绝不依赖 in-memory」的持久化纪律

SKILL.md 和各 phase 反复强调：

> 归因 debate 必须显式读取 report.md + design debate + research_log + 上一批归因。**不依赖 Phase 4 的 in-memory 产出**。

为什么：
- Claude 的 context 会被压缩，"记得"并不可靠
- 持久化到磁盘后，即便中途 session 崩溃重启也能接续
- 逼迫每个 phase 的契约（输入/输出文件）显式化，debug 时能 `cat` 看到

**可迁移**：多步骤 agent workflow 应当像 airflow DAG 一样——每一步的产出都落盘，下一步只从磁盘读，不依赖 in-memory 变量。

---

### D. 统计严谨性约束

每个结论必须附带：
- **95% CI**（Wilson interval for proportion）
- **Effect size**（Cohen's h）
- **Statistical power** 估计
- **样本量建议**（达到 power=0.8 需要多少 OOS 样本）

不允许说「这个实验比 baseline 好」，只能说「在 n=X 下提升 Δpp，95% CI=[lo,hi]，h=y，显著性 Y/N」。

**可迁移**：A/B test、性能 regression 对比、模型评估——都应该强制附带统计量，而不是「看着好就是好」。

---

## 可迁移到其他 skill 的设计范式

| 范式 | 本 skill 的实现 | 适用场景 |
|---|---|---|
| **Phase 化 + 子文件加载** | SKILL.md 只列 Phase 概览，详细指令在 `phases/{name}.md` | 任何多阶段 skill（发版、迁移、重构、审查） |
| **平行指令目录路由** | `phases/` vs `phases_ml/`，主 skill 靠 pipeline 参数路由 | 有多种策略分支的 skill |
| **硬门禁 + Agent 审查两层防线** | Layer 1 bash/python + Layer 2 3 审查者 | 任何「执行前不可撤销」的 skill |
| **多 agent 独立提案 + 交叉 critique + Lead 裁决** | Phase 1 / Phase 5 debate | 从大空间选 top-N 的场景 |
| **落盘强制 + 跨调用信息传递** | `debates/batch_*.md` | 需要长期积累经验的 agent workflow |
| **纯事实 vs 归因分离** | report.md vs attribution debate | 任何有"事实记录 + 解读"的场景 |
| **渐进式披露** | mode=run/version 给 agent 的信息范围不同 | prompt 工程 — 少即是多 |
| **永不停止循环 + 队列补位** | Phase 7 回到 Phase 1 + 事件驱动 `wait -n` | 长期自主任务 |
| **统计严谨性硬约束** | 95% CI + effect size + power | 所有对比类工作 |
| **子目录 + results 镜像** | `version/{path}/` 和 `results/{version_key}/` 结构对称 | 代码产物分离的项目 |

---

## 当前设计的权衡与隐忧

诚实地讲，这份 skill 不是没有代价：

### 1. LLM token 成本较高

每个 batch 要做 2 次 debate（设计 + 归因），每次 3 个 agent × 3 轮 = 9 次 LLM 调用，再加 Deep Review 的 3 个审查 agent。**一个 batch 光 agent 调用就 ~15 次**，加上 Phase 1.9 的 python 执行 —— 对上下文和预算都有压力。

**缓解**：在 SKILL.md 里要求「精简 context」，agent 只读必要文件，debate 落盘后下一批只读 Round 3 裁决。

### 2. Agent 共识 ≠ 正确

「2+ agent 共识的假设优先」是一个启发式，但 3 个 agent 可能有相同的盲点（比如 LLM 都偏爱学习率调整而忽略数据层改进）。

**缓解**：用户可以通过 `research_directions` 参数注入种子方向，agents 可以挑战但不能无视。

### 3. Phase 之间耦合太多隐式约定

例如 exp_no 命名规范、run 目录结构、落盘路径——任一 phase 改命名，其他 phase 都会 silently 出错。Phase 1.9 的硬门禁能挡一部分，但不全。

**缓解**：值得把这些约定显式抽出为一个 `CONVENTIONS.md`，所有 phase 共享引用。

### 4. Debate 有时会「自我重复」

如果 3 个专家都在同一个知识库训练出来，他们的独立思考可能并不独立，Round 2 critique 也可能只是同义复述。

**缓解**：设计 agent 时**不同的 system prompt 视角** + 后续批次显式告诉 agents「不要重复提 X」。

### 5. 没有显式的「提前终止」机制

max_rounds 到之前永远循环，但如果连续 5 批 neutral，靠归因 debate 自己找新方向并不总能奏效，可能陷入局部最优的鞍点。

**缓解**：Phase 7 的「连续 2 批无提升 → 建议新建 version」是一个软触发，但没有硬停止。

---

## 一页纸总结

**这个 skill 真正在教的三件事：**

### 1. 「让 LLM 做它擅长的事，让程序做它擅长的事」
- LLM 擅长：从巨大可能性空间里产生有根据的候选（debate 提案、归因）
- 程序擅长：确定性检查（硬门禁）、资源调度（队列）、统计计算（CI）
- 本 skill 的高光在于清晰区分两者并协作

### 2. 「经验资产 = 因果链的累积，不是结果的累积」
- 传统 log：「实验 X 得到 Y」
- 本 skill：「实验 X，因为 A→B→C，所以得到 Y，下一步应该验证 B' 假说」
- 后者一年后还有用，前者一周后就忘了

### 3. 「防御层级化：硬门禁 → Agent 审查 → 监控 kill → 归因修正」
- 每一层有各自的擅长区间
- 没有单一层能解决所有问题
- **组合使用才能既快速又安全**

---

## 附：相关文档

- 主 skill 文件：`.claude/skills/pm-autoresearch/SKILL.md`
- Phase 指令：`.claude/skills/pm-autoresearch/phases/*.md` 和 `phases_ml/*.md`
- 模板：`.claude/skills/pm-autoresearch/templates/{config.yaml, run_batch.sh, ml_*}`
- 历史教训：`docs/lessons/pm-autoresearch-lessons.md`（具体 bug 案例）
- 本文：设计哲学与可迁移范式