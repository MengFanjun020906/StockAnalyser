# UZI-Skill 使用踩坑与避坑指南

> 基于 2026-06-11 悦安新材 (688786.SH) + 2026-06-14 洛阳钼业 (603993.SH) 两次实测，记录关键坑点和完整流程建议。

---

## 1. 环境准备

### pip 安装权限问题
```bash
# 系统 Python 直接装会报 Permission denied
pip install -r requirements.txt  # ❌ 报错

# 正确做法：加 --user + 清华源
pip install --user -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
```

### ddgs 包名注意
```bash
# requirements.txt 写的 ddgs>=9.0.0，实际 PyPI 上叫 ddgs（不是 duckduckgo-search）
pip install --user 'ddgs>=9.0.0'  # ✅ 正确
pip install --user 'duckduckgo-search>=9.0.0'  # ❌ 找不到包
```

### 运行目录
```bash
# run.py 在仓库根目录，必须在 UZI-Skill 目录下执行
cd /path/to/UZI-Skill
python3 run.py 603993.SH --depth medium --no-browser
```

### Playwright 安装（报告截图用）
```bash
pip install --user playwright
python3 -m playwright install chromium
```

---

## 2. 核心瓶颈：K 线数据采集

**这是最大的坑，但确认是"慢"不是"拉不到"。** 两次实测 BaoStock K 线均成功返回完整数据（590-856 条），只是耗时较长。

### 实测耗时

| 阶段 | 耗时 | 备注 |
|---|---|---|
| API 数据采集（basic/financials/peers 等） | 1-2 分钟 | 正常 |
| K 线数据（BaoStock） | **5-7 分钟** | 主要瓶颈，但能拉到 |
| Medium 深度 wave2（research/events/macro 等） | 1-2 分钟 | ddgs 搜索 |
| 评分 + 报告生成 | 1-2 分钟 | 正常 |
| **总计（medium）** | **8-12 分钟** | 远超官方宣称 |

### 关键结论
- **K 线拉得到，只是慢** — 不要误判为失败而提前 kill
- resume 模式会跳过已缓存的数据，**第二次跑同一只票会很快**
- 首次跑设 timeout ≥ 600s（10 分钟），后台运行

---

## 3. ⭐ 完整流程（推荐方案）

这是经过洛阳钼业验证的**端到端跑通**的完整流程：

### 第一步：数据采集（stage1）

```bash
cd /path/to/UZI-Skill

# ⚠️ 必须用 medium 或 deep，lite 模式维度不全，stage2 会被 self-review 阻断
python3 run.py {ticker} --depth medium --no-browser
```

**为什么不能用 lite**：lite 模式只采 7 个维度，缺失同行对比(4)、产业链(5)、资金流(12)、护城河(14)、舆情(17)、杀猪盘(18) 等关键维度。stage2 的 self-review gate 会检测到 8 个 critical issue 并**拒绝生成 HTML**。

**medium vs deep**：
| 模式 | 维度数 | 评委数 | 耗时 | 适用场景 |
|---|---|---|---|---|
| lite | 7 | 10 | 5-8 分钟 | 仅快速扫描，**不能生成完整报告** |
| medium | 21 | 50+ | 8-12 分钟 | **日常分析推荐** |
| deep | 22 | 66 | 10-15 分钟 | 全覆盖深度分析 |

### 第二步：写 agent_analysis.json（可选但推荐）

stage1 跑完后，如果要有 agent 定性判断（而非纯规则引擎机械打分），需要写 `.cache/{ticker}/agent_analysis.json`：

