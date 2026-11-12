import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Activity, AlertTriangle, CalendarClock, CandlestickChart, ChevronRight, Database, Info, RefreshCw, ShieldCheck } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import {
  signalPreviewApi,
  type EntrySignal,
  type PricePlanSummary,
  type PositionWatch,
  type SignalPreviewDetail,
  type SignalEvaluationSummary,
  type SignalPreviewList,
} from '../api/signalPreview';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, Badge, EmptyState, Tooltip } from '../components/common';
import { cn } from '../utils/cn';
import { buildSignalPreviewChartOption } from './signalPreviewChartOption';
import { entryCurrentAction, entryDisplayState, entryPlanMode, entryTriggerAction } from './signalPreviewPresentation';

type PreviewMode = 'entry' | 'position';

const STATUS_TEXT: Record<string, string> = {
  active: '生效中',
  waiting_trigger: '等待触发',
  observation_only: '仅观察',
  risk_blocked: '风险门阻断',
  proposed: '待确认',
  price_triggered: '已触价',
  needs_review: '待复核',
  ambiguous: '路径不明',
  filled: '已成交',
  expired: '已到期',
  invalidated: '已失效',
  superseded: '已替代',
  cancelled: '已取消',
  closed: '持仓结束',
  hold: '持有',
  reduce: '减仓',
  exit: '退出',
  low: '低风险',
  medium: '中风险',
  high: '高风险',
  critical: '严重',
  unknown: '待确认',
  generated: '已生成',
  observation: '观察计划',
  no_plan: '无有效计划',
  waiting_touch: '等待触价',
  touched: '已触价',
  confirmed: '技术确认',
  review_pending: '等待证据评审',
  waiting_confirmation: '等待技术确认',
  observe_only: '仅观察',
  actionable: '可按计划执行',
  vetoed: '硬否决',
  system_unready: '系统未就绪',
  system_disabled: '系统已停用',
};

const REVIEW_TEXT: Record<string, string> = {
  support: '支持',
  mixed: '证据混合',
  insufficient: '证据不足',
  opposing: '反对',
  hard_veto: '硬否决',
};

const REVIEW_PERMISSION_TEXT: Record<string, string> = {
  allowed: '允许',
  conditional: '附加确认后允许',
  observe_only: '仅观察',
  vetoed: '禁止',
};

const RISK_PROFILE_TEXT: Record<string, string> = {
  aggressive: '进取型',
  balanced: '均衡型',
  conservative: '稳健型',
};

const PLAN_REASON_TEXT: Record<string, string> = {
  REVIEW_OBSERVE_ONLY: '模型证据仅允许观察',
  REVIEW_HARD_VETO: '模型证据触发硬否决',
  TECHNICAL_CONFIRMATION_PENDING: '技术确认尚未完成',
  EXTRA_CONFIRMATION_PENDING: '附加确认尚未完成',
  POSITION_SIZING_UNAVAILABLE: '确定性仓位上限暂不可用',
  ACCOUNT_CONTEXT_ABSENT_REFERENCE_COST: '未绑定账户，采用参考成本',
  ACCOUNT_COST_POLICY_UNAVAILABLE: '账户费率或资金字段不完整',
  ACCOUNT_ORDER_SIZE_UNAVAILABLE: '账户资金不足一手或无法计算数量',
  ACCOUNT_NET_RR_UNAVAILABLE: '账户净盈亏比暂不可计算',
  ACCOUNT_NET_RR_BELOW_MINIMUM: '账户净盈亏比未达门槛',
  RISK_GATE_PENDING: '等待风险门检查',
  RISK_GATE_BLOCKED: '风险门不允许执行',
  NO_SELECTABLE_SCENARIO: '没有可选择的正式场景',
};

const EVENT_TEXT: Record<string, string> = {
  signal_created: '信号创建',
  signal_revised: '信号修订',
  target_drift_rejected: '拒绝目标漂移',
  signal_checked: '信号复核',
  signal_needs_review: '证据不足，进入复核',
  signal_reactivated: '证据恢复，重新生效',
  entry_price_triggered: '价格进入计划区',
  signal_invalidated: '信号失效',
  signal_expired: '信号到期',
  signal_ambiguous: '日线路径不明',
  signal_superseded: '被新计划替代',
  position_opened: '纳入持仓监控',
  position_changed: '持仓数量或成本变化',
  position_checked: '每日确定性检查',
  position_risk_changed: '持仓风险变化',
  position_closed: '持仓周期结束',
  plan_created: '价格计划创建',
  touched: '价格进入场景区间',
  technical_confirmed: '技术确认完成',
  invalidated: '场景失效',
  expired: '场景到期',
  ambiguous: '日线路径不明',
  superseded: '被新价格计划替代',
};

const VERDICT_TEXT: Record<string, string> = {
  strong_confirmed: '强确认',
  buy_valid: '有效买入',
  buy_watch: '条件观察',
  buy_transition: '趋势过渡',
  observe_or_exit: '观察或退出',
  not_evaluable: '暂不可评估',
};

const PERMISSION_TEXT: Record<string, string> = {
  allowed: '允许入场',
  conditional: '条件入场',
  blocked: '暂不入场',
  not_evaluable: '暂不可评估',
};

