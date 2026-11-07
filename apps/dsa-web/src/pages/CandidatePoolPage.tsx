import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, BarChart3, Database, Layers3, ListFilter, RefreshCw, Search, ShieldCheck, Sparkles } from 'lucide-react';
import { candidatePoolApi, type CandidatePoolDetail, type CandidatePoolItem, type CandidatePoolRun, type FundamentalCandidateStatus } from '../api/candidatePool';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import type { CandidateDecisionTone } from '../components/candidates/CandidateDecisionTable';
import { CandidatePoolTable, type CandidatePoolRow } from '../components/candidates/CandidatePoolTable';
import { CandidatePoolDrawer } from '../components/candidates/CandidatePoolDrawer';
import { ApiErrorAlert, Badge, Collapsible, EmptyState, StatusDot } from '../components/common';
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
  fallback_seed_pool: '兜底种子池',
  unknown: '未知',
};

const candidateExpertLabels: Record<string, string> = {
  strategy_factor_expert: 'AlphaSift 策略多因子专家',
  technical_candidate_expert: 'Sequoia 技术形态专家',
  capital_flow_expert: '资金发现专家',
  fundamental_expert: '基本面发现专家',
  sector_theme_expert: '板块主题专家',
  news_event_expert: '消息事件专家',
  sentiment_theme_expert: '情绪/宏观专家',
};

const lifecycleLabels: Record<string, string> = {
  new: '新进入',
  active: '持续观察',
  watching: '观察中',
  decayed: '降权',
  removed: '移出',
};

const marketLabels: Record<string, string> = {
  cn: 'A 股',
  hk: '港股',
  us: '美股',
};

function labelFor(map: Record<string, string>, value?: string): string {
  const key = String(value || 'unknown');
  return map[key] || key;
}

function countEntries(counts?: Record<string, number>): Array<[string, number]> {
  return Object.entries(counts || {}).sort((a, b) => b[1] - a[1]);
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}

function formatMetric(value: unknown, suffix = ''): string {
  if (value == null || value === '') return '--';
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return num.toLocaleString(undefined, { maximumFractionDigits: 2 }) + suffix;
}

function displayCandidateExpertName(expert: string): string {
  return candidateExpertLabels[expert] || expert.replace(/_/g, ' ');
}

function displaySourceName(source: string): string {
  if (!source) return '未知';
  const [family, ...rest] = source.split(':');
  const familyLabel = labelFor(sourceLabels, family);
  if (!rest.length) return familyLabel;
  return familyLabel + ' · ' + rest.join(':');
}

