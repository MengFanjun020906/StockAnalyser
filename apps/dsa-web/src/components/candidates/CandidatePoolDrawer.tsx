import React, { useEffect } from 'react';
import { Database, Route, ShieldAlert, Sparkles, Target, X } from 'lucide-react';
import type { CandidateDecisionTone } from './CandidateDecisionTable';
import type { CandidatePoolRow } from './CandidatePoolTable';
import { cn } from '../../utils/cn';

type CandidatePoolDrawerProps = {
  row: CandidatePoolRow | null;
  onClose: () => void;
  onOpenTrace?: () => void;
};

const actionStyles: Record<CandidateDecisionTone, string> = {
  success: 'border-success/25 bg-success/10 text-success',
  info: 'border-cyan/25 bg-cyan/10 text-cyan',
  warning: 'border-warning/25 bg-warning/10 text-warning',
  danger: 'border-danger/25 bg-danger/10 text-danger',
  history: 'border-border/60 bg-surface-2 text-secondary-text',
  default: 'border-border/60 bg-surface-2 text-secondary-text',
};

const scoreColor = (value?: number | null): string => {
  if (value == null) return 'text-muted-text';
  if (value >= 80) return 'text-cyan';
  if (value >= 60) return 'text-success';
  if (value >= 40) return 'text-warning';
  return 'text-danger';
};

const formatScore = (value?: number | null): string => {
  if (value == null || Number.isNaN(Number(value))) return '--';
  return Number(value).toFixed(1);
};

