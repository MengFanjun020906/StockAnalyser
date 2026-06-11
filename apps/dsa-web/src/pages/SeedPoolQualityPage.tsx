import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Activity, BarChart3, CandlestickChart, DatabaseZap, ExternalLink, LineChart, RefreshCw, Target, TrendingUp } from 'lucide-react';
import { Link, useSearchParams } from 'react-router-dom';
import { seedPoolQualityApi, type SeedPoolChartData, type SeedPoolDeskOutcome, type SeedPoolQualityDate, type SeedPoolQualityGroupStat, type SeedPoolQualityResponse } from '../api/seedPoolQuality';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, EmptyState } from '../components/common';
import { cn } from '../utils/cn';

type AttributionTab = 'source' | 'desk' | 'catalyst';

const liquidityLabels: Record<string, string> = {
  NORMAL: '正常可交易',
  LIMIT_UP_UNABLE_BUY: '一字涨停不可买',
  LIMIT_DOWN_RISK: '一字跌停风险',
  UNKNOWN: '未知',
};

const deskLabels: Record<string, string> = {
  early_turn_desk: '低位启动席',
  momentum_desk: '动量席',
  quality_repair_desk: '质量修复席',
  theme_catalyst_desk: '主题催化席',
};

const DESK_ORDER = ['early_turn_desk', 'momentum_desk', 'quality_repair_desk', 'theme_catalyst_desk'];

function fmtPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  const sign = Number(value) > 0 ? '+' : '';
  return `${sign}${Number(value).toFixed(2)}%`;
}

function fmtNum(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(2);
}

