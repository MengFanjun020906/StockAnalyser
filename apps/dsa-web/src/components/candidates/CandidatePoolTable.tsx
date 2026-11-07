import React from 'react';
import { ChevronRight, Target } from 'lucide-react';
import type { CandidateDecisionTone } from './CandidateDecisionTable';
import { cn } from '../../utils/cn';

export type CandidatePoolRow = {
  id: string;
  code: string;
  name: string;
  market?: string;
  score?: number | null;
  action: {
    label: string;
    tone: CandidateDecisionTone;
    strength?: string;
    reason: string;
  };
  sources: string[];
  experts: string[];
  dimensions: string[];
  reason: string;
  lifecycle: string;
  occurrence: string;
  validUntil?: string;
  badges: Array<{ label: string; tone: CandidateDecisionTone }>;
  metrics: string[];
  evidence: Array<{ label: string; detail: string; tone: CandidateDecisionTone }>;
  risks: string[];
  note: string;
};

type CandidatePoolTableProps = {
  rows: CandidatePoolRow[];
  selectedId?: string;
  onSelect: (row: CandidatePoolRow) => void;
  toolbar?: React.ReactNode;
};

const actionStyles: Record<CandidateDecisionTone, string> = {
  success: 'border-success/25 bg-success/10 text-success',
  info: 'border-cyan/25 bg-cyan/10 text-cyan',
  warning: 'border-warning/25 bg-warning/10 text-warning',
  danger: 'border-danger/25 bg-danger/10 text-danger',
  history: 'border-border/60 bg-surface-2 text-secondary-text',
  default: 'border-border/60 bg-surface-2 text-secondary-text',
};

