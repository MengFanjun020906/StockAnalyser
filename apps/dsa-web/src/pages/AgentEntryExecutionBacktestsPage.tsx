import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Activity, BarChart3, CalendarDays, ChevronLeft, ChevronRight, CloudDownload, Database, Filter, GitCompareArrows, Percent, RefreshCw, Search, Target, Trophy } from 'lucide-react';
import { agentEntryExecutionBacktestsApi, type EntryExecutionBacktestBuildResult, type EntryExecutionBacktestResponse, type EntryExecutionBacktestRow, type EntryExecutionDailyBar, type EntryExecutionMinuteSyncResponse, type EntryExecutionStrategyMetrics, type EntryExecutionStrategyResult, type EntryExecutionTradePlan } from '../api/agentEntryExecutionBacktests';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, EmptyState, StatCard } from '../components/common';
import { cn } from '../utils/cn';

const strategyOptions = [
  '',
  'strict_ai_entry',
  'next_open_baseline',
  'atr_elastic_entry',
  'breakout_fallback_entry',
];
const comparisonStrategies = strategyOptions.filter((item) => item);

const strategyLabels: Record<string, string> = {
  strict_ai_entry: '严格入场',
  next_open_baseline: '次日开盘',
  atr_elastic_entry: 'ATR 弹性',
  breakout_fallback_entry: '突破跟随',
};

const PAGE_SIZE = 20;

const statusLabels: Record<string, string> = {
  filled: '已成交',
  not_filled: '未成交',
  strategy_skipped: '跳过',
  insufficient_start_price: '缺起始价',
  insufficient_forward_bars: '缺未来行情',
  invalid_trade_plan: '异常价位',
  unknown: '未知',
};

function fmtPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  const sign = Number(value) > 0 ? '+' : '';
  return `${sign}${Number(value).toFixed(2)}%`;
}

function fmtPrice(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(2);
}

function fmtCount(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '0';
  return String(Number(value));
}

function statusVariant(status?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (status === 'filled') return 'success';
  if (status === 'not_filled') return 'warning';
  if (status === 'strategy_skipped') return 'default';
  if (status === 'invalid_trade_plan' || status?.startsWith('insufficient')) return 'danger';
  return 'default';
}

function pnlClass(value?: number | null): string {
  return Number(value) > 0 ? 'text-danger' : Number(value) < 0 ? 'text-success' : 'text-foreground';
}

function strategyResult(row: EntryExecutionBacktestRow, strategy: string): EntryExecutionStrategyResult {
  return row.strategies?.[strategy] || {};
}

function dayKey(value?: string | null): string | null {
  return value ? value.slice(0, 10) : null;
}

function compactDateTime(value?: string | null): string {
  if (!value) return '--';
  const [datePart, timePart] = value.split(' ');
  if (!timePart) return value;
  return `${datePart.slice(5)} ${timePart.slice(0, 5)}`;
}

function priceDataVariant(granularity?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (granularity === 'minute') return 'success';
  if (granularity === 'daily') return 'warning';
  if (granularity === 'none') return 'danger';
  return 'default';
}

function priceDataLabel(granularity?: string): string {
  if (granularity === 'minute') return '分钟线';
  if (granularity === 'daily') return '日线兜底';
  if (granularity === 'none') return '无行情';
  return granularity || '未知';
}

