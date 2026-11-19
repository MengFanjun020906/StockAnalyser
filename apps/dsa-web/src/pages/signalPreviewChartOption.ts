import type * as echarts from 'echarts';
import type {
  ChanSignal,
  ChanPen,
  EntrySignal,
  EntryZone,
  LifecycleEvent,
  PositionWatch,
  PricePlanArtifact,
  SignalPreviewDetail,
} from '../api/signalPreview';

const COLORS = {
  rise: '#ef4444',
  fall: '#16a34a',
  guide: '#0ea5e9',
  ma57: '#f59e0b',
  dif: '#2563eb',
  dea: '#f97316',
  penUp: '#dc2626',
  penDown: '#15803d',
  grid: 'rgba(148, 163, 184, 0.18)',
  text: '#64748b',
};

const CHAN_SIGNAL_LABELS: Record<string, string> = {
  first_buy: '一买',
  second_buy: '二买',
  third_buy: '三买',
  first_sell: '一卖',
  second_sell: '二卖',
  third_sell: '三卖',
  center_breakout: '中枢突破',
  center_breakdown: '中枢下破',
  bottom_divergence: '底背驰',
  top_divergence: '顶背驰',
  bottom_exhaustion: '底钝化',
  top_exhaustion: '顶钝化',
  weekly_confirmation: '周线确认',
};

function day(value?: string | null): string {
  return String(value || '').slice(0, 10);
}

