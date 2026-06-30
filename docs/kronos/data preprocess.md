# 数据处理 Pipeline 详解

本文档完整描述从**原始 K 线数据下载**到**模型训练可用的 PyTorch 张量**的全链路数据处理过程。

---

## 全局流程总览

```
Binance Vision 网站
        │
        ▼
  ① 下载原始 K 线 ZIP
        │
        ▼
  ② 解压 → 合并 → 标准化 CSV
        │
        ▼
  ③ 打标（7 分类标签）
        │
        ▼
  ④ 添加 symbol 列 → Qlib 二进制转换
        │
        ▼
  ⑤ Qlib 加载 → 特征工程 → 时间切分 → Pickle
        │
        ▼
  ⑥ 滑动窗口 → 归一化 → 采样 → PyTorch 张量
```

每一步的输入、输出和关键参数在下面逐一说明。

---

## 第一步：原始 K 线数据下载

**工具**：`Kronos_hyper/tools/binance.py`

**做什么**：从 Binance Vision（币安的历史数据公开站点）批量下载 K 线数据的每日压缩包。

**数据来源**：
- 现货（Spot）：`https://data.binance.vision/data/spot/daily/klines`
- U 本位合约（Futures-UM）：`https://data.binance.vision/data/futures/um/daily/klines`
- 币本位合约（Futures-CM）：`https://data.binance.vision/data/futures/cm/daily/klines`

**关键参数**：
| 参数 | 含义 | 示例 |
|---|---|---|
| `market` | 市场类型 | `spot` / `futures_um` |
| `symbol` | 交易对 | `BTCUSDT` |
| `interval` | K 线周期 | `5m`（5 分钟线） |
| `start-date / end-date` | 下载日期范围 | `2025-01-10` ~ `2025-11-30` |

**输出文件**：
```
binance_data/
└── spot/
    └── BTCUSDT/
        └── 5m/
            ├── *.zip              ← 每日一个压缩包
            ├── extracted/*.csv    ← 解压后的原始 CSV
            └── BTCUSDT_5m_qlib.csv  ← 合并后的标准化 CSV
```

---

## 第二步：合并与标准化

**做什么**：将所有解压后的每日 CSV 文件合并为一个完整的 CSV，并统一格式。

**处理细节**：

1. **时间戳解析**：Binance 原始数据的 `open_time` 是毫秒级整数时间戳，自动推断精度（毫秒/微秒/秒），转换为标准 datetime。

2. **列筛选与命名**：只保留 `date, open, high, low, close, volume` 六列，丢弃 Binance 特有的 `quote_asset_volume`、`number_of_trades` 等冗余字段。

3. **数据类型规范化**：OHLCV 全部转为数值类型（Binance CSV 里可能是字符串），无效值 `dropna`。

4. **添加 factor 列**：固定为 `1.0`。股票数据需要复权因子，BTC 没有分红/拆股，所以始终为 1。

5. **按时间排序**：确保 K 线严格按时间升序排列。

**输出文件**：`BTCUSDT_5m_qlib.csv`

| date | open | high | low | close | volume | factor |
|---|---|---|---|---|---|---|
| 2025-01-10 00:00:00 | 94500.0 | 94600.0 | 94400.0 | 94550.0 | 123.45 | 1.0 |
| 2025-01-10 00:05:00 | 94550.0 | 94700.0 | 94500.0 | 94650.0 | 98.76 | 1.0 |
| ... | ... | ... | ... | ... | ... | ... |

---

## 第三步：打标（生成 7 分类标签）

**工具**：`Kronos_hyper/labeling/generate_enhanced_labels.py`，底层调用 `labeled_algo.py`

**做什么**：对每根 K 线，利用**未来窗口**内的价格走势，计算该时刻的最优操作方向和强度，生成 0~6 的整数标签。

