import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, BarChart3, Database, Layers3, ListFilter, RefreshCw, ShieldCheck, Sparkles, TrendingUp } from 'lucide-react';
import { candidatePoolApi, type CandidatePoolDetail, type CandidatePoolItem, type CandidatePoolRun } from '../api/candidatePool';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, EmptyState, StatusDot } from '../components/common';
import { cn } from '../utils/cn';

const dimensionLabels: Record<string, string> = {
  strategy: '策略',
  technical: '技术',
  capital: '资金',
  fundamental: '基本面',
  message: '消息',
  sentiment: '情绪',
  event: '事件',
  sector: '板块',
  unknown: '未标注',
};

const sourceLabels: Record<string, string> = {
  alphasift: 'AlphaSift',
  sequoia: 'Sequoia',
  fundamental: '基本面',
  sector: '板块',
  event_impact: '事件传导',
  news_momentum: '消息动量',
  news_sentiment: '热点新闻',
  user_seed: '用户输入',
  fallback: '兜底池',
  unknown: '未知',
};

const lifecycleLabels: Record<string, string> = {
  new: '新进入',
  active: '持续观察',
  watching: '观察中',
  decayed: '降权',
  removed: '移出',
};

function formatScore(value?: number | null): string {
  if (value == null || Number.isNaN(Number(value))) return '--';
  return Number(value).toFixed(0);
}

function labelFor(map: Record<string, string>, value?: string): string {
  const key = String(value || 'unknown');
  return map[key] || key;
}

function countEntries(counts?: Record<string, number>): Array<[string, number]> {
  return Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
}

