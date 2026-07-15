import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CalendarDays, Clock, GitBranch, Layers3, Network, Newspaper, RefreshCw, Search, ThumbsUp, VolumeX } from 'lucide-react';
import { newsSignalsApi, type NewsSignalCard, type NewsSignalGraph, type NewsSignalListResponse, type NewsSignalMetrics, type NewsSignalRebuildResult } from '../api/newsSignals';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, EmptyState } from '../components/common';
import { cn } from '../utils/cn';

type BadgeVariant = React.ComponentProps<typeof Badge>['variant'];

const horizonLabels: Record<string, string> = {
  short: '短期',
  medium: '中期',
  long: '长期',
};

const toneLabels: Record<string, string> = {
  positive: '积极',
  negative: '消极',
  neutral: '中性',
  mixed: '混合',
  unknown: '未知',
};

const statusLabels: Record<string, string> = {
  active: '有效',
  suppressed: '已降权',
  pending: '待处理',
  low_quality: '低质量',
};

const layerLabels: Record<string, string> = {
  industry: '产业层',
  company: '公司层',
  macro: '宏观层',
};

const edgeClassLabels: Record<string, string> = {
  typed_relation: '业务关系',
  event_clue: '事件线索',
  semantic_similarity: '语义相似',
};

const edgeQualityLabels: Record<string, string> = {
  high: '强边',
  medium: '中边',
  low: '弱边',
};

const edgeTypeLabels: Record<string, string> = {
  same_event: '同一事件演化',
  same_company: '同公司关联',
  same_theme: '同主题关联',
  same_industry: '同产业关联',
  impacts_industry: '影响产业',
  impacts_company: '影响公司',
  benefits_company: '潜在受益公司',
  harms_company: '潜在受损公司',
  semantic_similarity: '语义相似待核验',
};

function fmtNum(value?: number | null, digits = 1): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

function fmtDateTime(value?: string | null): string {
  if (!value) return '--';
  return value.replace('T', ' ').slice(0, 16);
}