```json
{
  "agent_reviewed": true,
  "dim_commentary": {
    "0_basic": "≥20字的公司基本面定性评语...",
    "1_financials": "≥20字的财务分析...",
    "2_kline": "≥20字的技术面判断..."
  },
  "panel_insights": "≥30字的评委投票分布和多空分歧分析",
  "great_divide_override": {
    "punchline": "≥10字的多空对决金句",
    "bull_say_rounds": ["多方论点1+引用数字", "论点2", "论点3"],
    "bear_say_rounds": ["空方论点1+引用数字", "论点2", "论点3"]
  },
  "narrative_override": {
    "core_conclusion": "≥20字的综合定论",
    "risks": ["风险1", "风险2", "风险3"],
    "buy_zones": {
      "value": {"price": 数值, "rationale": "≥5字逻辑"},
      "growth": {"price": 数值, "rationale": "≥5字逻辑"},
      "technical": {"price": 数值, "rationale": "≥5字逻辑"},
      "youzi": {"price": 数值, "rationale": "≥5字逻辑"}
    }
  },
  "qualitative_deep_dive": {
    "3_macro": {"evidence": [...], "associations": [...], "conclusion": "..."},
    "7_industry": {"evidence": [...], "associations": [...], "conclusion": "..."},
    "8_materials": {"evidence": [...], "associations": [...], "conclusion": "..."},
    "9_futures": {"evidence": [...], "associations": [...], "conclusion": "..."},
    "13_policy": {"evidence": [...], "associations": [...], "conclusion": "..."},
    "15_events": {"evidence": [...], "associations": [...], "conclusion": "..."}
  },
  "data_gap_acknowledged": {
    "dim_key": "尝试了什么但失败的原因"
  }
}
```

**不写 agent_analysis.json 的后果**：stage2 会退化为纯规则引擎模式，评委判断全是机械打分（"看多核心：Stage 2 ✓" 这种废话），报告会缺少洞察。

### 第三步：生成报告（stage2）

```bash
cd skills/deep-analysis/scripts
python3 -c "from run_real_test import stage2; stage2('{ticker}')"
```

stage2 会自动：
1. 加载 agent_analysis.json 合并到 synthesis
2. 运行 self-review（有 critical issue 则阻断）
3. 生成 HTML 报告
4. 生成朋友圈竖图 (share-card.png)
5. 生成战报横图 (war-report.png)
6. 生成一句话摘要 (one-liner.txt)

### 最终产物

```
reports/{ticker}_{date}/
├── full-report-standalone.html   # 自包含 HTML（747KB），浏览器直接打开
├── full-report.html              # 非自包含版本
├── share-card.png                # 朋友圈竖图 1080×1920
├── war-report.png                # 战报横图 1920×1080
└── one-liner.txt                 # 一句话摘要
```

---

## 4. 参数速查

| 参数 | 说明 | 推荐 |
|---|---|---|
| `--depth lite` | 7 维 + 10 投资者 | 仅快速扫描，不能生成完整报告 |
| `--depth medium` | 21 维 + 50+ 投资者 | **日常分析推荐** |
| `--depth deep` | 22 维 + 66 评委 | 全覆盖深度分析 |
| `--no-browser` | 不打开浏览器 | agent 环境必加 |
| `--no-resume` | 强制重抓所有数据 | 数据过期时用 |
| `--school A-I` | 锁定单一流派 | 减少 role-play 量 |
| `--remote` | 启动 Cloudflare Tunnel | 手机看报告用 |

### resume 机制
- **默认开启**（不需要加参数）
- 已缓存的 API 数据不会重抓
- 第二次跑同一只票会显著加速
- 要强制全部重抓才加 `--no-resume`

---

## 5. 已知 Bug（Python 3.11）

### f-string 反斜杠语法错误
Python 3.11 不支持 f-string 表达式中的反斜杠（Python 3.12+ 才支持）。UZI-Skill v3.9.0 有两处会报错：

**修复 1**：`lib/report/institutional.py` 第 623 行
```python
# 原始代码（3.11 报错）
f'    {f"<div style=\"margin-top:4px...\">{members_hint}</div>" if members_hint else ""}'

# 修复：提取到变量
members_div = f'<div style="margin-top:4px;color:#6b7280;font-size:11px">代表评委 · {members_hint}</div>' if members_hint else ""
f'    {members_div}'
```

**修复 2**：`lib/report/segmental.py` 第 242 行（同类问题）
```
⚠️ segmental block 跳过: SyntaxError: f-string expression part cannot include a backslash
```
这个不影响报告生成，只是分业务建模模块被跳过。

