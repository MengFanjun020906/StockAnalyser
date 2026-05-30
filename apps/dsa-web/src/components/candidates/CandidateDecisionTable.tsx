import React, { useCallback, useMemo, useRef, useState } from 'react';
import { AlertTriangle, BarChart3, ChevronDown, Eye, Target } from 'lucide-react';
import { Badge } from '../common';
import { cn } from '../../utils/cn';

export type CandidateDecisionTone = 'success' | 'info' | 'warning' | 'danger' | 'history' | 'default';

export type CandidateDecisionAction = {
  key: string;
  label: string;
  tone?: CandidateDecisionTone;
  strength?: string;
  reason?: string;
};

export type CandidateDecisionEvidence = {
  label: string;
  detail: string;
  tone?: CandidateDecisionTone;
};

export type CandidateDecisionScore = {
  label: string;
  value?: number | null;
  note?: string;
};

export type CandidateDecisionRow = {
  id?: string;
  code: string;
  name?: string;
  score?: number | null;
  scoreLabel?: string;
  scoreNote?: string;
  secondaryScores?: CandidateDecisionScore[];
  action: CandidateDecisionAction;
  primaryReason?: string;
  dimensionLabels?: string[];
  expertLabels?: string[];
  sourceLabels?: string[];
  lifecycleLabel?: string;
  occurrenceLabel?: string;
  dateLabel?: string;
  badges?: Array<{ label: string; tone?: CandidateDecisionTone }>;
  evidence?: CandidateDecisionEvidence[];
  metricHighlights?: string[];
  riskFlags?: string[];
  detailNote?: string;
};

type CandidateDecisionTableProps = {
  title: string;
  description?: string;
  items: CandidateDecisionRow[];
  emptyTitle?: string;
  emptyDescription?: string;
  className?: string;
  scoreColumnLabel?: string;
  actionColumnLabel?: string;
};

const actionStyles: Record<CandidateDecisionTone, string> = {
  success: 'border-success/25 bg-success/10 text-success',
  info: 'border-cyan/25 bg-cyan/10 text-cyan',
  warning: 'border-warning/25 bg-warning/10 text-warning',
  danger: 'border-danger/25 bg-danger/10 text-danger',
  history: 'border-border/60 bg-surface-2 text-secondary-text',
  default: 'border-border/60 bg-surface-2 text-secondary-text',
};

const accentBorder: Record<CandidateDecisionTone, string> = {
  success: 'border-l-success',
  info: 'border-l-cyan',
  warning: 'border-l-warning',
  danger: 'border-l-danger',
  history: 'border-l-border',
  default: 'border-l-border',
};

const chipStyles: Record<CandidateDecisionTone, string> = {
  success: 'border-success/20 bg-success/5 text-success',
  info: 'border-border/70 bg-surface-2 text-secondary-text',
  warning: 'border-warning/25 bg-warning/5 text-warning',
  danger: 'border-danger/25 bg-danger/5 text-danger',
  history: 'border-border/70 bg-surface-2 text-secondary-text',
  default: 'border-border/70 bg-surface-2 text-secondary-text',
};

const formatScore = (value?: number | null): string => {
  if (value == null || Number.isNaN(Number(value))) return '--';
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 });
};

const scoreColor = (value?: number | null): string => {
  if (value == null) return 'text-foreground';
  if (value >= 70) return 'text-success';
  if (value >= 40) return 'text-warning';
  return 'text-danger';
};

const unique = (values?: string[]): string[] => (
  Array.from(new Set((values || []).map((item) => item.trim()).filter(Boolean)))
);

const CompactChip: React.FC<{ children: React.ReactNode; tone?: CandidateDecisionTone }> = ({ children, tone = 'default' }) => (
  <span className={cn('inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-medium', chipStyles[tone])}>
    {children}
  </span>
);