function fmtDateTime(value?: string | null): string {
  if (!value) return '--';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function normalizeSeedDate(value?: string | null): string {
  const text = String(value || '').trim();
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(text);
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]}`;
  return text;
}

function describeJsonValue(value: unknown): string {
  if (value == null) return '';
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  if (Array.isArray(value)) {
    return value.map(describeJsonValue).filter(Boolean).join('；');
  }
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>;
    const summary = describeJsonValue(record.summary || record.message || record.reason || record.error);
    const type = describeJsonValue(record.type || record.kind || record.code);
    if (type && summary) return `${type}: ${summary}`;
    if (summary) return summary;
    if (type) return type;
    try {
      return JSON.stringify(record);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function toneForPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return 'text-secondary-text';
  if (Number(value) > 0) return 'text-danger';
  if (Number(value) < 0) return 'text-success';
  return 'text-secondary-text';
}

function catalystTierLabel(value?: number): string {
  if (value === 1) return '强催化';
  if (value === 2) return '中催化';
  if (value === 3) return '弱催化';
  return '无催化';
}

function deskLabel(value?: string): string {
  return deskLabels[String(value || '')] || String(value || '未知席位');
}

function stanceLabel(value?: string): string {
  const labels: Record<string, string> = {
    support: '支持',
    watch: '观察',
    neutral: '中性',
    oppose: '反对',
    invalid: '无效',
    missing: '缺失',
  };
  return labels[String(value || '')] || String(value || 'missing');
}

function stanceVariant(value?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (value === 'support') return 'success';
  if (value === 'watch') return 'info';
  if (value === 'oppose' || value === 'invalid') return 'danger';
  return 'default';
}

function orderedDeskOutcomes(outcomes?: SeedPoolDeskOutcome[]): SeedPoolDeskOutcome[] {
  const byDesk = new Map((outcomes || []).map((item) => [String(item.desk || ''), item]));
  const ordered = DESK_ORDER.map((desk) => byDesk.get(desk) || { desk, status: 'missing', stance: 'missing', decision: 'not_evaluated' });
  const extras = (outcomes || []).filter((item) => !DESK_ORDER.includes(String(item.desk || '')));
  return [...ordered, ...extras];
}

const MetricTile: React.FC<{ label: string; value: string; hint?: string; tone?: 'good' | 'bad' | 'warn' | 'neutral'; icon: React.ReactNode }> = ({ label, value, hint, tone = 'neutral', icon }) => {
  const toneClass = {
    good: 'text-danger border-danger/20 bg-danger/8',
    bad: 'text-success border-success/20 bg-success/8',
    warn: 'text-warning border-warning/20 bg-warning/8',
    neutral: 'text-foreground border-border/70 bg-card/70',
  }[tone];
  return (
    <div className={cn('min-h-[106px] border px-4 py-3', toneClass)}>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-medium uppercase tracking-[0.08em] text-secondary-text">{label}</span>
        <span className="text-secondary-text">{icon}</span>
      </div>
      <div className="mt-3 text-2xl font-semibold tabular-nums">{value}</div>
      {hint ? <div className="mt-1 text-xs text-secondary-text">{hint}</div> : null}
    </div>
  );
};

const CandlestickPanel: React.FC<{ data: SeedPoolChartData | null }> = ({ data }) => {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current || !data) return undefined;
    const chart = echarts.init(ref.current);
    const dates = data.bars.map((bar) => bar.tradeDate);
    const values = data.bars.map((bar) => [bar.open, bar.close, bar.low, bar.high]);
    const markLines = [];
    if (data.evaluation?.seedClose != null) {
      markLines.push({
        yAxis: data.evaluation.seedClose,
        name: 'Seed close',
        lineStyle: { color: '#64748b', width: 1.2, type: 'dashed' },
        label: { color: '#64748b', formatter: `seed close ${fmtNum(data.evaluation.seedClose)}` },
      });
    }
    if (data.evaluation?.evaluationDate) {
      markLines.push({
        xAxis: data.evaluation.evaluationDate,
        name: 'T+1',
        lineStyle: { color: '#0ea5e9', width: 1.2, type: 'dotted' },
        label: { color: '#0ea5e9', formatter: 'T+1' },
      });
    }
    chart.setOption({
      backgroundColor: 'transparent',
      grid: { left: 54, right: 24, top: 28, bottom: 42 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        borderWidth: 1,
        formatter: (params: unknown) => {
          const rows = Array.isArray(params) ? params : [params];
          const first = rows[0] as { dataIndex?: number } | undefined;
          const idx = Number(first?.dataIndex ?? 0);
          const bar = data.bars[idx];
          if (!bar) return '';
          return `${bar.tradeDate}<br/>开 ${fmtNum(bar.open)} / 收 ${fmtNum(bar.close)}<br/>高 ${fmtNum(bar.high)} / 低 ${fmtNum(bar.low)}`;
        },
      },
      xAxis: { type: 'category', data: dates, axisLine: { lineStyle: { color: '#94a3b8' } }, axisLabel: { color: '#64748b' } },
      yAxis: { scale: true, splitLine: { lineStyle: { color: 'rgba(148,163,184,0.18)' } }, axisLabel: { color: '#64748b' } },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8 }],
      series: [
        {
          type: 'candlestick',
          data: values,
          itemStyle: { color: '#dc2626', color0: '#16a34a', borderColor: '#dc2626', borderColor0: '#16a34a' },
          markLine: { symbol: 'none', data: markLines, silent: true },
        },
      ],
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [data]);

  if (!data) {
    return <EmptyState title="选择一只 Seed 查看 K 线" description="K 线会展示 T-20 到 T+5、seed close 和 T+1 标记。" icon={<CandlestickChart className="h-7 w-7" />} />;
  }

  return <div ref={ref} className="h-[420px] w-full border border-border/70 bg-card/60" data-testid="seed-quality-kline" />;
};

const AttributionChart: React.FC<{ rows: SeedPoolQualityGroupStat[] }> = ({ rows }) => {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const chart = echarts.init(ref.current);
    chart.setOption({
      grid: { left: 44, right: 16, top: 24, bottom: 44 },
      tooltip: { trigger: 'axis' },
      legend: { bottom: 0, textStyle: { color: '#64748b' } },
      xAxis: { type: 'category', data: rows.map((row) => row.key), axisLabel: { color: '#64748b', interval: 0, rotate: rows.length > 4 ? 22 : 0 } },
      yAxis: { type: 'value', axisLabel: { color: '#64748b' }, splitLine: { lineStyle: { color: 'rgba(148,163,184,0.18)' } } },
      series: [
        { name: 'Alpha', type: 'bar', data: rows.map((row) => row.avgAlphaReturnPct ?? 0), itemStyle: { color: '#0ea5e9' } },
        { name: '胜率', type: 'bar', data: rows.map((row) => row.winRatePct ?? 0), itemStyle: { color: '#22c55e' } },
        { name: 'T+1收益', type: 'bar', data: rows.map((row) => row.avgReturnPct ?? 0), itemStyle: { color: '#f59e0b' } },
      ],
    });
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [rows]);
  return <div ref={ref} className="h-[280px] w-full" data-testid="seed-quality-attribution-chart" />;
};

const SeedPoolQualityPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [dates, setDates] = useState<SeedPoolQualityDate[]>([]);
  const initialSeedDate = normalizeSeedDate(searchParams.get('seed_date'));
  const [selectedDate, setSelectedDate] = useState(initialSeedDate);
  const [quality, setQuality] = useState<SeedPoolQualityResponse | null>(null);
  const [selectedItemId, setSelectedItemId] = useState<number | null>(null);
  const [chartData, setChartData] = useState<SeedPoolChartData | null>(null);
  const [activeTab, setActiveTab] = useState<AttributionTab>('source');
  const [loading, setLoading] = useState(false);
  const [chartLoading, setChartLoading] = useState(false);
  const [evaluating, setEvaluating] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const loadDates = useCallback(async () => {
    const list = await seedPoolQualityApi.getDates();
    setDates(list);
    setSelectedDate((current) => {
      const normalized = normalizeSeedDate(current);
      if (!list.length) return normalized;
      if (normalized && list.some((item) => item.seedDate === normalized)) return normalized;
      return list[0].seedDate;
    });
  }, []);

  const loadQuality = useCallback(async (date: string) => {
    if (!date) return;
    setLoading(true);
    setError(null);
    try {
      const data = await seedPoolQualityApi.getByDate(date);
      setQuality(data);
      const firstId = data.items[0]?.id ?? null;
      setSelectedItemId(firstId);
    } catch (err) {
      setError(getParsedApiError(err));
      setQuality(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadDates().catch((err) => setError(getParsedApiError(err)));
  }, [loadDates]);

  useEffect(() => {
    if (selectedDate) void loadQuality(selectedDate);
  }, [selectedDate, loadQuality]);

  useEffect(() => {
    if (!selectedDate) return;
    const current = normalizeSeedDate(searchParams.get('seed_date'));
    if (current === selectedDate) return;
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set('seed_date', selectedDate);
      return next;
    }, { replace: true });
  }, [selectedDate, searchParams, setSearchParams]);

  useEffect(() => {
    if (!selectedItemId) {
      setChartData(null);
      return;
    }
    setChartLoading(true);
    void seedPoolQualityApi.getChartData(selectedItemId)
      .then(setChartData)
      .catch((err) => setError(getParsedApiError(err)))
      .finally(() => setChartLoading(false));
  }, [selectedItemId]);

  const summary = quality?.summary ?? {};
  const attributionRows = useMemo(() => {
    if (!quality) return [];
    if (activeTab === 'desk') return quality.deskStats;
    if (activeTab === 'catalyst') return quality.catalystTierStats.map((row) => ({ ...row, key: catalystTierLabel(Number(row.key)) }));
    return quality.sourceStats;
  }, [activeTab, quality]);

  const selectedItem = useMemo(
    () => quality?.items.find((item) => item.id === selectedItemId) ?? null,
    [quality?.items, selectedItemId],
  );

  const latestEvaluationUpdatedAt = useMemo(() => {
    const timestamps = (quality?.items || [])
      .map((item) => item.evaluation?.updatedAt)
      .filter((value): value is string => Boolean(value))
      .map((value) => {
        const date = new Date(value);
        return Number.isNaN(date.getTime()) ? null : date;
      })
      .filter((value): value is Date => Boolean(value))
      .sort((a, b) => b.getTime() - a.getTime());
    return timestamps[0]?.toISOString();
  }, [quality?.items]);

  const handleEvaluate = async () => {
    if (!selectedDate) return;
    setEvaluating(true);
    setError(null);
    try {
      await seedPoolQualityApi.evaluate(selectedDate);
      await loadQuality(selectedDate);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setEvaluating(false);
    }
  };

  return (
    <div className="min-h-full bg-base px-4 py-4 sm:px-6 lg:px-8">
      <div className="mx-auto flex max-w-[1600px] flex-col gap-5">
        <header className="border border-border/70 bg-card/80 px-5 py-4 shadow-soft-card">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between">
            <div>
              <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.18em] text-cyan">
                <DatabaseZap className="h-4 w-4" />
                Seed Pool Quality
              </div>
              <h1 className="mt-2 text-2xl font-semibold text-foreground">种子池质量监控</h1>
              <p className="mt-1 text-sm text-secondary-text">按日期复盘 Seed Pool 的 T+1 收盘收益、Alpha、流动性状态和四席位过滤质量。</p>
              <div className="mt-3 flex flex-wrap gap-2 text-xs text-secondary-text">
                <span className="border border-border/60 bg-elevated px-2 py-1">快照生成：{fmtDateTime(quality?.snapshot?.generatedAt)}</span>
                <span className="border border-border/60 bg-elevated px-2 py-1">T+1评估更新：{fmtDateTime(latestEvaluationUpdatedAt)}</span>
                <span className="border border-border/60 bg-elevated px-2 py-1">未评估：{summary.missingPriceCount ?? 0}</span>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <select
                value={selectedDate}
                onChange={(event) => setSelectedDate(normalizeSeedDate(event.target.value))}
                className="h-10 min-w-[180px] border border-border/70 bg-elevated px-3 text-sm text-foreground outline-none"
                aria-label="选择 seed date"
              >
                {dates.map((item) => (
                  <option key={item.seedDate} value={item.seedDate}>{item.seedDate} · 最新池</option>
                ))}
              </select>
              <button type="button" className="btn-secondary inline-flex h-10 items-center gap-2" onClick={() => void loadQuality(selectedDate)} disabled={!selectedDate || loading}>
                <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
                刷新
              </button>
              <button type="button" className="btn-primary inline-flex h-10 items-center gap-2" onClick={() => void handleEvaluate()} disabled={!selectedDate || evaluating}>
                <Activity className={cn('h-4 w-4', evaluating ? 'animate-pulse' : '')} />
                手动更新 T+1
              </button>
            </div>
          </div>
          <p className="mt-3 text-xs leading-5 text-muted-text">
            快照在选股链路生成 seed pool 时自动更新；T+1 评估需要读取 seed 日和下一交易日 OHLC，可在行情补齐后手动更新。
          </p>
        </header>

        {error ? <ApiErrorAlert error={error} /> : null}

        {!quality && !loading ? (
          <EmptyState title="暂无 Seed Pool 质量数据" description="跑一次四席位选股链路后，或从历史 trace 回填快照后，这里会显示质量复盘。" icon={<BarChart3 className="h-8 w-8" />} />
        ) : null}

        {quality ? (
          <>
            <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-6">
              <MetricTile label="Seed" value={String(summary.seedCount ?? 0)} hint={`已评估 ${summary.evaluatedCount ?? 0}`} icon={<Target className="h-4 w-4" />} />
              <MetricTile label="可交易样本" value={String(summary.tradableCount ?? 0)} hint={`剔除一字板 ${summary.limitUpUnableBuyCount ?? 0}`} icon={<DatabaseZap className="h-4 w-4" />} tone="warn" />
              <MetricTile label="胜率" value={fmtPct(summary.winRatePct)} hint={`${summary.upCount ?? 0}涨 / ${summary.downCount ?? 0}跌`} icon={<TrendingUp className="h-4 w-4" />} tone={(summary.winRatePct ?? 0) >= 50 ? 'good' : 'neutral'} />
              <MetricTile label="平均 Alpha" value={fmtPct(summary.avgAlphaReturnPct)} hint="基准：上证指数 000001.SH" icon={<LineChart className="h-4 w-4" />} tone={(summary.avgAlphaReturnPct ?? 0) >= 0 ? 'good' : 'bad'} />
              <MetricTile label="平均 T+1" value={fmtPct(summary.avgReturnPct)} hint="seed close 到 T+1 close" icon={<CandlestickChart className="h-4 w-4" />} tone={(summary.avgReturnPct ?? 0) >= 0 ? 'good' : 'bad'} />
              <MetricTile label="缺价样本" value={String(summary.missingPriceCount ?? 0)} hint="未参与 Alpha 汇总" icon={<CandlestickChart className="h-4 w-4" />} tone={(summary.missingPriceCount ?? 0) > 0 ? 'warn' : 'neutral'} />
            </section>

            <section className="grid gap-5 xl:grid-cols-[minmax(0,0.95fr)_minmax(0,1.35fr)]">
              <div className="border border-border/70 bg-card/70 p-4">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <h2 className="text-base font-semibold text-foreground">归因分析</h2>
                  <div className="inline-flex border border-border/70 bg-elevated p-1">
                    {([
                      ['source', 'Source'],
                      ['desk', 'Desk'],
                      ['catalyst', 'Catalyst Tier'],
                    ] as Array<[AttributionTab, string]>).map(([key, label]) => (
                      <button
                        key={key}
                        type="button"
                        onClick={() => setActiveTab(key)}
                        className={cn('h-8 px-3 text-xs font-medium', activeTab === key ? 'bg-cyan text-black' : 'text-secondary-text hover:text-foreground')}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                </div>
                <AttributionChart rows={attributionRows} />
              </div>

              <div className="border border-border/70 bg-card/70 p-4">
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h2 className="text-base font-semibold text-foreground">T+1 K 线复盘</h2>
                  {selectedItem ? <Badge variant="info">{selectedItem.code} {selectedItem.name}</Badge> : null}
                </div>
                {chartLoading ? (
                  <div className="flex h-[420px] items-center justify-center border border-border/70 text-sm text-secondary-text">加载 K 线...</div>
                ) : (
                  <CandlestickPanel data={chartData} />
                )}
              </div>
            </section>

            <section className="grid gap-5 xl:grid-cols-[minmax(0,1.2fr)_minmax(360px,0.8fr)]">
              <div className="overflow-hidden border border-border/70 bg-card/70">
                <div className="border-b border-border/70 px-4 py-3">
                  <h2 className="text-base font-semibold text-foreground">Seed 明细</h2>
                </div>
                <div className="overflow-x-auto">
                  <table className="min-w-full text-left text-sm">
                    <thead className="bg-elevated text-xs uppercase tracking-[0.08em] text-secondary-text">
                      <tr>
                        <th className="px-4 py-3">股票</th>
                        <th className="px-4 py-3">来源</th>
                        <th className="px-4 py-3">Catalyst</th>
                        <th className="px-4 py-3">Alpha</th>
                        <th className="px-4 py-3">T+1收盘</th>
                        <th className="px-4 py-3">流动性</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border/60">
                      {quality.items.map((item) => {
                        const evaluation = item.evaluation || {};
                        const active = item.id === selectedItemId;
                        return (
                          <tr
                            key={item.id}
                            className={cn('cursor-pointer transition-colors hover:bg-hover', active ? 'bg-cyan/8' : '')}
                            onClick={() => setSelectedItemId(item.id)}
                          >
                            <td className="px-4 py-3">
                              <div className="font-medium text-foreground">{item.name}</div>
                              <div className="text-xs text-secondary-text">{item.code}</div>
                            </td>
                            <td className="px-4 py-3 text-secondary-text">{item.source || '-'}</td>
                            <td className="px-4 py-3">
                              <div className="flex flex-wrap gap-1">
                                <Badge variant={item.catalystTier === 1 ? 'success' : item.catalystTier === 2 ? 'warning' : 'default'}>{catalystTierLabel(item.catalystTier)}</Badge>
                                {(item.catalystTags || []).slice(0, 2).map((tag) => <Badge key={tag} variant="history">{tag}</Badge>)}
                              </div>
                            </td>
                            <td className={cn('px-4 py-3 font-semibold tabular-nums', toneForPct(evaluation.alphaReturnPct))}>{fmtPct(evaluation.alphaReturnPct)}</td>
                            <td className={cn('px-4 py-3 font-semibold tabular-nums', toneForPct(evaluation.nextCloseReturnPct))}>{fmtPct(evaluation.nextCloseReturnPct)}</td>
                            <td className="px-4 py-3">
                              <Badge variant={evaluation.liquidityStatus === 'LIMIT_UP_UNABLE_BUY' ? 'danger' : 'default'}>
                                {liquidityLabels[evaluation.liquidityStatus || 'UNKNOWN'] || evaluation.liquidityStatus || '未评估'}
                              </Badge>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>

              <aside className="border border-border/70 bg-card/70 p-4">
                <h2 className="text-base font-semibold text-foreground">席位复盘</h2>
                {selectedItem ? (
                  <div className="mt-3 flex flex-col gap-3">
                    <div className="border border-border/60 bg-elevated/60 p-3">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <div className="text-sm font-medium text-foreground">{selectedItem.name} · {selectedItem.code}</div>
                          <div className="mt-1 text-xs text-secondary-text">{selectedItem.entryReason || '无入池理由摘要'}</div>
                        </div>
                        {quality.snapshot?.traceId ? (
                          <Link
                            to={`/agent-trace/${encodeURIComponent(quality.snapshot.traceId)}`}
                            className="inline-flex shrink-0 items-center gap-1 border border-border/70 bg-card px-2 py-1 text-xs text-secondary-text transition-colors hover:text-foreground"
                          >
                            <ExternalLink className="h-3.5 w-3.5" />
                            Trace
                          </Link>
                        ) : null}
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1">
                        <Badge variant={selectedItem.enteredDeepDive ? 'success' : 'default'}>
                          {selectedItem.enteredDeepDive ? '进入深挖' : '未深挖'}
                        </Badge>
                        <Badge variant={selectedItem.enteredFinalReport ? 'success' : 'default'}>
                          {selectedItem.enteredFinalReport ? '进入报告' : '未进报告'}
                        </Badge>
                        {selectedItem.freshness ? <Badge variant="default">{selectedItem.freshness}</Badge> : null}
                      </div>
                      <div className="mt-2 text-xs leading-5 text-muted-text">
                        Trace 页展示 seed pool 概览；本页按落库结果展开四席位理由，完整原始包仍以 Trace artifact JSON 为准。
                      </div>
                    </div>
                    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-1">
                      {orderedDeskOutcomes(selectedItem.deskOutcomes).map((outcome) => (
                      <div key={`${selectedItem.id}-${outcome.desk}-${outcome.stance}`} className={cn('border p-3', outcome.stance === 'missing' ? 'border-border/40 bg-elevated/30 opacity-80' : 'border-border/60')}>
                        <div className="flex items-center justify-between gap-2">
                          <span className="font-medium text-foreground">{deskLabel(outcome.desk)}</span>
                          <Badge variant={stanceVariant(outcome.stance)}>
                            {stanceLabel(outcome.stance)}
                          </Badge>
                        </div>
                        <div className="mt-2 flex flex-wrap gap-1">
                          <Badge variant="default">status: {outcome.status || 'missing'}</Badge>
                          <Badge variant="default">decision: {outcome.decision || 'not_evaluated'}</Badge>
                        </div>
                        <p className="mt-2 text-sm leading-6 text-secondary-text">{outcome.reason || '未落盘该席位理由'}</p>
                        {(outcome.risks || []).length ? (
                          <div className="mt-2">
                            <div className="text-xs font-medium text-muted-text">风险</div>
                            <ul className="mt-1 list-disc space-y-1 pl-4 text-xs leading-5 text-secondary-text">
                              {(outcome.risks || []).slice(0, 4).map((risk, index) => (
                                <li key={`${outcome.desk}-risk-${index}`}>{describeJsonValue(risk)}</li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                        {(outcome.errors || []).length ? (
                          <div className="mt-2">
                            <div className="text-xs font-medium text-danger">错误</div>
                            <ul className="mt-1 list-disc space-y-1 pl-4 text-xs leading-5 text-danger">
                              {(outcome.errors || []).slice(0, 3).map((errorItem, index) => (
                                <li key={`${outcome.desk}-error-${index}`}>{describeJsonValue(errorItem)}</li>
                              ))}
                            </ul>
                          </div>
                        ) : null}
                        {Object.keys(outcome.metrics || {}).length ? (
                          <div className="mt-2 flex flex-wrap gap-1">
                            {Object.entries(outcome.metrics || {}).map(([key, value]) => (
                              <Badge key={key} variant="default">{key}: {String(value)}</Badge>
                            ))}
                          </div>
                        ) : null}
                      </div>
                      ))}
                    </div>
                  </div>
                ) : (
                  <EmptyState title="未选择 Seed" description="点击左侧明细行查看四席位观点。" />
                )}
              </aside>
            </section>
          </>
        ) : null}
      </div>
    </div>
  );
};

export default SeedPoolQualityPage;