---

## 6. 进程管理

### 进程假死现象
K 线采集期间进程 CPU 使用率可能降到 0%，看起来像卡死，但实际仍在运行。**不要急于 kill**。

### 正确判断方法
```bash
# 1. 检查 cache 文件是否在更新
ls -la .cache/{ticker}/api_cache/

# 2. 看 raw_data.json 是否已生成（生成即 stage1 完成）
ls -la .cache/{ticker}/raw_data.json

# 3. 看 panel.json 是否已生成（生成即评分完成）
ls -la .cache/{ticker}/panel.json
```

### 杀进程要彻底
```bash
kill -9 <pid>
# 注意杀掉 shell wrapper 进程
ps aux | grep run.py | grep -v grep
```

---

## 7. 数据缓存结构

```
.cache/{ticker}/
├── api_cache/                    # API 原始缓存（JSON）
│   ├── basic__*.json             # 基本信息（价格/PE/PB/市值）
│   ├── kline__*.json             # K 线数据（最大文件）
│   ├── fund_holders_*.json       # 基金持仓（可能很大）
│   ├── lhb__*.json               # 龙虎榜
│   ├── hsgt__*.json              # 沪深港通
│   ├── tgb__*.json               # 淘股吧情绪
│   ├── xq_cubes_*.json           # 雪球组合（需登录）
│   └── ths_simu__*.json          # 同花顺模拟盘（需登录）
├── raw_data.json                 # stage1 整合后的 21 维数据
├── dimensions.json               # 维度打分
├── panel.json                    # 66 评委打分骨架
├── synthesis.json                # stage2 综合研判
├── agent_analysis.json           # agent 写的定性分析（你写）
├── _review_issues.json           # 数据质量自查结果
└── _data_gaps.json               # 数据缺口记录
```

**关键时间节点**：
- `api_cache/` 在 stage1 wave1 完成后就有
- `raw_data.json` + `panel.json` 在 stage1 全部完成后生成
- `synthesis.json` 在 stage2 完成后生成
- `reports/` 目录在 stage2 最后生成

---

## 8. 数据源限制

### 需要登录的数据源
| 数据源 | 缺失影响 | 登录方式 |
|---|---|---|
| 雪球 (xueqiu) | 组合数据缺失 | `UZI_XQ_LOGIN=1` + `python -m lib.xueqiu_browser login` |
| 同花顺模拟盘 | 模拟持仓缺失 | 需登录 |

### 需要 API Key 的数据源
| 数据源 | 说明 |
|---|---|
| 东财妙想 (MX_APIKEY) | 强烈建议配置，特别是境外环境 |

### 部分数据缺失是常态
- `api_cache` 中部分字段返回空数组或 null
- `_info_err`、`_snap_err` 等字段表示该数据源请求失败
- 脚本会自动 fallback，不影响整体分析
- 在 `agent_analysis.json` 的 `data_gap_acknowledged` 中标记即可

---

## 9. Self-Review 门控机制

stage2 在生成 HTML 前会运行 `review_stage_output.py`，有 critical issue 就**拒绝生成**：

| severity | 检查项 | 触发原因 |
|---|---|---|
| 🔴 critical | 维度完全缺失 | lite 模式漏采维度 |
| 🔴 critical | 空维度数据 | 网络请求全部失败 |
| 🔴 critical | 覆盖率 < 60% | 大面积数据缺失 |
| 🔴 critical | agent_analysis.json 缺失 | 没写 agent 分析文件 |
| 🟡 warning | DCF 内在价值为 0 | 负 FCF 或假设异常 |
| 🟡 warning | 行业映射异常 | industry 字段为 null |

**绕过方式**（仅调试）：`export UZI_SKIP_REVIEW=1`，但正式分析不建议。

---

## 10. Agent 调用最佳实践

### 完整流程（推荐）