**核心逻辑**（详见 `打标逻辑详解.md`）：
1. 在未来 `window_size`（默认 12）根 K 线内，找最高价和最低价
2. 计算潜在上涨/下跌幅度
3. 基于阈值（默认 0.2%）和强弱比例参数，分为 7 级
4. 分形过滤进一步修正信号强度

**标签映射**：

| 标签值 | 含义 | 说明 |
|---|---|---|
| 0 | strong_buy | 强买入信号 |
| 1 | weak_buy | 弱买入信号 |
| 2 | neutral_bull | 偏多中性 |
| 3 | neutral | 中性（含 invalid） |
| 4 | neutral_bear | 偏空中性 |
| 5 | weak_sell | 弱卖出信号 |
| 6 | strong_sell | 强卖出信号 |

**关键参数**（来自 `base.yaml` 的 `labeling` 部分）：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `label_window_size` | 12 | 未来窗口大小（K 线根数） |
| `label_threshold_percent` | 0.002 | 涨跌阈值（0.2%） |
| `label_fractal_window` | 12 | 分形过滤窗口 |
| `label_fractal_tolerance` | 0.001 | 分形过滤容差 |
| `label_weak_ratio` | 0.4 | 弱信号阈值比例 |
| `label_neutral_ratio` | 0.1 | 中性信号阈值比例 |

**输出文件**：`enhanced_labels_BTCUSDT_5m_qlib_w12_t0.0020_*.csv`

| date | open | high | low | close | volume | label |
|---|---|---|---|---|---|---|
| 2025-01-10 00:00:00 | 94500.0 | 94600.0 | 94400.0 | 94550.0 | 123.45 | 3 |
| 2025-01-10 00:05:00 | 94550.0 | 94700.0 | 94500.0 | 94650.0 | 98.76 | 0 |
| ... | ... | ... | ... | ... | ... | ... |

**注意**：`label` 列是用未来数据计算的，仅作为训练监督信号，模型输入中**绝不能包含** label。

---

## 第四步：Qlib 二进制格式转换

**工具**：`Kronos_hyper/dump_bin.py`（基于微软 Qlib 框架的 dump 工具）

**做什么**：将 CSV 转换为 Qlib 框架要求的二进制目录结构。Qlib 使用二进制存储来实现高效的列式数据随机访问，比反复读 CSV 快得多。

**前置处理**：给 CSV 添加一列 `symbol`（如 `btc_usdt`），让 Qlib 知道这是哪个标的。

**转换命令示例**：
```bash
python dump_bin.py dump_all \
  --data_path=labeled_data.csv \
  --qlib_dir=data/qlib_enhance \
  --freq=5min \
  --include_fields=open,high,low,close,volume,label
```

**输出目录结构**：
```
data/buy_sell_points_11m/qlib_enhance/
├── calendars/
│   └── 5min.txt          ← 时间索引（所有合法交易时间戳）
├── features/
│   └── btc_usdt/
│       ├── open.bin       ← 每个特征一个二进制文件
│       ├── high.bin
│       ├── low.bin
│       ├── close.bin
│       ├── volume.bin
│       └── label.bin      ← 标签也以二进制存储
└── instruments/
    └── all.txt            ← 标的列表及其有效时间范围
```

**为什么要做这一步**：Qlib 的 `DataLoader` 能高效地从这些 `.bin` 文件中按时间范围、按字段读取数据，避免每次训练都从头解析 CSV。

---

## 第五步：Qlib 加载 → 特征工程 → 时间切分 → Pickle

**工具**：`Kronos_hyper/finetune/qlib_data_preprocess.py`

**做什么**：从 Qlib 二进制数据中加载指定时间范围的数据，进行特征工程，然后按时间切分为训练/验证/测试集，保存为 pickle 文件。

### 5.1 Qlib 数据加载

初始化 Qlib 引擎，通过 `QlibDataLoader` 从二进制文件中读取 OHLCV + label 数据。加载时会自动扩展时间范围（前后各加 `lookback_window` 和 `predict_window`），确保边界样本也有完整的窗口。