export const CandidatePoolDrawer: React.FC<CandidatePoolDrawerProps> = ({ row, onClose, onOpenTrace }) => {
  useEffect(() => {
    if (!row) return undefined;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.addEventListener('keydown', handleKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [onClose, row]);

  if (!row) return null;

  return (
    <div className="fixed inset-0 z-50">
      <div
        className="absolute inset-0 bg-black/60 backdrop-blur-[2px]"
        role="presentation"
        onClick={onClose}
      />
      <aside
        className="absolute inset-y-0 right-0 flex w-full max-w-[440px] flex-col border-l border-border/70 bg-card shadow-2xl shadow-black/40"
        role="dialog"
        aria-modal="true"
        aria-labelledby="candidate-drawer-title"
      >
        <header className="sticky top-0 z-10 flex items-start justify-between gap-3 border-b border-border/60 bg-card/95 px-4 py-3.5 backdrop-blur">
          <div className="min-w-0">
            <div className="flex flex-wrap items-baseline gap-2">
              <h2 id="candidate-drawer-title" className="truncate text-lg font-semibold text-foreground">{row.name || row.code}</h2>
              <span className="font-mono text-[11px] text-muted-text">{row.code}</span>
              {row.market ? <span className="text-[10px] text-muted-text">{row.market}</span> : null}
            </div>
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className={cn('inline-flex items-center rounded-md border px-2 py-0.5 text-[11px] font-semibold', actionStyles[row.action.tone])}>{row.action.label}</span>
              <span className={cn('font-mono text-sm font-semibold tabular-nums', scoreColor(row.score))}>{formatScore(row.score)}</span>
              <span className="text-[10px] text-muted-text">候选评分</span>
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-transparent text-muted-text transition hover:border-border/60 hover:bg-surface-2 hover:text-foreground"
            aria-label="关闭候选详情"
          >
            <X className="h-4 w-4" />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
          <section className="rounded-lg border border-cyan/20 bg-cyan/5 p-3">
            <div className="flex items-center gap-2 text-[11px] font-medium text-cyan">
              <Target className="h-3.5 w-3.5" />
              Agent 判断
            </div>
            <p className="mt-2 text-sm leading-6 text-foreground">{row.action.reason || row.reason || '暂无判断理由'}</p>
            <div className="mt-3 grid grid-cols-3 gap-2">
              <div className="rounded-md border border-border/50 bg-card/60 px-2 py-1.5">
                <div className="text-[9px] text-muted-text">动作</div>
                <div className="mt-0.5 text-xs font-medium text-foreground">{row.action.label}</div>
              </div>
              <div className="rounded-md border border-border/50 bg-card/60 px-2 py-1.5">
                <div className="text-[9px] text-muted-text">强度</div>
                <div className="mt-0.5 text-xs font-medium text-foreground">{row.action.strength || '--'}</div>
              </div>
              <div className="rounded-md border border-border/50 bg-card/60 px-2 py-1.5">
                <div className="text-[9px] text-muted-text">出现</div>
                <div className="mt-0.5 text-xs font-medium text-foreground">{row.occurrence}</div>
              </div>
            </div>
          </section>

          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-[11px] font-medium tracking-[0.08em] text-secondary-text">证据拆解</h3>
              <span className="text-[10px] text-muted-text">{row.evidence.length} 条</span>
            </div>
            <div className="space-y-1.5">
              {row.evidence.length ? row.evidence.map((item, index) => (
                <div key={item.label + index} className="rounded-md border border-border/50 bg-surface-2/35 p-2.5">
                  <div className="flex items-center gap-2">
                    <span className={cn('rounded border px-1.5 py-0.5 text-[9px]', actionStyles[item.tone])}>{item.label}</span>
                    {item.detail ? <span className="text-[10px] text-muted-text">结构化证据</span> : null}
                  </div>
                  <p className="mt-1.5 text-xs leading-5 text-secondary-text">{item.detail || '暂无详情'}</p>
                </div>
              )) : <div className="rounded-md border border-dashed border-border/60 p-3 text-xs text-muted-text">暂无结构化证据</div>}
            </div>
          </section>

          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-[11px] font-medium tracking-[0.08em] text-secondary-text">来源与专家</h3>
              <span className="text-[10px] text-muted-text">{row.sources.length + row.experts.length} 个</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {row.sources.map((source) => (
                <span key={source} className="inline-flex items-center gap-1 rounded border border-border/60 bg-surface-2/60 px-1.5 py-0.5 text-[10px] text-secondary-text">
                  <Database className="h-3 w-3 text-muted-text" />
                  {source}
                </span>
              ))}
              {row.experts.map((expert) => (
                <span key={expert} className="inline-flex items-center gap-1 rounded border border-cyan/20 bg-cyan/5 px-1.5 py-0.5 text-[10px] text-cyan">
                  <Sparkles className="h-3 w-3" />
                  {expert}
                </span>
              ))}
              {!row.sources.length && !row.experts.length ? <span className="text-xs text-muted-text">暂无来源</span> : null}
            </div>
          </section>

          {row.metrics.length ? (
            <section>
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-[11px] font-medium tracking-[0.08em] text-secondary-text">关键指标</h3>
                <span className="text-[10px] text-muted-text">本地快照</span>
              </div>
              <div className="grid grid-cols-2 gap-1.5">
                {row.metrics.map((metric) => (
                  <div key={metric} className="rounded-md border border-border/50 bg-surface-2/35 px-2 py-1.5 text-[11px] text-secondary-text">{metric}</div>
                ))}
              </div>
            </section>
          ) : null}

          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-[11px] font-medium tracking-[0.08em] text-secondary-text">风险与限制</h3>
              <ShieldAlert className="h-3.5 w-3.5 text-warning" />
            </div>
            <div className="space-y-1.5">
              {row.risks.length ? row.risks.map((risk) => (
                <div key={risk} className="flex items-start gap-2 rounded-md border border-warning/20 bg-warning/5 px-2.5 py-2 text-[11px] leading-5 text-warning">
                  <ShieldAlert className="mt-0.5 h-3 w-3 shrink-0" />
                  <span>{risk}</span>
                </div>
              )) : (
                <div className="rounded-md border border-border/50 bg-surface-2/35 px-2.5 py-2 text-[11px] text-muted-text">
                  未发现硬风险；仍需结合市场状态和仓位约束。
                </div>
              )}
            </div>
          </section>

          <section className="rounded-md border border-border/50 bg-surface-2/30 p-3 text-[10px] leading-5 text-muted-text">
            <div className="flex items-center justify-between">
              <span>生命周期</span>
              <span className="text-secondary-text">{row.lifecycle}</span>
            </div>
            <div className="mt-1 flex items-center justify-between">
              <span>有效期</span>
              <span className="text-secondary-text">{row.validUntil || '--'}</span>
            </div>
            <div className="mt-1 flex items-start justify-between gap-3">
              <span>说明</span>
              <span className="text-right text-secondary-text">{row.note}</span>
            </div>
          </section>
        </div>

        <footer className="flex items-center gap-2 border-t border-border/60 bg-card/95 px-4 py-3">
          <button
            type="button"
            onClick={onClose}
            className="inline-flex h-9 flex-1 items-center justify-center rounded-md border border-border/70 bg-surface-2 px-3 text-xs font-medium text-secondary-text transition hover:text-foreground"
          >
            关闭
          </button>
          <button
            type="button"
            onClick={onOpenTrace}
            disabled={!onOpenTrace}
            title={onOpenTrace ? '打开关联的 Agent Trace' : '本轮没有可关联的 Trace session'}
            className="inline-flex h-9 flex-1 items-center justify-center gap-1.5 rounded-md border border-cyan/30 bg-cyan/10 px-3 text-xs font-medium text-cyan transition hover:bg-cyan/15 disabled:cursor-not-allowed disabled:opacity-40"
          >
            <Route className="h-3.5 w-3.5" />
            {onOpenTrace ? '查看 Trace' : '暂无可关联 Trace'}
          </button>
        </footer>
      </aside>
    </div>
  );
};