const StrategyBars: React.FC<{ summary: EntryExecutionBacktestResponse['summary'] }> = ({ summary }) => {
  const avg = summary.avgPnlPct || {};
  const counts = summary.strategyCounts || {};
  return (
    <div className="border border-border/70 bg-card/60 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
          <GitCompareArrows className="h-4 w-4 text-cyan" />
          四套策略平均收益
        </div>
        <span className="text-xs text-secondary-text">按当前日期/筛选结果汇总</span>
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {comparisonStrategies.map((strategy) => {
          const value = avg[strategy];
          const hasValue = value != null && Number.isFinite(Number(value));
          const positive = Number(value) > 0;
          return (
            <div key={strategy} className="border border-border/60 bg-elevated/50 p-3">
              <div className="mb-2 flex items-center justify-between gap-3 text-xs">
                <span className="font-medium text-secondary-text">{strategyLabels[strategy] || strategy}</span>
                <span className="tabular-nums text-secondary-text">{counts[strategy] ?? 0} 样本</span>
              </div>
              <div className={cn('text-2xl font-semibold tabular-nums', positive ? 'text-danger' : Number(value) < 0 ? 'text-success' : 'text-foreground')}>
                {fmtPct(value)}
              </div>
              <div className="mt-2 h-1.5 bg-muted">
                <div
                  className={cn('h-1.5', hasValue ? (positive ? 'bg-danger' : 'bg-success') : 'bg-border')}
                  style={{ width: `${hasValue ? Math.min(100, Math.max(10, Math.abs(Number(value)) * 6)) : 100}%` }}
                />
              </div>
              <div className="mt-2 text-xs text-secondary-text">{hasValue ? '已成交策略样本均值' : '暂无成交样本'}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
};

const SystemMetricsOverview: React.FC<{
  summary: EntryExecutionBacktestResponse['summary'];
  selectedStrategy: string;
  title: string;
  description: string;
}> = ({ summary, selectedStrategy, title, description }) => {
  const metrics = summary.strategyMetrics || {};
  const headline = summary.headlineMetrics || {};
  const bestStrategy = headline.bestStrategy || selectedStrategy || 'strict_ai_entry';
  const bestMetrics = metrics[bestStrategy] || {};
  const selectedMetrics = metrics[selectedStrategy] || {};

  return (
    <div className="border border-border/70 bg-card/60 p-4">
      <div className="mb-4 flex flex-col gap-1 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
            <Trophy className="h-4 w-4 text-cyan" />
            {title}
          </div>
          <div className="mt-1 text-xs text-secondary-text">{description}</div>
        </div>
        <div className="text-xs text-secondary-text">
          最佳策略 <span className="font-medium text-foreground">{strategyLabels[bestStrategy] || bestStrategy}</span>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-4">
        <StatCard
          label="最佳策略累计 PnL"
          value={fmtPct(headline.bestCompoundedPnlPct)}
          hint={`${strategyLabels[bestStrategy] || bestStrategy} · ${fmtCount(bestMetrics.filled)} 笔成交`}
          icon={<BarChart3 className="h-4 w-4" />}
          tone="primary"
        />
        <StatCard
          label="最佳策略胜率"
          value={fmtPct(headline.bestWinRatePct)}
          hint={`成交率 ${fmtPct(headline.bestFillRatePct)}`}
          icon={<Percent className="h-4 w-4" />}
        />
        <StatCard
          label="当前策略累计 PnL"
          value={fmtPct(selectedMetrics.compoundedPnlPct)}
          hint={strategyLabels[selectedStrategy] || selectedStrategy}
          icon={<GitCompareArrows className="h-4 w-4" />}
        />
        <StatCard
          label="当前策略胜率"
          value={fmtPct(selectedMetrics.winRatePct)}
          hint={`${fmtCount(selectedMetrics.winCount)} 胜 / ${fmtCount(selectedMetrics.filled)} 成交`}
          icon={<Activity className="h-4 w-4" />}
        />
      </div>

      <div className="mt-4 overflow-x-auto border border-border/60">
        <table className="min-w-[980px] divide-y divide-border/50 text-sm">
          <thead className="bg-elevated/70 text-xs uppercase tracking-[0.08em] text-secondary-text">
            <tr>
              <th className="px-3 py-2 text-left">策略</th>
              <th className="px-3 py-2 text-right">成交/样本</th>
              <th className="px-3 py-2 text-right">成交率</th>
              <th className="px-3 py-2 text-right">胜率</th>
              <th className="px-3 py-2 text-right">累计 PnL</th>
              <th className="px-3 py-2 text-right">平均 PnL</th>
              <th className="px-3 py-2 text-right">中位 PnL</th>
              <th className="px-3 py-2 text-right">最好/最差</th>
              <th className="px-3 py-2 text-right">盈亏比</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {comparisonStrategies.map((strategy) => {
              const item = metrics[strategy] || {} as EntryExecutionStrategyMetrics;
              return (
                <tr key={strategy} className={cn('hover:bg-hover/60', strategy === bestStrategy ? 'bg-cyan/5' : '')}>
                  <td className="px-3 py-2 font-medium text-foreground">{strategyLabels[strategy] || strategy}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{fmtCount(item.filled)} / {fmtCount(item.total)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{fmtPct(item.fillRatePct)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{fmtPct(item.winRatePct)}</td>
                  <td className={cn('px-3 py-2 text-right font-semibold tabular-nums', pnlClass(item.compoundedPnlPct))}>{fmtPct(item.compoundedPnlPct)}</td>
                  <td className={cn('px-3 py-2 text-right tabular-nums', pnlClass(item.avgPnlPct))}>{fmtPct(item.avgPnlPct)}</td>
                  <td className={cn('px-3 py-2 text-right tabular-nums', pnlClass(item.medianPnlPct))}>{fmtPct(item.medianPnlPct)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{fmtPct(item.bestPnlPct)} / {fmtPct(item.worstPnlPct)}</td>
                  <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{item.payoffRatio != null ? Number(item.payoffRatio).toFixed(2) : '--'}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const DatePager: React.FC<{
  decisionDate: string;
  availableDates: string[];
  page: number;
  totalPages: number;
  total: number;
  onDateChange: (value: string) => void;
  onPageChange: (value: number) => void;
}> = ({ decisionDate, availableDates, page, totalPages, total, onDateChange, onPageChange }) => {
  const dateIndex = decisionDate ? availableDates.indexOf(decisionDate) : -1;
  const canGoOlder = dateIndex >= 0 && dateIndex < availableDates.length - 1;
  const canGoNewer = dateIndex > 0;
  const safeTotalPages = Math.max(1, totalPages || 1);
  return (
    <div className="flex flex-col gap-3 border border-border/70 bg-card/60 p-3 md:flex-row md:items-center md:justify-between">
      <div className="flex items-center gap-2 text-sm text-secondary-text">
        <CalendarDays className="h-4 w-4 text-cyan" />
        <span>
          当前日期 <span className="font-medium text-foreground">{decisionDate || '全部日期'}</span>
          {' · '}共 <span className="font-medium text-foreground">{total}</span> 条
          {' · '}第 <span className="font-medium text-foreground">{page}/{safeTotalPages}</span> 页
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="btn-secondary inline-flex h-9 items-center gap-1 px-3"
          onClick={() => onDateChange(availableDates[dateIndex + 1])}
          disabled={!canGoOlder}
        >
          <ChevronLeft className="h-4 w-4" />
          前一日期
        </button>
        <button
          type="button"
          className="btn-secondary inline-flex h-9 items-center gap-1 px-3"
          onClick={() => onDateChange(availableDates[dateIndex - 1])}
          disabled={!canGoNewer}
        >
          后一日期
          <ChevronRight className="h-4 w-4" />
        </button>
        <span className="mx-1 h-5 w-px bg-border/70" />
        <button
          type="button"
          className="btn-secondary inline-flex h-9 items-center gap-1 px-3"
          onClick={() => onPageChange(Math.max(1, page - 1))}
          disabled={page <= 1}
        >
          <ChevronLeft className="h-4 w-4" />
          上一页
        </button>
        <button
          type="button"
          className="btn-secondary inline-flex h-9 items-center gap-1 px-3"
          onClick={() => onPageChange(Math.min(safeTotalPages, page + 1))}
          disabled={page >= safeTotalPages}
        >
          下一页
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
    </div>
  );
};

const EntryExecutionKlineChart: React.FC<{
  bars?: EntryExecutionDailyBar[];
  plan?: EntryExecutionTradePlan;
  result: EntryExecutionStrategyResult;
  baseline?: EntryExecutionStrategyResult;
  strategyName: string;
}> = ({ bars = [], plan = {}, result, baseline, strategyName }) => {
  const ref = useRef<HTMLDivElement | null>(null);
  const usableBars = useMemo(() => bars
    .filter((bar) => bar.date && Number.isFinite(Number(bar.high)) && Number.isFinite(Number(bar.low)) && Number.isFinite(Number(bar.close)))
    .slice(0, 40), [bars]);

  useEffect(() => {
    if (!ref.current || !usableBars.length) return undefined;
    const chart = echarts.init(ref.current);
    const dates = usableBars.map((bar) => bar.date || '');
    const values = usableBars.map((bar) => [
      Number(bar.open ?? bar.close),
      Number(bar.close),
      Number(bar.low),
      Number(bar.high),
    ]);
    const hasEntryRange = plan.entryZoneLow != null && plan.entryZoneHigh != null
      && Math.abs(Number(plan.entryZoneHigh) - Number(plan.entryZoneLow)) > 0.0001;
    const entryLineColor = '#38bdf8';
    const markLines = [
      plan.entryZoneHigh != null ? {
        yAxis: Number(plan.entryZoneHigh),
        name: hasEntryRange ? 'AI 入场上沿' : 'AI 入场',
        lineStyle: { color: entryLineColor, type: hasEntryRange ? 'dotted' : 'solid', width: hasEntryRange ? 1.4 : 2.2 },
        label: { color: entryLineColor, formatter: `${hasEntryRange ? 'AI High' : 'AI'} ${fmtPrice(plan.entryZoneHigh)}` },
      } : null,
      hasEntryRange && plan.entryZoneLow != null ? {
        yAxis: Number(plan.entryZoneLow),
        name: 'AI 入场下沿',
        lineStyle: { color: entryLineColor, type: 'dotted', width: 1.4 },
        label: { color: entryLineColor, formatter: `AI Low ${fmtPrice(plan.entryZoneLow)}` },
      } : null,
      plan.takeProfitPrice != null ? {
        yAxis: Number(plan.takeProfitPrice),
        name: '止盈',
        lineStyle: { color: '#ef4444', type: 'dashed', width: 1.2 },
        label: { color: '#ef4444', formatter: `TP ${fmtPrice(plan.takeProfitPrice)}` },
      } : null,
      plan.stopLossPrice != null ? {
        yAxis: Number(plan.stopLossPrice),
        name: '止损',
        lineStyle: { color: '#22c55e', type: 'dashed', width: 1.2 },
        label: { color: '#22c55e', formatter: `SL ${fmtPrice(plan.stopLossPrice)}` },
      } : null,
    ].filter(Boolean);
    const entryArea = hasEntryRange
      ? [[
        {
          yAxis: Number(plan.entryZoneHigh),
          itemStyle: { color: 'rgba(14, 165, 233, 0.14)' },
          label: { color: '#38bdf8', formatter: 'AI 入场区' },
        },
        { yAxis: Number(plan.entryZoneLow) },
      ]]
      : [];
    const marker = (item: EntryExecutionStrategyResult | undefined, kind: 'entry' | 'exit') => {
      const date = dayKey(kind === 'entry' ? item?.entryDate : item?.exitDate);
      const price = kind === 'entry' ? item?.entryPrice : item?.exitPrice;
      return date && price != null ? [[date, Number(price)]] : [];
    };

    chart.setOption({
      backgroundColor: 'transparent',
      animation: false,
      grid: { left: 44, right: 54, top: 18, bottom: 30 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        borderWidth: 1,
        formatter: (params: unknown) => {
          const rows = Array.isArray(params) ? params as Array<{ seriesType?: string; data?: unknown; dataIndex?: number; seriesName?: string; value?: unknown }> : [params as { seriesType?: string; data?: unknown; dataIndex?: number; seriesName?: string; value?: unknown }];
          const candle = rows.find((row) => row.seriesType === 'candlestick');
          const index = Number(candle?.dataIndex ?? 0);
          const bar = usableBars[index];
          if (!bar) return '';
          const markerRows = rows
            .filter((row) => row.seriesType === 'scatter')
            .map((row) => `${row.seriesName}: ${fmtPrice(Array.isArray(row.value) ? Number(row.value[1]) : null)}`)
            .join('<br/>');
          return `${bar.date}<br/>开 ${fmtPrice(bar.open)} / 收 ${fmtPrice(bar.close)}<br/>高 ${fmtPrice(bar.high)} / 低 ${fmtPrice(bar.low)}${markerRows ? `<br/>${markerRows}` : ''}`;
        },
      },
      xAxis: {
        type: 'category',
        data: dates,
        axisLine: { lineStyle: { color: '#64748b' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
      },
      yAxis: {
        scale: true,
        splitLine: { lineStyle: { color: 'rgba(148,163,184,0.16)' } },
        axisLabel: { color: '#64748b', fontSize: 10 },
      },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 2 }],
      series: [
        {
          name: '日K',
          type: 'candlestick',
          data: values,
          itemStyle: { color: '#ef4444', color0: '#22c55e', borderColor: '#ef4444', borderColor0: '#22c55e' },
          markArea: { silent: true, data: entryArea },
          markLine: { symbol: 'none', data: markLines, silent: true },
        },
        {
          name: `${strategyLabels[strategyName] || strategyName}入场`,
          type: 'scatter',
          data: marker(result, 'entry'),
          symbol: 'pin',
          symbolSize: 24,
          itemStyle: { color: '#38bdf8' },
          label: { show: true, formatter: '入', color: '#0f172a', fontSize: 10, fontWeight: 700 },
          z: 6,
        },
        {
          name: `${strategyLabels[strategyName] || strategyName}出场`,
          type: 'scatter',
          data: marker(result, 'exit'),
          symbol: 'rect',
          symbolSize: 12,
          itemStyle: { color: '#f59e0b' },
          z: 6,
        },
        { name: '次日开盘入场', type: 'scatter', data: strategyName === 'next_open_baseline' ? [] : marker(baseline, 'entry'), symbolSize: 9, itemStyle: { color: '#a78bfa' }, z: 4 },
        { name: '次日开盘出场', type: 'scatter', data: strategyName === 'next_open_baseline' ? [] : marker(baseline, 'exit'), symbol: 'rect', symbolSize: 9, itemStyle: { color: '#c4b5fd' }, z: 4 },
      ],
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [baseline, plan, result, strategyName, usableBars]);

  if (!usableBars.length) return <span className="text-xs text-secondary-text">--</span>;
  return <div ref={ref} className="h-[180px] w-[360px]" data-testid="entry-execution-kline" />;
};

const EntryBacktestTable: React.FC<{ rows: EntryExecutionBacktestRow[]; strategy: string }> = ({ rows, strategy }) => {
  const selectedStrategy = strategy || 'strict_ai_entry';
  return (
    <div className="overflow-hidden border border-border/70 bg-card/70">
      <div className="overflow-x-auto">
        <table className="min-w-[1600px] divide-y divide-border/60 text-sm">
          <thead className="bg-elevated/70 text-xs uppercase tracking-[0.08em] text-secondary-text">
            <tr>
              <th className="w-[96px] px-4 py-3 text-left">日期</th>
              <th className="w-[120px] px-4 py-3 text-left">标的</th>
              <th className="w-[170px] px-4 py-3 text-left">入场区间</th>
              <th className="w-[96px] px-4 py-3 text-left">数据</th>
              <th className="w-[110px] px-4 py-3 text-left">策略状态</th>
              <th className="w-[96px] px-4 py-3 text-right">收益</th>
              <th className="w-[190px] px-4 py-3 text-left">退出</th>
              <th className="w-[380px] px-4 py-3 text-left">交互日K</th>
              <th className="w-[140px] px-4 py-3 text-left">Trace</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {rows.map((row) => {
              const plan = row.tradePlan || {};
              const result = strategyResult(row, selectedStrategy);
              const status = result.status || 'unknown';
              const priceData = row.priceData || {};
              const strategyEntryLabel = result.entryPrice != null
                ? `${strategyLabels[selectedStrategy] || selectedStrategy} ${fmtPrice(result.entryPrice)}`
                : `${strategyLabels[selectedStrategy] || selectedStrategy} --`;
              return (
                <tr key={`${row.traceId}-${row.tsCode}-${row.rank}`} className="hover:bg-hover/70">
                  <td className="whitespace-nowrap px-4 py-3 text-secondary-text">{row.decisionDate || '--'}</td>
                  <td className="px-4 py-3">
                    <div className="font-medium text-foreground">{row.tsCode || '--'}</div>
                    <div className="text-xs text-secondary-text">{row.name || `rank ${row.rank ?? '-'}`}</div>
                  </td>
                  <td className="px-4 py-3">
                    <div className="text-foreground">{fmtPrice(plan.entryZoneLow)} - {fmtPrice(plan.entryZoneHigh)}</div>
                    <div className="text-xs text-cyan">{strategyEntryLabel}</div>
                    <div className="text-xs text-secondary-text">SL {fmtPrice(plan.stopLossPrice)} / TP {fmtPrice(plan.takeProfitPrice)}</div>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3">
                    <Badge variant={priceDataVariant(priceData.granularity)}>{priceDataLabel(priceData.granularity)}</Badge>
                    <div className="mt-1 text-xs text-secondary-text">
                      {priceData.barCount != null ? `${priceData.barCount} 根` : '--'}
                      {priceData.frequency ? ` / ${priceData.frequency}m` : ''}
                    </div>
                  </td>
                  <td className="whitespace-nowrap px-4 py-3">
                    <Badge variant={statusVariant(status)}>{statusLabels[status] || status}</Badge>
                    {result.ambiguousBar ? <div className="mt-1 text-xs text-warning">同日止盈止损，按止损优先</div> : null}
                  </td>
                  <td className={cn('whitespace-nowrap px-4 py-3 text-right font-semibold tabular-nums', Number(result.pnlPct) > 0 ? 'text-danger' : Number(result.pnlPct) < 0 ? 'text-success' : 'text-secondary-text')}>
                    {fmtPct(result.pnlPct)}
                    <div className="text-xs font-normal text-secondary-text">{result.holdingDays != null ? `${result.holdingDays} 日` : '--'}</div>
                  </td>
                  <td className="w-[190px] px-4 py-3">
                    <div className="text-foreground">{result.exitReason || result.entryReason || '--'}</div>
                    <div className="mt-1 grid grid-cols-[34px_1fr] gap-x-1 gap-y-0.5 text-xs text-secondary-text">
                      <span>入</span>
                      <span className="truncate tabular-nums" title={result.entryDate || ''}>{compactDateTime(result.entryDate)}</span>
                      <span>出</span>
                      <span className="truncate tabular-nums" title={result.exitDate || ''}>{compactDateTime(result.exitDate)}</span>
                    </div>
                    <div className="mt-1 text-xs tabular-nums text-secondary-text">价 {fmtPrice(result.exitPrice)}</div>
                  </td>
                  <td className="w-[380px] px-4 py-3">
                    <EntryExecutionKlineChart
                      bars={priceData.dailyBars}
                      plan={plan}
                      result={result}
                      baseline={strategyResult(row, 'next_open_baseline')}
                      strategyName={selectedStrategy}
                    />
                  </td>
                  <td className="w-[140px] max-w-[140px] truncate px-4 py-3 text-xs text-secondary-text" title={row.traceId || row.traceDir || ''}>
                    {row.traceId || '--'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
};

const AgentEntryExecutionBacktestsPage: React.FC = () => {
  const [data, setData] = useState<EntryExecutionBacktestResponse | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [buildResult, setBuildResult] = useState<EntryExecutionBacktestBuildResult | null>(null);
  const [syncResult, setSyncResult] = useState<EntryExecutionMinuteSyncResponse | null>(null);
  const [strategy, setStrategy] = useState('');
  const [symbol, setSymbol] = useState('');
  const [decisionDate, setDecisionDate] = useState('');
  const [hasAutoSelectedDate, setHasAutoSelectedDate] = useState(false);
  const [page, setPage] = useState(1);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await agentEntryExecutionBacktestsApi.list({
        strategy: strategy || undefined,
        symbol: symbol.trim() || undefined,
        decisionDate: decisionDate || undefined,
        page,
        pageSize: PAGE_SIZE,
        limit: 300,
      });
      setData(result);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, [decisionDate, page, strategy, symbol]);

  const rebuildData = useCallback(async () => {
    setRebuilding(true);
    setError(null);
    try {
      const result = await agentEntryExecutionBacktestsApi.rebuild({ limit: 300 });
      setBuildResult(result);
      await loadData();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRebuilding(false);
    }
  }, [loadData]);

  const syncMinuteBars = useCallback(async () => {
    setSyncing(true);
    setError(null);
    try {
      const result = await agentEntryExecutionBacktestsApi.syncMinuteBars({
        limit: 300,
        decisionDate: decisionDate || undefined,
        symbol: symbol.trim() || undefined,
        frequency: '5',
        adjustflag: '3',
        rebuild: true,
      });
      setSyncResult(result);
      setBuildResult(result.rebuild ?? null);
      await loadData();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setSyncing(false);
    }
  }, [decisionDate, loadData, symbol]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const summary = data?.summary || {};
  const historySummary = data?.historySummary || {};
  const rows = useMemo(() => data?.items ?? [], [data]);
  const availableDates = useMemo(() => data?.availableDates ?? [], [data]);
  const selectedStrategy = strategy || 'strict_ai_entry';
  const currentPage = data?.page ?? page;
  const totalPages = data?.totalPages ?? 0;

  useEffect(() => {
    if (!hasAutoSelectedDate && !decisionDate && availableDates.length) {
      setHasAutoSelectedDate(true);
      setDecisionDate(availableDates[0]);
      setPage(1);
    }
  }, [availableDates, decisionDate, hasAutoSelectedDate]);

  const changeDecisionDate = useCallback((value: string) => {
    setDecisionDate(value || '');
    setPage(1);
  }, []);

  const changePage = useCallback((value: number) => {
    setPage(Math.max(1, value));
  }, []);

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2">
            <Target className="h-5 w-5 text-cyan" />
            <h1 className="text-2xl font-semibold text-foreground">入场执行回测</h1>
          </div>
          <p className="max-w-3xl text-sm text-secondary-text">
            只评估选股报告最终输出标的，按 AI 入场区间、止盈、止损和 20 日超时退出回放，专门观察入场是否过保守。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary inline-flex h-10 items-center gap-2 px-4" onClick={() => void loadData()} disabled={loading || rebuilding}>
            <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
            刷新
          </button>
          <button type="button" className="btn-secondary inline-flex h-10 items-center gap-2 px-4" onClick={() => void rebuildData()} disabled={loading || rebuilding || syncing}>
            <Database className={cn('h-4 w-4', rebuilding ? 'animate-pulse' : '')} />
            重建样本
          </button>
          <button type="button" className="btn-primary inline-flex h-10 items-center gap-2 px-4" onClick={() => void syncMinuteBars()} disabled={loading || rebuilding || syncing || !decisionDate}>
            <CloudDownload className={cn('h-4 w-4', syncing ? 'animate-pulse' : '')} />
            同步当前日期分钟线
          </button>
        </div>
      </div>

      <div className="grid gap-3 border border-border/70 bg-card/60 p-4 md:grid-cols-[180px_180px_1fr_auto] md:items-end">
        <label className="text-sm">
          <span className="mb-1 block text-xs font-medium text-secondary-text">策略视角</span>
          <select
            className="input-surface input-focus-glow h-10 w-full"
            value={strategy}
            onChange={(event) => {
              setStrategy(event.target.value);
              setPage(1);
            }}
          >
            {strategyOptions.map((item) => (
              <option key={item || 'all'} value={item}>{item ? strategyLabels[item] || item : '默认：严格入场'}</option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs font-medium text-secondary-text">决策日期</span>
          <select
            className="input-surface input-focus-glow h-10 w-full"
            value={decisionDate}
            onChange={(event) => changeDecisionDate(event.target.value)}
          >
            <option value="">全部日期</option>
            {availableDates.map((item) => (
              <option key={item} value={item}>{item}</option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs font-medium text-secondary-text">股票代码</span>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-secondary-text" />
            <input
              className="input-surface input-focus-glow h-10 w-full pl-9"
              value={symbol}
              onChange={(event) => {
                setSymbol(event.target.value);
                setPage(1);
              }}
              placeholder="600519"
            />
          </div>
        </label>
        <button
          type="button"
          className="btn-primary inline-flex h-10 items-center gap-2 px-4"
          onClick={() => {
            setPage(1);
            void loadData();
          }}
        >
          <Filter className="h-4 w-4" />
          筛选
        </button>
      </div>

      {error ? <ApiErrorAlert error={error} /> : null}
      {syncResult?.sync ? (
        <div className="border border-cyan/40 bg-cyan/10 px-4 py-3 text-sm text-foreground">
          已同步 {syncResult.sync.fetchedSymbols ?? 0}/{syncResult.sync.symbolCount ?? 0} 只最终报告标的分钟线，
          拉取 {syncResult.sync.fetchedRows ?? 0} 根，新增 {syncResult.sync.writtenRows ?? 0} 根，
          失败 {syncResult.sync.failedSymbols ?? 0} 只。
        </div>
      ) : null}
      {buildResult ? (
        <div className="border border-cyan/40 bg-cyan/10 px-4 py-3 text-sm text-foreground">
          已重建 {buildResult.reviewCount ?? 0} 条入场执行样本，扫描 {buildResult.traceCount ?? 0} 个 Trace，跳过 {buildResult.skipped ?? 0} 个。
        </div>
      ) : null}

      <SystemMetricsOverview
        summary={summary}
        selectedStrategy={selectedStrategy}
        title={decisionDate ? '当日总览指标' : '当前筛选总览指标'}
        description={decisionDate ? `仅统计 ${decisionDate} 的样本；PnL 为逐信号等权复利口径。` : '按当前筛选范围统计；PnL 为逐信号等权复利口径。'}
      />

      <SystemMetricsOverview
        summary={historySummary}
        selectedStrategy={selectedStrategy}
        title="历史总览指标"
        description="统计历史全部日期样本；保留股票代码等非日期筛选，忽略当前决策日期。"
      />

      <div className="grid gap-3 md:grid-cols-4">
        <StatCard label="样本数" value={String(summary.total ?? data?.total ?? 0)} hint={data?.exists ? 'entry_execution_backtest.jsonl' : '文件未生成'} icon={<Database className="h-4 w-4" />} />
        <StatCard label="严格入场成交率" value={fmtPct(summary.fillRatePct)} hint="filled / final outputs" icon={<Activity className="h-4 w-4" />} tone="primary" />
        <StatCard label="当前策略均值" value={fmtPct(summary.avgPnlPct?.[selectedStrategy])} hint={strategyLabels[selectedStrategy] || selectedStrategy} icon={<BarChart3 className="h-4 w-4" />} />
        <StatCard label="当前策略中位数" value={fmtPct(summary.medianPnlPct?.[selectedStrategy])} hint="不设本金，逐信号等权" icon={<GitCompareArrows className="h-4 w-4" />} />
      </div>

      <div className="flex flex-col gap-3">
        <StrategyBars summary={summary} />
        <DatePager
          decisionDate={decisionDate}
          availableDates={availableDates}
          page={currentPage}
          totalPages={totalPages}
          total={data?.total ?? 0}
          onDateChange={changeDecisionDate}
          onPageChange={changePage}
        />
        {rows.length ? (
          <EntryBacktestTable rows={rows} strategy={strategy} />
        ) : (
          <EmptyState
            title="还没有可展示的入场执行样本"
            description="先点击重建样本，或运行 scripts/build_agent_entry_execution_backtests.py 生成 data/agent_reviews/entry_execution_backtest.jsonl。"
            icon={<Database className="h-7 w-7" />}
          />
        )}
      </div>
    </div>
  );
};

export default AgentEntryExecutionBacktestsPage;