const ScoreBlock: React.FC<{ label?: string; value?: number | null; note?: string }> = ({ label = '入池优先级', value, note }) => (
  <div className="inline-flex min-w-[80px] flex-col rounded-md border border-border/70 bg-surface-2 px-2 py-1.5">
    <span className={cn('font-mono text-sm font-semibold', scoreColor(value))}>{formatScore(value)}</span>
    <span className="mt-0.5 text-xs leading-4 text-muted-text">{label}</span>
    {note ? <span className="sr-only">{note}</span> : null}
  </div>
);

const sourceNameMap: Record<string, string> = {
  local_price_volume: '量价异动',
  fundamental_snapshot: '基本面筛选',
  low_base_structure: '低位启动',
  limit_up_pool: '涨停复盘',
  sequoia_technical: '技术形态',
  alphasift_factor: '多因子选股',
  sector_constituent: '板块成分',
  news_momentum: '消息动量',
  capital_flow: '资金流向',
};

const kvPattern = /value=([\d.]+)\s*[;；]\s*threshold=([\d.]+)\s*[;；]\s*deviation=([-\d.eE]+)/;
const prefixedPattern = /^(.+?)[：:]\s*value=([\d.]+)\s*[;；]\s*threshold=([\d.]+)\s*[;；]\s*deviation=([-\d.eE]+)$/;

const fmtYi = (x: number): string => `${(x / 1e8).toFixed(2)}亿`;

const humanizeKV = (label: string, v: number, t: number, d: number): string => {
  if (label.includes('放量突破'))
    return `收盘价 ${v.toFixed(2)} 突破 20 日高点 ${t.toFixed(2)}，量比 ${d.toFixed(1)}`;
  if (label.includes('价格突破'))
    return `收盘价 ${v.toFixed(2)} 突破 60 日高点 ${t.toFixed(2)}（涨幅 ${d.toFixed(1)}%）`;
  if (label === '量能突增')
    return `量比 ${v.toFixed(1)} 倍（阈值 ${t.toFixed(1)}），涨跌幅 ${d.toFixed(1)}%`;
  if (label === '成交额突增')
    return `成交额达到 20 日均值 ${v.toFixed(1)} 倍（阈值 ${t.toFixed(1)}），超出 ${fmtYi(d)}`;
  if (label === '波动扩张')
    return `日内振幅 ${v.toFixed(1)}%（阈值 ${t.toFixed(1)}%），量比 ${d.toFixed(1)}`;
  if (label.includes('跳空高开'))
    return `跳空缺口 ${v.toFixed(1)}%（阈值 ${t.toFixed(1)}%），量比 ${d.toFixed(1)}`;
  if (label.includes('跳空低开'))
    return `向下跳空 ${v.toFixed(1)}%（阈值 ${t.toFixed(1)}%），量比 ${d.toFixed(1)}`;
  if (label.includes('金叉'))
    return `MA20（${v.toFixed(2)}）上穿 MA60（${t.toFixed(2)}），偏离 ${d.toFixed(1)}%`;
  if (label.includes('死叉'))
    return `MA20（${v.toFixed(2)}）下穿 MA60（${t.toFixed(2)}），偏离 ${d.toFixed(1)}%`;
  if (label.includes('缩量蓄势'))
    return `近 5 日成交量仅为 20 日均量的 ${(v * 100).toFixed(0)}%，振幅收窄至 ${d.toFixed(1)}%`;
  if (label.includes('低位转强'))
    return `120 日区间位置 ${(v * 100).toFixed(0)}%（低于 45%），涨幅 ${d.toFixed(1)}%`;
  if (label.includes('资金') && label.includes('流'))
    return `主力资金净${v >= 0 ? '流入' : '流出'} ${fmtYi(v)}`;
  if (Math.abs(d) > 1e6)
    return `${label}：${fmtYi(d)}`;
  return `当前值 ${v.toFixed(1)}，阈值 ${t.toFixed(1)}（偏离 ${d >= 0 ? '+' : ''}${d.toFixed(1)}%）`;
};