function displayReasonText(reason: string): string {
  return reason
    .replaceAll('technical', '技术结构')
    .replaceAll('fundamental', '基本面')
    .replaceAll('capital', '资金')
    .replaceAll('sentiment', '情绪')
    .replaceAll('message', '消息');
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

function evidenceTone(dimension?: string): CandidateDecisionTone {
  if (dimension === 'fundamental') return 'success';
  if (dimension === 'capital') return 'warning';
  return 'info';
}

function isFallbackSource(source?: string): boolean {
  if (!source) return false;
  return source === 'fallback' || source.startsWith('fallback:') || source.includes('fallback');
}

function candidateActionFor(item: CandidatePoolItem): { key: string; label: string; tone: CandidateDecisionTone; strength?: string; reason: string } {
  const source = String(item.source || '');
  const recurrence = Number(item.recurrenceCount || 1);
  const lifecycle = String(item.lifecycleStatus || 'new');
  if (isFallbackSource(source)) {
    return {
      key: 'monitor',
      label: '观察跟踪',
      tone: 'warning',
      strength: '弱',
      reason: '兜底池仅用于补足链路，不代表真实推荐。',
    };
  }
  if (lifecycle === 'removed') {
    return {
      key: 'reject',
      label: '暂不纳入',
      tone: 'danger',
      strength: '无',
      reason: '该候选已从观察池移出。',
    };
  }
  if (lifecycle === 'decayed') {
    return {
      key: 'monitor',
      label: '观察跟踪',
      tone: 'warning',
      strength: '弱',
      reason: '候选已降权，需等待新的证据。',
    };
  }
  if ((item.recallSources || []).length > 1 || recurrence >= 3) {
    return {
      key: 'deep_dive',
      label: '进入深挖',
      tone: 'success',
      strength: '强',
      reason: '候选具备多源或多次出现证据，适合进入下一轮深度分析。',
    };
  }
  if (item.reasonDimensions?.length || recurrence >= 2) {
    return {
      key: 'monitor',
      label: '重点观察',
      tone: 'info',
      strength: '中',
      reason: '当前来源证据适合继续跟踪。',
    };
  }
  return {
    key: 'wait',
    label: '等待确认',
    tone: 'warning',
    strength: '弱',
    reason: '证据强度尚不够，先保持观察。',
  };
}

function candidateMetricHighlights(item: CandidatePoolItem): string[] {
  const metrics = item.metrics || {};
  const highlights = [
    ['换手率', formatMetric(metrics.turnover_rate, '%')],
    ['量比', formatMetric(metrics.volume_ratio)],
    ['PE', formatMetric(metrics.peTtm ?? metrics.pe_ttm)],
    ['PB', formatMetric(metrics.pb)],
  ]
    .filter(([, value]) => value !== '--')
    .map(([label, value]) => label + ' ' + value);
  if (isFundamentalCandidate(item)) {
    highlights.push(...fundamentalMetricPairs(item).map(([label, value]) => label + ' ' + value));
  }
  return Array.from(new Set(highlights)).slice(0, 8);
}

function candidateItemToRow(item: CandidatePoolItem): CandidatePoolRow {
  const action = candidateActionFor(item);
  const sources = unique([
    displaySourceName(item.source || ''),
    ...(item.recallSources || []).map(displaySourceName),
  ]).filter((value) => value !== '未知');
  const experts = unique((item.candidateExperts || []).map(displayCandidateExpertName));
  const dimensions = unique((item.candidateDimensions || ['unknown']).map((dimension) => labelFor(dimensionLabels, dimension)));
  const reason = displayReasonText(item.reason || '暂无入池理由');
  const evidence = (item.reasonDimensions || []).length
    ? (item.reasonDimensions || []).map((entry) => ({
      label: labelFor(dimensionLabels, entry.dimension),
      detail: displayReasonText(String(entry.detail || item.reason || '')),
      tone: evidenceTone(entry.dimension),
    }))
    : [{ label: '入池理由', detail: reason, tone: 'info' as CandidateDecisionTone }];
  const lifecycleKey = String(item.lifecycleStatus || 'new');
  const risks = [
    ...(isFallbackSource(item.source) ? ['兜底池，不代表真实推荐'] : []),
    ...(lifecycleKey === 'removed' ? ['已移出候选池'] : []),
    ...(lifecycleKey === 'decayed' ? ['已降权，需要新的证据确认'] : []),
  ];
  const badges: CandidatePoolRow['badges'] = [
    ...((item.recallSources || []).length > 1 ? [{ label: '多源共振', tone: 'success' as CandidateDecisionTone }] : []),
    ...(isFundamentalCandidate(item) ? [{ label: '基本面候选', tone: 'history' as CandidateDecisionTone }] : []),
  ];
  return {
    id: item.runId + '-' + item.code,
    code: item.code,
    name: item.name || item.code,
    market: marketLabels[item.market || ''] || item.market || 'A 股',
    score: item.signalScore,
    action,
    sources,
    experts,
    dimensions,
    reason,
    lifecycle: labelFor(lifecycleLabels, lifecycleKey),
    occurrence: '出现 ' + (item.recurrenceCount || 1) + ' 次',
    validUntil: item.validUntil,
    badges,
    metrics: candidateMetricHighlights(item),
    evidence,
    risks,
    note: '候选池页以日常跟踪为主，建议动作只表示下一步观察方向，不等同于最终买卖裁决。',
  };
}

const KpiCard: React.FC<{ label: string; value: React.ReactNode; foot: string; icon: React.ReactNode; tone?: 'default' | 'good' | 'warn' | 'bad' }> = ({
  label,
  value,
  foot,
  icon,
  tone = 'default',
}) => (
  <div className={cn(
    'rounded-xl border bg-card/70 px-4 py-3.5 shadow-sm shadow-black/5',
    tone === 'good' && 'border-success/25',
    tone === 'warn' && 'border-warning/25',
    tone === 'bad' && 'border-danger/25',
    tone === 'default' && 'border-border/60',
  )}>
    <div className="flex items-center justify-between gap-3">
      <span className="text-[10px] uppercase tracking-[0.12em] text-muted-text">{label}</span>
      <span className={cn(
        'inline-flex h-6 w-6 items-center justify-center rounded-md border',
        tone === 'good' && 'border-success/20 bg-success/10 text-success',
        tone === 'warn' && 'border-warning/20 bg-warning/10 text-warning',
        tone === 'bad' && 'border-danger/20 bg-danger/10 text-danger',
        tone === 'default' && 'border-border/60 bg-surface-2 text-secondary-text',
      )}>{icon}</span>
    </div>
    <div className="mt-2 flex items-baseline gap-2">
      <span className="text-2xl font-semibold tracking-tight text-foreground tabular-nums">{value}</span>
    </div>
    <div className="mt-1 text-[10px] text-muted-text">{foot}</div>
  </div>
);

const CountStrip: React.FC<{ title: string; counts?: Record<string, number>; labels?: Record<string, string> }> = ({ title, counts, labels }) => {
  const entries = countEntries(counts);
  return (
    <section className="rounded-xl border border-border/60 bg-card/60 p-3">
      <div className="mb-2 flex items-center justify-between gap-3">
        <h3 className="text-xs font-medium text-secondary-text">{title}</h3>
        <span className="text-[10px] text-muted-text">{entries.length} 类</span>
      </div>
      {entries.length ? (
        <div className="flex flex-wrap gap-1.5">
          {entries.map(([key, value]) => (
            <Badge key={key} variant="default">
              {labels ? labelFor(labels, key) : key}
              <span className="ml-1 font-mono text-muted-text">{value}</span>
            </Badge>
          ))}
        </div>
      ) : (
        <p className="text-xs text-muted-text">暂无分布数据</p>
      )}
    </section>
  );
};

const QualityItem: React.FC<{ label: string; value: React.ReactNode; foot?: string }> = ({ label, value, foot }) => (
  <div className="min-w-0 border-l border-border/50 pl-3 first:border-l-0 first:pl-0">
    <div className="text-[9px] uppercase tracking-[0.1em] text-muted-text">{label}</div>
    <div className="mt-1 truncate text-xs font-medium text-foreground">{value}</div>
    {foot ? <div className="mt-0.5 truncate text-[10px] text-muted-text">{foot}</div> : null}
  </div>
);

const CandidatePoolPage: React.FC = () => {
  const [detail, setDetail] = useState<CandidatePoolDetail | null>(null);
  const [runs, setRuns] = useState<CandidatePoolRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState('');
  const [activeDimension, setActiveDimension] = useState('all');
  const [activeView, setActiveView] = useState<'all' | 'multi' | 'strong' | 'risk'>('all');
  const [query, setQuery] = useState('');
  const [selectedRow, setSelectedRow] = useState<CandidatePoolRow | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  useEffect(() => {
    document.title = '候选池 - StockAnalyser';
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
      setSelectedRow(null);
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
      setSelectedRow(null);
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

  const items = useMemo(() => detail?.items || [], [detail?.items]);
  const summary = detail?.summary || {};
  const fundamentalStatus: FundamentalCandidateStatus = detail?.fundamentalStatus || {};
  const fundamentalTone = fundamentalStatusTone(fundamentalStatus.status);
  const hardStrategyMissing = Boolean(summary.hardStrategyTrunkMissing);
  const fallbackCount = Number(summary.fallbackCount || 0);
  const hardExclusionCount = Number(summary.hardExclusionCount || 0);

  const dimensionOptions = useMemo(() => {
    const counts = detail?.summary.dimensionCounts || {};
    return ['all', ...Object.keys(counts)];
  }, [detail?.summary.dimensionCounts]);

  const viewCounts = useMemo(() => ({
    all: items.length,
    multi: items.filter((item) => (item.recallSources || []).length > 1).length,
    strong: items.filter((item) => Number(item.signalScore || 0) >= 80).length,
    risk: items.filter((item) => (
      isFallbackSource(item.source)
      || item.lifecycleStatus === 'removed'
      || item.lifecycleStatus === 'decayed'
    )).length,
  }), [items]);

  const filteredItems = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return items.filter((item) => {
      if (activeDimension !== 'all' && !(item.candidateDimensions || []).includes(activeDimension)) {
        return false;
      }
      if (activeView === 'multi' && (item.recallSources || []).length <= 1) return false;
      if (activeView === 'strong' && Number(item.signalScore || 0) < 80) return false;
      if (activeView === 'risk') {
        const risky = isFallbackSource(item.source)
          || item.lifecycleStatus === 'removed'
          || item.lifecycleStatus === 'decayed';
        if (!risky) return false;
      }
      if (!normalizedQuery) return true;
      const haystack = [
        item.code,
        item.name,
        item.reason,
        item.source,
        ...(item.candidateExperts || []),
        ...(item.recallSources || []),
        ...(item.candidateDimensions || []),
      ].join(' ').toLowerCase();
      return haystack.includes(normalizedQuery);
    });
  }, [activeDimension, activeView, items, query]);

  const rows = useMemo(() => filteredItems.map(candidateItemToRow), [filteredItems]);
  const selectedId = selectedRow?.id;

  const openTrace = selectedRow && detail?.run?.sessionId
    ? () => {
      window.location.href = '/agent-trace/' + encodeURIComponent(detail.run?.sessionId || '');
    }
    : undefined;

  const tabItems: Array<{ key: 'all' | 'multi' | 'strong' | 'risk'; label: string }> = [
    { key: 'all', label: '全部' },
    { key: 'multi', label: '多源共振' },
    { key: 'strong', label: '强信号' },
    { key: 'risk', label: '风险' },
  ];

  const toolbar = (
    <div className="flex flex-col gap-2 xl:flex-row xl:items-center">
      <div className="inline-flex w-fit items-center gap-0.5 rounded-lg border border-border/60 bg-surface-2/40 p-0.5">
        {tabItems.map((tab) => (
          <button
            key={tab.key}
            type="button"
            onClick={() => setActiveView(tab.key)}
            className={cn(
              'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-[11px] transition',
              activeView === tab.key
                ? 'bg-card text-foreground shadow-sm'
                : 'text-muted-text hover:text-secondary-text',
            )}
          >
            {tab.label}
            <span className={cn('font-mono text-[10px]', activeView === tab.key ? 'text-cyan' : 'text-muted-text')}>{viewCounts[tab.key]}</span>
          </button>
        ))}
      </div>
      <div className="relative min-w-[190px] flex-1 xl:max-w-[280px]">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-text" />
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索代码 / 名称 / 来源 / 理由"
          aria-label="搜索候选"
          className="h-8 w-full rounded-md border border-border/60 bg-surface-2/40 pl-8 pr-2 text-xs text-foreground outline-none transition placeholder:text-muted-text focus:border-cyan/40"
        />
      </div>
      <select
        aria-label="筛选维度"
        value={activeDimension}
        onChange={(event) => setActiveDimension(event.target.value)}
        className="h-8 rounded-md border border-border/60 bg-surface-2/40 px-2 text-[11px] text-secondary-text outline-none focus:border-cyan/40"
      >
        {dimensionOptions.map((dimension) => (
          <option key={dimension} value={dimension}>
            {dimension === 'all' ? '全部维度' : labelFor(dimensionLabels, dimension)}
          </option>
        ))}
      </select>
      <div className="text-[10px] text-muted-text xl:ml-auto">
        显示 <span className="font-mono text-secondary-text">{rows.length}</span> / {items.length}
      </div>
    </div>
  );

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <header className="flex flex-col gap-4 border-b border-border/60 pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-[0.16em] text-cyan">
            <ListFilter className="h-3.5 w-3.5" />
            Agent L1 / Candidate Pool
          </div>
          <h1 className="mt-1.5 text-2xl font-semibold tracking-tight text-foreground">候选池</h1>
          <p className="mt-1.5 max-w-3xl text-xs leading-5 text-secondary-text">
            先看动作建议、信号评分和来源证据，再点开候选查看证据拆解、指标与风险；评分只用于池内排序，不等同于买卖裁决。
          </p>
          {detail?.run ? (
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-muted-text">
              <span>{detail.run.createdAt}</span>
              <span>{detail.run.candidateCount} 只候选</span>
              <span>{detail.run.candidateSource || '--'}</span>
              <span className="inline-flex items-center gap-1"><StatusDot tone={detail.run.status === 'ok' ? 'success' : 'warning'} />{detail.run.status || '--'}</span>
            </div>
          ) : null}
        </div>
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <label className="sr-only" htmlFor="candidate-run-select">选择候选池运行</label>
          <select
            id="candidate-run-select"
            aria-label="选择候选池运行"
            className="h-9 min-w-[230px] rounded-md border border-border/70 bg-surface-2/60 px-2.5 text-xs text-foreground outline-none focus:border-cyan/40"
            value={selectedRunId}
            onChange={(event) => void loadRun(event.target.value)}
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
            className="inline-flex h-9 items-center justify-center gap-1.5 rounded-md border border-border/70 bg-surface-2 px-3 text-xs font-medium text-secondary-text transition hover:bg-hover hover:text-foreground disabled:cursor-not-allowed disabled:opacity-60"
            onClick={() => void loadLatest()}
            disabled={isLoading}
          >
            <RefreshCw className={cn('h-3.5 w-3.5', isLoading && 'animate-spin')} />
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
            <KpiCard label="当前候选" value={summary.candidateCount ?? items.length} foot={'本轮 ' + (detail?.run?.candidateCount ?? items.length) + ' 只 · 去重后展示'} icon={<Layers3 className="h-3.5 w-3.5" />} />
            <KpiCard label="硬策略主干" value={hardStrategyMissing ? '缺失' : '可用'} tone={hardStrategyMissing ? 'bad' : 'good'} foot={hardStrategyMissing ? '需要检查 AlphaSift 策略源' : '策略源已参与本轮召回'} icon={<ShieldCheck className="h-3.5 w-3.5" />} />
            <KpiCard label="多源共振" value={summary.multiSourceCount ?? 0} tone="good" foot="两个以上来源同时命中" icon={<Sparkles className="h-3.5 w-3.5" />} />
            <KpiCard label="兜底 / 硬排除" value={String(fallbackCount) + ' / ' + String(hardExclusionCount)} tone={fallbackCount > 0 || hardExclusionCount > 0 ? 'warn' : 'default'} foot="兜底不代表真实推荐" icon={<AlertTriangle className="h-3.5 w-3.5" />} />
          </section>

          <section className="grid gap-3 rounded-xl border border-border/60 bg-card/50 px-4 py-3 md:grid-cols-4">
            <QualityItem label="运行状态" value={detail?.run?.status || '--'} foot={detail?.run?.market ? '市场 ' + (marketLabels[detail.run.market] || detail.run.market) : undefined} />
            <QualityItem
              label="基本面发现"
              value={<span className={cn('inline-flex items-center gap-1.5', fundamentalTone === 'bad' ? 'text-danger' : fundamentalTone === 'warn' ? 'text-warning' : 'text-success')}><StatusDot tone={fundamentalTone === 'bad' ? 'danger' : fundamentalTone === 'warn' ? 'warning' : 'success'} />{fundamentalStatus.status || '--'}</span>}
              foot={(fundamentalStatus.candidateCount ?? 0) + ' 只 · 快照 ' + (fundamentalStatus.rowCount ?? '--') + ' 行'}
            />
            <QualityItem label="候选来源" value={detail?.run?.candidateSource ? displaySourceName(detail.run.candidateSource) : '--'} foot={detail?.run?.fallbackUsed ? '使用了兜底池' : '未使用兜底池'} />
            <QualityItem label="运行说明" value={detail?.run?.note || '--'} foot={fundamentalStatus.latestPeriod ? '报告期 ' + fundamentalStatus.latestPeriod : undefined} />
          </section>

          <CandidatePoolTable
            rows={rows}
            selectedId={selectedId}
            onSelect={setSelectedRow}
            toolbar={toolbar}
          />

          <Collapsible title="分布概览" defaultOpen={false} icon={<BarChart3 className="h-4 w-4" />}>
            <div className="grid gap-3 xl:grid-cols-3">
              <CountStrip title="维度分布" counts={summary.dimensionCounts} labels={dimensionLabels} />
              <CountStrip title="来源分布" counts={summary.sourceCounts} labels={sourceLabels} />
              <CountStrip title="生命周期" counts={summary.lifecycleCounts} labels={lifecycleLabels} />
            </div>
          </Collapsible>

          <Collapsible title="数据诊断" defaultOpen={false} icon={<Database className="h-4 w-4" />}>
            <div className="grid gap-3 text-xs md:grid-cols-2">
              <div className="rounded-lg border border-border/60 bg-surface-2/30 p-3">
                <div className="mb-2 text-[10px] uppercase tracking-[0.1em] text-muted-text">基本面快照</div>
                <div className="space-y-1.5 text-secondary-text">
                  <div className="flex justify-between gap-3"><span>状态</span><span>{fundamentalStatus.status || '--'}</span></div>
                  <div className="flex justify-between gap-3"><span>候选</span><span>{fundamentalStatus.candidateCount ?? '--'}</span></div>
                  <div className="flex justify-between gap-3"><span>快照行数</span><span>{fundamentalStatus.rowCount ?? '--'}</span></div>
                  <div className="flex justify-between gap-3"><span>报告期</span><span>{fundamentalStatus.latestPeriod || '--'}</span></div>
                  <div className="flex justify-between gap-3"><span>数据表</span><span className="max-w-[60%] break-all text-right">{fundamentalStatus.table || '--'}</span></div>
                </div>
              </div>
              <div className="rounded-lg border border-border/60 bg-surface-2/30 p-3">
                <div className="mb-2 text-[10px] uppercase tracking-[0.1em] text-muted-text">诊断与限制</div>
                <div className="space-y-1.5 leading-5 text-secondary-text">
                  {[fundamentalStatus.dbPath, ...(fundamentalStatus.errors || []), ...(fundamentalStatus.warnings || [])].filter(Boolean).length ? (
                    [fundamentalStatus.dbPath, ...(fundamentalStatus.errors || []), ...(fundamentalStatus.warnings || [])].filter(Boolean).map((message, index) => (
                      <div key={String(message) + index} className="break-all">{String(message)}</div>
                    ))
                  ) : (
                    <div>基本面专家已参与本轮候选发现，未返回诊断错误。</div>
                  )}
                </div>
              </div>
            </div>
          </Collapsible>
        </>
      )}

      <CandidatePoolDrawer row={selectedRow} onClose={() => setSelectedRow(null)} onOpenTrace={openTrace} />
    </div>
  );
};

export default CandidatePoolPage;
