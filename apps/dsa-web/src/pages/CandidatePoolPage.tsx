import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, BarChart3, Database, Layers3, ListFilter, RefreshCw, ShieldCheck, Sparkles } from 'lucide-react';
import { candidatePoolApi, type CandidatePoolDetail, type CandidatePoolItem, type CandidatePoolRun } from '../api/candidatePool';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { CandidateDecisionTable, type CandidateDecisionEvidence, type CandidateDecisionRow, type CandidateDecisionTone } from '../components/candidates/CandidateDecisionTable';
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

function displayCandidateExpertName(expert: string): string {
  return candidateExpertLabels[expert] || expert.replace(/_/g, ' ');
}

function displaySourceName(source: string): string {
  if (!source) return '未知';
  const [family, ...rest] = source.split(':');
  const familyLabel = labelFor(sourceLabels, family);
  if (!rest.length) return familyLabel;
  return `${familyLabel} · ${rest.join(':')}`;
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

function candidateActionFor(item: CandidatePoolItem): { key: string; label: string; tone: CandidateDecisionTone; strength?: string; reason: string } {
  const source = String(item.source || '');
  const recurrence = Number(item.recurrenceCount || 1);
  const lifecycle = String(item.lifecycleStatus || 'new');
  if (source === 'fallback' || source.startsWith('fallback:')) {
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
    .map(([label, value]) => `${label} ${value}`);
  if (isFundamentalCandidate(item)) {
    highlights.push(...fundamentalMetricPairs(item).map(([label, value]) => `${label} ${value}`));
  }
  return Array.from(new Set(highlights)).slice(0, 8);
}

function candidateItemToDecisionRow(item: CandidatePoolItem): CandidateDecisionRow {
  const action = candidateActionFor(item);
  const sourceLabelsList = [displaySourceName(item.source || '')]
    .concat((item.recallSources || []).map(displaySourceName))
    .filter((value, index, arr) => value && arr.indexOf(value) === index);
  const reasonDimensions = item.reasonDimensions || [];
  const evidenceTone = (dimension?: string): CandidateDecisionTone => {
    if (dimension === 'fundamental') return 'success';
    if (dimension === 'capital') return 'warning';
    return 'info';
  };
  const evidence: CandidateDecisionEvidence[] = reasonDimensions.length ? reasonDimensions.map((entry) => ({
    label: labelFor(dimensionLabels, entry.dimension),
    detail: displayReasonText(String(entry.detail || item.reason || '')),
    tone: evidenceTone(entry.dimension),
  })) : (item.reason ? [{ label: '入池理由', detail: displayReasonText(item.reason), tone: 'info' }] : []);
  return {
    id: `${item.runId}-${item.code}`,
    code: item.code,
    name: item.name,
    score: undefined,
    scoreLabel: undefined,
    scoreNote: undefined,
    action,
    primaryReason: displayReasonText(item.reason || '暂无入池理由'),
    dimensionLabels: (item.candidateDimensions || ['unknown']).map((dimension) => labelFor(dimensionLabels, dimension)),
    expertLabels: (item.candidateExperts || []).map(displayCandidateExpertName),
    sourceLabels: sourceLabelsList,
    lifecycleLabel: labelFor(lifecycleLabels, item.lifecycleStatus || 'new'),
    occurrenceLabel: `出现 ${item.recurrenceCount || 1} 次`,
    dateLabel: item.validUntil ? `有效至 ${item.validUntil}` : undefined,
    badges: [
      ...(item.recallSources && item.recallSources.length > 1 ? [{ label: '多源共振', tone: 'success' as CandidateDecisionTone }] : []),
      ...(isFundamentalCandidate(item) ? [{ label: '基本面候选', tone: 'history' as CandidateDecisionTone }] : []),
    ],
    evidence,
    metricHighlights: candidateMetricHighlights(item),
    riskFlags: [
      ...(item.source === 'fallback' || item.source?.startsWith('fallback:') ? ['兜底池，不代表真实推荐'] : []),
      ...(item.lifecycleStatus === 'removed' ? ['已移出候选池'] : []),
      ...(item.lifecycleStatus === 'decayed' ? ['已降权，需要新的证据确认'] : []),
    ],
    detailNote: '候选池页以日常跟踪为主，建议动作只表示下一步观察方向，不等同于最终买卖裁决。',
  };
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
  const decisionRows = useMemo(() => (
    visibleItems.map(candidateItemToDecisionRow)
  ), [visibleItems]);

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
            面向日常看盘的候选入池榜：先看后续动作、来源证据和核心依据，再展开查看专家来源、证据拆解和风险。
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

          <CandidateDecisionTable
            title="候选入池榜"
            description="按动作建议和证据质量展示；seed pool 召回分只作为来源内诊断，不做跨来源评分比较。"
            items={decisionRows}
            scoreColumnLabel="评估口径"
            emptyTitle="当前筛选下没有候选"
            emptyDescription="切换到全部维度可查看本轮完整候选池。"
          />

          <Collapsible title="筛选维度" defaultOpen={false} icon={<ListFilter className="h-4 w-4" />}>
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
          </Collapsible>
        </>
      )}
    </div>
  );
};

export default CandidatePoolPage;
