# AIClaw 二期

---

## Stage 3：环境已知 → 识别价格结构

> 在环境判断（Stage 2）完成后，进入价格结构识别阶段。本阶段包含两套互补的分析框架：**缠论**（东方技术分析体系）和 **SMC**（西方 Smart Money Concepts），共同作为 LLM 决策的结构化输入。

### 3.1 缠论引擎（chan_engine.py）

这是在开源交易工具中比较少见的**完整缠论流水线**：

```
原始 K 线
  → 包含关系合并
  → 严格交替顶底分型
  → 笔（最小跨度约束）
  → 中枢（连续三笔区间重叠，ZG=min_highs，ZD=max_lows）
  → MACD 柱面积比 + 幅度比（笔力度对比）
  → 未完成笔检测（实时推演）
```

**设计哲学**：有意**不做多空判断**，只输出结构化的笔 / 中枢 / 力度数据，把"怎么用"的决策权交给 LLM。这比硬编码"三买三卖"更灵活，适合作为多维度信号的一个输入源。

### 3.2 SMC 结构分析（BOS / CHoCH / OB / FVG）

基于最近摆动序列的简化 **Smart Money Concepts**：

| 概念 | 全称 | 定义 |
|------|------|------|
| 摆动识别 | Swing Structure | window 根 K 线为半径的局部极值，标注 HH / HL / LH / LL |
| BOS | Break of Structure | 上行结构中价格突破最后一高点 = 看涨延续 |
| CHoCH | Change of Character | 上行结构跌破最后一低点 = 看跌反转 |
| Order Block | OB | 冲动 K 线（实体占全幅 >60% 且量 > 均量 ×1.3）的前一根反色 K 线区间 |
| FVG | Fair Value Gap | 三根 K 线的典型缺口结构 |