### 5.2 特征工程

在原始 OHLCV 基础上，派生一个新特征：

- **`amt`（成交额）** = `(open + high + low + close) / 4 × volume`

这使得最终特征列表为：**`open, high, low, close, volume, amt`**（共 6 维）。

### 5.3 数据清洗

- 所有含 NaN 的行 `dropna`
- `label` 列强制转为整数类型
- 数据量不足（小于 `lookback_window + predict_window + 1`）的标的直接丢弃

### 5.4 时间切分

按 YAML 配置中的时间范围，将数据切分为三个集合：

| 集合 | 默认时间范围 | 用途 |
|---|---|---|
| **训练集** | 2025-01-10 ~ 2025-06-30 | 模型参数学习 |
| **验证集** | 2025-06-01 ~ 2025-07-31 | 早停/超参调优 |
| **测试集** | 2025-07-01 ~ 2025-11-30 | 最终评估 |

注意：训练集和验证集有一个月的**重叠期**（6 月），这是有意为之——验证集需要前序数据来构造完整的回看窗口。

**切分方式**：纯时间掩码，**不是**随机打乱。金融时序数据必须按时间切分，否则会产生未来信息泄露。

### 5.5 输出文件

```
data/buy_sell_points_11m/processed_datasets/
├── train_data.pkl    ← dict[symbol → DataFrame]
├── val_data.pkl
└── test_data.pkl
```

每个 pickle 文件内部是 `dict`，键为标的名称（如 `btc_usdt`），值为 DataFrame（index 为 datetime，列为 `open, high, low, close, volume, amt, label`）。

---

## 第六步：滑动窗口 → 归一化 → 采样 → PyTorch 张量

**工具**：`Kronos_hyper/finetune/dataset.py`（`QlibDataset` 类）

**做什么**：将 pickle 中的 DataFrame 转化为模型实际消费的 `(x, x_stamp, label)` 张量。这是 `torch.utils.data.Dataset` 的子类，在训练循环中被 `DataLoader` 调用。

### 6.1 滑动窗口构造

窗口总长度 = `lookback_window + predict_window + 1` = 288 + 12 + 1 = **301 根 K 线**

含义：
- 前 288 根 K 线是模型的**输入上下文**（约 24 小时的 5 分钟线）
- 第 289 根 K 线的位置取 label（这是预测目标点）
- 后 12 根 K 线是 predict_window（用于 token 生成任务的目标序列）

```
|←―――― lookback_window (288) ―――――→|target|←― predict_window (12) ―→|
|  K1  K2  K3  ...  K287  K288     | K289 | K290 ... K301          |
|  模型输入上下文                    |取label| token 生成目标          |
```

### 6.2 时间特征提取

从每根 K 线的 datetime 中提取 5 个时间特征（**不做归一化**，直接用整数值）：

| 特征 | 值域 | 含义 |
|---|---|---|
| `minute` | 0~55 | 分钟（5 分钟线只有 0/5/10/.../55） |
| `hour` | 0~23 | 小时 |
| `weekday` | 0~6 | 星期几（0=周一） |
| `day` | 1~31 | 日 |
| `month` | 1~12 | 月 |

### 6.3 逐样本归一化（Instance Normalization）

对每个窗口的**价格特征**（OHLCV + amt）独立做 z-score 归一化：

```
x_normalized = (x - mean) / (std + 1e-5)
x_clipped = clip(x_normalized, -5.0, +5.0)
```

- 均值和标准差在**该窗口的 301 根 K 线上**逐特征计算
- clip 到 ±5.0 防止极端离群值
- **逐样本**而非全局归一化——这样不同价格量级的时段可以直接比较形态

### 6.4 采样策略

`QlibDataset` 支持多种采样方式：