function finite(value: unknown): number | null {
  // Number(null) === 0 和 Number('') === 0 会把"没有这个价位"画成 0 元止损线。
  if (value == null || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function price(value: number): string {
  return Number(value).toFixed(2);
}

export type SignalChartRenderOptions = {
  /** 默认窗口内展示的 K 线根数；窄屏传更小的值，避免 180 根挤成一团。 */
  visibleBars?: number;
  /** 窄屏模式：隐藏价格线标签、缩小事件点，减少遮挡。 */
  compact?: boolean;
};

function categoryDate(value: string | null | undefined, dates: string[], edge: 'start' | 'end'): string | undefined {
  if (!dates.length) return undefined;
  const target = day(value);
  if (!target) return edge === 'start' ? dates[0] : dates.at(-1);
  if (edge === 'start') return dates.find((date) => date >= target) || dates.at(-1);
  return dates.slice().reverse().find((date) => date <= target) || dates[0];
}

function entryPlanAreas(
  entry: EntrySignal | null,
  events: LifecycleEvent[],
  dates: string[],
): Array<Array<Record<string, unknown>>> {
  if (!entry || !dates.length) return [];
  const normalizedZones = (value: unknown): EntryZone[] => {
    if (!Array.isArray(value)) return [];
    return value.flatMap((candidate) => {
      if (!candidate || typeof candidate !== 'object') return [];
      const zone = candidate as Record<string, unknown>;
      const low = finite(zone.low);
      const high = finite(zone.high);
      if (low == null || high == null) return [];
      return [{
        label: String(zone.label || 'entry'),
        low: Math.min(low, high),
        high: Math.max(low, high),
        rationale: typeof zone.rationale === 'string' ? zone.rationale : undefined,
      }];
    });
  };
  const revisions = events
    .filter((event) => ['signal_created', 'signal_revised'].includes(event.eventType))
    .flatMap((event) => {
      let zones = normalizedZones(event.payload?.entryZones);
      if (!zones.length) {
        const low = finite(event.payload?.entryLow);
        const high = finite(event.payload?.entryHigh);
        if (low == null || high == null) return [];
        zones = [{ label: 'entry', low: Math.min(low, high), high: Math.max(low, high) }];
      }
      return [{
        occurredAt: event.occurredAt,
        zones,
        revision: Number(event.payload?.revision) || 1,
      }];
    })
    .sort((left, right) => left.occurredAt.localeCompare(right.occurredAt));

  if (!revisions.length) {
    let zones = normalizedZones(entry.triggerRule?.zones);
    if (!zones.length) {
      const low = finite(entry.entryLow);
      const high = finite(entry.entryHigh);
      if (low == null || high == null) return [];
      zones = [{ label: 'entry', low: Math.min(low, high), high: Math.max(low, high) }];
    }
    revisions.push({
      occurredAt: entry.createdAsOf,
      zones,
      revision: entry.revision || 1,
    });
  }

  return revisions.flatMap((revision, index) => {
    const start = categoryDate(revision.occurredAt, dates, 'start');
    const nextRevision = revisions[index + 1];
    const end = categoryDate(nextRevision?.occurredAt || entry.validUntil, dates, 'end');
    if (!start || !end || start > end) return [];
    const isCurrent = index === revisions.length - 1;
    const labelText: Record<string, string> = { ideal: '理想', secondary: '次优', entry: '入场' };
    const currentColors = [
      ['rgba(14, 165, 233, 0.18)', '#38bdf8'],
      ['rgba(13, 148, 136, 0.16)', '#2dd4bf'],
    ];
    return revision.zones.map((zone, zoneIndex) => {
      const baseName = revisions.length > 1 ? `计划区 R${revision.revision}` : '计划入场区';
      const name = revision.zones.length > 1
        ? `${baseName} · ${labelText[zone.label] || zone.label}`
        : baseName;
      const colors = currentColors[zoneIndex % currentColors.length];
      return [
        {
          name,
          xAxis: start,
          yAxis: zone.low,
          itemStyle: {
            color: isCurrent ? colors[0] : 'rgba(100, 116, 139, 0.10)',
            borderColor: isCurrent ? colors[1] : '#94a3b8',
            borderWidth: 1,
            borderType: isCurrent ? 'solid' : 'dashed',
          },
        },
        { xAxis: end, yAxis: zone.high },
      ];
    });
  });
}

function deterministicPlanAreas(
  plan: PricePlanArtifact | undefined,
  dates: string[],
): Array<Array<Record<string, unknown>>> {
  if (!plan || !dates.length) return [];
  const labels = { primary: '主入场区', secondary: '次入场区', observation: '观察区' };
  const colors = {
    primary: ['rgba(14, 165, 233, 0.24)', '#0284c7'],
    secondary: ['rgba(13, 148, 136, 0.20)', '#2dd4bf'],
    observation: ['rgba(100, 116, 139, 0.12)', 'rgba(148, 163, 184, 0.55)'],
  };
  return plan.scenarios.flatMap((scenario) => {
    const start = categoryDate(scenario.validFrom || plan.actualAsOfSession, dates, 'start');
    const end = categoryDate(scenario.confirmationDeadline || dates.at(-1), dates, 'end');
    if (!start || !end || start > end) return [];
    const palette = colors[scenario.role];
    // 计划窗口完全落在最后一根 K 线之后（新计划常见）时没有可画的时间跨度，
    // 退化为整幅价格带，标签交给页面上的"价格区间"图例，避免把标签塞进 K 线。
    const fullWidth = start === end;
    const label = labels[scenario.role];
    return [[
      {
        name: label,
        ...(fullWidth ? {} : { xAxis: start }),
        yAxis: scenario.entryLow,
        itemStyle: {
          color: palette[0],
          borderColor: palette[1],
          borderWidth: 1,
        },
      },
      { ...(fullWidth ? {} : { xAxis: end }), yAxis: scenario.entryHigh },
    ]];
  });
}

function subjectEntry(detail: SignalPreviewDetail): EntrySignal | null {
  return detail.kind === 'entry_signal' ? detail.subject as EntrySignal : null;
}

function subjectWatch(detail: SignalPreviewDetail): PositionWatch | null {
  return detail.kind === 'position_watch' ? detail.subject as PositionWatch : null;
}

function escapeTooltipText(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatTooltipValue(value: unknown, seriesName: string): string {
  if (Array.isArray(value)) {
    // ECharts category-axis candlesticks prepend the category index to value.
    const numbers = value.map((item) => Number(item));
    const [open, close, low, high] = numbers.length >= 5 ? numbers.slice(-4) : numbers;
    if ([open, close, low, high].every(Number.isFinite)) {
      return `开 ${open.toFixed(2)} / 收 ${close.toFixed(2)} / 高 ${high.toFixed(2)} / 低 ${low.toFixed(2)}`;
    }
  }
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return escapeTooltipText(value);
  if (seriesName.includes('成交量') || seriesName.includes('量 MA')) {
    return `${(numeric / 10000).toFixed(1)} 万`;
  }
  return numeric.toFixed(2);
}

function hasTooltipValue(value: unknown): boolean {
  if (Array.isArray(value)) return value.length > 0 && value.every((item) => Number.isFinite(Number(item)));
  if (value == null || value === '-') return false;
  return Number.isFinite(Number(value));
}

function compactTooltipFormatter(params: unknown): string {
  const rows = (Array.isArray(params) ? params : [params]) as Array<{
    axisValue?: unknown;
    axisValueLabel?: unknown;
    seriesName?: unknown;
    value?: unknown;
    marker?: string;
  }>;
  const first = rows[0];
  const dateLabel = first?.axisValueLabel ?? first?.axisValue ?? '--';
  const valueRows = rows
    .filter((row) => row?.seriesName && row.seriesName !== '缠论笔' && hasTooltipValue(row.value))
    .map((row) => {
      const name = escapeTooltipText(row.seriesName);
      const value = formatTooltipValue(row.value, String(row.seriesName));
      return `<div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;line-height:1.35"><span style="color:rgba(226,232,240,.78);white-space:nowrap">${row.marker || ''}${name}</span><strong style="color:#f8fafc;font-weight:600;white-space:nowrap">${value}</strong></div>`;
    });
  return `<div style="min-width:170px;max-width:240px"><div style="margin-bottom:5px;padding-bottom:4px;border-bottom:1px solid rgba(148,163,184,.25);color:#f8fafc;font-weight:600;line-height:1.2">${escapeTooltipText(dateLabel)}</div>${valueRows.join('')}</div>`;
}

const EVENT_CHART_LABELS: Record<string, string> = {
  signal_created: '创建',
  signal_revised: '修订',
  target_drift_rejected: '拒绝漂移',
  signal_checked: '复核',
  signal_needs_review: '待复核',
  signal_reactivated: '重新生效',
  entry_price_triggered: '触价',
  signal_invalidated: '失效',
  signal_expired: '到期',
  signal_ambiguous: '待确认',
  position_opened: '纳入监控',
  position_changed: '持仓变化',
  position_checked: '日检',
  position_risk_changed: '风险变化',
  position_closed: '持仓结束',
  plan_created: '价格计划创建',
  touched: '进入区间',
  technical_confirmed: '技术确认',
  invalidated: '场景失效',
  expired: '场景到期',
  ambiguous: '路径不明',
  superseded: '被替代',
  closed: '场景结束',
  center_breakout: '中枢突破',
  center_breakdown: '中枢下破',
};

function eventLabel(event: LifecycleEvent): string {
  // 兜底必须是中文，不能把后端枚举直接漏到图上。
  return EVENT_CHART_LABELS[event.eventType] || '信号事件';
}

function penSeries(pens: ChanPen[]) {
  return pens.flatMap((pen, index) => {
    const startTime = day(pen.start?.time);
    const endTime = day(pen.end?.time);
    const startPrice = finite(pen.start?.price);
    const endPrice = finite(pen.end?.price);
    if (!startTime || !endTime || startPrice == null || endPrice == null) return [];
    return [{
      name: `缠论笔 ${pen.id ?? index + 1}`,
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: [[startTime, startPrice], [endTime, endPrice]],
      showSymbol: true,
      symbolSize: 5,
      connectNulls: false,
      silent: true,
      lineStyle: {
        color: pen.direction === 'up' ? COLORS.penUp : COLORS.penDown,
        width: 1.5,
        opacity: 0.8,
      },
      itemStyle: { color: pen.direction === 'up' ? COLORS.penUp : COLORS.penDown },
      tooltip: { show: false },
      z: 8,
    }];
  });
}

function chanSignalPoints(
  signals: ChanSignal[],
  pens: ChanPen[],
  dates: string[],
  fallbackPrice: number,
  compact: boolean,
) {
  const seen = new Set<string>();
  return signals.flatMap((signal) => {
    if (signal.triggered === false) return [];
    const metrics = signal.metrics || {};
    const penId = Number(metrics.current_pen_id);
    const pen = Number.isFinite(penId) ? pens.find((item) => Number(item.id) === penId) : undefined;
    const pointDate = day(pen?.end?.time) || dates.at(-1);
    const pointPrice = finite(pen?.end?.price) ?? fallbackPrice;
    if (!pointDate || pointPrice == null) return [];
    const name = CHAN_SIGNAL_LABELS[signal.name || ''] || signal.name || '结构信号';
    // 同一根 K 线上多个结构信号只留一个点，细节交给 tooltip。
    const key = `${name}:${pointDate}`;
    if (seen.has(key)) return [];
    seen.add(key);
    const isSell = /sell|divergence|exhaustion|breakdown/.test(signal.name || '') && !/bottom/.test(signal.name || '');
    const isBuy = /buy|bottom|breakout/.test(signal.name || '') && !/top/.test(signal.name || '');
    return [{
      name,
      coord: [pointDate, pointPrice],
      value: name,
      symbol: 'triangle',
      symbolRotate: isSell ? 180 : 0,
      symbolSize: compact ? 10 : 14,
      itemStyle: { color: isSell ? '#16a34a' : isBuy ? '#dc2626' : '#0ea5e9' },
      label: { show: false },
      tooltip: { formatter: `${name}${signal.timeframe ? ` · ${signal.timeframe}` : ''}` },
    }];
  });
}

export function buildSignalPreviewChartOption(
  detail: SignalPreviewDetail,
  renderOptions: SignalChartRenderOptions = {},
): echarts.EChartsOption {
  const bars = detail.chart.bars || [];
  const dates = bars.map((bar) => bar.date);
  const entry = subjectEntry(detail);
  const watch = subjectWatch(detail);
  const lastClose = finite(bars.at(-1)?.close) ?? 0;
  const centers = detail.chart.structure?.centers || [];
  const structureSignals = detail.chart.structure?.signals || [];
  const compact = renderOptions.compact ?? false;
  const visibleBars = Math.max(20, Math.floor(renderOptions.visibleBars ?? 180));

  const markAreas: Array<Array<Record<string, unknown>>> = [
    ...entryPlanAreas(entry, detail.events, dates),
    ...deterministicPlanAreas(detail.pricePlan, dates),
  ];
  centers.forEach((center) => {
    const low = finite(center.zd ?? center.ZD);
    const high = finite(center.zg ?? center.ZG);
    const startDate = center.startDate || dates[center.startIndex ?? -1];
    const endDate = center.endDate || dates[center.endIndex ?? -1];
    if (low == null || high == null || !startDate || !endDate) return;
    // 只画面 ZG–ZD 的重叠核心区间；GG/DD（含进出段极值）不画，否则整段行情都会被框住。
    const name = `中枢 ${center.id ?? ''}`.trim();
    markAreas.push([
      {
        name,
        xAxis: startDate,
        yAxis: low,
        // 中枢只画"淡填充 + 明确描边"，文字标签交给页面图例：
        // markArea 的标签在窄带/密集区域会被挤位或压到 K 线上，反而更乱。
        itemStyle: { color: 'rgba(245, 158, 11, 0.06)', borderColor: 'rgba(245, 158, 11, 0.75)', borderWidth: 1 },
      },
      { xAxis: endDate, yAxis: high },
    ]);
  });

  const markLines: Array<Record<string, unknown>> = [];
  const seenLines = new Set<string>();
  const pushPriceLine = (kind: string, name: string, value: number | null, style: Record<string, unknown>) => {
    if (value == null) return;
    // 同一价位只画一条：三个场景的目标价通常完全相同，重复绘制会叠成"虚线墙"。
    const key = `${kind}:${value}`;
    if (seenLines.has(key)) return;
    seenLines.add(key);
    markLines.push({
      name: `${name} ${price(value)}`,
      yAxis: value,
      lineStyle: style,
      label: {
        show: !compact,
        position: 'insideStartTop',
        formatter: '{b}',
        color: '#cbd5e1',
        fontSize: 10,
        backgroundColor: 'rgba(2, 6, 23, 0.72)',
        padding: [2, 4],
        borderRadius: 3,
      },
    });
  };

  pushPriceLine('stop', '止损', finite(entry?.stopPrice ?? watch?.stopPrice), { color: '#dc2626', type: 'dashed', width: 1.4 });
  pushPriceLine('cost', '持仓成本', finite(watch?.averageCost), { color: '#7c3aed', type: 'dashed', width: 1.4 });

  const scenarios = detail.pricePlan?.scenarios || [];
  const selectedScenarioId = detail.accountDecision?.selectedScenarioId;
  // 观察场景没有可执行的止损/目标，不再往图上堆虚线；有选中场景时只画选中场景。
  const executableScenarios = scenarios.filter((scenario) => scenario.role !== 'observation');
  const selectedScenarios = executableScenarios.filter((scenario) => scenario.scenarioId === selectedScenarioId);
  (selectedScenarios.length ? selectedScenarios : executableScenarios).forEach((scenario) => {
    const prefix = scenario.role === 'primary' ? '主' : '次';
    const color = scenario.role === 'primary' ? '#0284c7' : '#0f766e';
    pushPriceLine('stop', `${prefix}止损`, finite(scenario.stopLoss), { color: '#dc2626', type: 'dashed', width: 1.2 });
    pushPriceLine('target1', `${prefix}目标一`, finite(scenario.target1), { color, type: 'dotted', width: 1.2 });
    pushPriceLine('target2', `${prefix}目标二`, finite(scenario.target2), { color, type: 'dashed', width: 1.2 });
  });

  const markPoints: Array<Record<string, unknown>> = [];
  const seenPoints = new Set<string>();
  detail.events.forEach((event) => {
    const occurred = day(event.occurredAt);
    if (!occurred) return;
    // 同一时刻的同类事件只留一个点（如三个场景各记一条 plan_created）。
    const key = `${event.eventType}:${occurred}`;
    if (seenPoints.has(key)) return;
    seenPoints.add(key);
    const payloadBar = event.payload?.bar as Record<string, unknown> | undefined;
    const value = finite(payloadBar?.close) ?? lastClose;
    const isRisk = /invalidated|risk_changed|closed|expired|ambiguous|superseded/.test(event.eventType);
    markPoints.push({
      name: eventLabel(event),
      coord: [occurred, value],
      value: eventLabel(event),
      symbol: isRisk ? 'diamond' : 'circle',
      symbolSize: compact ? (isRisk ? 10 : 8) : (isRisk ? 14 : 10),
      itemStyle: { color: isRisk ? '#dc2626' : '#0284c7' },
      label: { show: false },
    });
  });
  (detail.chart.corporateActions || []).forEach((action) => {
    markPoints.push({
      name: action.note || action.actionType,
      coord: [action.date, lastClose],
      value: '除权',
      symbol: 'pin',
      symbolSize: 18,
      itemStyle: { color: '#7c3aed' },
      label: { show: true, color: '#ffffff', fontSize: 9, formatter: '权' },
    });
  });
  markPoints.push(...chanSignalPoints(structureSignals, detail.chart.structure?.pens || [], dates, lastClose, compact));

  const zoomStart = Math.max(0, dates.length - visibleBars);
  const commonCategoryAxis = {
    type: 'category' as const,
    data: dates,
    boundaryGap: false,
    axisLine: { lineStyle: { color: COLORS.grid } },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text, fontSize: 10, hideOverlap: true },
    splitLine: { show: false },
  };
  const noPointerLabel = { label: { show: false } };

  const option: echarts.EChartsOption = {
    animation: false,
    backgroundColor: 'transparent',
    legend: {
      type: 'scroll',
      top: 4,
      left: 12,
      right: 12,
      itemWidth: 14,
      itemHeight: 8,
      textStyle: { color: COLORS.text, fontSize: 11 },
      data: ['日K', 'EMA13 指导线', 'SMA57', '成交量', '量 MA5', 'MACD', 'DIF', 'DEA'],
    },
    axisPointer: { link: [{ xAxisIndex: [0, 1, 2] }], label: { backgroundColor: '#334155' } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      confine: true,
      borderWidth: 1,
      borderColor: 'rgba(148, 163, 184, .35)',
      backgroundColor: 'rgba(15, 23, 42, .94)',
      padding: [7, 9],
      textStyle: { color: '#f8fafc', fontSize: 11, lineHeight: 15 },
      extraCssText: 'max-width:240px;border-radius:6px;box-shadow:0 10px 24px rgba(2,6,23,.28);',
      formatter: compactTooltipFormatter,
      order: 'seriesDesc',
    },
    grid: [
      { left: 14, right: 64, top: 34, height: '53%' },
      { left: 14, right: 64, top: '62%', height: '11%' },
      { left: 14, right: 64, top: '77%', height: '14%' },
    ],
    xAxis: [
      { ...commonCategoryAxis, gridIndex: 0, axisLabel: { show: false }, axisPointer: noPointerLabel },
      { ...commonCategoryAxis, gridIndex: 1, axisLabel: { show: false }, axisPointer: noPointerLabel },
      { ...commonCategoryAxis, gridIndex: 2 },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, position: 'right', axisLabel: { color: COLORS.text, fontSize: 10 }, splitLine: { lineStyle: { color: COLORS.grid } } },
      {
        scale: true,
        gridIndex: 1,
        position: 'right',
        splitNumber: compact ? 2 : 3,
        axisLabel: {
          // 窄屏成交量副图只有 ~46px 高，放不下刻度文字，数值交给 tooltip。
          show: !compact,
          color: COLORS.text,
          fontSize: 10,
          hideOverlap: true,
          formatter: (value: number) => (Math.abs(value) >= 1e8 ? `${(value / 1e8).toFixed(1)}亿` : `${Math.round(value / 1e4)}万`),
        },
        splitLine: { lineStyle: { color: COLORS.grid } },
      },
      { scale: true, gridIndex: 2, position: 'right', splitNumber: compact ? 2 : 3, axisLabel: { color: COLORS.text, fontSize: 10, hideOverlap: true }, splitLine: { lineStyle: { color: COLORS.grid } } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], startValue: zoomStart, endValue: Math.max(0, dates.length - 1), minValueSpan: 20 },
      { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 4, height: 18, startValue: zoomStart, endValue: Math.max(0, dates.length - 1), borderColor: COLORS.grid, fillerColor: 'rgba(14, 165, 233, .12)', handleStyle: { color: '#0ea5e9' }, textStyle: { color: COLORS.text } },
    ],
    series: [
      {
        name: '日K',
        type: 'candlestick',
        xAxisIndex: 0,
        yAxisIndex: 0,
        data: bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]),
        itemStyle: { color: COLORS.rise, color0: COLORS.fall, borderColor: COLORS.rise, borderColor0: COLORS.fall },
        markArea: {
          silent: true,
          z: 1,
          label: { show: false },
          data: markAreas,
        },
        markLine: { silent: true, symbol: ['none', 'none'], data: markLines },
        markPoint: { data: markPoints, tooltip: { formatter: '{b}' }, label: { show: false } },
        z: 3,
      },
      { name: 'EMA13 指导线', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map((bar) => bar.guideLine), showSymbol: false, connectNulls: false, lineStyle: { color: COLORS.guide, width: 1.4 }, z: 5 },
      { name: 'SMA57', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: bars.map((bar) => bar.ma57), showSymbol: false, connectNulls: false, lineStyle: { color: COLORS.ma57, width: 1.3 }, z: 5 },
      ...penSeries(detail.chart.structure?.pens || []),
      { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: bars.map((bar) => ({ value: bar.volume, itemStyle: { color: Number(bar.close) >= Number(bar.open) ? 'rgba(239, 68, 68, .58)' : 'rgba(22, 163, 74, .58)' } })), z: 2 },
      { name: '量 MA5', type: 'line', xAxisIndex: 1, yAxisIndex: 1, data: bars.map((bar) => bar.volumeMa5), showSymbol: false, lineStyle: { color: '#7c3aed', width: 1.2 }, z: 4 },
      { name: 'MACD', type: 'bar', xAxisIndex: 2, yAxisIndex: 2, data: bars.map((bar) => ({ value: bar.macdHistogram, itemStyle: { color: Number(bar.macdHistogram) >= 0 ? 'rgba(239, 68, 68, .6)' : 'rgba(22, 163, 74, .6)' } })), z: 2 },
      { name: 'DIF', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: bars.map((bar) => bar.dif), showSymbol: false, lineStyle: { color: COLORS.dif, width: 1.2 }, z: 3 },
      { name: 'DEA', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: bars.map((bar) => bar.dea), showSymbol: false, lineStyle: { color: COLORS.dea, width: 1.2 }, z: 3 },
    ] as echarts.SeriesOption[],
  };
  return option;
}