function formatMetric(value: unknown, suffix = ''): string {
  if (value == null || value === '') return '--';
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return `${num.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
}

function fundamentalMetricPairs(item: CandidatePoolItem): Array<[string, string]> {
  const metrics = item.metrics || {};
  const pairs: Array<[string, string]> = [
    ['ROE', formatMetric(metrics.roe, '%')],
    ['营收增速', formatMetric(metrics.revenueGrowth ?? metrics.revenue_growth, '%')],
    ['利润增速', formatMetric(metrics.profitGrowth ?? metrics.profit_growth, '%')],
    ['现金流/利润', formatMetric(metrics.operatingCashflowRatio ?? metrics.operating_cashflow_ratio, '%')],
    ['PE', formatMetric(metrics.peTtm ?? metrics.pe_ttm)],
    ['PB', formatMetric(metrics.pb)],
  ];
  return pairs.filter(([, value]) => value !== '--');
}

function isFundamentalCandidate(item: CandidatePoolItem): boolean {
  return Boolean(
    item.candidateDimensions?.includes('fundamental')
    || item.candidateExperts?.includes('fundamental_expert')
    || item.source?.startsWith('fundamental:'),
  );
}

function fundamentalStatusTone(status?: string): 'default' | 'good' | 'warn' | 'bad' {
  if (status === 'ok') return 'good';
  if (status === 'empty' || status === 'unavailable' || status === 'missing_packet') return 'warn';
  if (status === 'failed' || status === 'timeout') return 'bad';
  return 'default';
}

const MetricTile: React.FC<{ label: string; value: string | number; tone?: 'default' | 'good' | 'warn' | 'bad'; icon: React.ReactNode }> = ({
  label,
  value,
  tone = 'default',
  icon,
}) => (
  <div
    className={cn(
      'min-h-[92px] rounded-lg border bg-surface-2/70 px-4 py-3',
      tone === 'good' && 'border-success/25 bg-success/5',
      tone === 'warn' && 'border-warning/25 bg-warning/5',
      tone === 'bad' && 'border-danger/25 bg-danger/5',
      tone === 'default' && 'border-border/60',
    )}
  >
    <div className="flex items-center justify-between gap-3">
      <span className="text-xs text-muted-text">{label}</span>
      <span className="text-secondary-text">{icon}</span>
    </div>
    <div className="mt-2 text-2xl font-semibold text-foreground">{value}</div>
  </div>
);

const CountStrip: React.FC<{ title: string; counts?: Record<string, number>; labels?: Record<string, string> }> = ({ title, counts, labels }) => {
  const entries = countEntries(counts);
  return (
    <section className="rounded-lg border border-border/60 bg-card/55 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        <span className="text-xs text-muted-text">{entries.length} 类</span>
      </div>
      {entries.length ? (
        <div className="flex flex-wrap gap-2">
          {entries.map(([key, value]) => (
            <Badge key={key} variant="default">
              {labels ? labelFor(labels, key) : key}
              <span className="font-mono text-muted-text">{value}</span>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted-text">暂无分布数据</p>
      )}
    </section>
  );
};

const CandidateRow: React.FC<{ item: CandidatePoolItem }> = ({ item }) => {
  const dimensions = item.candidateDimensions?.length ? item.candidateDimensions : ['unknown'];
  const lifecycle = item.lifecycleStatus || 'new';
  return (
    <tr className="border-b border-border/50 align-top last:border-0">
      <td className="w-[170px] px-4 py-4">
        <div className="font-mono text-sm font-semibold text-foreground">{item.code}</div>
        <div className="mt-1 max-w-[150px] truncate text-sm text-secondary-text">{item.name || item.code}</div>
      </td>
      <td className="w-[90px] px-4 py-4">
        <span className="inline-flex h-9 min-w-12 items-center justify-center rounded-md border border-cyan/25 bg-cyan/10 px-2 font-mono text-sm font-semibold text-cyan">
          {formatScore(item.signalScore)}
        </span>
      </td>
      <td className="px-4 py-4">
        <div className="flex flex-wrap gap-1.5">
          {dimensions.map((dimension) => (
            <Badge key={dimension} variant={dimension === 'unknown' ? 'warning' : 'info'}>
              {labelFor(dimensionLabels, dimension)}
            </Badge>
          ))}
        </div>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary-text">{item.reason || '暂无入池理由'}</p>
        {item.recallSources?.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {item.recallSources.slice(0, 5).map((source) => (
              <span key={source} className="rounded-md border border-border/50 bg-surface-2 px-2 py-0.5 text-[11px] text-muted-text">
                {source}
              </span>
            ))}
          </div>
        ) : null}
        {isFundamentalCandidate(item) ? (
          <div className="mt-3 rounded-lg border border-stone-200 bg-stone-50/70 px-3 py-2">
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-semibold text-stone-700">
              <BarChart3 className="h-3.5 w-3.5" />
              P2 基本面指标
              {item.validUntil ? <span className="font-normal text-stone-500">有效至 {item.validUntil}</span> : null}
            </div>
            {fundamentalMetricPairs(item).length ? (
              <div className="flex flex-wrap gap-1.5">
                {fundamentalMetricPairs(item).map(([label, value]) => (
                  <span key={label} className="rounded-md bg-white/80 px-2 py-0.5 text-[11px] text-stone-700">
                    {label} {value}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-[11px] text-stone-600">候选来自基本面专家，但本条未带可展示指标。</p>
            )}
          </div>
        ) : null}
      </td>
      <td className="w-[150px] px-4 py-4">
        <Badge variant={lifecycle === 'new' ? 'success' : 'history'}>
          {labelFor(lifecycleLabels, lifecycle)}
        </Badge>
        <div className="mt-2 text-xs text-muted-text">出现 {item.recurrenceCount || 1} 次</div>
      </td>
    </tr>
  );
};

const CandidatePoolPage: React.FC = () => {
  const [detail, setDetail] = useState<CandidatePoolDetail | null>(null);
  const [runs, setRuns] = useState<CandidatePoolRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [activeDimension, setActiveDimension] = useState('all');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  useEffect(() => {
    document.title = '候选池 - DSA';
  }, []);

  const loadLatest = useCallback(async () => {
    setIsLoading(true);
    try {
      const [latest, recentRuns] = await Promise.all([
        candidatePoolApi.getLatest(),
        candidatePoolApi.getRuns(20),
      ]);
      setDetail(latest);
      setRuns(recentRuns.runs || []);
      setSelectedRunId(latest.run?.runId || '');
      setError(null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, []);

  const loadRun = useCallback(async (runId: string) => {
    if (!runId) return;
    setIsLoading(true);
    try {
      const result = await candidatePoolApi.getRun(runId);
      setDetail(result);
      setSelectedRunId(runId);
      setActiveDimension('all');
      setError(null);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadLatest();
  }, [loadLatest]);

  const dimensionOptions = useMemo(() => {
    const counts = detail?.summary.dimensionCounts || {};
    return ['all', ...Object.keys(counts)];
  }, [detail?.summary.dimensionCounts]);

  const visibleItems = useMemo(() => {
    const items = detail?.items || [];
    if (activeDimension === 'all') return items;
    return items.filter((item) => (item.candidateDimensions || []).includes(activeDimension));
  }, [activeDimension, detail?.items]);

  const summary = detail?.summary || {};
  const hardStrategyMissing = Boolean(summary.hardStrategyTrunkMissing);
  const fallbackCount = Number(summary.fallbackCount || 0);
  const hardExclusionCount = Number(summary.hardExclusionCount || 0);
  const fundamentalStatus = detail?.fundamentalStatus || {};
  const fundamentalCandidates = (detail?.items || []).filter(isFundamentalCandidate);
  const fundamentalTone = fundamentalStatusTone(fundamentalStatus.status);

  return (
    <div className="space-y-6 p-4 sm:p-6 lg:p-8">
      <header className="flex flex-col gap-4 border-b border-border/60 pb-5 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-[0.12em] text-cyan">
            <ListFilter className="h-4 w-4" />
            Agent L1 Candidate Pool
          </div>
          <h1 className="mt-2 text-2xl font-semibold text-foreground">候选池</h1>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary-text">
            这里展示候选发现阶段的独立结果：候选来自哪些专家/策略、为什么入池、是否使用兜底，以及硬排除和生命周期状态。
          </p>
        </div>
        <div className="flex flex-col gap-2 sm:flex-row">
          <select
            className="input-surface input-focus-glow h-10 rounded-lg border bg-transparent px-3 text-sm text-foreground"
            value={selectedRunId}
            onChange={(event) => void loadRun(event.target.value)}
            aria-label="选择候选池运行"
          >
            {runs.length === 0 ? <option value="">暂无运行</option> : null}
            {runs.map((run) => (
              <option key={run.runId} value={run.runId}>
                {run.createdAt} · {run.candidateCount} 只
              </option>
            ))}
          </select>
          <button
            type="button"
            className="inline-flex h-10 items-center justify-center gap-2 rounded-lg border border-border/70 bg-surface-2 px-4 text-sm font-medium text-foreground transition hover:bg-hover disabled:cursor-not-allowed disabled:opacity-60"
            onClick={() => void loadLatest()}
            disabled={isLoading}
          >
            <RefreshCw className={cn('h-4 w-4', isLoading && 'animate-spin')} />
            刷新
          </button>
        </div>
      </header>

      {error ? <ApiErrorAlert error={error} actionLabel="重试" onAction={() => void loadLatest()} /> : null}

      {!detail?.run && !isLoading ? (
        <EmptyState
          title="还没有候选池记录"
          description="运行一次选股 Trace 或调用 discover_watchlist_candidates 后，这里会显示最新候选池。"
          icon={<Database className="h-8 w-8" />}
        />
      ) : (
        <>
          <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
            <MetricTile label="当前候选" value={summary.candidateCount ?? detail?.items.length ?? 0} icon={<Layers3 className="h-4 w-4" />} />
            <MetricTile
              label="硬策略主干"
              value={hardStrategyMissing ? '缺失' : '可用'}
              tone={hardStrategyMissing ? 'bad' : 'good'}
              icon={<ShieldCheck className="h-4 w-4" />}
            />
            <MetricTile label="多源共振" value={summary.multiSourceCount ?? 0} tone="good" icon={<Sparkles className="h-4 w-4" />} />
            <MetricTile
              label="兜底/硬排除"
              value={`${fallbackCount}/${hardExclusionCount}`}
              tone={fallbackCount > 0 || hardExclusionCount > 0 ? 'warn' : 'default'}
              icon={<AlertTriangle className="h-4 w-4" />}
            />
          </section>

          <section className={cn(
            'rounded-lg border p-4',
            fundamentalTone === 'good' && 'border-stone-300 bg-stone-50',
            fundamentalTone === 'warn' && 'border-warning/30 bg-warning/5',
            fundamentalTone === 'bad' && 'border-danger/30 bg-danger/5',
            fundamentalTone === 'default' && 'border-border/60 bg-card/55',
          )}>
            <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
              <div>
                <div className="flex items-center gap-2">
                  <BarChart3 className="h-4 w-4 text-secondary-text" />
                  <h2 className="text-sm font-semibold text-foreground">P2 基本面发现状态</h2>
                  <Badge variant={fundamentalTone === 'good' ? 'success' : fundamentalTone === 'bad' ? 'danger' : 'warning'}>
                    {fundamentalStatus.status || 'unknown'}
                  </Badge>
                </div>
                <p className="mt-2 max-w-3xl text-sm leading-6 text-secondary-text">
                  基本面专家读取本地预计算表生成候选，不在 Trace 运行时实时全市场拉财报。这里用于判断 P2 是否真正参与了本轮候选池。
                </p>
              </div>
              <div className="grid min-w-[280px] grid-cols-2 gap-2 text-xs">
                <div className="rounded-md bg-white/70 px-3 py-2">
                  <div className="text-muted-text">基本面候选</div>
                  <div className="mt-1 font-semibold text-foreground">{fundamentalStatus.candidateCount ?? fundamentalCandidates.length}</div>
                </div>
                <div className="rounded-md bg-white/70 px-3 py-2">
                  <div className="text-muted-text">快照行数</div>
                  <div className="mt-1 font-semibold text-foreground">{fundamentalStatus.rowCount ?? '--'}</div>
                </div>
                <div className="rounded-md bg-white/70 px-3 py-2">
                  <div className="text-muted-text">报告期</div>
                  <div className="mt-1 font-semibold text-foreground">{fundamentalStatus.latestPeriod || '--'}</div>
                </div>
                <div className="rounded-md bg-white/70 px-3 py-2">
                  <div className="text-muted-text">事件数</div>
                  <div className="mt-1 font-semibold text-foreground">{fundamentalStatus.eventCount ?? '--'}</div>
                </div>
              </div>
            </div>
            <div className="mt-3 grid gap-2 text-xs lg:grid-cols-[1fr_1fr]">
              <div className="rounded-md bg-white/65 px-3 py-2">
                <div className="text-muted-text">数据表</div>
                <div className="mt-1 break-all text-secondary-text">{[fundamentalStatus.table, fundamentalStatus.dbPath].filter(Boolean).join(' · ') || '--'}</div>
              </div>
              <div className="rounded-md bg-white/65 px-3 py-2">
                <div className="text-muted-text">诊断</div>
                <div className="mt-1 text-secondary-text">
                  {[...(fundamentalStatus.errors || []), ...(fundamentalStatus.warnings || [])].slice(0, 2).join('；') || '基本面专家已参与本轮候选发现。'}
                </div>
              </div>
            </div>
          </section>

          {detail?.run ? (
            <section className="rounded-lg border border-border/60 bg-card/50 p-4">
              <div className="grid gap-3 text-sm md:grid-cols-4">
                <div>
                  <div className="text-xs text-muted-text">运行时间</div>
                  <div className="mt-1 text-foreground">{detail.run.createdAt}</div>
                </div>
                <div>
                  <div className="text-xs text-muted-text">来源模式</div>
                  <div className="mt-1 text-foreground">{detail.run.candidateSource || '--'}</div>
                </div>
                <div>
                  <div className="text-xs text-muted-text">状态</div>
                  <div className="mt-1 flex items-center gap-2 text-foreground">
                    <StatusDot tone={detail.run.status === 'ok' ? 'success' : 'warning'} />
                    {detail.run.status || '--'}
                  </div>
                </div>
                <div>
                  <div className="text-xs text-muted-text">说明</div>
                  <div className="mt-1 line-clamp-2 text-secondary-text">{detail.run.note || '--'}</div>
                </div>
              </div>
            </section>
          ) : null}

          <section className="grid gap-4 xl:grid-cols-[1fr_1fr_1fr]">
            <CountStrip title="维度分布" counts={summary.dimensionCounts} labels={dimensionLabels} />
            <CountStrip title="来源分布" counts={summary.sourceCounts} labels={sourceLabels} />
            <CountStrip title="生命周期" counts={summary.lifecycleCounts} labels={lifecycleLabels} />
          </section>

          <section className="rounded-lg border border-border/60 bg-card/60">
            <div className="flex flex-col gap-3 border-b border-border/60 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <h2 className="text-base font-semibold text-foreground">候选明细</h2>
                <p className="mt-1 text-xs text-muted-text">入池理由按结构化摘要展示，原始证据仍保留在 Trace 中。</p>
              </div>
              <div className="flex flex-wrap gap-2">
                {dimensionOptions.map((dimension) => (
                  <button
                    type="button"
                    key={dimension}
                    className={cn(
                      'rounded-full border px-3 py-1 text-xs transition',
                      activeDimension === dimension
                        ? 'border-cyan/45 bg-cyan/12 text-cyan'
                        : 'border-border/60 bg-surface-2 text-secondary-text hover:text-foreground',
                    )}
                    onClick={() => setActiveDimension(dimension)}
                  >
                    {dimension === 'all' ? '全部' : labelFor(dimensionLabels, dimension)}
                  </button>
                ))}
              </div>
            </div>
            {visibleItems.length ? (
              <div className="overflow-x-auto">
                <table className="min-w-full text-left">
                  <thead className="border-b border-border/60 text-xs text-muted-text">
                    <tr>
                      <th className="px-4 py-3 font-medium">股票</th>
                      <th className="px-4 py-3 font-medium">评分</th>
                      <th className="px-4 py-3 font-medium">入池依据</th>
                      <th className="px-4 py-3 font-medium">生命周期</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visibleItems.map((item) => (
                      <CandidateRow key={`${item.runId}-${item.code}`} item={item} />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="p-6">
                <EmptyState title="当前筛选下没有候选" description="切换到全部维度可查看本轮完整候选池。" icon={<TrendingUp className="h-8 w-8" />} />
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
};

export default CandidatePoolPage;