function compactText(value?: string | null, limit = 240): string {
  const text = String(value || '').replace(/\s+/g, ' ').trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, Math.max(0, limit - 1)).trim()}...`;
}

function toneVariant(value?: string): BadgeVariant {
  if (value === 'positive') return 'success';
  if (value === 'negative') return 'danger';
  if (value === 'mixed') return 'warning';
  return 'default';
}

function horizonVariant(value?: string): BadgeVariant {
  if (value === 'long') return 'history';
  if (value === 'medium') return 'info';
  return 'default';
}

function mappingVariant(value?: string): BadgeVariant {
  if (value === 'mapped') return 'success';
  if (value === 'ambiguous') return 'warning';
  if (value === 'unmapped') return 'danger';
  return 'default';
}

function layerVariant(value?: string): BadgeVariant {
  if (value === 'macro') return 'history';
  if (value === 'company') return 'info';
  return 'default';
}

function qualityVariant(value?: string): BadgeVariant {
  if (value === 'high') return 'success';
  if (value === 'medium') return 'info';
  if (value === 'low') return 'warning';
  return 'default';
}

const MetricTile: React.FC<{ label: string; value: string; icon: React.ReactNode; hint?: string }> = ({ label, value, icon, hint }) => (
  <div className="min-h-[96px] border border-border/70 bg-card/70 px-4 py-3">
    <div className="flex items-center justify-between gap-3 text-secondary-text">
      <span className="text-xs font-medium uppercase tracking-[0.08em]">{label}</span>
      {icon}
    </div>
    <div className="mt-3 text-2xl font-semibold tabular-nums text-foreground">{value}</div>
    {hint ? <div className="mt-1 text-xs text-secondary-text">{hint}</div> : null}
  </div>
);

const FeedbackButton: React.FC<{
  active: boolean;
  disabled?: boolean;
  tone: 'success' | 'danger' | 'warning';
  icon: React.ReactNode;
  children: React.ReactNode;
  onClick: () => void;
}> = ({ active, disabled, tone, icon, children, onClick }) => {
  const activeClass = {
    success: 'border-success/40 bg-success/12 text-success shadow-[0_0_0_1px_rgba(34,197,94,0.18)]',
    danger: 'border-danger/40 bg-danger/12 text-danger shadow-[0_0_0_1px_rgba(239,68,68,0.18)]',
    warning: 'border-warning/40 bg-warning/12 text-warning shadow-[0_0_0_1px_rgba(245,158,11,0.18)]',
  }[tone];

  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'inline-flex h-10 items-center gap-2 border px-3 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60',
        active ? activeClass : 'border-border/70 bg-elevated text-secondary-text hover:bg-hover/70 hover:text-foreground',
      )}
    >
      {icon}
      <span>{children}</span>
    </button>
  );
};

const SignalCardRow: React.FC<{ item: NewsSignalCard; selected: boolean; onSelect: () => void }> = ({ item, selected, onSelect }) => {
  const feedback = item.feedbackCounts || {};
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        'block w-full border px-4 py-3 text-left transition-colors',
        selected ? 'border-cyan/50 bg-cyan/8' : 'border-border/70 bg-card/70 hover:bg-hover/70',
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={toneVariant(item.marketImpact || item.newsTone)}>{toneLabels[item.marketImpact || item.newsTone || 'unknown'] || item.marketImpact || item.newsTone || '--'}</Badge>
            <Badge variant={layerVariant(item.signalLayer)}>{layerLabels[item.signalLayer || ''] || item.signalLayer || '产业层'}</Badge>
            <Badge variant={horizonVariant(item.impactHorizon)}>{horizonLabels[item.impactHorizon || ''] || item.impactHorizon || '--'}</Badge>
            <Badge variant={mappingVariant(item.mappingStatus)}>{item.mappingStatus || '--'}</Badge>
            {item.status && item.status !== 'active' ? <Badge variant="warning">{statusLabels[item.status] || item.status}</Badge> : null}
          </div>
          <div className="mt-2 line-clamp-2 text-sm font-medium leading-6 text-foreground">{item.summaryShort || '--'}</div>
        </div>
        <div className="w-[62px] shrink-0 text-right">
          <div className="text-lg font-semibold tabular-nums text-foreground">{fmtNum(item.adjustedSignalScore ?? item.signalScore)}</div>
          <div className="text-xxs text-secondary-text">score</div>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs text-secondary-text">
        <span className="inline-flex items-center gap-1"><CalendarDays className="h-3.5 w-3.5" />{item.signalDate || '--'} {item.session || ''}</span>
        <span className="inline-flex items-center gap-1"><Clock className="h-3.5 w-3.5" />{fmtDateTime(item.validUntil)}</span>
        {(item.primaryIndustries || []).slice(0, 3).map((industry) => (
          <span key={industry} className="border border-border/60 bg-elevated px-2 py-0.5 text-xs text-secondary-text">{industry}</span>
        ))}
        {feedback.wrong || feedback.noisy || feedback.duplicate ? (
          <span className="text-warning">反馈 {Number(feedback.wrong || 0) + Number(feedback.noisy || 0) + Number(feedback.duplicate || 0)}</span>
        ) : null}
      </div>
    </button>
  );
};

const SignalDetail: React.FC<{
  item: NewsSignalCard | null;
  graph: NewsSignalGraph | null;
  graphLoading: boolean;
  onFeedback: (type: string) => void;
  feedbacking: boolean;
}> = ({ item, graph, graphLoading, onFeedback, feedbacking }) => {
  if (!item) {
    return (
      <div className="border border-border/70 bg-card/60 p-6">
        <EmptyState title="暂无选中卡片" description="左侧列表为空或尚未加载。" icon={<Newspaper className="h-8 w-8" />} />
      </div>
    );
  }

  const feedbackCounts = item.feedbackCounts || {};
  const usefulCount = Number(feedbackCounts.useful || 0);
  const wrongCount = Number(feedbackCounts.wrong || 0);
  const noisyCount = Number(feedbackCounts.noisy || 0);
  const graphNodeById = new Map((graph?.nodes || []).map((node) => [node.id, node]));

  return (
    <div className="border border-border/70 bg-card/60">
      <div className="border-b border-border/70 px-5 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={toneVariant(item.marketImpact || item.newsTone)}>{toneLabels[item.marketImpact || item.newsTone || 'unknown'] || item.marketImpact || item.newsTone || '--'}</Badge>
          <Badge variant={layerVariant(item.signalLayer)}>{layerLabels[item.signalLayer || ''] || item.signalLayer || '产业层'}</Badge>
          <Badge variant={horizonVariant(item.impactHorizon)}>{horizonLabels[item.impactHorizon || ''] || item.impactHorizon || '--'}</Badge>
          <Badge variant={mappingVariant(item.mappingStatus)}>{item.mappingStatus || '--'}</Badge>
          <Badge variant="default">{item.evidenceGrade || '--'} / {item.inferenceLevel || '--'}</Badge>
        </div>
        <h2 className="mt-3 text-lg font-semibold leading-7 text-foreground">{item.summaryShort || '--'}</h2>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-secondary-text">
          <span>{item.signalDate || '--'} {item.session || ''}</span>
          <span>有效至 {fmtDateTime(item.validUntil)}</span>
          <span>刷新：{item.refreshTrigger || '--'}</span>
        </div>
      </div>

      <div className="grid gap-5 p-5">
        <section>
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
            <Layers3 className="h-4 w-4 text-cyan" />
            产业影响
          </div>
          <div className="space-y-2">
            {(item.industryImpacts || []).map((impact, index) => (
              <div key={`${impact.industry}-${index}`} className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-foreground">{impact.industry || '--'}</span>
                  <Badge variant={toneVariant(impact.direction === 'benefit' ? 'positive' : impact.direction === 'harm' ? 'negative' : 'neutral')}>{impact.direction || '--'}</Badge>
                  <span className="text-xs text-secondary-text">{impact.strength || '--'}</span>
                </div>
                <p className="mt-1 text-xs leading-5 text-secondary-text">{impact.rationale || '--'}</p>
              </div>
            ))}
          </div>
        </section>

        <section>
          <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
            <GitBranch className="h-4 w-4 text-cyan" />
            公司映射
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {(item.companyImpacts || []).slice(0, 8).map((impact, index) => (
              <div key={`${impact.symbol}-${impact.name}-${index}`} className="min-h-[86px] border border-border/60 bg-elevated/50 px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <div className="min-w-0">
                    <div className="truncate text-sm font-medium text-foreground">{impact.name || impact.symbol || '--'}</div>
                    <div className="text-xs text-secondary-text">{impact.symbol || '--'}</div>
                  </div>
                  <Badge variant={mappingVariant(impact.mappingStatus)}>{fmtNum((impact.confidence || 0) * 100, 0)}%</Badge>
                </div>
                <p className="mt-2 line-clamp-2 text-xs leading-5 text-secondary-text">{impact.rationale || impact.role || '--'}</p>
              </div>
            ))}
            {!(item.companyImpacts || []).length ? <div className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm text-secondary-text">仅产业级证据</div> : null}
          </div>
        </section>

        <section>
          <div className="mb-2 text-sm font-semibold text-foreground">事件事实</div>
          <div className="space-y-2">
            {(item.extractedEvents || []).slice(0, 5).map((event, index) => (
              <div key={event.eventId || `${event.rawEpisodeId}-${index}`} className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={event.verificationStatus === 'source_verified' ? 'success' : 'warning'}>
                    {event.verificationStatus || 'source_only'}
                  </Badge>
                  <span className="font-medium text-foreground">[{event.eventType || '--'}]</span>
                  <span className="text-xs tabular-nums text-secondary-text">置信 {fmtNum((event.confidence || 0) * 100, 0)}%</span>
                  <span className="text-xs text-secondary-text">{event.extractor || '--'}</span>
                </div>
                <div className="mt-1 text-xs leading-5 text-secondary-text">
                  {event.subject || '--'} {event.trigger ? `· ${event.trigger}` : ''} {event.object ? `-> ${event.object}` : ''}
                  {event.metricValue ? <span className="tabular-nums"> · {event.metricValue}</span> : null}
                </div>
                {event.evidenceSentence ? <p className="mt-1 text-xs leading-5 text-foreground">{event.evidenceSentence}</p> : null}
                {(event.entityLinks || []).length ? (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {(event.entityLinks || []).slice(0, 6).map((link, linkIndex) => (
                      <span key={`${String(link.name || link.symbol || linkIndex)}`} className="border border-border/60 bg-card/70 px-2 py-0.5 text-xs text-secondary-text">
                        {String(link.name || link.symbol || '--')}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            ))}
            {!(item.extractedEvents || []).length ? <div className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm text-secondary-text">事件事实待抽取</div> : null}
          </div>
        </section>

        <section>
          <div className="mb-2 text-sm font-semibold text-foreground">传导路径</div>
          <div className="space-y-2">
            {(item.transmissionPaths || []).map((path, index) => (
              <div key={`${path.source}-${index}`} className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm text-secondary-text">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={path.eventCategory === '供应链/替代' ? 'warning' : path.eventCategory === '政策/宏观' ? 'history' : 'info'}>
                    {path.eventCategory || '消息催化'}
                  </Badge>
                  <span className="font-medium text-foreground">{path.source || '--'} {'->'} {path.target || '--'}</span>
                  {path.eventScore != null ? <span className="text-xs tabular-nums">事件得分 {fmtNum(path.eventScore, 1)}</span> : null}
                </div>
                {(path.chainSteps || []).length ? (
                  <div className="mt-2 space-y-1">
                    {(path.chainSteps || []).slice(0, 4).map((step, stepIndex) => (
                      <div key={`${step.label}-${stepIndex}`} className="text-xs leading-5">
                        <span className="font-medium text-foreground">[{step.label || '--'}]</span>{' '}
                        <span>{step.text || '--'}</span>
                        {step.score != null ? <span className="tabular-nums"> {'->'} {fmtNum(step.score, 1)}分</span> : null}
                      </div>
                    ))}
                  </div>
                ) : (
                  <div className="mt-1 text-xs leading-5">{path.mechanism || '--'}</div>
                )}
                {path.conclusion ? <p className="mt-2 text-xs leading-5 text-foreground">{path.conclusion}</p> : null}
              </div>
            ))}
          </div>
        </section>

        <section>
          <div className="mb-2 flex items-center justify-between gap-3">
            <div className="text-sm font-semibold text-foreground">入库质量</div>
            <div className="text-xs text-secondary-text">{(item.rawEpisodes || []).length} 个来源</div>
          </div>
          <div className="space-y-2">
            {(item.rawEpisodes || []).map((episode) => (
              <div key={`${episode.episodeId}-quality`} className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant={qualityVariant(episode.qualityGrade)}>{episode.qualityGrade || 'unknown'}</Badge>
                  <span className="font-medium tabular-nums text-foreground">{fmtNum(episode.qualityScore, 0)} 分</span>
                  {episode.status === 'low_quality' ? <Badge variant="warning">低质量入库</Badge> : null}
                </div>
                {(episode.qualityFlags || []).length ? (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {(episode.qualityFlags || []).slice(0, 6).map((flag) => (
                      <span key={flag} className="border border-border/60 bg-card/70 px-2 py-0.5 text-xs text-secondary-text">{flag}</span>
                    ))}
                  </div>
                ) : null}
                {episode.normalizedContent ? (
                  <p className="mt-2 text-xs leading-5 text-secondary-text">{compactText(episode.normalizedContent, 260)}</p>
                ) : null}
              </div>
            ))}
            {!(item.rawEpisodes || []).length ? <div className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm text-secondary-text">来源质量待展开</div> : null}
          </div>
        </section>

        <section>
          <div className="mb-2 text-sm font-semibold text-foreground">来源</div>
          <div className="space-y-2">
            {(item.rawEpisodes || []).map((episode) => (
              <a
                key={episode.episodeId}
                href={episode.url || undefined}
                target="_blank"
                rel="noreferrer"
                className="block border border-border/60 bg-elevated/50 px-3 py-2 text-sm hover:bg-hover/60"
              >
                <div className="font-medium text-foreground">{episode.title || '--'}</div>
                <div className="mt-1 text-xs text-secondary-text">{episode.source || '--'} · {fmtDateTime(episode.publishedAt)}</div>
              </a>
            ))}
            {!(item.rawEpisodes || []).length ? <div className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm text-secondary-text">来源待展开</div> : null}
          </div>
        </section>

        <section>
          <div className="mb-2 flex items-center justify-between gap-3">
            <div className="text-sm font-semibold text-foreground">事件线索</div>
            <div className="text-xs text-secondary-text">
              {graphLoading ? '加载中...' : `${graph?.summary?.edgeCount ?? graph?.edges?.length ?? 0} 条边`}
            </div>
          </div>
          <div className="space-y-2">
            {(graph?.edges || []).slice(0, 8).map((edge) => {
              const relatedId = edge.relatedCardId || edge.targetCardId || edge.targetId || '';
              const relatedNode = graphNodeById.get(relatedId);
              const relatedLabel = edge.relatedLabel || edge.targetLabel || relatedNode?.label || (edge.targetType === 'card' ? '关联新闻' : edge.targetId) || '--';
              const relatedDate = edge.relatedSignalDate || edge.targetSignalDate || relatedNode?.signalDate;
              const paths = edge.relatedTransmissionPaths || edge.targetTransmissionPaths || relatedNode?.transmissionPaths || [];
              return (
                <div key={edge.edgeId || `${edge.sourceCardId}-${edge.targetId}-${edge.edgeType}`} className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={edge.edgeClass === 'semantic_similarity' ? 'info' : edge.edgeClass === 'event_clue' ? 'history' : 'default'}>
                      {edgeClassLabels[edge.edgeClass || ''] || edge.edgeClass || '--'}
                    </Badge>
                    <Badge variant={qualityVariant(edge.qualityGrade)}>{edgeQualityLabels[edge.qualityGrade || ''] || edge.qualityGrade || '--'}</Badge>
                    <span className="font-medium text-foreground">{edgeTypeLabels[edge.edgeType || ''] || edge.edgeType || '--'}</span>
                    <span className="text-xs tabular-nums text-secondary-text">
                      质量 {fmtNum(edge.edgeQuality, 0)} / 权重 {fmtNum((edge.weight || 0) * 100, 0)}%
                    </span>
                  </div>
                  <div className="mt-2 font-medium leading-6 text-foreground">{relatedLabel}</div>
                  {relatedDate ? <div className="mt-0.5 text-xs text-secondary-text">{relatedDate}</div> : null}
                  {paths.slice(0, 2).map((path, index) => (
                    <div key={`${edge.edgeId || relatedId}-path-${index}`} className="mt-2 border-l-2 border-cyan/40 pl-3 text-xs leading-5 text-secondary-text">
                      <div className="font-medium text-foreground">{path.eventCategory || '传导路径'}</div>
                      <div>{[path.mechanism, path.target].filter(Boolean).join(' -> ') || path.conclusion || path.rationale || '传导信息待补充'}</div>
                      {path.conclusion && (path.mechanism || path.target) ? <div className="mt-0.5">结论：{path.conclusion}</div> : null}
                    </div>
                  ))}
                  {edge.rationale ? <p className="mt-1 text-xs leading-5 text-secondary-text">关系说明：{edge.rationale}</p> : null}
                  {(edge.qualityFlags || []).length ? (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {(edge.qualityFlags || []).slice(0, 4).map((flag) => (
                        <span key={flag} className="border border-border/60 bg-card/70 px-2 py-0.5 text-xs text-secondary-text">{flag}</span>
                      ))}
                    </div>
                  ) : null}
                </div>
              );
            })}
            {!graphLoading && !(graph?.edges || []).length ? (
              <div className="border border-border/60 bg-elevated/50 px-3 py-2 text-sm text-secondary-text">暂无关联边，重建卡片或同步图谱后会自动补规则线索。</div>
            ) : null}
          </div>
        </section>

        <div className="flex flex-wrap gap-2 border-t border-border/70 pt-4">
          <FeedbackButton
            active={usefulCount > 0}
            disabled={feedbacking || usefulCount > 0}
            tone="success"
            icon={<ThumbsUp className="h-4 w-4" />}
            onClick={() => onFeedback('useful')}
          >
            有用
          </FeedbackButton>
          <FeedbackButton
            active={wrongCount > 0}
            disabled={feedbacking || wrongCount > 0}
            tone="danger"
            icon={<AlertTriangle className="h-4 w-4" />}
            onClick={() => onFeedback('wrong')}
          >
            错误
          </FeedbackButton>
          <FeedbackButton
            active={noisyCount > 0}
            disabled={feedbacking || noisyCount > 0}
            tone="warning"
            icon={<VolumeX className="h-4 w-4" />}
            onClick={() => onFeedback('noisy')}
          >
            噪音
          </FeedbackButton>
        </div>
      </div>
    </div>
  );
};

const NewsSignalsPage: React.FC = () => {
  const today = new Date().toISOString().slice(0, 10);
  const [signalDate, setSignalDate] = useState(today);
  const [signalLayer, setSignalLayer] = useState('');
  const [industry, setIndustry] = useState('');
  const [horizon, setHorizon] = useState('');
  const [status, setStatus] = useState('active');
  const [includeSemanticEdges, setIncludeSemanticEdges] = useState(false);
  const [data, setData] = useState<NewsSignalListResponse | null>(null);
  const [metrics, setMetrics] = useState<NewsSignalMetrics | null>(null);
  const [selectedId, setSelectedId] = useState<string>('');
  const [selected, setSelected] = useState<NewsSignalCard | null>(null);
  const [selectedGraph, setSelectedGraph] = useState<NewsSignalGraph | null>(null);
  const [loading, setLoading] = useState(false);
  const [graphLoading, setGraphLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);
  const [syncingGraphiti, setSyncingGraphiti] = useState(false);
  const [feedbacking, setFeedbacking] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [buildResult, setBuildResult] = useState<NewsSignalRebuildResult | null>(null);
  const [graphSyncResult, setGraphSyncResult] = useState<Record<string, unknown> | null>(null);

  const loadData = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [listResult, metricResult] = await Promise.all([
        newsSignalsApi.list({
          signalDate: signalDate || undefined,
          signalLayer: signalLayer || undefined,
          industry: industry.trim() || undefined,
          horizon: horizon || undefined,
          status: status || undefined,
          limit: 160,
        }),
        newsSignalsApi.metrics(signalDate || undefined),
      ]);
      setData(listResult);
      setMetrics(metricResult);
      const firstId = listResult.items[0]?.cardId || '';
      setSelectedId((current) => current || firstId);
      if (!listResult.items.some((item) => item.cardId === selectedId)) {
        setSelectedId(firstId);
      }
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, [horizon, industry, selectedId, signalDate, signalLayer, status]);

  const loadSelected = useCallback(async () => {
    if (!selectedId) {
      setSelected(null);
      setSelectedGraph(null);
      return;
    }
    setGraphLoading(true);
    try {
      const [item, graph] = await Promise.all([
        newsSignalsApi.get(selectedId),
        newsSignalsApi.graph(selectedId, 200),
      ]);
      setSelected(item);
      setSelectedGraph(graph);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setGraphLoading(false);
    }
  }, [selectedId]);

  const rebuild = useCallback(async () => {
    setRebuilding(true);
    setError(null);
    setGraphSyncResult(null);
    try {
      const result = await newsSignalsApi.rebuild({
        targetDate: signalDate || undefined,
        includeCjzc: true,
        includeCls: true,
        includeXueqiu: true,
        includeMacroFinance: true,
        clsLimit: 50,
        xueqiuLimit: 30,
        macroFinanceLimit: 30,
        syncGraphiti: false,
        includeSemanticEdges,
      });
      setBuildResult(result);
      setSelectedId('');
      await loadData();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRebuilding(false);
    }
  }, [includeSemanticEdges, loadData, signalDate]);

  const syncGraphiti = useCallback(async () => {
    setSyncingGraphiti(true);
    setError(null);
    try {
      const result = await newsSignalsApi.graphSync({
        signalDate: signalDate || undefined,
        limit: 100,
        includeSemanticEdges,
      });
      setGraphSyncResult(result);
      await loadData();
      await loadSelected();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setSyncingGraphiti(false);
    }
  }, [includeSemanticEdges, loadData, loadSelected, signalDate]);

  const sendFeedback = useCallback(async (feedbackType: string) => {
    if (!selected?.cardId) return;
    setFeedbacking(true);
    setError(null);
    try {
      await newsSignalsApi.feedback(selected.cardId, feedbackType);
      await loadData();
      await loadSelected();
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setFeedbacking(false);
    }
  }, [loadData, loadSelected, selected?.cardId]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    void loadSelected();
  }, [loadSelected]);

  const rows = useMemo(() => data?.items ?? [], [data]);

  return (
    <div className="mx-auto flex w-full max-w-7xl flex-col gap-5 px-4 py-6">
      <div className="flex flex-col gap-3 border-b border-border/70 pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="mb-2 flex items-center gap-2">
            <Newspaper className="h-5 w-5 text-cyan" />
            <span className="text-sm font-medium text-cyan">News Signals</span>
          </div>
          <h1 className="text-2xl font-semibold tracking-normal text-foreground">消息面</h1>
        </div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-[150px_120px_140px_120px_180px_110px_auto_auto]">
          <input type="date" value={signalDate} onChange={(event) => setSignalDate(event.target.value)} className="h-10 border border-border/70 bg-elevated px-3 text-sm text-foreground outline-none" />
          <select value={signalLayer} onChange={(event) => setSignalLayer(event.target.value)} className="h-10 border border-border/70 bg-elevated px-3 text-sm text-foreground outline-none">
            <option value="">层级</option>
            <option value="industry">产业层</option>
            <option value="company">公司层</option>
            <option value="macro">宏观层</option>
          </select>
          <select value={horizon} onChange={(event) => setHorizon(event.target.value)} className="h-10 border border-border/70 bg-elevated px-3 text-sm text-foreground outline-none">
            <option value="">周期</option>
            <option value="short">短期</option>
            <option value="medium">中期</option>
            <option value="long">长期</option>
          </select>
          <select value={status} onChange={(event) => setStatus(event.target.value)} className="h-10 border border-border/70 bg-elevated px-3 text-sm text-foreground outline-none">
            <option value="">状态</option>
            <option value="active">有效</option>
            <option value="suppressed">已降权</option>
            <option value="pending">待处理</option>
            <option value="low_quality">低质量</option>
          </select>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-secondary-text" />
            <input value={industry} onChange={(event) => setIndustry(event.target.value)} placeholder="产业" className="h-10 w-full border border-border/70 bg-elevated pl-9 pr-3 text-sm text-foreground outline-none" />
          </div>
          <label className="inline-flex h-10 items-center justify-center gap-2 border border-border/70 bg-elevated px-3 text-sm text-secondary-text">
            <input
              type="checkbox"
              checked={includeSemanticEdges}
              onChange={(event) => setIncludeSemanticEdges(event.target.checked)}
              className="h-4 w-4 accent-cyan"
            />
            语义边
          </label>
          <button type="button" onClick={() => void rebuild()} disabled={rebuilding} className="btn-primary inline-flex h-10 items-center justify-center gap-2 whitespace-nowrap">
            <RefreshCw className={cn('h-4 w-4', rebuilding ? 'animate-spin' : '')} />重建卡片
          </button>
          <button type="button" onClick={() => void syncGraphiti()} disabled={syncingGraphiti} className="btn-secondary inline-flex h-10 items-center justify-center gap-2 whitespace-nowrap">
            <Network className={cn('h-4 w-4', syncingGraphiti ? 'animate-pulse' : '')} />同步图谱
          </button>
        </div>
      </div>

      {error ? <ApiErrorAlert error={error} /> : null}
      {buildResult ? (
        <div className="border border-border/70 bg-card/70 px-4 py-3 text-sm text-secondary-text">
          {buildResult.status || '--'} · 原始 {buildResult.rawEpisodesUpserted ?? 0} · 卡片 {buildResult.cardsUpserted ?? 0}
        </div>
      ) : null}
      {graphSyncResult ? (
        <div className="border border-border/70 bg-card/70 px-4 py-3 text-sm text-secondary-text">
          图谱同步 {String(graphSyncResult.status || '--')} · 成功 {String(graphSyncResult.synced ?? 0)} · 失败 {String(graphSyncResult.failed ?? 0)}
        </div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <MetricTile label="卡片" value={String(metrics?.totalCards ?? data?.total ?? 0)} icon={<Newspaper className="h-4 w-4" />} hint={`有效 ${metrics?.activeCards ?? data?.summary?.active ?? 0}`} />
        <MetricTile label="均分" value={fmtNum(metrics?.avgSignalScore)} icon={<GitBranch className="h-4 w-4" />} hint={`降权 ${metrics?.suppressedCards ?? data?.summary?.suppressed ?? 0}`} />
        <MetricTile label="层级" value={String(Object.keys(data?.summary?.layerCounts || {}).length || Object.keys(metrics?.layerCounts || {}).length)} icon={<Layers3 className="h-4 w-4" />} hint={`产业 ${data?.summary?.layerCounts?.industry ?? metrics?.layerCounts?.industry ?? 0} / 公司 ${data?.summary?.layerCounts?.company ?? metrics?.layerCounts?.company ?? 0} / 宏观 ${data?.summary?.layerCounts?.macro ?? metrics?.layerCounts?.macro ?? 0}`} />
        <MetricTile label="反馈" value={String(Object.values(metrics?.feedbackCounts || {}).reduce((sum, value) => sum + Number(value || 0), 0))} icon={<ThumbsUp className="h-4 w-4" />} hint={`Graph ${metrics?.graphSyncCounts?.pending ?? 0} pending`} />
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(360px,0.95fr)_minmax(0,1.25fr)]">
        <div className="space-y-3">
          {loading ? (
            <div className="border border-border/70 bg-card/70 p-6 text-sm text-secondary-text">加载中...</div>
          ) : rows.length ? (
            rows.map((item) => (
              <SignalCardRow key={item.cardId} item={item} selected={item.cardId === selectedId} onSelect={() => setSelectedId(item.cardId)} />
            ))
          ) : (
            <div className="border border-border/70 bg-card/60 p-6">
              <EmptyState title="暂无消息卡片" description="当前筛选条件下没有卡片。" icon={<Newspaper className="h-8 w-8" />} />
            </div>
          )}
        </div>
        <SignalDetail item={selected} graph={selectedGraph} graphLoading={graphLoading} onFeedback={sendFeedback} feedbacking={feedbacking} />
      </div>
    </div>
  );
};

export default NewsSignalsPage;