const lifecycleStyles: Record<string, string> = {
  new: 'border-cyan/25 bg-cyan/10 text-cyan',
  active: 'border-success/25 bg-success/10 text-success',
  watching: 'border-warning/25 bg-warning/10 text-warning',
  decayed: 'border-warning/25 bg-warning/5 text-warning',
  removed: 'border-danger/25 bg-danger/5 text-danger',
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

const scoreWidth = (value?: number | null): string => {
  if (value == null) return '0%';
  return Math.max(0, Math.min(100, Number(value))) + '%';
};

const unique = (values: string[]): string[] => Array.from(new Set(values.filter(Boolean)));

export const CandidatePoolTable: React.FC<CandidatePoolTableProps> = ({ rows, selectedId, onSelect, toolbar }) => {
  return (
    <section className="overflow-hidden rounded-xl border border-border/60 bg-card/80 shadow-sm shadow-black/10">
      <div className="flex flex-col gap-2 border-b border-border/60 bg-surface-2/35 px-4 py-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Target className="h-4 w-4 text-cyan" />
            <h2 className="text-base font-semibold text-foreground">候选入池榜</h2>
          </div>
          <p className="mt-1 max-w-3xl text-xs leading-5 text-secondary-text">
            按动作建议、信号评分和证据质量排序；点击任意一行查看证据、指标与风险。
          </p>
        </div>
        <div className="inline-flex h-7 items-center rounded-md border border-border/60 bg-card px-2.5 text-xs font-medium text-secondary-text">
          {rows.length} 只
        </div>
      </div>

      {toolbar ? <div className="border-b border-border/60 px-4 py-2.5">{toolbar}</div> : null}

      <div className="max-h-[calc(100vh-360px)] overflow-auto">
        <table className="w-full min-w-[1040px] border-collapse text-left">
          <thead className="sticky top-0 z-10 bg-surface-2/95 backdrop-blur">
            <tr className="border-b border-border/60 text-[10px] uppercase tracking-[0.12em] text-muted-text">
              <th className="w-[190px] px-4 py-2.5 font-medium">代码 / 名称</th>
              <th className="w-[104px] px-2 py-2.5 font-medium">动作</th>
              <th className="w-[122px] px-2 py-2.5 font-medium">评分</th>
              <th className="w-[210px] px-2 py-2.5 font-medium">来源 / 专家</th>
              <th className="px-2 py-2.5 font-medium">入池理由</th>
              <th className="w-[92px] px-2 py-2.5 font-medium">生命周期</th>
              <th className="w-[132px] px-2 py-2.5 font-medium">出现 / 有效期</th>
              <th className="w-[34px] px-2 py-2.5" />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => {
              const sources = unique([...row.experts, ...row.sources]);
              const visibleSources = sources.slice(0, 2);
              const hiddenSources = Math.max(0, sources.length - visibleSources.length);
              const isSelected = selectedId === row.id;
              return (
                <tr
                  key={row.id}
                  className={cn(
                    'cursor-pointer border-b border-border/40 transition-colors hover:bg-hover/45',
                    isSelected && 'bg-cyan/5',
                  )}
                  onClick={() => onSelect(row)}
                >
                  <td className="px-4 py-3 align-top">
                    <div className="flex flex-col gap-0.5">
                      <span className="font-mono text-[11px] font-medium text-muted-text">{row.code}</span>
                      <span className="text-sm font-medium text-foreground">{row.name || row.code}</span>
                      <span className="text-[10px] text-muted-text">{row.market || 'A 股'}</span>
                    </div>
                  </td>
                  <td className="px-2 py-3 align-top">
                    <span className={cn('inline-flex items-center whitespace-nowrap rounded-md border px-2 py-0.5 text-[11px] font-semibold', actionStyles[row.action.tone])}>
                      {row.action.label}
                    </span>
                    {row.action.strength ? <div className="mt-1 text-[10px] text-muted-text">强度 {row.action.strength}</div> : null}
                  </td>
                  <td className="px-2 py-3 align-top">
                    <div className="flex items-baseline gap-1.5">
                      <span className={cn('font-mono text-base font-semibold tabular-nums', scoreColor(row.score))}>{formatScore(row.score)}</span>
                      <span className="text-[10px] text-muted-text">分</span>
                    </div>
                    <div className="mt-1 h-1 w-14 overflow-hidden rounded-full bg-border/50">
                      <div className="h-full rounded-full bg-cyan/80" style={{ width: scoreWidth(row.score) }} />
                    </div>
                  </td>
                  <td className="px-2 py-3 align-top">
                    <div className="flex max-w-[200px] flex-wrap gap-1">
                      {visibleSources.map((source) => (
                        <span key={source} className="inline-flex max-w-full items-center truncate rounded border border-border/60 bg-surface-2/70 px-1.5 py-0.5 text-[10px] text-secondary-text" title={source}>
                          {source}
                        </span>
                      ))}
                      {hiddenSources ? <span className="rounded border border-border/60 bg-surface-2/70 px-1.5 py-0.5 text-[10px] text-muted-text">+{hiddenSources}</span> : null}
                      {!sources.length ? <span className="text-[10px] text-muted-text">--</span> : null}
                    </div>
                    {row.dimensions.length ? <div className="mt-1 text-[10px] text-muted-text">{row.dimensions.slice(0, 3).join(' · ')}</div> : null}
                  </td>
                  <td className="px-2 py-3 align-top">
                    <p className="line-clamp-2 max-w-[360px] text-xs leading-5 text-secondary-text" title={row.reason}>{row.reason || '暂无入池理由'}</p>
                    {row.badges.length ? (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {row.badges.slice(0, 3).map((badge) => (
                          <span key={badge.label} className={cn('rounded border px-1.5 py-0.5 text-[9px]', actionStyles[badge.tone])}>{badge.label}</span>
                        ))}
                      </div>
                    ) : null}
                  </td>
                  <td className="px-2 py-3 align-top">
                    <span className={cn('inline-flex whitespace-nowrap rounded border px-1.5 py-0.5 text-[10px]', lifecycleStyles[row.lifecycle] || lifecycleStyles.new)}>
                      {row.lifecycle}
                    </span>
                  </td>
                  <td className="px-2 py-3 align-top text-[10px] leading-5 text-muted-text">
                    <div>{row.occurrence}</div>
                    <div className="truncate" title={row.validUntil || ''}>{row.validUntil ? '有效至 ' + row.validUntil : '--'}</div>
                  </td>
                  <td className="px-2 py-3 align-middle text-muted-text">
                    <ChevronRight className="h-4 w-4" />
                  </td>
                </tr>
              );
            })}
            {!rows.length ? (
              <tr>
                <td colSpan={8} className="px-4 py-14 text-center text-sm text-muted-text">
                  当前筛选下没有候选，试试切换筛选或清空搜索。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
      <div className="flex items-center justify-between border-t border-border/60 px-4 py-2.5 text-[10px] text-muted-text">
        <span>评分仅用于候选池内排序，不代表买卖裁决</span>
        <span>点击行查看证据</span>
      </div>
    </section>
  );
};
