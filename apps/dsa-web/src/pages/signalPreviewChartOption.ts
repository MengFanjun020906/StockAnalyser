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
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

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
          label: {
            show: true,
            position: 'insideTopLeft',
            color: colors[1],
            fontSize: 10,
            fontWeight: 700,
            formatter: name,
          },
          itemStyle: {
            color: isCurrent ? colors[0] : 'rgba(100, 116, 139, 0.10)',
            borderColor: isCurrent ? colors[1] : '#94a3b8',
            borderWidth: 2,
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
    observation: ['rgba(100, 116, 139, 0.15)', '#cbd5e1'],
  };
  return plan.scenarios.flatMap((scenario) => {
    const start = categoryDate(scenario.validFrom || plan.actualAsOfSession, dates, 'start');
    const end = categoryDate(scenario.confirmationDeadline || dates.at(-1), dates, 'end');
    if (!start || !end || start > end) return [];
    const palette = colors[scenario.role];
    const fullWidth = start === end;
    const label = labels[scenario.role];
    return [[
      {
        name: label,
        ...(fullWidth ? {} : { xAxis: start }),
        yAxis: scenario.entryLow,
        label: {
          show: true,
          position: 'insideTopLeft',
          color: palette[1],
          fontSize: 11,
          fontWeight: 700,
          formatter: label,
        },
        itemStyle: {
          color: palette[0],
          borderColor: palette[1],
          borderWidth: 2,
          borderType: scenario.role === 'observation' ? 'dashed' : 'solid',
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

function eventLabel(event: LifecycleEvent): string {
  const labels: Record<string, string> = {
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
  };
  return labels[event.eventType] || event.eventType;
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

function chanSignalPoints(signals: ChanSignal[], pens: ChanPen[], dates: string[], fallbackPrice: number) {
  return signals.flatMap((signal) => {
    if (signal.triggered === false) return [];
    const metrics = signal.metrics || {};
    const penId = Number(metrics.current_pen_id);
    const pen = Number.isFinite(penId) ? pens.find((item) => Number(item.id) === penId) : undefined;
    const pointDate = day(pen?.end?.time) || dates.at(-1);
    const pointPrice = finite(pen?.end?.price) ?? fallbackPrice;
    if (!pointDate || pointPrice == null) return [];
    const name = CHAN_SIGNAL_LABELS[signal.name || ''] || signal.name || '结构信号';
    const isSell = /sell|divergence|exhaustion|breakdown/.test(signal.name || '') && !/bottom/.test(signal.name || '');
    const isBuy = /buy|bottom|breakout/.test(signal.name || '') && !/top/.test(signal.name || '');
    return [{
      name,
      coord: [pointDate, pointPrice],
      value: name,
      symbol: 'triangle',
      symbolRotate: isSell ? 180 : 0,
      symbolSize: name.length > 3 ? 28 : 24,
      itemStyle: { color: isSell ? '#16a34a' : isBuy ? '#dc2626' : '#0ea5e9' },
      label: { color: '#ffffff', fontSize: 10, fontWeight: 700, formatter: name },
      tooltip: { formatter: `${name}${signal.timeframe ? ` · ${signal.timeframe}` : ''}` },
    }];
  });
}

export function buildSignalPreviewChartOption(detail: SignalPreviewDetail): echarts.EChartsOption {
  const bars = detail.chart.bars || [];
  const dates = bars.map((bar) => bar.date);
  const entry = subjectEntry(detail);
  const watch = subjectWatch(detail);
  const lastClose = finite(bars.at(-1)?.close) ?? 0;
  const centers = detail.chart.structure?.centers || [];
  const structureSignals = detail.chart.structure?.signals || [];

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
    markAreas.push([
      {
        name: `中枢 ${center.id ?? ''}`.trim(),
        xAxis: startDate,
        yAxis: low,
        itemStyle: { color: 'rgba(245, 158, 11, 0.08)', borderColor: 'rgba(245, 158, 11, 0.38)', borderWidth: 1 },
      },
      { xAxis: endDate, yAxis: high },
    ]);
  });

  const markLines: Array<Record<string, unknown>> = [];
  const stop = finite(entry?.stopPrice ?? watch?.stopPrice);
  if (stop != null) {
    markLines.push({ name: '止损', yAxis: stop, lineStyle: { color: '#dc2626', type: 'dashed', width: 1.4 } });
  }
  if (watch && finite(watch.averageCost) != null) {
    markLines.push({ name: '持仓成本', yAxis: watch.averageCost, lineStyle: { color: '#7c3aed', type: 'dashed', width: 1.4 } });
  }
  const roleNames = { primary: '主场景', secondary: '次场景', observation: '观察场景' };
  const roleColors = { primary: '#0284c7', secondary: '#0f766e', observation: '#64748b' };
  (detail.pricePlan?.scenarios || []).forEach((scenario) => {
    const prefix = roleNames[scenario.role];
    const color = roleColors[scenario.role];
    const planStop = finite(scenario.stopLoss);
    const target1 = finite(scenario.target1);
    const target2 = finite(scenario.target2);
    if (planStop != null) markLines.push({ name: `${prefix}止损`, yAxis: planStop, lineStyle: { color: '#dc2626', type: 'dashed', width: 1.2 } });
    if (target1 != null) markLines.push({ name: `${prefix}目标一`, yAxis: target1, lineStyle: { color, type: 'dotted', width: 1.2 } });
    if (target2 != null) markLines.push({ name: `${prefix}目标二`, yAxis: target2, lineStyle: { color, type: 'dashed', width: 1.2 } });
  });

  const markPoints: Array<Record<string, unknown>> = detail.events.map((event) => {
    const payloadBar = event.payload?.bar as Record<string, unknown> | undefined;
    const value = finite(payloadBar?.close) ?? lastClose;
    const isRisk = /invalidated|risk_changed|closed|expired|ambiguous/.test(event.eventType);
    return {
      name: eventLabel(event),
      coord: [day(event.occurredAt), value],
      value: eventLabel(event),
      symbol: isRisk ? 'diamond' : 'circle',
      symbolSize: isRisk ? 28 : 20,
      itemStyle: { color: isRisk ? '#dc2626' : '#0284c7' },
      label: { color: '#ffffff', fontSize: 9, formatter: eventLabel(event) },
    };
  });
  (detail.chart.corporateActions || []).forEach((action) => {
    markPoints.push({
      name: action.note || action.actionType,
      coord: [action.date, lastClose],
      value: '除权',
      symbol: 'pin',
      symbolSize: 28,
      itemStyle: { color: '#7c3aed' },
      label: { color: '#ffffff', fontSize: 9, formatter: '权' },
    });
  });
  markPoints.push(...chanSignalPoints(structureSignals, detail.chart.structure?.pens || [], dates, lastClose));

  const zoomStart = Math.max(0, dates.length - 180);
  const commonCategoryAxis = {
    type: 'category' as const,
    data: dates,
    boundaryGap: false,
    axisLine: { lineStyle: { color: COLORS.grid } },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text, fontSize: 10, hideOverlap: true },
    splitLine: { show: false },
  };

  const option: echarts.EChartsOption = {
    animation: false,
    backgroundColor: 'transparent',
    legend: {
      top: 4,
      left: 12,
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
      { left: 58, right: 18, top: 38, height: '54%' },
      { left: 58, right: 18, top: '63%', height: '12%' },
      { left: 58, right: 18, top: '78%', height: '14%' },
    ],
    xAxis: [
      { ...commonCategoryAxis, gridIndex: 0, axisLabel: { show: false } },
      { ...commonCategoryAxis, gridIndex: 1, axisLabel: { show: false } },
      { ...commonCategoryAxis, gridIndex: 2 },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, position: 'right', axisLabel: { color: COLORS.text, fontSize: 10 }, splitLine: { lineStyle: { color: COLORS.grid } } },
      { scale: true, gridIndex: 1, position: 'right', axisLabel: { color: COLORS.text, fontSize: 10, formatter: (value: number) => `${Math.round(value / 10000)}万` }, splitLine: { lineStyle: { color: COLORS.grid } } },
      { scale: true, gridIndex: 2, position: 'right', axisLabel: { color: COLORS.text, fontSize: 10 }, splitLine: { lineStyle: { color: COLORS.grid } } },
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
          label: { show: true, color: COLORS.text, fontSize: 10 },
          data: markAreas,
        },
        markLine: { silent: true, symbol: ['none', 'none'], label: { color: COLORS.text, formatter: '{b} {c}' }, data: markLines },
        markPoint: { data: markPoints, tooltip: { formatter: '{b}' } },
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