```
1. 安装依赖（一次性）
   cd /path/to/UZI-Skill
   pip install --user -r requirements.txt
   pip install --user playwright && python3 -m playwright install chromium

2. 后台运行 stage1（timeout ≥ 600s，用 medium 深度）
   python3 run.py {ticker} --depth medium --no-browser
   # 用 is_background=true 运行

3. 等待完成（检查 .cache/{ticker}/panel.json 是否存在）

4. 读取 panel.json + raw_data.json + WebSearch 补充
   → 写 agent_analysis.json（严格按 schema）

5. 跑 stage2 生成完整产物
   cd skills/deep-analysis/scripts
   python3 -c "from run_real_test import stage2; stage2('{ticker}')"

6. 产物在 reports/{ticker}_{date}/
   - full-report-standalone.html（自包含 HTML）
   - share-card.png（朋友圈竖图）
   - war-report.png（战报横图）
   - one-liner.txt（一句话摘要）
```

### 快速替代方案（2-3 分钟）

如果不想等 stage1 完整跑完：

1. 后台启动 stage1（让它在后台跑）
2. 同时读已有的 `api_cache/*.json` 获取基础数据
3. WebSearch 补充最新新闻和研报
4. Agent 自己完成多维度分析 → 写成 MD 报告
5. 如果后续需要完整产物（HTML + 图片），等 stage1 跑完后再补

---

## 11. 两次实测对比

| 项目 | 悦安新材 (688786) | 洛阳钼业 (603993) |
|---|---|---|
| 日期 | 2026-06-11 | 2026-06-14 |
| depth | lite | medium |
| K 线数据 | 856 条 / 超时未完成 | 590 条 / 成功（慢但拉到了） |
| 维度覆盖 | 7 维（lite 限制） | 21 维（完整） |
| agent_analysis.json | 未写 | 已写（16KB） |
| self-review | 未跑到 stage2 | 通过（0 critical） |
| HTML 报告 | 未生成 | ✅ 747KB |
| 朋友圈竖图 | 未生成 | ✅ 1.1MB |
| 战报横图 | 未生成 | ✅ 297KB |
| 一句话摘要 | 未生成 | ✅ |
| MD 报告 | ✅ 手写 | ✅ 手写 + UZI 产物 |
| 结论 | **流程不完整** | **端到端完整跑通** |

**核心教训**：
1. **必须用 medium 或 deep**，lite 模式维度不全导致 stage2 被阻断
2. **K 线慢不是失败**，给它足够时间（≥10 分钟）就能拉到
3. **agent_analysis.json 是质量关键**，不写的话报告全是规则引擎的废话
4. **Python 3.11 有 f-string bug**，需要手动修两处代码

---

## 12. 常见问题速查

| 问题 | 原因 | 解决 |
|---|---|---|
| `can't open file 'run.py'` | 不在 UZI-Skill 目录 | `cd /path/to/UZI-Skill` |
| `Permission denied` | 系统 Python 无写权限 | 加 `--user` |
| `No module named 'ddgs'` | 包名不对 | `pip install --user ddgs` |
| 进程看起来卡死 | K 线采集 CPU 低 | 检查 cache 文件是否在更新 |
| `unrecognized arguments: --resume` | resume 是默认行为 | 去掉 `--resume` |
| stage2 报 8 个 critical | lite 模式维度不全 | 改用 `--depth medium` |
| `f-string backslash` SyntaxError | Python 3.11 限制 | 手动修 institutional.py + segmental.py |
| self-review 阻断 | 有 critical issue | 按 `_review_issues.json` 的 suggested_fix 修 |
| `xq_cubes` 返回空 | 雪球未登录 | `UZI_XQ_LOGIN=1` |
| 报告没有生成 | 进程被提前 kill | 等 panel.json 生成后再 kill |

---

## 13. 版本信息

- 仓库：https://github.com/wbh604/UZI-Skill
- 实测版本：v3.9.0（游资 Skills）
- 实测环境：macOS arm64, Python 3.11, 中国大陆网络
- 实测标的：悦安新材 (688786.SH) + 洛阳钼业 (603993.SH)
- 最后更新：2026-06-14
