import { describe, expect, it } from 'vitest';
import type { SignalPreviewDetail } from '../../api/signalPreview';
import { buildSignalPreviewChartOption } from '../signalPreviewChartOption';

describe('signalPreviewChartOption', () => {
  it('renders K-line, fixed indicators, entry zone, stop, and Chan evidence', () => {
    const option = buildSignalPreviewChartOption(makeDetail());
    const series = option.series as Array<Record<string, unknown>>;
    const candle = series.find((item) => item.type === 'candlestick') as {
      markArea: { data: unknown[] };
      markLine: { data: Array<{ name: string }> };
      markPoint: { data: Array<{ name: string }> };
    };

    expect(series.some((item) => item.name === 'EMA13 指导线')).toBe(true);
    expect(series.some((item) => item.name === 'SMA57')).toBe(true);
    expect(series.some((item) => item.name === 'MACD')).toBe(true);
    expect(series.some((item) => String(item.name).startsWith('缠论笔'))).toBe(true);
    expect(candle.markArea.data).toHaveLength(2);
    expect(candle.markLine.data[0].name).toBe('止损');
    expect(candle.markPoint.data[0].name).toBe('触价');
    expect(candle.markPoint.data.some((item) => item.name === '一买')).toBe(true);
  });

  it('renders each revised entry zone only for its effective date range', () => {
    const detail = makeDetail();
    (detail.subject as { revision: number }).revision = 2;
    detail.events = [
      {
        eventId: 'event-created',
        eventType: 'signal_created',
        occurredAt: '2026-08-01T15:00:00',
        asOf: '2026-08-01T15:00:00',
        source: 'single_stock_query',
        payload: { entryLow: 10, entryHigh: 11, revision: 1 },
      },
      {
        eventId: 'event-revised',
        eventType: 'signal_revised',
        occurredAt: '2026-08-02T15:00:00',
        asOf: '2026-08-02T15:00:00',
        source: 'single_stock_query',
        payload: { entryLow: 10.4, entryHigh: 11.2, revision: 2 },
      },
    ];

    const option = buildSignalPreviewChartOption(detail);
    const candle = (option.series as Array<Record<string, unknown>>).find((item) => item.type === 'candlestick') as {
      markArea: { data: Array<Array<{ name?: string; xAxis?: string; yAxis?: number }>> };
    };

    expect(candle.markArea.data).toHaveLength(3);
    expect(candle.markArea.data[0][0]).toMatchObject({ name: '计划区 R1', xAxis: '2026-08-01', yAxis: 10 });
    expect(candle.markArea.data[0][1]).toMatchObject({ xAxis: '2026-08-02', yAxis: 11 });
    expect(candle.markArea.data[1][0]).toMatchObject({ name: '计划区 R2', xAxis: '2026-08-02', yAxis: 10.4 });
  });

  it('renders ideal and secondary entry zones as separate chart areas', () => {
    const detail = makeDetail();
    detail.events = [{
      eventId: 'event-created',
      eventType: 'signal_created',
      occurredAt: '2026-08-01T15:00:00',
      asOf: '2026-08-01T15:00:00',
      source: 'single_stock_query',
      payload: {
        entryLow: 10,
        entryHigh: 11,
        entryZones: [
          { label: 'ideal', low: 10, high: 11 },
          { label: 'secondary', low: 12, high: 13 },
        ],
        revision: 1,
      },
    }];

    const option = buildSignalPreviewChartOption(detail);
    const candle = (option.series as Array<Record<string, unknown>>).find((item) => item.type === 'candlestick') as {
      markArea: { data: Array<Array<{ name?: string; xAxis?: string; yAxis?: number; label?: Record<string, unknown>; itemStyle?: Record<string, unknown> }>> };
    };

    expect(candle.markArea.data).toHaveLength(3);
    expect(candle.markArea.data[0][0]).toMatchObject({ name: '计划入场区 · 理想', yAxis: 10 });
    expect(candle.markArea.data[1][0]).toMatchObject({ name: '计划入场区 · 次优', yAxis: 12 });
  });

  it('renders deterministic scenarios with independent Chinese zones, stops, and targets', () => {
    const detail = makeDetail();
    detail.kind = 'price_plan';
    detail.subject = {
      planId: 'plan-1', stockCode: '600519', market: 'cn', generationStatus: 'generated',
      revision: 1, actualAsOfSession: '2026-08-02', scenarioCount: 2, reasonCodes: [],
    };
    detail.pricePlan = {
      planId: 'plan-1', generationStatus: 'generated', actualAsOfSession: '2026-08-02',
      reasonCodes: [],
      scenarios: [
        { scenarioId: 's1', role: 'primary', entryLow: 10, entryHigh: 11, stopLoss: 9, target1: 12, target2: 13, sourceFamilies: ['trend', 'price_structure'], scenarioReasonCodes: [] },
        { scenarioId: 's2', role: 'observation', entryLow: 8, entryHigh: 8.5, stopLoss: 7.5, target1: 10, target2: null, sourceFamilies: ['trend'], scenarioReasonCodes: ['INSUFFICIENT_SOURCE_FAMILIES'] },
      ],
    };

    const option = buildSignalPreviewChartOption(detail);
    const candle = (option.series as Array<Record<string, unknown>>).find((item) => item.type === 'candlestick') as {
      markArea: { data: Array<Array<{ name?: string; xAxis?: string; yAxis?: number; label?: Record<string, unknown>; itemStyle?: Record<string, unknown> }>> };
      markLine: { data: Array<{ name: string; yAxis: number }> };
    };

    expect(candle.markArea.data.some((area) => area[0].name === '主入场区')).toBe(true);
    expect(candle.markArea.data.some((area) => area[0].name === '观察区')).toBe(true);
    const primaryArea = candle.markArea.data.find((area) => area[0].name === '主入场区');
    expect(primaryArea?.[0]).toMatchObject({
      yAxis: 10,
      label: { show: true, formatter: '主入场区' },
      itemStyle: { borderWidth: 2 },
    });
    expect(primaryArea?.[0].xAxis).toBeUndefined();
    expect(candle.markLine.data.some((line) => line.name === '主场景止损' && line.yAxis === 9)).toBe(true);
    expect(candle.markLine.data.some((line) => line.name === '主场景目标一' && line.yAxis === 12)).toBe(true);
    expect(candle.markLine.data.some((line) => line.name === '主场景目标二' && line.yAxis === 13)).toBe(true);
  });

  it('keeps axis tooltip content compact and aligned by row', () => {
    const option = buildSignalPreviewChartOption(makeDetail());
    const tooltip = option.tooltip as {
      formatter: (params: unknown) => string;
      extraCssText?: string;
      padding?: number[];
    };
    const html = tooltip.formatter([
      { axisValue: '2026-08-02', seriesName: '日K', value: [10.2, 10.5, 10, 11.2], marker: '' },
      { axisValue: '2026-08-02', seriesName: 'EMA13 指导线', value: 10.1, marker: '' },
      { axisValue: '2026-08-02', seriesName: '成交量', value: 120000, marker: '' },
    ]);

    expect(html).toContain('开 10.20 / 收 10.50 / 高 11.20 / 低 10.00');
    expect(tooltip.formatter([
      { axisValue: '2026-08-14', seriesName: '日K', value: [691, 24.21, 24.98, 23.5, 25.6], marker: '' },
    ])).toContain('开 24.21 / 收 24.98 / 高 25.60 / 低 23.50');
    expect(html).toContain('12.0 万');
    expect(html).toContain('justify-content:space-between');
    expect(tooltip.extraCssText).toContain('max-width:240px');
    expect(tooltip.padding).toEqual([7, 9]);
  });
});

