import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, BarChart3, Database, Filter, GitBranch, RefreshCw, Search, Target } from 'lucide-react';
import { agentVerdictReviewsApi, type VerdictReviewBuildResult, type VerdictReviewResponse, type VerdictReviewRow } from '../api/agentVerdictReviews';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, EmptyState } from '../components/common';
import { cn } from '../utils/cn';

type ChainFilter = '' | 'stock_selection' | 'single_stock_analysis';

const reviewLabels = ['', 'hit', 'missed_up', 'avoided_down', 'wrong_direction', 'no_edge', 'neutral_ok', 'insufficient_data'];

const chainLabels: Record<string, string> = {
  stock_selection: '选股链路',
  single_stock_analysis: '单股链路',
};

const labelText: Record<string, string> = {
  hit: '命中',
  missed_up: '错过上涨',
  missedUp: '错过上涨',
  avoided_down: '避开下跌',
  avoidedDown: '避开下跌',
  wrong_direction: '方向错误',
  wrongDirection: '方向错误',
  no_edge: '无明显优势',
  noEdge: '无明显优势',
  neutral_ok: '中性',
  neutralOk: '中性',
  insufficient_data: '数据不足',
  insufficientData: '数据不足',
  unclassified: '未分类',
};

const canonicalReviewLabel: Record<string, string> = {
  missedUp: 'missed_up',
  avoidedDown: 'avoided_down',
  wrongDirection: 'wrong_direction',
  noEdge: 'no_edge',
  neutralOk: 'neutral_ok',
  insufficientData: 'insufficient_data',
};

function fmtPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  const sign = Number(value) > 0 ? '+' : '';
  return `${sign}${Number(value).toFixed(2)}%`;
}

function labelVariant(label?: string): React.ComponentProps<typeof Badge>['variant'] {
  const key = canonicalReviewLabel[label || ''] || label;
  if (key === 'hit' || key === 'avoided_down' || key === 'neutral_ok') return 'success';
  if (key === 'missed_up' || key === 'wrong_direction') return 'danger';
  if (key === 'insufficient_data') return 'warning';
  return 'default';
}

function chainVariant(chain?: string): React.ComponentProps<typeof Badge>['variant'] {
  return chain === 'stock_selection' ? 'info' : 'history';
}

function preferredWindow(row: VerdictReviewRow) {
  return row.windows?.['30'] || row.windows?.['7'] || Object.values(row.windows || {}).find((item) => item?.evalStatus === 'completed') || {};
}

function countValue(counts: Record<string, number> | undefined, ...keys: string[]): number {
  if (!counts) return 0;
  for (const key of keys) {
    const value = counts[key];
    if (typeof value === 'number') return value;
  }
  return 0;
}

const MetricTile: React.FC<{ label: string; value: string; hint?: string; icon: React.ReactNode }> = ({ label, value, hint, icon }) => (
  <div className="min-h-[100px] border border-border/70 bg-card/70 px-4 py-3">
    <div className="flex items-center justify-between gap-3 text-secondary-text">
      <span className="text-xs font-medium uppercase tracking-[0.08em]">{label}</span>
      {icon}
    </div>
    <div className="mt-3 text-2xl font-semibold tabular-nums text-foreground">{value}</div>
    {hint ? <div className="mt-1 text-xs text-secondary-text">{hint}</div> : null}
  </div>
);

const LabelDistribution: React.FC<{ counts?: Record<string, number> }> = ({ counts = {} }) => {
  const rows = Object.entries(counts)
    .filter(([, count]) => Number(count) > 0)
    .sort((a, b) => Number(b[1]) - Number(a[1]));
  if (!rows.length) return null;
  const max = Math.max(...rows.map(([, count]) => Number(count) || 0), 1);
  return (
    <div className="border border-border/70 bg-card/60 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground">
        <BarChart3 className="h-4 w-4 text-cyan" />
        标签分布
      </div>
      <div className="space-y-2">
        {rows.map(([label, count]) => (
          <div key={label} className="grid grid-cols-[92px_1fr_40px] items-center gap-3 text-xs">
            <span className="truncate text-secondary-text">{labelText[label] || labelText[canonicalReviewLabel[label]] || label}</span>
            <div className="h-2 bg-muted">
              <div className="h-2 bg-cyan" style={{ width: `${Math.max(6, (Number(count) / max) * 100)}%` }} />
            </div>
            <span className="text-right tabular-nums text-foreground">{count}</span>
          </div>
        ))}
      </div>
    </div>
  );
};