const humanizeOneLine = (label: string, line: string): string | null => {
  const trimmed = line.trim();
  if (!trimmed) return null;

  const pre = trimmed.match(prefixedPattern);
  if (pre) {
    const humanPrefix = sourceNameMap[pre[1]] || pre[1];
    const v = Number(pre[2]);
    const t = Number(pre[3]);
    const d = Number(pre[4]);
    if (Math.abs(d) > 1e6) return `${humanPrefix}：${fmtYi(d)}`;
    const ratio = t > 0 ? v / t : 0;
    if (ratio >= 2) return `${humanPrefix}：当前 ${v.toFixed(1)}，正常 ${t.toFixed(1)}（${ratio.toFixed(1)}倍）`;
    return `${humanPrefix}：当前 ${v.toFixed(1)}，阈值 ${t.toFixed(1)}（偏离 ${d >= 0 ? '+' : ''}${d.toFixed(1)}%）`;
  }

  const kv = trimmed.match(kvPattern);
  if (kv) return humanizeKV(label, Number(kv[1]), Number(kv[2]), Number(kv[3]));

  if (sourceNameMap[trimmed]) return sourceNameMap[trimmed];
  return null;
};

const humanizeEvidence = (label: string, raw: string): string => {
  if (!raw) return '暂无详情';
  const single = humanizeOneLine(label, raw);
  if (single) return single;
  const lines = raw.split('\n').map((s) => s.trim()).filter(Boolean);
  const parts = lines.map((l) => humanizeOneLine(label, l) || l);
  return parts.join('；') || raw;
};

const humanizeReason = (raw: string): string => {
  if (!raw) return '暂无结构化依据';
  const parts = raw.split('\n').map((s) => s.trim()).filter(Boolean);
  const humanized: string[] = [];
  for (const part of parts) {
    if (/^value=[\d.]+;\s*threshold=[\d.]+;\s*deviation=[\d.]+$/.test(part)) {
      const match = part.match(/value=([\d.]+);\s*threshold=([\d.]+);\s*deviation=([\d.]+)/);
      if (match) {
        const [, val, thresh, dev] = match;
        humanized.push(`当前值 ${Number(val).toFixed(1)} 超过阈值 ${Number(thresh).toFixed(1)}（偏离 ${Number(dev) >= 0 ? '+' : ''}${Number(dev).toFixed(1)}%）`);
      }
      continue;
    }
    if (sourceNameMap[part]) {
      humanized.push(sourceNameMap[part]);
      continue;
    }
    humanized.push(part);
  }
  return humanized.join('；') || '暂无结构化依据';
};

