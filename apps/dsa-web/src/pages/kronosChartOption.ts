import type { KronosAnalyzeResponse } from '../api/kronos';

type ChartGap = '-';
type ChartLineValue = number | ChartGap;
type ChartCandleValue = [number, number, number, number] | ChartGap;

function fmtChartNum(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(2);
}

function toFiniteChartNumber(value?: number | null): number | null {
  if (value == null) return null;
  const numberValue = Number(value);
  return Number.isFinite(numberValue) ? numberValue : null;
}

function toLineValue(value?: number | null): ChartLineValue {
  return toFiniteChartNumber(value) ?? '-';
}

function toCandleValue(bar?: { open?: number | null; high?: number | null; low?: number | null; close?: number | null }): ChartCandleValue {
  if (!bar) return '-';
  const open = toFiniteChartNumber(bar.open);
  const close = toFiniteChartNumber(bar.close);
  const low = toFiniteChartNumber(bar.low);
  const high = toFiniteChartNumber(bar.high);
  if (open == null || close == null || low == null || high == null) return '-';
  return [open, close, low, high];
}

export function buildKronosChartOption(data: KronosAnalyzeResponse) {
  const historyByTime = new Map(data.bars.map((bar) => [bar.time, bar]));
  const forecastByTime = new Map(data.forecast.map((bar) => [bar.time, bar]));
  const dates = Array.from(new Set([...data.bars.map((bar) => bar.time), ...data.forecast.map((bar) => bar.time)]));
  const candleValues = dates.map((time) => toCandleValue(historyByTime.get(time)));
  const closeValues = dates.map((time) => toLineValue(historyByTime.get(time)?.close));
  const forecastValues = dates.map((time) => toLineValue(forecastByTime.get(time)?.close));
  const markAreaData = data.patternInstances
    .map((pattern) => {
      const box = pattern.redBox;
      const y0 = toFiniteChartNumber(box?.y0 ?? pattern.priceLow);
      const y1 = toFiniteChartNumber(box?.y1 ?? pattern.priceHigh);
      if (!pattern.startTime || !pattern.endTime || y0 == null || y1 == null) return null;
      return [
        { name: pattern.label || pattern.patternType, xAxis: pattern.startTime, yAxis: y0 },
        { xAxis: pattern.endTime, yAxis: y1 },
      ];
    })
    .filter((item): item is [{ name: string; xAxis: string; yAxis: number }, { xAxis: string; yAxis: number }] => item != null);

  return {
    backgroundColor: 'transparent',
    grid: { left: 56, right: 28, top: 42, bottom: 54 },
    legend: { top: 6, textStyle: { color: '#64748b' } },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      borderWidth: 1,
      formatter: (params: unknown) => {
        const rows = Array.isArray(params) ? params : [params];
        const first = rows[0] as { axisValue?: string } | undefined;
        const time = String(first?.axisValue || '');
        const bar = historyByTime.get(time);
        const forecast = forecastByTime.get(time);
        const parts = [time];
        if (bar) {
          parts.push(`开 ${fmtChartNum(bar.open)} / 收 ${fmtChartNum(bar.close)}`);
          parts.push(`高 ${fmtChartNum(bar.high)} / 低 ${fmtChartNum(bar.low)}`);
        }
        if (forecast) {
          parts.push(`Kronos 预测收盘 ${fmtChartNum(forecast.close)}`);
        }
        return parts.join('<br/>');
      },
    },
    xAxis: { type: 'category', data: dates, boundaryGap: true, axisLine: { lineStyle: { color: '#94a3b8' } }, axisLabel: { color: '#64748b' } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: 'rgba(148,163,184,0.18)' } }, axisLabel: { color: '#64748b' } },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 14 }],
    series: [
      {
        name: '日线',
        type: 'candlestick',
        data: candleValues,
        itemStyle: { color: '#dc2626', color0: '#16a34a', borderColor: '#dc2626', borderColor0: '#16a34a' },
        markArea: {
          silent: true,
          itemStyle: { color: 'rgba(220, 38, 38, 0.08)', borderColor: '#dc2626', borderWidth: 1 },
          label: { color: '#dc2626', fontSize: 11 },
          data: markAreaData,
        },
      },
      { name: '收盘', type: 'line', data: closeValues, showSymbol: false, smooth: true, lineStyle: { color: '#64748b', width: 1.2 } },
      { name: 'Kronos预测', type: 'line', data: forecastValues, showSymbol: true, smooth: true, lineStyle: { color: '#0ea5e9', width: 2 } },
    ],
  };
}