| 策略 | 行为 | 适用场景 |
|---|---|---|
| **balanced**（默认，训练集） | 轮流从每个类别中采样，使每轮每类出现次数相同 | 解决类别不平衡 |
| **focus / weighted** | 按权重采样各类别，`focus_labels`（如 0/6）乘以额外的 `focus_multiplier` | 聚焦少数类的训练实验 |
| **index / sequential** | 按顺序遍历所有样本 | 验证/测试集，确保指标可复现 |
| **random** | 完全随机 | 基础方案 |

### 6.5 输出张量

每次 `__getitem__` 返回一个三元组：

| 张量 | 形状 | 内容 |
|---|---|---|
| `x_tensor` | `[301, 6]` | 归一化后的 OHLCV + amt |
| `x_stamp_tensor` | `[301, 5]` | 时间特征（minute/hour/weekday/day/month） |
| `label_tensor` | `scalar` | 0~6 的整数标签 |

这三个张量被 `DataLoader` 按 `batch_size`（默认 32）打包后送入模型。

---

## 完整文件流转示意

```
Binance Vision (HTTPS)
    │
    │  binance.py download
    ▼
*.zip (每日压缩包)
    │
    │  binance.py extract + combine
    ▼
BTCUSDT_5m_qlib.csv           ← date/OHLCV/factor
    │
    │  generate_enhanced_labels.py
    ▼
enhanced_labels_*.csv          ← date/OHLCV/label(0~6)
    │
    │  添加 symbol 列
    ▼
labeled_with_symbol.csv        ← date/OHLCV/label/symbol
    │
    │  dump_bin.py
    ▼
qlib_enhance/                  ← calendars/ + features/*.bin + instruments/
    │
    │  qlib_data_preprocess.py
    ▼
processed_datasets/
├── train_data.pkl             ← dict[symbol → DataFrame(OHLCV+amt+label)]
├── val_data.pkl
└── test_data.pkl
    │
    │  QlibDataset.__getitem__()
    ▼
(x_tensor[301,6], x_stamp[301,5], label)  ← 模型直接消费
```

---

## 关键设计决策与注意事项

### 1. 为什么用 Qlib 而不是直接读 CSV？

Qlib 的二进制存储支持**按列按时间范围的高效随机访问**。对于 11 个月的 5 分钟线数据（约 96,000 根 K 线），直接读 CSV 每次预处理需要 ~10 秒，而 Qlib 的 DataLoader 只需 ~1 秒。在频繁迭代实验时，这个差异累积起来很显著。

### 2. 为什么是逐样本归一化而非全局归一化？

BTC 价格在 2025 年从 ~90,000 波动到 ~100,000+，不同时段的绝对价格差异巨大。如果用全局均值/标准差归一化，模型会学到"价格在 9 万还是 10 万"，而我们关心的是"K 线形态"——逐样本归一化消除了绝对价格的影响，让模型专注于窗口内的相对走势模式。

### 3. 为什么训练集和验证集有时间重叠？

不是数据泄露。重叠是因为构造一个训练样本需要 `lookback_window`（288 根，约 24 小时）的历史上下文。如果验证集从 6 月 1 日开始，那么 6 月 1 日的样本需要 5 月 31 日的数据作为输入。但**标签**（来自未来窗口）不会跨集合泄露——验证集样本的 label 始终由验证时间范围内的未来数据计算。

### 4. balanced 采样的意义

7 分类标签中，neutral（类别 3）占比远超 strong_buy（类别 0）和 strong_sell（类别 6）。如果用自然分布采样，模型会大量接触 neutral 样本，对少数类的学习严重不足。balanced 采样强制每批数据中各类别数量一致，确保模型均匀地学习所有类别。

### 5. label 取在 lookback_window 处，而不是最后一根 K 线

label 取在窗口的第 289 个位置（`iloc[lookback_window]`），即模型看完 288 根历史 K 线后的**当前时刻**。后面的 12 根 K 线是 Kronos 基础模型做 token 生成任务（预测下一根 K 线）用的目标序列。分类任务（买/卖信号）和生成任务（预测 K 线）共享同一个窗口，但目标取值位置不同。