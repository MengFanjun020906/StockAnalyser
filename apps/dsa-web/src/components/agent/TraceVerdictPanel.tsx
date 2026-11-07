import React from 'react';
import { Activity, AlertTriangle, CheckCircle2, Database, Download, ShieldAlert, Target } from 'lucide-react';
import type { AgentTraceRunResponse } from '../../api/agent';
import { cn } from '../../utils/cn';

type TraceStatus = 'idle' | 'running' | 'done' | 'error';

type TraceVerdictPanelProps = {
  result: AgentTraceRunResponse;
  traceStatus: TraceStatus;
  planner: Record<string, unknown> | null;
  debate: Record<string, unknown> | null;
  compositeVerdict: Record<string, unknown> | null;
  riskPayload: Record<string, unknown> | null;
  llmTelemetry: Record<string, unknown> | null;
  l1Narrative: string;
  decisionNarrative: string;
  riskNarrative: string;
  failedToolCount: number;
  routeLabel?: string;
  onExport?: () => void;
};

const asRecord = (value: unknown): Record<string, unknown> | null => (
  value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
);

const toStringList = (value: unknown): string[] => (
  Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []
);

const toFiniteNumber = (value: unknown): number | null => {
  const num = Number(value);
  return Number.isFinite(num) ? num : null;
};

const formatCount = (value: unknown): string => {
  const num = Number(value);
  if (!Number.isFinite(num)) return '--';
  return num.toLocaleString(undefined, { maximumFractionDigits: 0 });
};

const ACTION_LABELS: Record<string, string> = {
  buy: '买入',
  add: '加仓',
  hold: '持有',
  reduce: '减仓',
  sell: '卖出',
  wait: '等待',
  monitor: '观察',
  reject: '回避',
  avoid: '回避',
  insufficient: '证据不足',
  insufficient_data: '证据不足',
};

const actionLabel = (value: string): string => {
  if (!value) return '--';
  const key = value.trim().toLowerCase();
  return ACTION_LABELS[key] ? ACTION_LABELS[key] + ' · ' + value : value;
};

const statusStyles: Record<string, string> = {
  success: 'border-success/25 bg-success/10 text-success',
  error: 'border-danger/25 bg-danger/10 text-danger',
  running: 'border-warning/25 bg-warning/10 text-warning',
  default: 'border-border/60 bg-surface-2 text-secondary-text',
};