function makeDetail(): SignalPreviewDetail {
  return {
    kind: 'entry_signal',
    subject: {
      signalId: 'signal-1',
      stockCode: '600519',
      market: 'cn',
      signalType: 'entry',
      status: 'active',
      revision: 1,
      entryLow: 10,
      entryHigh: 11,
      stopPrice: 9,
      createdAsOf: '2026-08-01T00:00:00',
      lastCheckedAsOf: '2026-08-01T00:00:00',
      validUntil: '2026-08-08',
    },
    events: [{
      eventId: 'event-1',
      eventType: 'entry_price_triggered',
      occurredAt: '2026-08-02T00:00:00',
      asOf: '2026-08-02T00:00:00',
      source: 'test',
      payload: { bar: { close: 10.5 } },
    }],
    chart: {
      stockCode: '600519',
      market: 'cn',
      status: 'ok',
      bars: [
        { date: '2026-08-01', open: 9.8, high: 10.6, low: 9.5, close: 10.2, volume: 1000, guideLine: 10, ma57: 9.7, volumeMa5: 900, dif: 0.2, dea: 0.1, macdHistogram: 0.2 },
        { date: '2026-08-02', open: 10.2, high: 11.2, low: 10, close: 10.5, volume: 1200, guideLine: 10.1, ma57: 9.8, volumeMa5: 950, dif: 0.3, dea: 0.2, macdHistogram: 0.2 },
      ],
      structure: {
        pens: [{ direction: 'up', start: { time: '2026-08-01', price: 9.5 }, end: { time: '2026-08-02', price: 11.2 } }],
        centers: [{ id: 1, zd: 10, zg: 10.8, startDate: '2026-08-01', endDate: '2026-08-02' }],
        signals: [{ name: 'first_buy', timeframe: '1d', triggered: true, metrics: { current_pen_id: 1 } }],
      },
    },
  };
}