const ReviewTable: React.FC<{ rows: VerdictReviewRow[] }> = ({ rows }) => (
  <div className="overflow-hidden border border-border/70 bg-card/70">
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-border/60 text-sm">
        <thead className="bg-elevated/70 text-xs uppercase tracking-[0.08em] text-secondary-text">
          <tr>
            <th className="px-4 py-3 text-left">日期</th>
            <th className="px-4 py-3 text-left">链路</th>
            <th className="px-4 py-3 text-left">标的</th>
            <th className="px-4 py-3 text-left">动作</th>
            <th className="px-4 py-3 text-left">标签</th>
            <th className="px-4 py-3 text-right">后验收益</th>
            <th className="px-4 py-3 text-left">Trace</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/50">
          {rows.map((row) => {
            const window = preferredWindow(row);
            const returnPct = window.futureReturnPct;
            return (
              <tr key={`${row.traceId}-${row.symbol}-${row.chainType}`} className="hover:bg-hover/70">
                <td className="whitespace-nowrap px-4 py-3 text-secondary-text">{row.decisionDate || '--'}</td>
                <td className="whitespace-nowrap px-4 py-3">
                  <Badge variant={chainVariant(row.chainType)}>{chainLabels[row.chainType || ''] || row.chainType || '--'}</Badge>
                </td>
                <td className="px-4 py-3">
                  <div className="font-medium text-foreground">{row.symbol || '--'}</div>
                  <div className="text-xs text-secondary-text">{row.name || row.regime || '--'}</div>
                </td>
                <td className="px-4 py-3">
                  <div className="text-foreground">{row.symbolAction || row.finalAction || '--'}</div>
                  <div className="text-xs text-secondary-text">{row.operationAdvice || row.primaryPlanVerdict || row.decisionType || '--'}</div>
                </td>
                <td className="whitespace-nowrap px-4 py-3">
                  <Badge variant={labelVariant(row.reviewLabel)}>{labelText[row.reviewLabel || ''] || row.reviewLabel || '--'}</Badge>
                </td>
                <td className={cn('whitespace-nowrap px-4 py-3 text-right font-semibold tabular-nums', Number(returnPct) > 0 ? 'text-danger' : Number(returnPct) < 0 ? 'text-success' : 'text-secondary-text')}>
                  {fmtPct(returnPct)}
                  <div className="text-xs font-normal text-secondary-text">{window.evalStatus || 'unknown'}</div>
                </td>
                <td className="max-w-[220px] truncate px-4 py-3 text-xs text-secondary-text" title={row.traceId || row.traceDir || ''}>
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

const AgentVerdictReviewsPage: React.FC = () => {
  const [data, setData] = useState<VerdictReviewResponse | null>(null);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [buildResult, setBuildResult] = useState<VerdictReviewBuildResult | null>(null);
  const [chainType, setChainType] = useState<ChainFilter>('');
  const [reviewLabel, setReviewLabel] = useState('');
  const [symbol, setSymbol] = useState('');

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await agentVerdictReviewsApi.list({
        chainType: chainType || undefined,
        reviewLabel: reviewLabel || undefined,
        symbol: symbol.trim() || undefined,
        limit: 300,
      });
      setData(result);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, [chainType, reviewLabel, symbol]);

  const rebuildData = useCallback(async () => {
    setRebuilding(true);
    setError(null);
    try {
      const result = await agentVerdictReviewsApi.rebuild({ windows: '7,30', limit: 300 });
      setBuildResult(result);
      await loadData();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRebuilding(false);
    }
  }, [loadData]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const summary = data?.summary || {};
  const rows = useMemo(() => data?.items ?? [], [data]);

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2">
            <Target className="h-5 w-5 text-cyan" />
            <h1 className="text-2xl font-semibold text-foreground">Agent 后验复盘</h1>
          </div>
          <p className="max-w-3xl text-sm text-secondary-text">
            从 verdict review JSONL 读取选股与单股链路后验表现，当前页面只展示复盘证据，不改写线上决策。
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn-secondary inline-flex h-10 items-center gap-2 px-4" onClick={() => void loadData()} disabled={loading || rebuilding}>
            <RefreshCw className={cn('h-4 w-4', loading ? 'animate-spin' : '')} />
            刷新
          </button>
          <button type="button" className="btn-primary inline-flex h-10 items-center gap-2 px-4" onClick={() => void rebuildData()} disabled={loading || rebuilding}>
            <Database className={cn('h-4 w-4', rebuilding ? 'animate-pulse' : '')} />
            重建样本
          </button>
        </div>
      </div>

      <div className="grid gap-3 border border-border/70 bg-card/60 p-4 md:grid-cols-[180px_180px_1fr_auto] md:items-end">
        <label className="text-sm">
          <span className="mb-1 block text-xs font-medium text-secondary-text">链路</span>
          <select className="input-surface input-focus-glow h-10 w-full" value={chainType} onChange={(event) => setChainType(event.target.value as ChainFilter)}>
            <option value="">全部链路</option>
            <option value="stock_selection">选股链路</option>
            <option value="single_stock_analysis">单股链路</option>
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs font-medium text-secondary-text">标签</span>
          <select className="input-surface input-focus-glow h-10 w-full" value={reviewLabel} onChange={(event) => setReviewLabel(event.target.value)}>
            {reviewLabels.map((item) => (
              <option key={item || 'all'} value={item}>{item ? (labelText[item] || item) : '全部标签'}</option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs font-medium text-secondary-text">股票代码</span>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-secondary-text" />
            <input className="input-surface input-focus-glow h-10 w-full pl-9" value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="600519" />
          </div>
        </label>
        <button type="button" className="btn-primary inline-flex h-10 items-center gap-2 px-4" onClick={() => void loadData()}>
          <Filter className="h-4 w-4" />
          筛选
        </button>
      </div>

      {error ? <ApiErrorAlert error={error} /> : null}
      {buildResult ? (
        <div className="border border-cyan/40 bg-cyan/10 px-4 py-3 text-sm text-foreground">
          已重建 {buildResult.reviewCount ?? 0} 条复盘样本，扫描 {buildResult.traceCount ?? 0} 个 Trace，跳过 {buildResult.skipped ?? 0} 个，窗口 {(buildResult.evalWindows ?? []).join('/') || '7/30'} 日。
        </div>
      ) : null}

      <div className="grid gap-3 md:grid-cols-4">
        <MetricTile label="样本数" value={String(summary.total ?? data?.total ?? 0)} hint={data?.exists ? 'verdict_review.jsonl' : '文件未生成'} icon={<Database className="h-4 w-4" />} />
        <MetricTile label="已完成窗口" value={`${fmtPct(summary.completionRatePct)}`} hint={`${summary.completedCount ?? 0} completed`} icon={<Activity className="h-4 w-4" />} />
        <MetricTile label="平均后验收益" value={fmtPct(summary.avgFutureReturnPct)} hint="优先 30d / 7d 窗口" icon={<BarChart3 className="h-4 w-4" />} />
        <MetricTile label="链路覆盖" value={`${countValue(summary.chainCounts, 'stock_selection', 'stockSelection')}/${countValue(summary.chainCounts, 'single_stock_analysis', 'singleStockAnalysis')}`} hint="选股 / 单股" icon={<GitBranch className="h-4 w-4" />} />
      </div>

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <LabelDistribution counts={summary.labelCounts} />
        {rows.length ? (
          <ReviewTable rows={rows} />
        ) : (
          <EmptyState
            title="还没有可展示的复盘样本"
            description="先运行 scripts/build_agent_verdict_reviews.py 生成 data/agent_reviews/verdict_review.jsonl，或调整筛选条件。"
            icon={<Database className="h-7 w-7" />}
          />
        )}
      </div>
    </div>
  );
};

export default AgentVerdictReviewsPage;