export const TraceVerdictPanel: React.FC<TraceVerdictPanelProps> = ({
  result,
  traceStatus,
  planner,
  debate,
  compositeVerdict,
  riskPayload,
  llmTelemetry,
  l1Narrative,
  decisionNarrative,
  riskNarrative,
  failedToolCount,
  routeLabel,
  onExport,
}) => {
  const judge = asRecord(debate?.judge_decision) || {};
  const gate = asRecord(riskPayload?.risk_gate) || {};
  const composite = asRecord(compositeVerdict?.composite_verdict) || {};
  const quality = asRecord(result.report_quality) || {};
  const tradePlan = asRecord(riskPayload?.trade_plan) || {};
  const planAction = String(tradePlan.action || '');
  const reviewTriggers = toStringList(tradePlan.review_triggers);
  const entryLow = toFiniteNumber(tradePlan.entry_zone_low);
  const entryHigh = toFiniteNumber(tradePlan.entry_zone_high);
  const stopLoss = toFiniteNumber(tradePlan.stop_loss_price);
  const takeProfit = toFiniteNumber(tradePlan.take_profit_price);
  const targetPosition = toFiniteNumber(tradePlan.target_position_pct);
  const fmtPlanNumber = (value: number | null): string => (value != null ? value.toFixed(2) : '--');

  const action = String(judge.final_action || gate.allowed_action || (result.success ? 'done' : '--'));
  const actionKey = action.trim().toLowerCase();
  const actionTone = ['buy', 'add'].includes(actionKey)
    ? 'success'
    : ['reduce', 'sell', 'reject', 'avoid'].includes(actionKey)
      ? 'danger'
      : 'info';
  const gateStatus = String(gate.status || (riskPayload?.status === 'failed' ? 'failed' : '--'));
  const blockedReasons = toStringList(gate.blocked_reasons);
  const riskControls = toStringList(judge.risk_controls);
  const failedChecks = toFiniteNumber(gate.failed_check_count);
  const score = toFiniteNumber(composite.score);
  const scaleMax = toFiniteNumber(composite.scale_max);
  const totalCalls = toFiniteNumber(llmTelemetry?.total_calls);
  const totalTokens = toFiniteNumber(llmTelemetry?.total_tokens);
  const statusKey = traceStatus === 'done' && result.success
    ? 'success'
    : traceStatus === 'error' || result.success === false
      ? 'error'
      : traceStatus === 'running'
        ? 'running'
        : 'default';
  const statusText = traceStatus === 'running' ? '运行中' : traceStatus === 'error' || result.success === false ? '执行失败' : traceStatus === 'done' ? '执行成功' : '待运行';
  const riskItems = [
    ...blockedReasons,
    ...riskControls,
    ...(failedToolCount > 0 ? [failedToolCount + ' 个工具调用失败'] : []),
    ...(failedChecks && failedChecks > 0 ? [failedChecks + ' 条风控检查未通过'] : []),
  ];

  return (
    <aside className="overflow-y-auto rounded-xl border border-border/60 bg-card/80 shadow-sm shadow-black/10 xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)]">
      <div className="flex items-center justify-between border-b border-border/60 px-3 py-2.5">
        <div className="flex items-center gap-2">
          <Target className="h-4 w-4 text-cyan" />
          <span className="text-xs font-medium text-foreground">Agent 结论</span>
        </div>
        <span className={cn('inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[9px]', statusStyles[statusKey])}>
          {statusKey === 'success' ? <CheckCircle2 className="h-2.5 w-2.5" /> : null}
          {statusKey === 'error' ? <AlertTriangle className="h-2.5 w-2.5" /> : null}
          {statusText}
        </span>
      </div>
      {routeLabel ? (
        <div className="border-b border-border/60 px-3 py-1.5">
          <span className="inline-flex rounded border border-border/60 bg-surface-2 px-1.5 py-0.5 text-[9px] text-secondary-text">{routeLabel}</span>
        </div>
      ) : null}

      <div className="space-y-3 p-3">
        <section className={cn(
          'rounded-lg border p-3',
          actionTone === 'success' && 'border-success/25 bg-success/5',
          actionTone === 'danger' && 'border-danger/25 bg-danger/5',
          actionTone === 'info' && 'border-cyan/20 bg-cyan/5',
        )}>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <div className="text-[10px] uppercase tracking-[0.1em] text-muted-text">Judge 动作</div>
              <div className={cn(
                'mt-1 text-base font-semibold',
                actionTone === 'success' && 'text-success',
                actionTone === 'danger' && 'text-danger',
                actionTone === 'info' && 'text-cyan',
              )}>{actionLabel(action)}</div>
            </div>
            {score != null ? (
              <div className="text-right">
                <div className="font-mono text-xl font-semibold text-foreground tabular-nums">{score.toFixed(1)}</div>
                <div className="text-[9px] text-muted-text">S4 / {scaleMax != null ? scaleMax.toFixed(0) : '--'}</div>
              </div>
            ) : null}
          </div>
          <p className="mt-2 text-[11px] leading-5 text-secondary-text">{decisionNarrative}</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <span className="rounded border border-border/60 bg-card/60 px-1.5 py-0.5 text-[9px] text-secondary-text">
              风控 {gateStatus}
            </span>
            {planner?.intent ? (
              <span className="rounded border border-border/60 bg-card/60 px-1.5 py-0.5 text-[9px] text-secondary-text">
                意图 {String(planner.intent)}
              </span>
            ) : null}
          </div>
        </section>

        <section className="grid grid-cols-2 gap-1.5">
          <div className="rounded-md border border-border/50 bg-surface-2/30 px-2 py-1.5">
            <div className="text-[9px] text-muted-text">步骤</div>
            <div className="mt-0.5 font-mono text-xs text-foreground">{formatCount(result.total_steps)}</div>
          </div>
          <div className="rounded-md border border-border/50 bg-surface-2/30 px-2 py-1.5">
            <div className="text-[9px] text-muted-text">Token</div>
            <div className="mt-0.5 font-mono text-xs text-foreground">{formatCount(result.total_tokens)}</div>
          </div>
          <div className="rounded-md border border-border/50 bg-surface-2/30 px-2 py-1.5">
            <div className="text-[9px] text-muted-text">工具调用</div>
            <div className="mt-0.5 font-mono text-xs text-foreground">{result.tool_calls.length}</div>
          </div>
          <div className="rounded-md border border-border/50 bg-surface-2/30 px-2 py-1.5">
            <div className="text-[9px] text-muted-text">失败</div>
            <div className={cn('mt-0.5 font-mono text-xs', failedToolCount ? 'text-danger' : 'text-success')}>{failedToolCount}</div>
          </div>
        </section>

        <section>
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.08em] text-muted-text">
            <Target className="h-3 w-3 text-cyan" />
            执行方案
          </div>
          <div className="rounded border border-border/50 bg-surface-2/30 p-2.5">
            {planAction ? (
              <>
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[10px] text-muted-text">当前动作</span>
                  <span className={cn('text-[11px] font-medium', actionTone === 'danger' ? 'text-danger' : actionTone === 'success' ? 'text-success' : 'text-cyan')}>{actionLabel(planAction)}</span>
                </div>
                {planAction === 'wait' || planAction === 'monitor' ? (
                  <p className="mt-1.5 text-[10px] leading-5 text-secondary-text">当前没有有效入场区，等待触发条件；不要在观察阶段追价。</p>
                ) : (
                  <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-1 text-[10px] text-secondary-text">
                    <div className="flex justify-between gap-1"><span className="text-muted-text">入场区</span><span className="font-mono">{fmtPlanNumber(entryLow)} – {fmtPlanNumber(entryHigh)}</span></div>
                    <div className="flex justify-between gap-1"><span className="text-muted-text">止损</span><span className="font-mono">{fmtPlanNumber(stopLoss)}</span></div>
                    <div className="flex justify-between gap-1"><span className="text-muted-text">目标</span><span className="font-mono">{fmtPlanNumber(takeProfit)}</span></div>
                    <div className="flex justify-between gap-1"><span className="text-muted-text">仓位</span><span className="font-mono">{targetPosition != null ? targetPosition.toFixed(1) + '%' : '--'}</span></div>
                  </div>
                )}
                {reviewTriggers.length ? (
                  <div className="mt-2 space-y-0.5 border-t border-border/40 pt-1.5">
                    {reviewTriggers.slice(0, 2).map((trigger, index) => (
                      <div key={trigger + index} className="text-[9px] leading-4 text-muted-text">· {trigger}</div>
                    ))}
                  </div>
                ) : null}
              </>
            ) : (
              <p className="text-[10px] text-muted-text">尚未生成执行方案。</p>
            )}
          </div>
        </section>

        <section>
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.08em] text-muted-text">
            <ShieldAlert className="h-3 w-3 text-warning" />
            风险与限制
          </div>
          <div className="space-y-1">
            {riskItems.length ? riskItems.slice(0, 5).map((item, index) => (
              <div key={item + index} className="flex items-start gap-1.5 rounded border border-warning/15 bg-warning/5 px-2 py-1.5 text-[10px] leading-4 text-warning">
                <AlertTriangle className="mt-0.5 h-2.5 w-2.5 shrink-0" />
                <span>{item}</span>
              </div>
            )) : (
              <div className="rounded border border-border/50 bg-surface-2/30 px-2 py-1.5 text-[10px] text-muted-text">未发现硬阻断，仍需结合仓位约束。</div>
            )}
          </div>
        </section>

        <section>
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.08em] text-muted-text">
            <Activity className="h-3 w-3 text-cyan" />
            证据账本
          </div>
          <div className="divide-y divide-border/40 rounded border border-border/50 bg-surface-2/20">
            <div className="flex items-start gap-2 px-2 py-1.5">
              <span className="mt-0.5 font-mono text-[9px] text-cyan">L1</span>
              <span className="min-w-0 flex-1 text-[10px] leading-4 text-secondary-text line-clamp-2">{l1Narrative}</span>
            </div>
            <div className="flex items-start gap-2 px-2 py-1.5">
              <span className="mt-0.5 font-mono text-[9px] text-cyan">L2</span>
              <span className="min-w-0 flex-1 text-[10px] leading-4 text-secondary-text">工具 {result.tool_calls.length} 次，失败 {failedToolCount} 次</span>
            </div>
            <div className="flex items-start gap-2 px-2 py-1.5">
              <span className="mt-0.5 font-mono text-[9px] text-cyan">L4</span>
              <span className="min-w-0 flex-1 text-[10px] leading-4 text-secondary-text">Judge {actionLabel(action)}</span>
            </div>
            <div className="flex items-start gap-2 px-2 py-1.5">
              <span className="mt-0.5 font-mono text-[9px] text-cyan">S4</span>
              <span className="min-w-0 flex-1 text-[10px] leading-4 text-secondary-text">{score != null ? '评分 ' + score.toFixed(1) + ' / ' + (scaleMax != null ? scaleMax.toFixed(0) : '--') : '评分尚未生成'}</span>
            </div>
            <div className="flex items-start gap-2 px-2 py-1.5">
              <span className="mt-0.5 font-mono text-[9px] text-cyan">L5</span>
              <span className="min-w-0 flex-1 text-[10px] leading-4 text-secondary-text">{riskNarrative}</span>
            </div>
          </div>
        </section>

        <section className="rounded-md border border-border/50 bg-surface-2/30 p-2.5">
          <div className="mb-1 flex items-center gap-1.5 text-[10px] uppercase tracking-[0.08em] text-muted-text">
            <Database className="h-3 w-3" />
            模型与数据
          </div>
          <div className="space-y-1 text-[10px] text-secondary-text">
            <div className="flex justify-between gap-2"><span>模型</span><span className="truncate text-right">{result.provider || '--'} / {result.model || '--'}</span></div>
            <div className="flex justify-between gap-2"><span>模式</span><span className="truncate text-right">{result.mode || '--'}</span></div>
            <div className="flex justify-between gap-2"><span>LLM 用量</span><span>{totalCalls != null ? formatCount(totalCalls) : '--'} 次 / {totalTokens != null ? formatCount(totalTokens) : '--'} tok</span></div>
            <div className="flex justify-between gap-2"><span>报告质量</span><span className="truncate text-right">{String(quality.status || '--')}</span></div>
            <div className="flex justify-between gap-2"><span>Session</span><span className="truncate text-right font-mono text-[9px]" title={result.session_id}>{result.session_id ? '…' + result.session_id.slice(-8) : '--'}</span></div>
          </div>
        </section>

        {onExport && result.content ? (
          <button
            type="button"
            onClick={onExport}
            className="inline-flex h-8 w-full items-center justify-center gap-1.5 rounded-md border border-border/60 bg-surface-2 px-2 text-[10px] font-medium text-secondary-text transition hover:bg-hover hover:text-foreground"
            aria-label="导出 MD"
          >
            <Download className="h-3 w-3" />
            导出 MD
          </button>
        ) : null}
      </div>
    </aside>
  );
};
