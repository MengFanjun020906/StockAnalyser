(function () {
  "use strict";

  var candidates = [
    { code: "002050", name: "三花智控", market: "深市主板", industry: "汽车零部件", price: 28.64, change: 3.21, amount: "42.8 亿", turnover: "2.4%", signal: "strong", score: 86, flow: 1.2, catalyst: ["机器人", "热管理"], updated: "14:56", seed: 7, entryLow: 27.8, entryHigh: 28.2, stop: 26.9, target1: 30.5, target2: 32.0, verdict: "偏多 · 等待回踩", confidence: 78, reasons: ["日线突破 28.0 平台后回踩不破，MA20 上穿 MA60", "近 3 日主力净流入 1.2 亿，量比 1.34", "机器人热管理订单催化，板块强度排名前 10%"], risks: ["大盘处于震荡区间，追高容易吃回撤", "29.2 附近有前高压力，突破需要放量"] },
    { code: "300308", name: "中际旭创", market: "创业板", industry: "通信设备", price: 158.2, change: 5.62, amount: "78.9 亿", turnover: "3.9%", signal: "strong", score: 91, flow: 4.2, catalyst: ["CPO", "AI 算力"], updated: "14:48", seed: 11, entryLow: 153.0, entryHigh: 156.0, stop: 148.5, target1: 168.0, target2: 176.0, verdict: "偏多 · 趋势延续", confidence: 84, reasons: ["CPO 板块强度第一，龙头效应明显", "放量突破前高，成交额 78.9 亿", "北向与机构资金同步流入"], risks: ["高位波动大，单日回撤可能超过 5%", "海外算力资本开支预期变化"] },
    { code: "603986", name: "兆易创新", market: "沪市主板", industry: "半导体", price: 128.7, change: 4.86, amount: "65.3 亿", turnover: "4.2%", signal: "strong", score: 88, flow: 3.1, catalyst: ["存储涨价", "国产替代"], updated: "14:49", seed: 13, entryLow: 124.0, entryHigh: 127.0, stop: 119.8, target1: 136.0, target2: 142.0, verdict: "偏多 · 突破确认", confidence: 81, reasons: ["存储价格上行，基本面预期改善", "放量突破 125 平台，换手 4.2%", "半导体板块资金净流入居前"], risks: ["板块拥挤度上升", "价格周期反转不及预期"] },
    { code: "688981", name: "中芯国际", market: "科创板", industry: "半导体", price: 112.3, change: 2.45, amount: "88.4 亿", turnover: "2.8%", signal: "strong", score: 83, flow: 2.4, catalyst: ["国产替代", "先进制程"], updated: "14:53", seed: 17, entryLow: 109.0, entryHigh: 111.5, stop: 105.2, target1: 118.0, target2: 124.0, verdict: "偏多 · 观察放量", confidence: 74, reasons: ["国产替代逻辑持续，板块中军", "站稳 MA20，成交额维持高位", "但 115 附近套牢盘较重"], risks: ["突破需要持续放量", "外部制裁与设备限制"] },
    { code: "601138", name: "工业富联", market: "沪市主板", industry: "消费电子", price: 24.86, change: 2.18, amount: "56.1 亿", turnover: "1.1%", signal: "strong", score: 81, flow: 1.8, catalyst: ["AI 服务器", "英伟达链"], updated: "14:51", seed: 19, entryLow: 24.2, entryHigh: 24.7, stop: 23.4, target1: 26.6, target2: 27.8, verdict: "偏多 · 回踩确认", confidence: 76, reasons: ["AI 服务器订单能见度提升", "回踩 24.2 后快速收回", "机构资金连续 3 日净流入"], risks: ["海外客户集中度高", "毛利率承压"] },
    { code: "601399", name: "国机重装", market: "沪市主板", industry: "专用设备", price: 4.12, change: 1.98, amount: "18.6 亿", turnover: "3.1%", signal: "watch", score: 72, flow: 0.32, catalyst: ["核电", "一带一路"], updated: "14:55", seed: 23, entryLow: 4.02, entryHigh: 4.1, stop: 3.88, target1: 4.38, target2: 4.55, verdict: "观察 · 等待确认", confidence: 61, reasons: ["核电设备订单预期", "低位放量，但趋势尚未完全转多", "股价低，弹性大但波动也大"], risks: ["低价股情绪退潮快", "订单兑现节奏不确定"] },
    { code: "300750", name: "宁德时代", market: "创业板", industry: "电池", price: 268.4, change: -0.86, amount: "96.2 亿", turnover: "1.2%", signal: "watch", score: 74, flow: -1.8, catalyst: ["储能", "海外扩产"], updated: "14:54", seed: 29, entryLow: 262.0, entryHigh: 266.0, stop: 255.0, target1: 284.0, target2: 296.0, verdict: "观察 · 等企稳", confidence: 58, reasons: ["储能与海外扩产逻辑仍在", "但今日主力净流出 1.8 亿", "270 附近有均线压力"], risks: ["锂价波动", "海外政策与关税"] },
    { code: "002594", name: "比亚迪", market: "深市主板", industry: "汽车整车", price: 312.6, change: 1.12, amount: "74.5 亿", turnover: "1.0%", signal: "watch", score: 70, flow: 0.8, catalyst: ["出海", "智驾"], updated: "14:52", seed: 31, entryLow: 306.0, entryHigh: 310.0, stop: 298.0, target1: 328.0, target2: 340.0, verdict: "观察 · 区间震荡", confidence: 55, reasons: ["出海销量维持高增", "智驾平权带来估值支撑", "但短期缺乏催化，成交额一般"], risks: ["价格战压缩利润", "海外贸易摩擦"] },
    { code: "000001", name: "平安银行", market: "深市主板", industry: "银行", price: 11.82, change: 0.51, amount: "22.7 亿", turnover: "0.6%", signal: "watch", score: 68, flow: 0.45, catalyst: ["息差企稳", "高股息"], updated: "14:50", seed: 37, entryLow: 11.6, entryHigh: 11.75, stop: 11.2, target1: 12.4, target2: 12.8, verdict: "观察 · 防御配置", confidence: 53, reasons: ["高股息与低估值提供安全垫", "息差企稳预期", "但弹性有限，适合防御"], risks: ["资产质量与地产敞口", "息差继续下行"] },
    { code: "600519", name: "贵州茅台", market: "沪市主板", industry: "白酒", price: 1468.0, change: -0.32, amount: "52.1 亿", turnover: "0.3%", signal: "watch", score: 66, flow: -2.1, catalyst: ["消费复苏", "分红"], updated: "14:51", seed: 41, entryLow: 1440.0, entryHigh: 1460.0, stop: 1398.0, target1: 1530.0, target2: 1580.0, verdict: "观察 · 等需求验证", confidence: 51, reasons: ["品牌壁垒与分红支撑", "但消费需求恢复偏慢", "主力资金连续净流出"], risks: ["批价波动", "消费复苏不及预期"] },
    { code: "601318", name: "中国平安", market: "沪市主板", industry: "保险", price: 58.36, change: -1.24, amount: "36.4 亿", turnover: "0.5%", signal: "risk", score: 41, flow: -1.5, catalyst: ["地产敞口", "利率下行"], updated: "14:47", seed: 43, entryLow: 57.2, entryHigh: 58.0, stop: 55.8, target1: 62.0, target2: 64.0, verdict: "回避 · 风险未出清", confidence: 69, reasons: ["地产敞口与投资端压力仍在", "跌破 MA20，趋势偏弱", "主力净流出 1.5 亿"], risks: ["地产风险暴露", "长端利率下行压制估值"] },
    { code: "002466", name: "天齐锂业", market: "深市主板", industry: "能源金属", price: 32.18, change: -3.42, amount: "28.9 亿", turnover: "2.6%", signal: "risk", score: 48, flow: -1.1, catalyst: ["锂价下跌", "库存压力"], updated: "14:46", seed: 47, entryLow: 31.4, entryHigh: 32.0, stop: 30.2, target1: 35.0, target2: 36.8, verdict: "回避 · 等锂价企稳", confidence: 66, reasons: ["锂价仍在寻底", "跌破 33 平台，量能放大", "行业库存偏高"], risks: ["锂价继续下跌", "高杠杆与减值压力"] }
  ];

  var traceSteps = [
    { id: "planner", group: "规划", name: "Planner", status: "ok", duration: "1.2s", tokens: "1.8k", summary: "拆解目标：判断三花智控当前是否具备入场条件，要求给出数学区间、止损、目标与证据链。", metrics: [{ label: "任务类型", value: "单股入场", cls: "accent" }, { label: "计划步骤", value: "6", cls: "" }, { label: "超时预算", value: "180s", cls: "" }, { label: "数据日期", value: "11-06", cls: "" }], input: '{"symbol":"002050","question":"现在能不能买？","horizon":"3-10d"}', output: [{ k: "目标", v: "判断入场条件与价格区间" }, { k: "约束", v: "必须给出止损 / 失效条件" }, { k: "工具计划", v: "行情 → 新闻 → 结构 → 财务 → 资金" }], evidence: [{ title: "用户目标", grade: "A", source: "request", freshness: "实时", text: "判断 002050 当前是否具备入场条件，给出区间、止损与证据。" }] },
    { id: "market_data", group: "工具", name: "market_data", status: "ok", duration: "3.2s", tokens: "1.4k", summary: "拉取实时报价、近 240 日复权日线与指数上下文，行情与本地缓存一致。", metrics: [{ label: "最新价", value: "28.64", cls: "up" }, { label: "涨跌幅", value: "+3.21%", cls: "up" }, { label: "量比", value: "1.34", cls: "accent" }, { label: "数据源", value: "Tushare", cls: "" }], input: '{"symbol":"002050","lookback":240,"adjust":"qfq"}', output: [{ k: "日线记录", v: "240 条" }, { k: "最新交易日", v: "2026-11-06" }, { k: "复权方式", v: "前复权" }, { k: "缺口", v: "无" }], evidence: [{ title: "行情快照", grade: "A", source: "Tushare + 本地缓存", freshness: "收盘", text: "收盘 28.64，涨 3.21%，成交额 42.8 亿，量比 1.34。" }] },
    { id: "news_search", group: "工具", name: "news_search", status: "ok", duration: "12.1s", tokens: "4.8k", summary: "检索近 14 天公司新闻与行业催化，命中机器人热管理、订单与板块强度相关证据。", metrics: [{ label: "原始条数", value: "46", cls: "" }, { label: "相关条数", value: "9", cls: "accent" }, { label: "强证据", value: "3", cls: "down" }, { label: "窗口", value: "14d", cls: "" }], input: '{"query":"三花智控 机器人 热管理","window":14,"sources":["news","announcement"]}', output: [{ k: "催化", v: "机器人热管理订单" }, { k: "情绪", v: "偏正面" }, { k: "风险", v: "板块拥挤度上升" }, { k: "日期覆盖", v: "2026-10-24 ~ 11-06" }], evidence: [{ title: "机器人热管理订单", grade: "A", source: "公司公告", freshness: "3 天前", text: "公司披露热管理业务新增定点，预计明年开始放量。" }, { title: "板块强度", grade: "B", source: "新闻信号", freshness: "收盘", text: "机器人概念近 5 日强度排名前 10%。" }, { title: "估值讨论", grade: "C", source: "券商研报", freshness: "1 周前", text: "当前估值处于近三年中位数附近，弹性来自订单超预期。" }] },
    { id: "price_structure", group: "工具", name: "price_structure", status: "ok", duration: "11.2s", tokens: "3.4k", summary: "日线结构处于 28.0–28.6 的突破回踩区间，MA20 上穿 MA60，量能温和放大；但 29.2 附近有前高压力，追高性价比低。", metrics: [{ label: "趋势", value: "多头排列", cls: "accent" }, { label: "支撑", value: "27.80", cls: "" }, { label: "压力", value: "29.20", cls: "" }, { label: "量比", value: "1.34", cls: "up" }], chart: true, input: '{"symbol":"002050","timeframe":"1d","lookback":240,"levels":[27.8,29.2]}', output: [{ k: "结构", v: "突破回踩" }, { k: "入场区", v: "27.80 – 28.20" }, { k: "止损", v: "26.90" }, { k: "目标", v: "30.50 / 32.00" }, { k: "失效条件", v: "收盘跌破 26.90" }], evidence: [{ title: "日线结构", grade: "A", source: "price_structure", freshness: "收盘", text: "28.0 平台突破后回踩不破，低点抬高。" }, { title: "均线", grade: "A", source: "price_structure", freshness: "收盘", text: "MA20 上穿 MA60，MA5/10/20 多头排列。" }, { title: "量能", grade: "B", source: "price_structure", freshness: "收盘", text: "突破日量比 1.34，回踩缩量，未出现放量下跌。" }, { title: "压力位", grade: "B", source: "price_structure", freshness: "收盘", text: "29.2 为前高与整数关口叠加，突破需要放量。" }] },
    { id: "fundamental", group: "工具", name: "fundamental", status: "ok", duration: "6.7s", tokens: "2.1k", summary: "财务快照显示收入与利润保持增长，毛利率稳定；估值处于历史中位数附近，基本面不构成否决项。", metrics: [{ label: "营收增速", value: "+18.6%", cls: "up" }, { label: "净利增速", value: "+22.1%", cls: "up" }, { label: "毛利率", value: "27.4%", cls: "" }, { label: "估值分位", value: "52%", cls: "" }], input: '{"code":"002050","report":"2026Q3","fields":["revenue","profit","margin","valuation"]}', output: [{ k: "营收", v: "同比增长 18.6%" }, { k: "净利润", v: "同比增长 22.1%" }, { k: "毛利率", v: "27.4%，环比持平" }, { k: "估值", v: "近三年 52% 分位" }], evidence: [{ title: "2026Q3 财报", grade: "A", source: "Tushare", freshness: "1 周前", text: "营收与净利保持双位数增长，毛利率稳定。" }, { title: "估值", grade: "B", source: "本地快照", freshness: "收盘", text: "PE 处于近三年中位数附近，未明显高估。" }] },
    { id: "capital_flow", group: "工具", name: "capital_flow", status: "ok", duration: "9.6s", tokens: "2.8k", summary: "近 3 日主力净流入 1.2 亿，北向与机构席位同步流入，资金面支持回踩后的二次上攻。", metrics: [{ label: "3 日主力", value: "+1.2 亿", cls: "up" }, { label: "北向", value: "+0.4 亿", cls: "up" }, { label: "大单占比", value: "18.7%", cls: "accent" }, { label: "连续性", value: "3 日", cls: "" }], input: '{"symbol":"002050","window":3,"sources":["main","northbound","institution"]}', output: [{ k: "主力净流入", v: "+1.2 亿" }, { k: "北向净流入", v: "+0.4 亿" }, { k: "机构席位", v: "2 买 0 卖" }, { k: "大单占比", v: "18.7%" }], evidence: [{ title: "主力资金", grade: "A", source: "Tushare moneyflow", freshness: "收盘", text: "近 3 日主力净流入 1.2 亿，流入集中在回踩日。" }, { title: "北向", grade: "B", source: "Tushare", freshness: "收盘", text: "北向净流入 0.4 亿，连续 2 日为正。" }] },
    { id: "debate", group: "辩论", name: "技术 / 资金 / 基本面", status: "ok", duration: "38.0s", tokens: "12.6k", summary: "三位专家一致偏多，但技术专家强调 29.2 压力，资金专家强调回踩买入，基本面专家认为估值不构成否决。", metrics: [{ label: "技术", value: "偏多", cls: "up" }, { label: "资金", value: "偏多", cls: "up" }, { label: "基本面", value: "中性", cls: "" }, { label: "分歧", value: "低", cls: "accent" }], input: '{"roles":["technical","capital","fundamental"],"rounds":2}', output: [{ k: "技术", v: "回踩 27.8–28.2 可试，破 26.9 止损" }, { k: "资金", v: "主力流入支持，回踩是机会" }, { k: "基本面", v: "估值中性，不构成否决" }, { k: "综合", v: "偏多，等待回踩" }], evidence: [{ title: "技术专家", grade: "A", source: "debate.technical", freshness: "本轮", text: "结构偏多，但 29.2 前高压力明显，建议回踩买而非追高。" }, { title: "资金专家", grade: "A", source: "debate.capital", freshness: "本轮", text: "主力与北向同步流入，回踩缩量是健康信号。" }, { title: "基本面专家", grade: "B", source: "debate.fundamental", freshness: "本轮", text: "增长与估值均不构成否决，但缺少超预期催化。" }] },
    { id: "judge", group: "裁决", name: "Judge verdict", status: "ok", duration: "22.0s", tokens: "9.2k", summary: "裁决为偏多但等待回踩：不追高，27.80–28.20 分批，止损 26.90，目标 30.50 / 32.00。", metrics: [{ label: "结论", value: "偏多", cls: "up" }, { label: "置信度", value: "78%", cls: "accent" }, { label: "入场区", value: "27.8–28.2", cls: "" }, { label: "风控", value: "通过", cls: "down" }], input: '{"evidence":"4 dimensions","risk_gate":true,"format":"structured"}', output: [{ k: "动作", v: "等待回踩后分批" }, { k: "入场区", v: "27.80 – 28.20" }, { k: "止损", v: "26.90" }, { k: "目标", v: "30.50 / 32.00" }, { k: "失效", v: "收盘跌破 26.90" }], evidence: [{ title: "Judge 裁决", grade: "A", source: "judge", freshness: "本轮", text: "四维证据一致偏多，但位置不适合追高，等待回踩。" }] },
    { id: "risk_gate", group: "风控", name: "Risk Gate", status: "ok", duration: "1.1s", tokens: "0.8k", summary: "风险门禁通过：单票仓位上限 8%，止损距离 6.1%，未触发追高与流动性限制。", metrics: [{ label: "仓位上限", value: "8%", cls: "" }, { label: "止损距离", value: "6.1%", cls: "accent" }, { label: "追高", value: "未触发", cls: "down" }, { label: "流动性", value: "通过", cls: "down" }], input: '{"price":28.64,"stop":26.9,"max_position":0.08}', output: [{ k: "结果", v: "通过" }, { k: "建议仓位", v: "≤ 8%" }, { k: "单笔风险", v: "0.49%" }, { k: "备注", v: "高开超过 3% 不追" }], evidence: [{ title: "风险规则", grade: "A", source: "risk_gate", freshness: "本轮", text: "止损距离 6.1%，单票仓位上限 8%，流动性通过。" }] }
  ];

  var state = {
    view: "candidates",
    filter: "all",
    query: "",
    sortDesc: true,
    selectedStep: "price_structure",
    traceTab: "overview",
    traceFilter: "all"
  };

  function byId(id) { return document.getElementById(id); }
  function esc(value) { return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function icon(name, cls) { return '<svg class="icon ' + (cls || "") + '"><use href="#i-' + name + '"></use></svg>'; }
  function fmtPrice(value) { return Number(value).toFixed(2); }
  function fmtPct(value) { return (value > 0 ? "+" : "") + Number(value).toFixed(2) + "%"; }
  function fmtFlow(value) { return (value > 0 ? "+" : "") + Number(value).toFixed(2) + " 亿"; }
  function changeClass(value) { return value > 0 ? "up" : (value < 0 ? "down" : "flat"); }
  function signalLabel(signal) { return signal === "strong" ? "强信号" : (signal === "watch" ? "观察" : (signal === "risk" ? "风险" : "中性")); }

  function sparkline(values, color) {
    var w = 96, h = 30, p = 2;
    var min = Math.min.apply(null, values), max = Math.max.apply(null, values);
    var span = (max - min) || 1;
    var pts = values.map(function (v, i) {
      var x = p + i * (w - p * 2) / (values.length - 1);
      var y = h - p - (v - min) / span * (h - p * 2);
      return [x, y];
    });
    var line = pts.map(function (pt) { return pt[0].toFixed(1) + "," + pt[1].toFixed(1); }).join(" ");
    var area = "M" + pts[0][0].toFixed(1) + "," + (h - p) + " L" + line.replace(/ /g, " L") + " L" + pts[pts.length - 1][0].toFixed(1) + "," + (h - p) + " Z";
    var last = pts[pts.length - 1];
    return '<svg class="kpi-spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
      '<path d="' + area + '" fill="' + color + '" opacity="0.08"></path>' +
      '<polyline points="' + line + '" fill="none" stroke="' + color + '" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"></polyline>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="1.8" fill="' + color + '"></circle>' +
      '</svg>';
  }

  function seeded(seed) {
    var t = seed >>> 0;
    return function () {
      t += 0x6D2B79F5;
      var r = Math.imul(t ^ (t >>> 15), 1 | t);
      r ^= r + Math.imul(r ^ (r >>> 7), 61 | r);
      return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
    };
  }

  function makeCandles(seed, count, base) {
    var rnd = seeded(seed);
    var rows = [];
    var price = base * 0.9;
    for (var i = 0; i < count; i++) {
      var drift = 0.0018 + (rnd() - 0.46) * 0.018;
      var open = price;
      var close = open * (1 + drift);
      var high = Math.max(open, close) * (1 + rnd() * 0.012);
      var low = Math.min(open, close) * (1 - rnd() * 0.012);
      rows.push({ open: open, close: close, high: high, low: low, volume: 0.4 + rnd() });
      price = close;
    }
    return rows;
  }

  function candleSvg(stock, width, height) {
    var w = width || 392, h = height || 168;
    var pad = { l: 6, r: 42, t: 10, b: 16 };
    var candles = makeCandles(stock.seed, 42, stock.price);
    var lows = candles.map(function (c) { return c.low; });
    var highs = candles.map(function (c) { return c.high; });
    var min = Math.min.apply(null, lows.concat([stock.stop, stock.entryLow]));
    var max = Math.max.apply(null, highs.concat([stock.target1, stock.entryHigh]));
    var span = (max - min) || 1;
    var innerW = w - pad.l - pad.r;
    var innerH = h - pad.t - pad.b;
    var step = innerW / candles.length;
    var bodyW = Math.max(2.2, step * 0.56);
    function y(v) { return pad.t + (max - v) / span * innerH; }
    function x(i) { return pad.l + i * step + step / 2; }
    var out = '<svg viewBox="0 0 ' + w + ' ' + h + '" width="100%" height="' + h + '" role="img" aria-label="K线图">';
    out += '<defs><linearGradient id="entryBand" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#22d3ee" stop-opacity="0.12"/><stop offset="1" stop-color="#22d3ee" stop-opacity="0.03"/></linearGradient></defs>';
    for (var g = 0; g <= 4; g++) {
      var gy = pad.t + innerH * g / 4;
      var val = max - span * g / 4;
      out += '<line x1="' + pad.l + '" y1="' + gy + '" x2="' + (w - pad.r) + '" y2="' + gy + '" stroke="rgba(148,163,184,0.08)" stroke-width="1"></line>';
    }
    var bandTop = y(stock.entryHigh), bandBottom = y(stock.entryLow);
    out += '<rect x="' + pad.l + '" y="' + bandTop + '" width="' + innerW + '" height="' + Math.max(2, bandBottom - bandTop) + '" fill="url(#entryBand)"></rect>';
    [{ v: stock.stop, color: "#f04452", label: "止损" }, { v: stock.entryLow, color: "#22d3ee", label: "入场" }, { v: stock.target1, color: "#16c784", label: "目标" }].forEach(function (line) {
      out += '<line x1="' + pad.l + '" y1="' + y(line.v) + '" x2="' + (w - pad.r) + '" y2="' + y(line.v) + '" stroke="' + line.color + '" stroke-width="0.9" stroke-dasharray="3 3" opacity="0.75"></line>';
      out += '<text x="' + (w - pad.r + 5) + '" y="' + (y(line.v) + 3) + '" fill="' + line.color + '" font-size="6.5" font-family="monospace">' + line.label + ' ' + line.v.toFixed(2) + '</text>';
    });
    candles.forEach(function (c, i) {
      var up = c.close >= c.open;
      var color = up ? "#f04452" : "#16c784";
      var cx = x(i);
      out += '<line x1="' + cx + '" y1="' + y(c.high) + '" x2="' + cx + '" y2="' + y(c.low) + '" stroke="' + color + '" stroke-width="0.8" opacity="0.9"></line>';
      out += '<rect x="' + (cx - bodyW / 2) + '" y="' + Math.min(y(c.open), y(c.close)) + '" width="' + bodyW + '" height="' + Math.max(1, Math.abs(y(c.open) - y(c.close))) + '" fill="' + color + '" opacity="' + (up ? "0.9" : "0.85") + '"></rect>';
    });
    var ma = [];
    candles.forEach(function (c, i) {
      if (i < 4) return;
      var sum = 0;
      for (var k = i - 4; k <= i; k++) { sum += candles[k].close; }
      ma.push([x(i), y(sum / 5)]);
    });
    out += '<polyline points="' + ma.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" ") + '" fill="none" stroke="#22d3ee" stroke-width="1.2" opacity="0.85"></polyline>';
    out += '</svg>';
    return out;
  }

  function renderKpis() {
    var html = "";
    html += '<div class="kpi-card"><div class="kpi-top"><span>今日候选</span><span>' + icon("filter") + '</span></div><div class="kpi-value"><b>12</b><span>只</span><span class="kpi-delta up">+3</span></div><div class="kpi-foot">较昨日新增 3 只</div>' + sparkline([6, 8, 7, 9, 8, 10, 9, 11, 10, 12], "#22d3ee") + '</div>';
    html += '<div class="kpi-card"><div class="kpi-top"><span>强信号</span><span>评分 ≥ 80</span></div><div class="kpi-value"><b>5</b><span>只</span><span class="kpi-delta up">+2</span></div><div class="kpi-foot">全市场前 8.4%</div>' + sparkline([2, 3, 2, 4, 3, 5, 4, 4, 5, 5], "#16c784") + '</div>';
    html += '<div class="kpi-card"><div class="kpi-top"><span>平均评分</span><span>候选池</span></div><div class="kpi-value"><b>72.3</b><span>分</span><span class="kpi-delta up">+1.2</span></div><div class="kpi-foot">评分区间 41 – 91</div>' + sparkline([68, 69, 67, 70, 71, 72, 71, 73, 74, 74], "#8b5cf6") + '</div>';
    html += '<div class="kpi-card"><div class="kpi-top"><span>数据覆盖</span><span>日线</span></div><div class="kpi-value"><b>98.7</b><span>%</span></div><div class="kpi-foot">5,585 / 5,599 标的</div><div class="kpi-ring"><span>98.7</span></div></div>';
    byId("kpi-grid").innerHTML = html;
  }

  function filteredCandidates() {
    return candidates.filter(function (row) {
      var matchFilter = state.filter === "all" || row.signal === state.filter;
      var q = state.query.trim().toLowerCase();
      var matchQuery = !q || (row.code + row.name + row.industry).toLowerCase().indexOf(q) >= 0;
      return matchFilter && matchQuery;
    }).sort(function (a, b) {
      return state.sortDesc ? b.score - a.score : a.score - b.score;
    });
  }

  function renderTable() {
    var rows = filteredCandidates();
    var html = "";
    rows.forEach(function (row) {
      var cc = changeClass(row.change);
      var flowCls = row.flow >= 0 ? "up" : "down";
      var flowIcon = row.flow >= 0 ? "arrow-up" : "arrow-down";
      html += '<tr data-code="' + row.code + '" class="' + (state.selectedCode === row.code ? "is-selected" : "") + '">' +
        '<td><span class="ticker">' + row.code + '</span></td>' +
        '<td><div class="name-cell"><b>' + row.name + '</b><span>' + row.market + '</span></div></td>' +
        '<td><span class="industry">' + row.industry + '</span></td>' +
        '<td class="num price">' + fmtPrice(row.price) + '</td>' +
        '<td class="num change ' + cc + '">' + fmtPct(row.change) + '</td>' +
        '<td class="num amount">' + row.amount + '</td>' +
        '<td class="num turnover">' + row.turnover + '</td>' +
        '<td><span class="signal ' + row.signal + '">' + signalLabel(row.signal) + '</span></td>' +
        '<td><div class="score-cell"><div class="score-bar"><i style="width:' + row.score + '%"></i></div><b>' + row.score + '</b></div></td>' +
        '<td class="num"><span class="flow ' + flowCls + '">' + icon(flowIcon) + fmtFlow(row.flow) + '</span></td>' +
        '<td><span class="catalyst">' + row.catalyst.map(function (c) { return '<span>' + c + '</span>'; }).join("") + '</span></td>' +
        '<td class="num updated">' + row.updated + '</td>' +
        '</tr>';
    });
    byId("candidate-rows").innerHTML = html;
    byId("visible-count").textContent = String(rows.length);
    var sortInd = document.querySelector('[data-sort="score"] .sort-ind');
    if (sortInd) sortInd.textContent = state.sortDesc ? "↓" : "↑";
  }

  function renderCounts() {
    var counts = { all: candidates.length, strong: 0, watch: 0, risk: 0 };
    candidates.forEach(function (row) { if (counts[row.signal] !== undefined) counts[row.signal] += 1; });
    byId("count-all").textContent = String(counts.all);
    byId("count-strong").textContent = String(counts.strong);
    byId("count-watch").textContent = String(counts.watch);
    byId("count-risk").textContent = String(counts.risk);
  }

  function openDrawer(row) {
    state.selectedCode = row.code;
    renderTable();
    var cc = changeClass(row.change);
    var stance = row.signal === "strong" ? "等待回踩 · 不追高" : (row.signal === "risk" ? "风险未出清 · 回避" : "等待确认 · 控制仓位");
    var html = '<div class="drawer-head">' +
      '<div><div class="drawer-title"><h2>' + row.name + '</h2><span class="drawer-code">' + row.code + ' · ' + row.market + '</span></div>' +
      '<div class="drawer-price-row"><span class="drawer-price">' + fmtPrice(row.price) + '</span><span class="drawer-change ' + cc + '">' + fmtPct(row.change) + '</span><span class="updated">' + row.updated + ' 更新</span></div>' +
      '<div class="drawer-tags"><span class="tag">' + row.industry + '</span><span class="tag">成交额 ' + row.amount + '</span><span class="tag">换手 ' + row.turnover + '</span></div></div>' +
      '<button class="icon-btn" onclick="closeDrawer()" aria-label="关闭">' + icon("x") + '</button></div>' +
      '<div class="drawer-body">' +
      '<div><div class="drawer-section-title"><b>价格结构</b><span>日线 · 前复权</span></div><div class="chart-box"><div class="chart-legend"><span><i style="background:#f04452"></i>上涨</span><span><i style="background:#16c784"></i>下跌</span><span><i style="background:#22d3ee"></i>MA5</span><span>虚线：入场 / 止损 / 目标</span></div>' + candleSvg(row, 392, 168) + '</div></div>' +
      '<div><div class="drawer-section-title"><b>Agent 判断</b><span>Judge · ' + row.confidence + '% 置信度</span></div><div class="verdict-box">' +
      '<div class="verdict-top"><div><div class="verdict-label">' + row.verdict + '</div><div class="verdict-sub">' + stance + '</div></div><div class="confidence"><b>' + row.confidence + '%</b><span>置信度</span></div></div>' +
      '<div class="level-grid"><div class="level entry"><span>入场区</span><b>' + row.entryLow.toFixed(2) + ' – ' + row.entryHigh.toFixed(2) + '</b></div><div class="level stop"><span>止损</span><b>' + row.stop.toFixed(2) + '</b></div><div class="level target"><span>目标</span><b>' + row.target1.toFixed(2) + '</b></div></div>' +
      '</div></div>' +
      '<div><div class="drawer-section-title"><b>为什么是它</b><span>4 维证据</span></div><ul class="reason-list">' + row.reasons.map(function (r) { return '<li>' + r + '</li>'; }).join("") + '</ul></div>' +
      '<div><div class="drawer-section-title"><b>证据账本</b><span>可回溯</span></div><div class="evidence-stack">' +
      '<div class="evidence-row">' + icon("chart") + '<b>技术结构</b><span>突破回踩 · 收盘</span><span class="grade a">A</span></div>' +
      '<div class="evidence-row">' + icon("database") + '<b>资金流</b><span>' + fmtFlow(row.flow) + ' · 3 日</span><span class="grade a">A</span></div>' +
      '<div class="evidence-row">' + icon("news") + '<b>新闻催化</b><span>' + row.catalyst[0] + ' · 3 天前</span><span class="grade b">B</span></div>' +
      '<div class="evidence-row">' + icon("layers") + '<b>板块强度</b><span>前 10% · 收盘</span><span class="grade b">B</span></div>' +
      '</div></div>' +
      '<div class="drawer-actions"><button class="btn" onclick="showToast(\'打开个股详情：原型下一步补\')">' + icon("candles") + '打开个股</button><button class="btn primary" onclick="showToast(\'打开 Trace：原型下一步补\')">' + icon("route") + '查看 Trace</button></div>' +
      '</div>';
    byId("drawer").innerHTML = html;
    byId("drawer").classList.add("is-open");
    byId("drawer-overlay").classList.add("is-open");
  }

  function closeDrawer() {
    byId("drawer").classList.remove("is-open");
    byId("drawer-overlay").classList.remove("is-open");
    state.selectedCode = null;
    renderTable();
  }

  function groupTraceSteps() {
    var groups = [];
    traceSteps.forEach(function (step) {
      var group = groups.filter(function (g) { return g.name === step.group; })[0];
      if (!group) { group = { name: step.group, steps: [] }; groups.push(group); }
      group.steps.push(step);
    });
    return groups;
  }

  function renderTraceSteps() {
    var groups = groupTraceSteps();
    var html = "";
    groups.forEach(function (group) {
      var steps = group.steps.filter(function (step) {
        if (state.traceFilter === "tools") return step.group === "工具";
        if (state.traceFilter === "error") return step.status !== "ok";
        return true;
      });
      if (!steps.length) return;
      html += '<div class="trace-group"><div class="trace-group-label">' + group.name + '</div>';
      steps.forEach(function (step) {
        html += '<div class="trace-step ' + (state.selectedStep === step.id ? "is-active" : "") + '" data-step="' + step.id + '">' +
          '<span class="step-dot ' + step.status + '"></span>' +
          '<div class="step-copy"><span class="step-name">' + step.name + '</span><span class="step-meta"><span>' + step.duration + '</span><span class="tok">' + step.tokens + ' tok</span></span></div>' +
          '</div>';
      });
      html += '</div>';
    });
    byId("trace-steps").innerHTML = html;
  }

  function stepHead(step) {
    return '<div class="detail-title"><h2>' + step.name + '</h2><span class="badge success"><span class="status-dot ok"></span>成功</span><span class="run-id">' + step.duration + ' · ' + step.tokens + ' tokens</span></div>' +
      '<div class="detail-sub">' + step.group + ' · 2026-11-06 18:44:12 · trace run-20261106-1842</div>';
  }

  function stepBody(step, tab) {
    if (tab === "overview") {
      var html = '<div class="detail-section"><div class="detail-section-title"><b>执行摘要</b><span>model output</span></div><p class="summary-text">' + step.summary + '</p></div>';
      html += '<div class="detail-section"><div class="detail-section-title"><b>关键指标</b><span>structured</span></div><div class="metric-grid">' + step.metrics.map(function (m) { return '<div class="metric-card"><span>' + m.label + '</span><b class="' + (m.cls || "") + '">' + m.value + '</b></div>'; }).join("") + '</div></div>';
      if (step.chart) {
        html += '<div class="detail-section"><div class="detail-section-title"><b>价格结构</b><span>002050 · 日线</span></div><div class="mini-chart">' + candleSvg(candidates[0], 560, 220) + '</div></div>';
      }
      html += '<div class="detail-section"><div class="detail-section-title"><b>输出摘要</b><span>可读视图</span></div><div class="kv-list">' + step.output.map(function (item) { return '<div class="kv"><span>' + item.k + '</span><b>' + item.v + '</b></div>'; }).join("") + '</div></div>';
      html += '<div class="detail-section"><div class="detail-section-title"><b>下一步</b><span>actionable</span></div><div class="next-action"><div class="next-action-title">等待回踩 · 不追高</div><p>27.80–28.20 分批；高开超过 3% 不追；收盘跌破 26.90 失效。</p><div class="next-tags"><span>条件单</span><span>观察</span><span>失效条件明确</span></div></div></div>';
      return html;
    }
    if (tab === "input") {
      return '<div class="detail-section"><div class="detail-section-title"><b>工具输入</b><span>JSON</span></div><pre class="pre-block">' + esc(step.input) + '</pre></div>' +
        '<div class="detail-section"><div class="detail-section-title"><b>上下文</b><span>injected</span></div><div class="kv-list"><div class="kv"><span>股票</span><b>三花智控 002050</b></div><div class="kv"><span>数据日期</span><b>2026-11-06</b></div><div class="kv"><span>复权</span><b>前复权</b></div><div class="kv"><span>调用方</span><b>Agent Planner</b></div></div></div>';
    }
    if (tab === "output") {
      return '<div class="detail-section"><div class="detail-section-title"><b>结构化输出</b><span>validated</span></div><div class="kv-list">' + step.output.map(function (item) { return '<div class="kv"><span>' + item.k + '</span><b>' + item.v + '</b></div>'; }).join("") + '</div></div>' +
        '<div class="detail-section"><div class="detail-section-title"><b>原始输出</b><span>JSON</span></div><pre class="pre-block">{"status":"ok","step":"' + step.id + '","confidence":0.78,"data_date":"2026-11-06"}</pre></div>';
    }
    return '<div class="detail-section"><div class="detail-section-title"><b>证据引用</b><span>' + step.evidence.length + ' 条</span></div><div class="evidence-grid">' + step.evidence.map(function (e) { return '<div class="evidence-card"><div class="evidence-card-head">' + icon("database") + '<b>' + e.title + '</b><span class="grade ' + (e.grade === "A" ? "a" : "b") + '">' + e.grade + '</span></div><p>' + e.text + '</p><div class="evidence-card-head" style="margin-top:6px"><span>' + e.source + '</span><span>' + e.freshness + '</span></div></div>'; }).join("") + '</div></div>';
  }

  function renderTraceDetail() {
    var step = traceSteps.filter(function (s) { return s.id === state.selectedStep; })[0] || traceSteps[0];
    byId("trace-detail-head").innerHTML = stepHead(step);
    byId("trace-detail-body").innerHTML = stepBody(step, state.traceTab);
    var tabs = byId("trace-tabs").querySelectorAll("button");
    tabs.forEach(function (tab) { tab.classList.toggle("is-active", tab.getAttribute("data-tab") === state.traceTab); });
    var evidenceCount = byId("trace-tabs").querySelector('[data-tab="evidence"] .tab-count');
    if (evidenceCount) evidenceCount.textContent = String(step.evidence.length);
  }

  function renderVerdict() {
    var stock = candidates[0];
    var html = '<div class="verdict-hero"><div class="verdict-hero-top"><div><div class="verdict-big">' + stock.verdict + '</div><div class="verdict-note">Judge · 2026-11-06 18:44</div></div><div class="verdict-conf"><b>' + stock.confidence + '%</b><span>置信度</span></div></div>' +
      '<div class="level-grid"><div class="level entry"><span>入场区</span><b>' + stock.entryLow.toFixed(2) + ' – ' + stock.entryHigh.toFixed(2) + '</b></div><div class="level stop"><span>止损</span><b>' + stock.stop.toFixed(2) + '</b></div><div class="level target"><span>目标</span><b>' + stock.target1.toFixed(2) + '</b></div></div></div>' +
      '<div class="verdict-section"><div class="verdict-section-title">判断依据</div><ul class="reason-list">' + stock.reasons.map(function (r) { return '<li>' + r + '</li>'; }).join("") + '</ul></div>' +
      '<div class="verdict-section"><div class="verdict-section-title">风险提示</div><div class="risk-list">' + stock.risks.map(function (r) { return '<div class="risk-item">' + icon("alert") + '<span>' + r + '</span></div>'; }).join("") + '</div></div>' +
      '<div class="verdict-section"><div class="verdict-section-title">证据账本</div><div class="ledger">' +
      '<div class="ledger-row"><span>技术结构</span><b>已引用</b><span class="grade a">A</span></div>' +
      '<div class="ledger-row"><span>资金流</span><b>已引用</b><span class="grade a">A</span></div>' +
      '<div class="ledger-row"><span>新闻催化</span><b>已引用</b><span class="grade b">B</span></div>' +
      '<div class="ledger-row"><span>基本面</span><b>已引用</b><span class="grade b">B</span></div>' +
      '</div></div>' +
      '<div class="verdict-section"><div class="verdict-section-title">模型与数据</div><div class="kv-list"><div class="kv"><span>模型</span><b>deepseek-v3</b></div><div class="kv"><span>数据版本</span><b>2026-11-06 收盘</b></div><div class="kv"><span>Trace</span><b>run-20261106-1842</b></div><div class="kv"><span>Token</span><b>42.8k</b></div></div></div>' +
      '<div class="verdict-section"><div class="verdict-section-title">质量与限制</div><div class="kv-list"><div class="kv"><span>数据新鲜度</span><b>收盘</b></div><div class="kv"><span>证据覆盖</span><b>4 / 4</b></div><div class="kv"><span>回测验证</span><b>未验证</b></div></div></div>';
    byId("verdict-card").innerHTML = html;
  }

  function renderTrace() {
    renderTraceSteps();
    renderTraceDetail();
    renderVerdict();
  }

  function setView(view) {
    state.view = view;
    document.querySelectorAll(".view").forEach(function (v) { v.classList.toggle("is-active", v.id === "view-" + view); });
    document.querySelectorAll(".nav-item[data-view]").forEach(function (item) { item.classList.toggle("is-active", item.getAttribute("data-view") === view); });
    if (view === "trace") renderTrace();
    try {
      if (window.history && window.history.replaceState) window.history.replaceState(null, "", "#" + view);
    } catch (error) { /* file:// preview */ }
  }

  var toastTimer = null;
  function showToast(message) {
    var toast = byId("toast");
    toast.textContent = message;
    toast.classList.add("is-show");
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { toast.classList.remove("is-show"); }, 2400);
  }

  function bind() {
    document.querySelectorAll(".nav-item[data-view]").forEach(function (item) {
      item.addEventListener("click", function (event) { event.preventDefault(); setView(item.getAttribute("data-view")); });
    });
    document.querySelectorAll("[data-toast]").forEach(function (item) {
      item.addEventListener("click", function (event) { event.preventDefault(); showToast(item.getAttribute("data-toast")); });
    });
    document.querySelectorAll(".market-tabs button").forEach(function (button) {
      button.addEventListener("click", function () {
        document.querySelectorAll(".market-tabs button").forEach(function (b) { b.classList.remove("is-active"); });
        button.classList.add("is-active");
        if (!button.classList.contains("is-active")) return;
        showToast("原型仅演示沪深市场；港股/美股数据接入后再展开。");
      });
    });
    var tabs = byId("signal-tabs").querySelectorAll("button");
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        tabs.forEach(function (t) { t.classList.remove("is-active"); });
        tab.classList.add("is-active");
        state.filter = tab.getAttribute("data-filter");
        renderTable();
      });
    });
    byId("candidate-search").addEventListener("input", function (event) { state.query = event.target.value; renderTable(); });
    byId("candidate-rows").addEventListener("click", function (event) {
      var tr = event.target.closest("tr[data-code]");
      if (!tr) return;
      var row = candidates.filter(function (c) { return c.code === tr.getAttribute("data-code"); })[0];
      if (row) openDrawer(row);
    });
    document.querySelector('[data-sort="score"]').addEventListener("click", function () {
      state.sortDesc = !state.sortDesc;
      renderTable();
    });
    byId("drawer-overlay").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") closeDrawer();
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        var input = state.view === "trace" ? byId("global-search") : byId("candidate-search");
        if (input) input.focus();
      }
    });
    byId("refresh-btn").addEventListener("click", function () { showToast("已刷新（原型数据，未请求后端）"); });
    byId("global-search").addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      if (state.view === "candidates") {
        state.query = event.target.value;
        byId("candidate-search").value = event.target.value;
        renderTable();
        showToast("已在候选池中搜索：" + (event.target.value || "全部"));
      } else {
        showToast("Trace 搜索原型下一步补。");
      }
    });
    byId("trace-steps").addEventListener("click", function (event) {
      var step = event.target.closest("[data-step]");
      if (!step) return;
      state.selectedStep = step.getAttribute("data-step");
      state.traceTab = "overview";
      renderTraceSteps();
      renderTraceDetail();
    });
    byId("trace-tabs").addEventListener("click", function (event) {
      var tab = event.target.closest("button[data-tab]");
      if (!tab) return;
      state.traceTab = tab.getAttribute("data-tab");
      renderTraceDetail();
    });
    document.querySelectorAll("[data-trace-filter]").forEach(function (button) {
      button.addEventListener("click", function () {
        document.querySelectorAll("[data-trace-filter]").forEach(function (b) { b.classList.remove("is-active"); });
        button.classList.add("is-active");
        state.traceFilter = button.getAttribute("data-trace-filter");
        renderTraceSteps();
      });
    });
  }

  window.closeDrawer = closeDrawer;
  window.showToast = showToast;

  function init() {
    renderKpis();
    renderCounts();
    renderTable();
    renderTrace();
    bind();
    window.addEventListener("hashchange", function () {
      setView(window.location.hash === "#trace" ? "trace" : "candidates");
    });
    if (window.location.hash === "#trace") setView("trace");
    else setView("candidates");
  }

  init();
})();
