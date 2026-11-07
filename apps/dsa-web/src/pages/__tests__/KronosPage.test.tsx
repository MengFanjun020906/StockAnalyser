import { describe, expect, it } from 'vitest';
import type { KronosAnalyzeResponse } from '../../api/kronos';
import { buildKronosChartOption } from '../kronosChartOption';

describe('KronosPage chart option', () => {
  it('uses ECharts gaps instead of null candles when forecast dates extend the x-axis', () => {
    const option = buildKronosChartOption(makeKronosResponse());
    const series = option.series as Array<{ type: string; data: unknown[] }>;
    const candleSeries = series.find((item) => item.type === 'candlestick');
    const closeSeries = series.find((item) => item.type === 'line' && item.data[0] === 11);
    const forecastSeries = series.find((item) => item.type === 'line' && item.data[1] === 12.5);

    expect(candleSeries?.data).toEqual([[10, 11, 9, 12], '-']);
    expect(closeSeries?.data).toEqual([11, '-']);
    expect(forecastSeries?.data).toEqual(['-', 12.5]);
    expect(JSON.stringify(candleSeries?.data)).not.toContain('null');
  });
});

function makeKronosResponse(): KronosAnalyzeResponse {
  return {
    status: 'ok',
    code: '600519',
    market: 'cn',
    frequency: '1d',
    bars: [
      {
        time: '2026-01-01',
        open: 10,
        high: 12,
        low: 9,
        close: 11,
        volume: 100,
        amount: 1000,
      },
    ],
    forecast: [
      {
        time: '2026-01-02',
        open: 11.2,
        high: 13,
        low: 10.8,
        close: 12.5,
      },
    ],
    signal: {},
    forecastSummary: {},
    patternInstances: [
      {
        patternId: 'p1',
        patternType: 'kronos_forecast_breakout',
        label: 'Kronos预测突破',
        direction: 'bullish',
        confidence: 0.8,
        startTime: '2026-01-01',
        endTime: '2026-01-02',
        priceLow: 9,
        priceHigh: 13,
      },
    ],
    sourceChain: [],
  };
}