const FORMALIZATION_REASON_TEXT: Record<string, string> = {
  S4_WEEKLY_BAR_UNCONFIRMED: '本周周线尚未收盘，策略 4 与综合分当前为临时分；日线指标与已确认的缠论信号仍然有效。',
  S4_FEATURES_INCOMPLETE: '策略 4 必需特征不完整，综合分暂不成立。',
  S4_NOT_IMPLEMENTED: '策略 4 指标尚未实现，综合分暂不成立。',
  CHAN_EVIDENCE_INSUFFICIENT: '缠论结构证据不足，缠论分暂不计入。',
};

const ACTION_TEXT: Record<string, string> = {
  none: '无',
  hold: '持有',
  add: '加仓',
  reduce: '减仓',
  exit: '退出',
  sell: '卖出',
  manual_review: '人工复核',
  open: '开仓',
  wait: '等待',
  reject: '暂不入场',
};

const SOURCE_TEXT: Record<string, string> = {
  local_db: '本地数据库',
  LocalDB: '本地数据库',
  Baostock: 'Baostock',
  TushareFetcher: 'Tushare',
  Akshare: 'Akshare',
  quote_provider: '行情源',
  lazy_reconcile: '信号补算',
  single_stock_query: '单股分析',
  portfolio_scheduler: '持仓定时任务',
  deterministic_price_plan: '确定性价格计划',
};

const ZONE_ROLE_TEXT: Record<string, string> = {
  primary: '主入场区',
  secondary: '次入场区',
  observation: '观察区',
};

const ZONE_SWATCH: Record<string, string> = {
  primary: 'border-sky-400 bg-sky-400/40',
  secondary: 'border-teal-300 bg-teal-400/35',
  observation: 'border-slate-300 bg-slate-400/25',
};

const ENTRY_ZONE_TEXT: Record<string, string> = {
  ideal: '理想入场',
  secondary: '次优入场',
  entry: '计划入场',
};

const SCENARIO_ROLE_TEXT: Record<string, string> = {
  primary: '主场景',
  secondary: '次场景',
  observation: '观察场景',
};

const SETUP_TEXT: Record<string, string> = {
  agent_entry: '智能体入场',
  chan_first_buy: '缠论一买',
  chan_second_buy: '缠论二买',
  chan_third_buy: '缠论三买',
  center_breakout: '中枢向上突破',
  trend_pullback: '趋势回调',
  breakout: '价格突破',
  spot_entry: '现价入场',
  unclassified: '未分类',
};

function fmt(value?: number | null, digits = 2): string {
  return value == null || !Number.isFinite(Number(value)) ? '--' : Number(value).toFixed(digits);
}

function fmtPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%`;
}

function shortDate(value?: string | null): string {
  return value ? value.slice(0, 10) : '--';
}

function statusTone(value?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (['active', 'price_triggered', 'low', 'hold', 'actionable'].includes(value || '')) return 'success';
  if (['needs_review', 'ambiguous', 'medium', 'reduce', 'proposed', 'waiting_trigger', 'observation_only', 'review_pending', 'waiting_confirmation', 'observe_only'].includes(value || '')) return 'warning';
  if (['invalidated', 'critical', 'high', 'exit', 'risk_blocked', 'vetoed', 'system_unready', 'system_disabled'].includes(value || '')) return 'danger';
  return 'default';
}

const SignalChart: React.FC<{ detail: SignalPreviewDetail | null }> = ({ detail }) => {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current || !detail?.chart.bars.length) return undefined;
    const chart = echarts.init(ref.current);
    // 窄屏塞不下 180 根 K 线，默认窗口收到 60 根，同时隐藏价格线标签。
    const compact = ref.current.getBoundingClientRect().width < 520;
    chart.setOption(
      buildSignalPreviewChartOption(detail, { compact, visibleBars: compact ? 60 : 180 }),
      { notMerge: true },
    );
    let resizeFrame = 0;
    const resize = () => {
      window.cancelAnimationFrame(resizeFrame);
      resizeFrame = window.requestAnimationFrame(() => {
        const element = ref.current;
        if (!element) return;
        chart.resize({
          width: Math.floor(element.getBoundingClientRect().width),
          height: element.clientHeight,
        });
      });
    };
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
    observer?.observe(ref.current);
    window.addEventListener('resize', resize);
    window.visualViewport?.addEventListener('resize', resize);
    resize();
    return () => {
      window.cancelAnimationFrame(resizeFrame);
      observer?.disconnect();
      window.removeEventListener('resize', resize);
      window.visualViewport?.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [detail]);

  if (!detail?.chart.bars.length) {
    return (
      <EmptyState
        title="暂无本地 K 线"
        description="当前标的在本地数据库中没有可展示的日线数据。"
        icon={<CandlestickChart className="h-7 w-7" />}
        className="min-h-[420px]"
      />
    );
  }
  return <div ref={ref} data-testid="signal-preview-chart" className="h-[420px] min-h-[360px] w-full min-w-0 max-w-full overflow-hidden [&>div]:!w-full [&_canvas]:!w-full sm:h-[560px] xl:h-[680px]" />;
};

const ModeTabs: React.FC<{ mode: PreviewMode; data: SignalPreviewList | null; onChange: (mode: PreviewMode) => void }> = ({ mode, data, onChange }) => (
  <div className="inline-flex h-10 border border-border/70 bg-card/70 p-1" role="tablist" aria-label="信号类型">
    <button
      type="button"
      role="tab"
      aria-selected={mode === 'entry'}
      onClick={() => onChange('entry')}
      className={cn('min-w-[112px] px-3 text-sm font-medium transition-colors', mode === 'entry' ? 'bg-foreground text-background' : 'text-secondary-text hover:text-foreground')}
    >
    个股计划 {data?.counts.plans ?? data?.counts.entry ?? 0}
    </button>
    <button
      type="button"
      role="tab"
      aria-selected={mode === 'position'}
      onClick={() => onChange('position')}
      className={cn('min-w-[112px] px-3 text-sm font-medium transition-colors', mode === 'position' ? 'bg-foreground text-background' : 'text-secondary-text hover:text-foreground')}
    >
      持仓监控 {data?.counts.positions ?? 0}
    </button>
  </div>
);

type SubjectListProps = {
  mode: PreviewMode;
  data: SignalPreviewList | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
};

const SubjectList: React.FC<SubjectListProps> = ({ mode, data, selectedId, onSelect }) => {
  const legacySignals = (data?.entrySignals ?? []).filter((item) => !item.planId);
  const rows = mode === 'entry' ? [...(data?.pricePlans ?? []), ...legacySignals] : data?.positionWatches ?? [];
  // 列表为空时不再渲染第二个空态，右侧主区域已有一次统一说明。
  if (!rows.length) return null;
  return (
    <div className="divide-y divide-border/60 border-y border-border/60">
      {rows.map((row) => {
        const isEntry = mode === 'entry';
        const plan = row as PricePlanSummary;
        const signal = row as EntrySignal;
        const watch = row as PositionWatch;
        const isPlan = isEntry && Boolean(plan.planId);
        const id = isEntry ? (isPlan ? plan.planId : signal.signalId) : watch.watchId;
        const state = isEntry ? (isPlan ? plan.generationStatus : entryDisplayState(signal)) : watch.currentAction;
        return (
          <button
            type="button"
            key={id}
            onClick={() => onSelect(id)}
            className={cn(
              'group flex min-h-[88px] w-full items-center gap-3 px-3 py-3 text-left transition-colors',
              selectedId === id ? 'bg-[hsl(var(--primary)/0.10)]' : 'hover:bg-hover/70',
            )}
          >
            <span className={cn('h-10 w-1 shrink-0', selectedId === id ? 'bg-cyan' : 'bg-border')} />
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-2">
                <span className="truncate text-sm font-semibold text-foreground">{row.stockName || row.stockCode}</span>
                <Badge variant={statusTone(state)}>{STATUS_TEXT[state] || state}</Badge>
              </span>
              <span className="mt-1 block text-xs tabular-nums text-secondary-text">{row.stockCode} · {row.market.toUpperCase()}</span>
              <span className="mt-1 block truncate text-xs text-secondary-text">
                {isEntry
                  ? isPlan
                    ? `${plan.scenarioCount} 个场景 · ${fmt(plan.entryLow)}-${fmt(plan.entryHigh)} · ${shortDate(plan.actualAsOfSession)}`
                    : `${entryPlanMode(signal) === 'observation_only' || entryPlanMode(signal) === 'risk_blocked' ? '观察区' : '入场'} ${fmt(signal.entryLow)}-${fmt(signal.entryHigh)} · 至 ${shortDate(signal.validUntil)}`
                  : `成本 ${fmt(watch.averageCost)} · 评分 ${watch.latestScore ?? '--'}/20`}
              </span>
            </span>
            <ChevronRight className="h-4 w-4 shrink-0 text-secondary-text transition-transform group-hover:translate-x-0.5" />
          </button>
        );
      })}
    </div>
  );
};

const S4_HINT = (
  <span className="block">
    <span className="block font-semibold text-foreground">策略 4 · strategy4.v1（满分 10）</span>
    <span className="mt-1 block">区域 2：EMA13 指导线 &gt; MA57，且 MA57 上升</span>
    <span className="block">多周期共振 2：日线多/强、周线多/强四项中 ≥ 3 项</span>
    <span className="block">量比 2：当日成交量 ÷ 5 日均量 ≥ 1.2</span>
    <span className="block">波动率 2：ATR14 ÷ MA20 ≤ 6%</span>
    <span className="block">近 10 日 1：涨幅 ≤ 9%（防追高）</span>
    <span className="block">金叉 1：当日 DIF 上穿 DEA，且其余条件全部满足</span>
    <span className="mt-1 block text-secondary-text/80">每项满足阈值才得分；周线未收盘时只出临时分。</span>
  </span>
);

const METRIC_HINTS: Record<string, React.ReactNode> = {
  '综合评分': '策略 4 分 + 缠论分，满分 20。正式分被阻塞时（如本周周线未收盘）这里显示后端临时分。',
  '策略 4': S4_HINT,
  '缠论': '按已触发信号加减：一买 +5、二买 +4、三买 +2、底钝化 +2、周线确认 +1；顶背驰 -3、顶钝化 -2，上下限 0–10 分。未收盘的周线信号暂不计分。',
  '多周期共振': '日线多头、日线强势、周线多头、周线强势四项中满足的数量；≥ 3 项才拿满策略 4 的共振分。',
  '量比': '当日成交量 ÷ 5 日均量。≥ 1.2 记满策略 4 的量比分。',
  '波动率': 'ATR14 ÷ MA20 × 100。≤ 6% 记满策略 4 的波动率分。',
  '近 10 日': '最近 10 个交易日涨幅。≤ 9% 记满 1 分（防止追高）。',
  '金叉 / 全满足': '金叉：当日 DIF 上穿 DEA。全满足：区域、共振、量比、波动率、近 10 日五项条件同时达标。',
};

type MetricItem = { label: string; value: string; tag?: string; hint?: React.ReactNode };

const MetricsStrip: React.FC<{ detail: SignalPreviewDetail }> = ({ detail }) => {
  const composite = detail.chart.composite;
  const verdict = composite?.compositeVerdict;
  const s4 = composite?.snapshot?.s4;
  const scaleMax = verdict?.scaleMax ?? 20;
  const s4Score = verdict?.components?.s4?.score;
  const s4Provisional = s4Score == null ? verdict?.components?.s4?.provisionalScore ?? null : null;
  const chanScore = verdict?.components?.chan?.score;
  // 正式分被阻塞（例如周线未收盘）时，后端会给出临时分；不显示会让整条指标看起来像坏了。
  const provisional = verdict?.score == null ? verdict?.provisionalScore ?? null : null;
  const metrics: MetricItem[] = [
    {
      label: '综合评分',
      value: verdict?.score != null ? `${verdict.score} / ${scaleMax}` : provisional != null ? `${provisional} / ${scaleMax}` : `-- / ${scaleMax}`,
      tag: provisional != null ? '临时' : undefined,
      hint: METRIC_HINTS['综合评分'],
    },
    {
      label: '策略 4',
      value: s4Score != null ? `${s4Score} / 10` : s4Provisional != null ? `${s4Provisional} / 10` : '-- / 10',
      tag: s4Provisional != null ? '临时' : undefined,
      hint: METRIC_HINTS['策略 4'],
    },
    { label: '缠论', hint: METRIC_HINTS['缠论'], value: chanScore == null ? '-- / 10' : `${chanScore} / 10` },
    { label: '多周期共振', hint: METRIC_HINTS['多周期共振'], value: s4?.resonanceCount == null ? '--' : `${s4.resonanceCount} / 4` },
    { label: '量比', hint: METRIC_HINTS['量比'], value: fmt(s4?.volumeRatio5D) },
    { label: '波动率', hint: METRIC_HINTS['波动率'], value: fmtPct(s4?.volatilityPct) },
    { label: '近 10 日', hint: METRIC_HINTS['近 10 日'], value: fmtPct(s4?.gain10dPct) },
    { label: '金叉 / 全满足', hint: METRIC_HINTS['金叉 / 全满足'], value: `${s4?.goldenCrossToday === true ? '是' : s4?.goldenCrossToday === false ? '否' : '--'} / ${s4?.allOk === true ? '是' : s4?.allOk === false ? '否' : '--'}` },
  ];
  return (
    <div className="grid grid-cols-2 gap-px border-y border-border/70 bg-border/50 sm:grid-cols-4 2xl:grid-cols-8">
      {metrics.map((metric) => (
        <div key={metric.label} className="min-h-[62px] bg-background px-3 py-2.5">
          <div className="flex items-center gap-1 text-[11px] font-medium text-secondary-text">
            {metric.label}
            {metric.hint ? (
              <Tooltip content={metric.hint} focusable className="-m-1 p-1">
                <Info className="h-3 w-3 shrink-0 text-secondary-text/60" aria-hidden />
              </Tooltip>
            ) : null}
          </div>
          <div className="mt-1 flex items-baseline gap-1.5">
            <span className="text-sm font-semibold tabular-nums text-foreground">{metric.value}</span>
            {metric.tag ? <span className="rounded bg-warning/15 px-1 py-0.5 text-[10px] font-medium text-warning">{metric.tag}</span> : null}
          </div>
        </div>
      ))}
    </div>
  );
};

const SchedulerStatus: React.FC<{ data: SignalPreviewList | null }> = ({ data }) => {
  const states = data?.scheduler ?? [];
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border/60 pt-3 text-xs text-secondary-text">
      <span className="inline-flex items-center gap-1.5 font-medium text-foreground"><CalendarClock className="h-3.5 w-3.5" />定时扫描</span>
      {states.length === 0 ? (
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2 w-2 rounded-full bg-secondary-text/40" />暂无运行记录
        </span>
      ) : (['cn', 'hk', 'us'] as const).map((market) => {
        const state = states.find((item) => item.market === market);
        return (
          <span key={market} className="inline-flex items-center gap-1.5">
            <span className={cn('h-2 w-2 rounded-full', state?.online ? 'bg-success' : 'bg-secondary-text/40')} />
            {market.toUpperCase()} {state?.status === 'failed' ? '失败' : state?.lastSuccessAt ? shortDate(state.lastSuccessAt) : '待首轮'}
          </span>
        );
      })}
    </div>
  );
};

const EvaluationPanel: React.FC<{ evaluation: SignalEvaluationSummary | null }> = ({ evaluation }) => {
  if (!evaluation) return null;
  const metricItems = [
    ['评估样本', `${evaluation.sampleCount}`],
    ['可评估样本', `${evaluation.eligibleCount}`],
    ['触达率', fmtPct(evaluation.hitRatePct)],
    ['平均等待', evaluation.averageWaitTradingDays == null ? '--' : `${evaluation.averageWaitTradingDays.toFixed(1)} 个交易日`],
    ['未成交机会成本', fmtPct(evaluation.opportunityCostPct)],
    ['执行调整收益', fmtPct(evaluation.executionAdjustedReturnPct)],
  ];
  return (
    <section className="mt-5 border-t border-border/70 pt-5" aria-label="信号影子评估">
      <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-foreground">信号影子评估</h2>
          <p className="mt-1 text-xs text-secondary-text">只统计已成熟样本，不回写买点策略；样本门槛为 {evaluation.reviewPolicy.minSamples} 笔、{evaluation.reviewPolicy.minCoverageDays} 天。</p>
        </div>
        <Badge variant="warning">影子模式</Badge>
      </div>
      <div className="grid border-y border-border/60 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6">
        {metricItems.map(([label, value], index) => (
          <div key={label} className={cn('min-h-[64px] px-3 py-3', index > 0 ? 'border-t border-border/50 sm:border-l sm:border-t-0' : '')}>
            <div className="text-[11px] text-secondary-text">{label}</div>
            <div className="mt-1 text-sm font-semibold tabular-nums text-foreground">{value}</div>
          </div>
        ))}
      </div>
      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)]">
        <div className="border-y border-border/60">
          <div className="grid grid-cols-5 bg-card/50 px-3 py-2 text-[11px] font-medium text-secondary-text">
            <span>周期</span><span>信号收益</span><span>基准</span><span>超额</span><span>样本</span>
          </div>
          {[5, 10, 20].map((horizon) => {
            const row = evaluation.horizons[String(horizon)] || {};
            return (
              <div key={horizon} className="grid grid-cols-5 border-t border-border/50 px-3 py-2 text-xs tabular-nums text-foreground">
                <span>{horizon} 日</span>
                <span>{fmtPct(row.signalReturnPct)}</span>
                <span>{fmtPct(row.benchmarkReturnPct)}</span>
                <span>{fmtPct(row.excessReturnPct)}</span>
                <span>{row.sampleCount ?? 0}</span>
              </div>
            );
          })}
        </div>
        <div className="border-y border-border/60">
          <div className="hidden grid-cols-7 bg-card/50 px-3 py-2 text-[11px] font-medium text-secondary-text sm:grid">
            <span className="col-span-2">信号类型</span><span>样本</span><span>触发</span><span>触达率</span><span>覆盖</span><span>状态</span>
          </div>
          {evaluation.bySetupType.length ? evaluation.bySetupType.map((row) => (
            <div key={row.setupType} className="border-t border-border/50 px-3 py-2 text-xs tabular-nums text-foreground first:border-t-0 sm:grid sm:grid-cols-7">
              <div className="flex min-w-0 items-center justify-between gap-2 sm:col-span-2 sm:block">
                <span className="truncate font-medium">{SETUP_TEXT[row.setupType] || row.setupType}</span>
                <span className={cn('shrink-0 sm:hidden', row.reviewStatus === 'reviewable' ? 'text-success' : 'text-warning')}>{row.reviewStatus === 'reviewable' ? '可评审' : '样本不足'}</span>
              </div>
              <div className="mt-1 grid grid-cols-4 gap-2 text-secondary-text sm:contents sm:text-foreground">
                <span><span className="sm:hidden">样本 </span>{row.sampleCount}</span>
                <span><span className="sm:hidden">触发 </span>{row.triggeredCount}</span>
                <span><span className="sm:hidden">触达 </span>{fmtPct(row.hitRatePct)}</span>
                <span><span className="sm:hidden">覆盖 </span>{row.coverageDays} 天</span>
                <span className={cn('hidden sm:block', row.reviewStatus === 'reviewable' ? 'text-success' : 'text-warning')}>{row.reviewStatus === 'reviewable' ? '可评审' : '样本不足'}</span>
              </div>
            </div>
          )) : <div className="px-3 py-4 text-xs text-secondary-text">暂无可评估的信号样本</div>}
        </div>
      </div>
    </section>
  );
};

const SignalPreviewPage: React.FC = () => {
  const [searchParams, setSearchParams] = useSearchParams();
  const [mode, setMode] = useState<PreviewMode>(searchParams.get('mode') === 'position' ? 'position' : 'entry');
  const [data, setData] = useState<SignalPreviewList | null>(null);
  const [evaluation, setEvaluation] = useState<SignalEvaluationSummary | null>(null);
  const [detail, setDetail] = useState<SignalPreviewDetail | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const detailRef = useRef<HTMLElement | null>(null);

  const legacyEntrySignals = useMemo(
    () => (data?.entrySignals ?? []).filter((item) => !item.planId),
    [data],
  );
  const ids = useMemo(
    () => (mode === 'entry'
      ? [...(data?.pricePlans ?? []).map((item) => item.planId), ...legacyEntrySignals.map((item) => item.signalId)]
      : (data?.positionWatches ?? []).map((item) => item.watchId)),
    [data, legacyEntrySignals, mode],
  );

  const loadList = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await signalPreviewApi.list());
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadEvaluation = useCallback(async () => {
    try {
      setEvaluation(await signalPreviewApi.evaluation());
    } catch (err) {
      // Evaluation is read-only shadow telemetry; a temporary data gap must not
      // hide the chart and lifecycle controls.
      console.warn('加载信号影子评估失败', err);
      setEvaluation(null);
    }
  }, []);

  const loadDetail = useCallback(async (id: string, refreshTail = true) => {
    setDetailLoading(true);
    setError(null);
    try {
      const isPlan = mode === 'entry' && Boolean(data?.pricePlans?.some((item) => item.planId === id));
      const payload = mode === 'entry'
        ? isPlan
          ? await signalPreviewApi.plan(id, refreshTail)
          : await signalPreviewApi.entry(id, refreshTail)
        : await signalPreviewApi.position(id, refreshTail);
      setDetail(payload);
    } catch (err) {
      setDetail(null);
      setError(getParsedApiError(err));
    } finally {
      setDetailLoading(false);
    }
  }, [data?.pricePlans, mode]);

  useEffect(() => { void loadList(); void loadEvaluation(); }, [loadEvaluation, loadList]);
  useEffect(() => {
    if (!ids.length) {
      setSelectedId(null);
      setDetail(null);
      return;
    }
    if (selectedId && ids.includes(selectedId)) return;
    const fromUrl = searchParams.get('id');
    if (fromUrl && ids.includes(fromUrl)) {
      setSelectedId(fromUrl);
      return;
    }
    setSelectedId(ids[0]);
  }, [ids, searchParams, selectedId]);
  useEffect(() => {
    if (selectedId) void loadDetail(selectedId);
  }, [loadDetail, selectedId]);
  // 选中标的写回地址栏，刷新或分享链接不会跳回第一只票。
  useEffect(() => {
    // 列表还没回来时不要动 URL，否则刷新会把 ?id= 擦掉。
    if (!data) return;
    if (!selectedId && ids.length) return;
    const urlMode = searchParams.get('mode') ?? 'entry';
    const urlId = searchParams.get('id') ?? '';
    if (urlMode === mode && urlId === (selectedId ?? '')) return;
    const next = new URLSearchParams(searchParams);
    next.set('mode', mode);
    if (selectedId) next.set('id', selectedId);
    else next.delete('id');
    setSearchParams(next, { replace: true });
  }, [data, ids.length, mode, searchParams, selectedId, setSearchParams]);

  const subject = detail?.subject;
  const verdict = detail?.chart.composite?.compositeVerdict;
  const accountDecision = detail?.accountDecision;
  const planReview = detail?.review;
  const displayState = detail?.kind === 'entry_signal'
    ? entryDisplayState(subject as EntrySignal)
    : detail?.kind === 'price_plan'
      ? accountDecision?.executionStatus || (subject as PricePlanSummary).generationStatus
    : (subject as PositionWatch | undefined)?.currentAction;
  const entryActionSummary = detail?.kind === 'entry_signal'
    ? `${ACTION_TEXT[entryCurrentAction(subject as EntrySignal)] || '--'} / ${ACTION_TEXT[entryTriggerAction(subject as EntrySignal)] || '--'}`
    : '--';

  const formalizationNotes = verdict?.verdictStatus === 'evaluable'
    ? []
    : (verdict?.formalizationReasonCodes ?? []).map((code) => FORMALIZATION_REASON_TEXT[code] || code);
  const scoreBadge = verdict?.verdictStatus === 'evaluable'
    ? { label: '评分完整', variant: 'success' as const }
    : verdict?.score == null && verdict?.provisionalScore != null
      ? { label: '临时评分', variant: 'warning' as const }
      : { label: '评分不完整', variant: 'warning' as const };

  const zoneLegend = useMemo<Array<{ key: string; tone: string; label: string; range: string }>>(() => {
    if (!detail) return [];
    if (detail.kind === 'price_plan') {
      return (detail.pricePlan?.scenarios ?? []).map((scenario) => ({
        key: scenario.scenarioId,
        tone: scenario.role,
        label: ZONE_ROLE_TEXT[scenario.role] || scenario.role,
        range: `${fmt(scenario.entryLow)}–${fmt(scenario.entryHigh)}`,
      }));
    }
    if (detail.kind === 'entry_signal') {
      const signal = detail.subject as EntrySignal;
      const zones = signal.triggerRule?.zones ?? [];
      if (zones.length) {
        return zones.map((zone, index) => ({
          key: `${zone.label}-${index}`,
          tone: index === 0 ? 'primary' : 'secondary',
          label: ENTRY_ZONE_TEXT[zone.label] || zone.label,
          range: `${fmt(zone.low)}–${fmt(zone.high)}`,
        }));
      }
      if (signal.entryLow != null || signal.entryHigh != null) {
        return [{
          key: 'entry',
          tone: 'primary',
          label: '计划入场区',
          range: `${fmt(signal.entryLow)}–${fmt(signal.entryHigh)}`,
        }];
      }
    }
    return [];
  }, [detail]);

  const groupedZoneLegend = useMemo(() => {
    const grouped = new Map<string, { key: string; tone: string; label: string; ranges: string[] }>();
    zoneLegend.forEach((zone) => {
      const existing = grouped.get(zone.label);
      if (existing) existing.ranges.push(zone.range);
      else grouped.set(zone.label, { key: zone.label, tone: zone.tone, label: zone.label, ranges: [zone.range] });
    });
    return [...grouped.values()];
  }, [zoneLegend]);

  const hasPriceLines = Boolean(detail?.pricePlan?.scenarios?.some((scenario) => scenario.role !== 'observation'));

  const handleRefresh = async () => {
    await loadList();
    await loadEvaluation();
    if (selectedId) await loadDetail(selectedId, true);
  };

  const handleModeChange = (next: PreviewMode) => {
    setMode(next);
    setSelectedId(null);
    setDetail(null);
  };

  const handleSelect = useCallback((id: string) => {
    setSelectedId(id);
    // 单栏（<xl）时详情在列表下方，点选后直接滚到图，省一次手动下滑。
    if (typeof window !== 'undefined' && window.matchMedia('(max-width: 1279px)').matches) {
      window.requestAnimationFrame(() => {
        detailRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }, []);

  const listContent = <SubjectList mode={mode} data={data} selectedId={selectedId} onSelect={handleSelect} />;

  return (
    <div className="w-full px-3 pb-8 pt-4 md:px-5 xl:px-7">
      <header className="mb-4 flex flex-col gap-3 border-b border-border/70 pb-4 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <CandlestickChart className="h-5 w-5 text-cyan" />
            <h1 className="text-xl font-semibold text-foreground">信号预览</h1>
          </div>
          <SchedulerStatus data={data} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <ModeTabs mode={mode} data={data} onChange={handleModeChange} />
          <button type="button" className="btn-secondary inline-flex h-10 w-10 items-center justify-center !p-0" onClick={() => void handleRefresh()} disabled={loading || detailLoading} aria-label="刷新信号预览">
            <RefreshCw className={cn('h-4 w-4 shrink-0', loading || detailLoading ? 'animate-spin' : '')} />
          </button>
        </div>
      </header>

      {error ? <div className="mb-4"><ApiErrorAlert error={error} /></div> : null}

      <div className="grid gap-4 xl:grid-cols-[320px_minmax(0,1fr)] xl:items-start">
        <section className="border-y border-border/70 bg-card/20 xl:sticky xl:top-4 xl:z-10">
          <div className="flex items-center justify-between px-3 py-3 text-xs text-secondary-text">
            <span className="font-medium text-foreground">{mode === 'entry' ? '个股计划' : '持仓监控'}</span>
            <span>{mode === 'entry' ? '按最近更新排序' : '按风险等级排序'} · {ids.length} 项</span>
          </div>
          {ids.length ? (
            <div className="max-h-[300px] overflow-y-auto border-t border-border/60 xl:max-h-[calc(100vh-9rem)]">
              {listContent}
            </div>
          ) : null}
        </section>

        <section ref={detailRef} className="min-w-0 scroll-mt-4">
          {!detail && !detailLoading ? (
            <EmptyState
              title={ids.length ? '请选择一个标的' : mode === 'entry' ? '暂无可展示信号' : '暂无可展示持仓'}
              description={mode === 'entry' ? '单股分析产生的入场、等待与观察区间会出现在这里。' : '首次定时持仓扫描完成后会出现在这里。'}
              icon={<Activity className="h-7 w-7" />}
              className="min-h-[280px] xl:min-h-[520px]"
            />
          ) : null}
          {detailLoading ? (
            <div className="flex min-h-[280px] items-center justify-center text-sm text-secondary-text xl:min-h-[520px]">
              <RefreshCw className="mr-2 h-4 w-4 animate-spin" /> 正在读取本地历史与指标
            </div>
          ) : null}
          {detail && !detailLoading ? (
            <>
              <div className="mb-3 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <h2 className="truncate text-lg font-semibold text-foreground">{subject?.stockName || subject?.stockCode}</h2>
                    <span className="font-mono text-sm text-secondary-text">{subject?.stockCode}</span>
                    <Badge variant={statusTone(displayState)}>{STATUS_TEXT[displayState || ''] || displayState || '--'}</Badge>
                    {verdict?.exitWarning?.level ? <Badge variant="danger">退出预警 L{verdict.exitWarning.level}</Badge> : null}
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-secondary-text">
                    <span>{detail.chart.market.toUpperCase()} · 日线 · 原始现金价格</span>
                    <span>{detail.chart.dataWindow?.start || '--'} 至 {detail.chart.dataWindow?.end || '--'}</span>
                    <span>{detail.chart.dataWindow?.barCount ?? 0} 根</span>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="inline-flex items-center gap-1 text-secondary-text"><Database className="h-3.5 w-3.5" />{(detail.chart.dataWindow?.sources || []).map((source) => SOURCE_TEXT[source] || source).join(' / ') || '本地数据库'}</span>
                  <Badge variant={scoreBadge.variant}>{scoreBadge.label}</Badge>
                </div>
              </div>

              <MetricsStrip detail={detail} />
              {formalizationNotes.length ? (
                <div className="flex items-start gap-2 border-b border-border/70 bg-warning/[0.06] px-3 py-2 text-xs text-warning">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                  <span>{formalizationNotes.join('；')}</span>
                </div>
              ) : null}
              {zoneLegend.length ? (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border/70 px-3 py-2 text-xs text-secondary-text">
                  <span className="font-medium text-foreground">价格区间</span>
                  {groupedZoneLegend.map((zone) => (
                    <span key={zone.key} className="inline-flex items-center gap-1.5">
                      <span className={cn('h-2.5 w-2.5 border', ZONE_SWATCH[zone.tone] || ZONE_SWATCH.observation)} />
                      {zone.label}
                      <span className="tabular-nums text-foreground/80">{zone.ranges.join('、')}</span>
                    </span>
                  ))}
                  {hasPriceLines ? (
                    <span className="inline-flex items-center gap-1.5">
                      <span className="h-0 w-4 border-t border-dashed border-red-500" />止损
                      <span className="ml-1 h-0 w-4 border-t border-dotted border-sky-500" />目标
                    </span>
                  ) : null}
                  <span className="inline-flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 border border-amber-500/75 bg-amber-500/10" />缠论中枢
                  </span>
                  <span className="text-secondary-text/75">圆点/三角为事件与结构点，悬停可看详情</span>
                </div>
              ) : null}
              <div className="border-b border-border/70 bg-card/35"><SignalChart detail={detail} /></div>

              <div className="grid gap-5 pt-5 xl:grid-cols-[minmax(0,1fr)_300px]">
                <div>
                  <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground"><ShieldCheck className="h-4 w-4 text-cyan" />固定判决</div>
                  <dl className="grid grid-cols-2 gap-px border-y border-border/60 bg-border/40 text-sm xl:grid-cols-3">
                    {[
                      ['信号等级', VERDICT_TEXT[verdict?.signalLevel || ''] || verdict?.signalLevel || '--'],
                      ['入场权限', PERMISSION_TEXT[verdict?.entryPermission || ''] || verdict?.entryPermission || '--'],
                      [detail.kind === 'entry_signal' ? '当前 / 触发动作' : detail.kind === 'price_plan' ? '执行状态' : '持仓动作', detail.kind === 'entry_signal' ? entryActionSummary : detail.kind === 'price_plan' ? STATUS_TEXT[accountDecision?.executionStatus || (subject as PricePlanSummary).generationStatus] || '--' : ACTION_TEXT[verdict?.requiredHoldingAction || ''] || ACTION_TEXT[(subject as PositionWatch).currentAction] || '--'],
                      ['模型证据评审', planReview?.conclusion ? REVIEW_TEXT[planReview.conclusion] || planReview.conclusion : '--'],
                      ['评审权限', accountDecision?.reviewPermission ? REVIEW_PERMISSION_TEXT[accountDecision.reviewPermission] || accountDecision.reviewPermission : '--'],
                      ['风险偏好 / 仓位上限', accountDecision ? `${RISK_PROFILE_TEXT[accountDecision.riskProfile || ''] || accountDecision.riskProfile || '--'} / ${accountDecision.targetPositionPct == null ? '--' : `${fmt(accountDecision.targetPositionPct)}%`}` : '--'],
                      ['计划数量 / 账户净盈亏比', accountDecision ? `${accountDecision.plannedQuantity == null ? '--' : `${accountDecision.plannedQuantity} 股`} / ${accountDecision.accountNetRr1 == null ? '--' : `${fmt(accountDecision.accountNetRr1)}:1`}` : '--'],
                      ['选中场景', (() => {
                        const selectedScenario = detail.pricePlan?.scenarios.find((scenario) => scenario.scenarioId === accountDecision?.selectedScenarioId);
                        return selectedScenario
                          ? `${ZONE_ROLE_TEXT[selectedScenario.role] || '场景'} ${fmt(selectedScenario.entryLow)}–${fmt(selectedScenario.entryHigh)}`
                          : '--';
                      })()],
                      ['数据更新', detail.chart.dataWindow?.end || '--'],
                      ['证据缺失', (verdict?.missingFeatures || []).join(', ') || '无'],
                      ['限制原因', accountDecision?.reasonCodes?.map((code) => PLAN_REASON_TEXT[code] || code).join('；') || verdict?.exitWarning?.items?.join(', ') || '无'],
                    ].map(([label, value]) => (
                      <div key={label} className="min-h-[60px] bg-background px-3 py-3">
                        <dt className="text-xs text-secondary-text">{label}</dt>
                        <dd className="mt-1 break-words font-medium text-foreground">{value}</dd>
                      </div>
                    ))}
                    {/* 11 项在 2/3 列下都会空出一格，用同色占位避免露出分隔底色。 */}
                    <div aria-hidden className="bg-background" />
                  </dl>
                </div>
                <div>
                  <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground"><AlertTriangle className="h-4 w-4 text-warning" />{detail.kind === 'price_plan' ? '场景状态' : '生命周期事件'}</div>
                  <div className="max-h-[260px] overflow-y-auto border-l border-border/70 pl-4">
                    {detail.kind === 'price_plan' && detail.lifecycleStates?.length ? detail.lifecycleStates.map((state) => {
                      const scenario = detail.pricePlan?.scenarios.find((item) => item.scenarioId === state.scenarioId);
                      return (
                        <div key={state.scenarioId} className="relative pb-4 text-sm before:absolute before:-left-[19px] before:top-1.5 before:h-2 before:w-2 before:rounded-full before:bg-cyan">
                          <div className="flex flex-wrap items-center gap-2">
                            <span className="font-medium text-foreground">{SCENARIO_ROLE_TEXT[scenario?.role || ''] || '价格场景'}</span>
                            <Badge variant={statusTone(state.state)}>{STATUS_TEXT[state.state] || state.state}</Badge>
                          </div>
                          <div className="mt-1 text-xs text-secondary-text">评估至 {shortDate(state.lastEvaluatedAsOf)} · 状态版本 {state.stateVersion}</div>
                        </div>
                      );
                    }) : detail.events.length ? detail.events.slice().reverse().map((event) => (
                      <div key={event.eventId} className="relative pb-4 text-sm before:absolute before:-left-[19px] before:top-1.5 before:h-2 before:w-2 before:rounded-full before:bg-cyan">
                        <div className="font-medium text-foreground">{EVENT_TEXT[event.eventType] || event.eventType}</div>
                        <div className="mt-0.5 text-xs text-secondary-text">{event.occurredAt.replace('T', ' ').slice(0, 16)} · {SOURCE_TEXT[event.source] || event.source}</div>
                      </div>
                    )) : <p className="text-sm text-secondary-text">暂无事件</p>}
                  </div>
                  {detail.kind === 'price_plan' && detail.events.length ? (
                    <div className="mt-3 text-xs text-secondary-text">已记录 {detail.events.length} 条场景审计事件</div>
                  ) : null}
                </div>
              </div>
            </>
          ) : null}
        </section>
      </div>

      <EvaluationPanel evaluation={evaluation} />
    </div>
  );
};

export default SignalPreviewPage;