export const CandidateDecisionTable: React.FC<CandidateDecisionTableProps> = ({
  title,
  description,
  items,
  emptyTitle = '暂无候选',
  emptyDescription = '当前运行没有可展示的候选股票。',
  className,
}) => {
  const [expandedCode, setExpandedCode] = useState<string>('');
  const rows = useMemo(() => items.filter((item) => item.code), [items]);
  const expandRef = useRef<HTMLDivElement>(null);

  const handleToggle = useCallback((rowKey: string) => {
    setExpandedCode((prev) => {
      if (prev === rowKey) return '';
      requestAnimationFrame(() => {
        expandRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      });
      return rowKey;
    });
  }, []);

  return (
    <section className={cn('overflow-hidden rounded-lg border border-border/60 bg-card/80 shadow-sm shadow-black/5', className)}>
      {/* Header */}
      <div className="flex flex-col gap-3 border-b border-border/60 bg-surface-2/35 px-5 py-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Target className="h-4 w-4 text-secondary-text" />
            <h2 className="text-lg font-semibold text-foreground">{title}</h2>
          </div>
          {description ? <p className="mt-1 max-w-2xl text-sm leading-6 text-secondary-text">{description}</p> : null}
        </div>
        <div className="inline-flex h-8 items-center rounded-md border border-border/60 bg-card px-3 text-sm font-medium text-secondary-text">
          {rows.length} 只
        </div>
      </div>

      {/* Card List */}
      {rows.length ? (
        <div className="divide-y divide-border/50">
          {rows.map((item, index) => {
            const rowKey = item.id || item.code;
            const isExpanded = expandedCode === rowKey;
            const tone = item.action.tone || 'default';
            const dimensions = unique(item.dimensionLabels);
            const badges = item.badges || [];
            const risks = unique(item.riskFlags);
            const experts = unique(item.expertLabels);
            const sources = unique(item.sourceLabels);
            const visibleSources = unique([...experts, ...sources]).slice(0, 3);
            const hiddenSourceCount = Math.max(0, experts.length + sources.length - visibleSources.length);

            return (
              <div key={rowKey} className={cn('border-l-[3px] px-4 py-4 sm:px-5', accentBorder[tone])}>
                {/* Row 1: Code + Name + Action badge */}
                <div className="flex items-start justify-between gap-3">
                  <div className="flex items-baseline gap-2 min-w-0">
                    <span className="font-mono text-xs font-semibold text-muted-text">#{index + 1}</span>
                    <span className="font-mono text-sm font-semibold text-foreground">{item.code}</span>
                    <span className="truncate text-sm text-secondary-text">{item.name || item.code}</span>
                    {item.lifecycleLabel ? <Badge variant="history">{item.lifecycleLabel}</Badge> : null}
                    {item.dateLabel ? <span className="text-xs text-muted-text">{item.dateLabel}</span> : null}
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <span className={cn('inline-flex items-center rounded-md border px-2 py-0.5 text-xs font-semibold whitespace-nowrap', actionStyles[tone])}>
                      {item.action.label}
                    </span>
                    {item.action.strength ? <span className="text-xs text-muted-text">{item.action.strength}</span> : null}
                  </div>
                </div>

                {/* Row 2: Scores + Reason */}
                <div className="mt-2.5 flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
                  {/* Score inline */}
                  <div className="flex shrink-0 items-baseline gap-2">
                    <span className={cn('font-mono text-lg font-bold', scoreColor(item.score))}>{formatScore(item.score)}</span>
                    {(item.secondaryScores || []).slice(0, 2).map((score) => (
                      <span key={score.label} className="flex items-baseline gap-1 text-xs text-muted-text">
                        <span className="text-secondary-text">/</span>
                        <span className={cn('font-mono font-semibold', scoreColor(score.value))}>{formatScore(score.value)}</span>
                        <span>{score.label}</span>
                      </span>
                    ))}
                    {item.scoreNote ? <span className="text-xs text-muted-text">{item.scoreNote}</span> : null}
                  </div>
                  {/* Reason text */}
                  <p className="line-clamp-2 text-sm leading-6 text-secondary-text">{humanizeReason(item.primaryReason || '')}</p>
                </div>

                {/* Row 3: Chips + Evidence toggle */}
                <div className="mt-3 flex items-center justify-between gap-2">
                  <div className="flex flex-wrap gap-1.5">
                    {dimensions.slice(0, 3).map((dimension) => (
                      <CompactChip key={dimension}>{dimension}</CompactChip>
                    ))}
                    {badges.slice(0, 4).map((badge) => (
                      <CompactChip key={badge.label} tone={badge.tone}>{badge.label}</CompactChip>
                    ))}
                    {visibleSources.map((source) => (
                      <CompactChip key={source}>{source}</CompactChip>
                    ))}
                    {risks.length ? <CompactChip tone="warning">风险 {risks.length}</CompactChip> : null}
                    {hiddenSourceCount > 0 ? <CompactChip>来源 +{hiddenSourceCount}</CompactChip> : null}
                    {item.occurrenceLabel ? <span className="text-xs text-muted-text">{item.occurrenceLabel}</span> : null}
                  </div>
                  <button
                    type="button"
                    className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-border/60 bg-surface-2 px-2.5 py-1.5 text-xs text-secondary-text transition hover:bg-hover hover:text-foreground"
                    onClick={() => handleToggle(rowKey)}
                    aria-expanded={isExpanded}
                  >
                    <Eye className="h-3.5 w-3.5" />
                    证据
                    <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', isExpanded && 'rotate-180')} />
                  </button>
                </div>

                {/* Expanded Evidence Panel */}
                {isExpanded ? (
                  <div ref={expandRef} className="mt-4 rounded-lg border border-border/50 bg-surface-2/35 p-4">
                    {(() => {
                      const evidence = item.evidence || [];
                      const metrics = unique(item.metricHighlights);
                      const expandRisks = unique(item.riskFlags);
                      const expandSources = unique(item.sourceLabels);
                      const expandExperts = unique(item.expertLabels);
                      const scoreItems: CandidateDecisionScore[] = [
                        { label: item.scoreLabel || '入池优先级', value: item.score, note: item.scoreNote },
                        ...(item.secondaryScores || []),
                      ];
                      return (
                        <div className="grid gap-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(240px,0.6fr)]">
                          <div>
                            <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-foreground">
                              <BarChart3 className="h-4 w-4 text-secondary-text" />
                              {item.code} {item.name || ''} 证据拆解
                            </div>
                            {evidence.length ? (
                              <div className="grid gap-2 md:grid-cols-2">
                                {evidence.slice(0, 8).map((entry, i) => (
                                  <div key={`${entry.label}-${i}`} className={cn('rounded-md border px-3 py-2', chipStyles[entry.tone || 'default'])}>
                                    <div className="text-xs font-semibold">{entry.label}</div>
                                    <p className="mt-1 text-xs leading-5 opacity-90">{humanizeEvidence(entry.label, entry.detail)}</p>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <p className="text-sm text-muted-text">本条候选没有结构化证据维度。</p>
                            )}
                          </div>
                          <div className="space-y-3">
                            <div>
                              <div className="mb-1.5 text-xs font-semibold text-foreground">评估口径</div>
                              <div className="grid grid-cols-2 gap-2">
                                {scoreItems.slice(0, 4).map((score) => (
                                  <ScoreBlock key={score.label} label={score.label} value={score.value} note={score.note} />
                                ))}
                              </div>
                            </div>
                            <div>
                              <div className="mb-1.5 text-xs font-semibold text-foreground">动作依据</div>
                              <p className="rounded-md border border-border/60 bg-card/75 px-3 py-2 text-xs leading-5 text-secondary-text">
                                {item.action.reason || item.detailNote || '候选池阶段只代表值得继续取证。'}
                              </p>
                            </div>
                            {metrics.length ? (
                              <div>
                                <div className="mb-1.5 text-xs font-semibold text-foreground">关键指标</div>
                                <div className="flex flex-wrap gap-1.5">
                                  {metrics.slice(0, 10).map((metric) => <CompactChip key={metric}>{metric}</CompactChip>)}
                                </div>
                              </div>
                            ) : null}
                            {expandRisks.length ? (
                              <div className="rounded-md border border-warning/25 bg-warning/5 px-3 py-2 text-warning">
                                <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold">
                                  <AlertTriangle className="h-3.5 w-3.5" />
                                  风险/待确认
                                </div>
                                <ul className="space-y-1 text-xs leading-5">
                                  {expandRisks.slice(0, 5).map((risk) => <li key={risk}>{risk}</li>)}
                                </ul>
                              </div>
                            ) : null}
                            {expandSources.length || expandExperts.length ? (
                              <div>
                                <div className="mb-1.5 text-xs font-semibold text-foreground">来源链</div>
                                <div className="flex flex-wrap gap-1.5">
                                  {[...expandExperts, ...expandSources].slice(0, 10).map((source) => <CompactChip key={source}>{source}</CompactChip>)}
                                </div>
                              </div>
                            ) : null}
                          </div>
                        </div>
                      );
                    })()}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      ) : (
        <div className="px-6 py-10 text-center">
          <p className="text-sm font-semibold text-foreground">{emptyTitle}</p>
          <p className="mt-1 text-sm text-muted-text">{emptyDescription}</p>
        </div>
      )}
    </section>
  );
};
