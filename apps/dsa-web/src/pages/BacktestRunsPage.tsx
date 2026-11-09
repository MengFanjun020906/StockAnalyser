import type React from 'react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Activity,
  BarChart3,
  ChevronRight,
  CircleDollarSign,
  CloudDownload,
  Download,
  Gauge,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Table2,
  TrendingUp,
} from 'lucide-react';
import {
  backtestRunsApi,
  type BacktestRunDetail,
  type BacktestRunMetrics,
  type BacktestRunSummary,
  type CandidatePlan,
  type CoverageRow,
  type DataJob,
  type EquityPoint,
  type FeePolicy,
  type FillRow,
  type InputRow,
  type OrderRow,
  type BenchmarkPayload,
  type BenchmarkSeries,
  type Paginated,
  type PositionRow,
  type RiskPolicyPreset,
  type TradeRow,
} from '../api/backtestRuns';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import {
  ApiErrorAlert,
  Badge,
  Button,
  EmptyState,
  InlineAlert,
  Input,
  Pagination,
  SectionCard,
  Select,
  StatCard,
} from '../components/common';
import { EquityChart } from '../components/backtest/EquityChart';
import { cn } from '../utils/cn';

type FormState = {
  mode: 'recorded_plan_replay' | 'historical_agent_research';
  startDate: string;
  endDate: string;
  initialCash: string;
  sourcePipeline: 'selection_confirmed' | 'single_stock';
  riskProfile: 'conservative' | 'balanced' | 'aggressive';
  granularity: '5m' | '1d';
  missingDataPolicy: 'strict' | 'daily_downgrade';
  feePolicy: string;
  slippageBps: string;
  participationRate: string;
  maxSingle: string;
  maxTotal: string;
  corporateActionPolicy: 'strict' | 'assume_none';
  benchmarkSymbol: string;
  planIdsText: string;
  traceIdsText: string;
  selectionLinksText: string;
  researchUniverse: string;
  researchUniverseMode: 'explicit' | 'auto_local_coverage';
  researchAutoLimit: string;
  researchConfirmationMode: 'auto_accept' | 'llm';
  researchEntryMode: 'adaptive' | 'pullback' | 'breakout';
  researchOrderDays: string;
  researchTopN: string;
  researchConfirmLimit: string;
  researchMaxCalls: string;
  researchMinBars: string;
  researchModel: string;
  notes: string;
};

type DetailTab = 'overview' | 'orders' | 'fills' | 'trades' | 'inputs' | 'diagnostics';
type PrecheckPayload = Record<string, unknown>;

const STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  validating: '校验中',
  running: '运行中',
  succeeded: '成功',
  partial: '部分完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
};

const PIPELINE_LABELS: Record<string, string> = {
  selection_confirmed: '选股确认',
  single_stock: '单股直连',
};

const RISK_LABELS: Record<string, string> = {
  conservative: '保守',
  balanced: '平衡',
  aggressive: '激进',
  custom: '自定义',
};

const TAB_ITEMS: Array<{ key: DetailTab; label: string }> = [
  { key: 'overview', label: '概览' },
  { key: 'orders', label: '订单' },
  { key: 'fills', label: '成交' },
  { key: 'trades', label: '已平仓' },
  { key: 'inputs', label: '输入' },
  { key: 'diagnostics', label: '诊断' },
];

function localIsoDate(offsetDays: number): string {
  const value = new Date();
  value.setDate(value.getDate() + offsetDays);
  const year = value.getFullYear();
  const month = String(value.getMonth() + 1).padStart(2, '0');
  const day = String(value.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

const INITIAL_FORM: FormState = {
  mode: 'historical_agent_research',
  startDate: localIsoDate(-90),
  endDate: localIsoDate(0),
  initialCash: '100000',
  sourcePipeline: 'single_stock',
  riskProfile: 'balanced',
  granularity: '1d',
  missingDataPolicy: 'strict',
  feePolicy: 'cn-a-share-2023.v1',
  slippageBps: '5',
  participationRate: '0.01',
  maxSingle: '100',
  maxTotal: '100',
  corporateActionPolicy: 'assume_none',
  benchmarkSymbol: '',
  planIdsText: '',
  traceIdsText: '',
  selectionLinksText: '',
  researchUniverse: '600519, 600330, 002475',
  researchUniverseMode: 'auto_local_coverage',
  researchAutoLimit: '100',
  researchConfirmationMode: 'auto_accept',
  researchEntryMode: 'adaptive',
  researchOrderDays: '15',
  researchTopN: '5',
  researchConfirmLimit: '5',
  researchMaxCalls: '500',
  researchMinBars: '40',
  researchModel: '',
  notes: '',
};

const TEXTAREA_CLASS =
  'input-surface input-focus-glow min-h-24 w-full rounded-xl border bg-transparent px-4 py-3 text-sm text-foreground transition-all focus:outline-none';

function isTerminal(status?: string): boolean {
  return status === 'succeeded' || status === 'partial' || status === 'failed' || status === 'cancelled' || status === 'interrupted';
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

function fmtPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  const number = Number(value);
  const sign = number > 0 ? '+' : '';
  return `${sign}${number.toFixed(2)}%`;
}

function fmtNum(value?: number | null, digits = 2): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(digits);
}

function shortRunId(value?: string | null): string {
  if (!value) return '--';
  return value.length > 22 ? `${value.slice(0, 22)}…` : value;
}

function statusVariant(status?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (status === 'succeeded') return 'success';
  if (status === 'partial') return 'warning';
  if (status === 'failed' || status === 'interrupted') return 'danger';
  if (status === 'cancelled') return 'default';
  if (status === 'running' || status === 'validating') return 'info';
  return 'default';
}

function metricTone(value?: number | null): React.ComponentProps<typeof StatCard>['tone'] {
  if (value == null) return 'default';
  if (value > 0) return 'success';
  if (value < 0) return 'danger';
  return 'default';
}

function splitCsv(value: string): string[] {
  return value
    .split(/[\s,，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function parseSelectionLinks(value: string): Array<Record<string, unknown>> {
  const text = value.trim();
  if (!text) return [];
  const parsed = JSON.parse(text) as unknown;
  if (!Array.isArray(parsed)) throw new Error('selection_links 必须是 JSON 数组');
  return parsed.map((item, index) => {
    if (!item || typeof item !== 'object') throw new Error(`selection_links[${index}] 不是对象`);
    const record = item as Record<string, unknown>;
    if (!record.selection_trace_ref || !record.confirmation_trace_ref || !record.symbol) {
      throw new Error(`selection_links[${index}] 缺少 selection_trace_ref / confirmation_trace_ref / symbol`);
    }
    return record;
  });
}

function buildConfig(form: FormState): Record<string, unknown> {
  if (form.mode === 'historical_agent_research') {
    const universe = form.researchUniverseMode === 'explicit' ? splitCsv(form.researchUniverse) : [];
    if (form.researchUniverseMode === 'explicit' && !universe.length) throw new Error('研究标的池不能为空');
    return {
      mode: 'historical_agent_research',
      source_pipeline: 'research',
      start_date: form.startDate,
      end_date: form.endDate,
      initial_cash: Number(form.initialCash),
      granularity: '1d',
      price_basis: 'daily_declared_unadjusted',
      missing_data_policy: 'strict',
      risk_profile: form.riskProfile,
      max_single_position_pct: Number(form.maxSingle),
      max_total_exposure_pct: Number(form.maxTotal),
      fee_policy_ref: form.feePolicy,
      slippage_bps: Number(form.slippageBps),
      participation_rate: Number(form.participationRate),
      corporate_action_policy: form.corporateActionPolicy,
      plan_ids: [],
      trace_ids: [],
      selection_links: [],
      research: {
        universe,
        universe_mode: form.researchUniverseMode,
        auto_universe_limit: Number(form.researchAutoLimit),
        confirmation_mode: form.researchConfirmationMode,
        entry_mode: form.researchEntryMode,
        order_valid_trading_days: Number(form.researchOrderDays),
        decision_frequency: 'monthly',
        selection_top_n: Number(form.researchTopN),
        max_confirmations_per_decision: Number(form.researchConfirmLimit),
        max_llm_calls: Number(form.researchMaxCalls),
        min_history_bars: Number(form.researchMinBars),
        model_ref: form.researchModel.trim() || null,
        prompt_version: 'research-prompt.v1',
        plan_version: 'research-plan.v1',
      },
      notes: form.notes,
    };
  }
  return {
    mode: 'historical_agent_research',
    source_pipeline: form.sourcePipeline,
    start_date: form.startDate,
    end_date: form.endDate,
    initial_cash: Number(form.initialCash),
    granularity: form.granularity,
    price_basis: form.granularity === '1d' ? 'daily_declared_unadjusted' : 'minute_unadjusted',
    missing_data_policy: form.granularity === '1d' ? 'strict' : form.missingDataPolicy,
    risk_profile: form.riskProfile,
    max_single_position_pct: Number(form.maxSingle),
    max_total_exposure_pct: Number(form.maxTotal),
    fee_policy_ref: form.feePolicy,
    slippage_bps: Number(form.slippageBps),
    participation_rate: Number(form.participationRate),
    corporate_action_policy: form.corporateActionPolicy,
    plan_ids: splitCsv(form.planIdsText),
    trace_ids: splitCsv(form.traceIdsText),
    selection_links: parseSelectionLinks(form.selectionLinksText),
    benchmark: form.benchmarkSymbol.trim()
      ? { symbol: form.benchmarkSymbol.trim(), price_basis: 'price' }
      : null,
    notes: form.notes,
  };
}

const MetricGrid: React.FC<{
  metrics?: BacktestRunMetrics | null;
  excessVsShanghai?: number | null;
  shanghaiReturn?: number | null;
}> = ({ metrics, excessVsShanghai = null, shanghaiReturn = null }) => {
  if (!metrics) return null;
  const single = metrics.singleTrade || {};
  return (
    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StatCard label="总收益" value={fmtPct(metrics.totalReturnPct)} tone={metricTone(metrics.totalReturnPct)} hint={`${metrics.tradingDays ?? 0} 个交易日`} icon={<CircleDollarSign className="h-5 w-5" />} />
      <StatCard label="上证超额" value={fmtPct(excessVsShanghai)} tone={metricTone(excessVsShanghai)} hint={shanghaiReturn != null ? `上证 ${fmtPct(shanghaiReturn)} · 正=跑赢` : '等待指数数据'} icon={<TrendingUp className="h-5 w-5" />} />
      <StatCard label="最大回撤" value={fmtPct(metrics.maxDrawdownPct)} tone="danger" hint={metrics.maxDrawdownTrough ? `低点 ${metrics.maxDrawdownTrough}` : '未发生回撤'} icon={<Activity className="h-5 w-5" />} />
      <StatCard label="Sharpe" value={fmtNum(metrics.sharpe, 3)} hint={metrics.notApplicable?.sharpe || '日收益序列'} icon={<Gauge className="h-5 w-5" />} />
      <StatCard label="年化收益" value={fmtPct(metrics.annualizedReturnPct)} tone={metricTone(metrics.annualizedReturnPct)} hint={metrics.notApplicable?.annualizedReturnPct || `样本 ${metrics.dailyReturnCount ?? 0} 日`} icon={<BarChart3 className="h-5 w-5" />} />
      {metrics.benchmarkReturnPct != null || metrics.excessReturnPct != null ? (
        <StatCard label="基准超额" value={fmtPct(metrics.excessReturnPct)} hint={metrics.notApplicable?.excessReturnPct || `基准 ${fmtPct(metrics.benchmarkReturnPct)}`} />
      ) : null}
      <StatCard label="平均现金比例" value={fmtPct(metrics.cashRatioPct)} hint="日终权益口径" />
      <StatCard label="换手率" value={fmtPct(metrics.turnoverPct)} hint={`费用拖累 ${fmtPct(metrics.feeDragPct)} / 滑点 ${fmtPct(metrics.slippageDragPct)}`} />
      <StatCard label="已平仓胜率" value={fmtPct(single.winRatePct as number | null)} hint={`${single.closedTrades ?? 0} 笔已平仓 / ${single.unclosedTrades ?? 0} 笔未平仓`} />
    </div>
  );
};

const CoverageTable: React.FC<{ rows?: CoverageRow[] }> = ({ rows }) => {
  if (!rows?.length) return null;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-sm">
        <thead className="text-xs uppercase tracking-[0.16em] text-secondary-text">
          <tr>
            <th className="px-3 py-2">标的</th>
            <th className="px-3 py-2">完整</th>
            <th className="px-3 py-2">部分</th>
            <th className="px-3 py-2">缺失</th>
            <th className="px-3 py-2">停牌</th>
            <th className="px-3 py-2">质量</th>
            <th className="px-3 py-2">标记</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol} className="border-t border-border/40">
              <td className="px-3 py-2 font-medium text-foreground">{row.symbol}</td>
              <td className="px-3 py-2">{row.sessionsFull}</td>
              <td className="px-3 py-2">{row.sessionsPartial}</td>
              <td className="px-3 py-2">{row.sessionsMissing}</td>
              <td className="px-3 py-2">{row.sessionsSuspended}</td>
              <td className="px-3 py-2">
                <Badge variant={row.quality === 'complete' ? 'success' : row.quality === 'unavailable' ? 'danger' : 'warning'}>
                  {row.quality}
                </Badge>
              </td>
              <td className="px-3 py-2 text-xs text-secondary-text">{row.flags.join(', ') || '--'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const OrdersTable: React.FC<{ data: Paginated<OrderRow> | null; onPage: (page: number) => void }> = ({ data, onPage }) => {
  if (!data?.items.length) return <EmptyState title="暂无订单" description="该实验还没有生成订单，或运行尚未完成。" />;
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[920px] text-left text-sm">
          <thead className="text-xs uppercase tracking-[0.16em] text-secondary-text">
            <tr>
              <th className="px-3 py-2">时间</th>
              <th className="px-3 py-2">标的</th>
              <th className="px-3 py-2">方向</th>
              <th className="px-3 py-2">类型</th>
              <th className="px-3 py-2">数量</th>
              <th className="px-3 py-2">成交/剩余</th>
              <th className="px-3 py-2">均价</th>
              <th className="px-3 py-2">状态</th>
              <th className="px-3 py-2">原因</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((order) => (
              <tr key={order.orderId} className="border-t border-border/40">
                <td className="px-3 py-2 text-xs text-secondary-text">{order.submittedAt || '--'}</td>
                <td className="px-3 py-2 font-medium text-foreground">{order.symbol}</td>
                <td className={cn('px-3 py-2', order.side === 'buy' ? 'text-danger' : 'text-success')}>{order.side === 'buy' ? '买入' : '卖出'}</td>
                <td className="px-3 py-2">{order.orderType}</td>
                <td className="px-3 py-2 tabular-nums">{order.quantity}</td>
                <td className="px-3 py-2 tabular-nums">{order.filledQuantity} / {order.remaining}</td>
                <td className="px-3 py-2 tabular-nums">{fmtNum(order.avgFillPrice)}</td>
                <td className="px-3 py-2"><Badge variant={order.status === 'filled' ? 'success' : order.status === 'rejected' ? 'danger' : 'warning'}>{order.status}</Badge></td>
                <td className="px-3 py-2 text-xs text-secondary-text">{order.reason || order.exitReason || '--'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination currentPage={data.page} totalPages={Math.max(1, Math.ceil(data.total / data.pageSize))} onPageChange={onPage} />
    </div>
  );
};

const FillsTable: React.FC<{ data: Paginated<FillRow> | null; onPage: (page: number) => void }> = ({ data, onPage }) => {
  if (!data?.items.length) return <EmptyState title="暂无成交" description="严格模式下缺失关键行情不会成交。" />;
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-left text-sm">
          <thead className="text-xs uppercase tracking-[0.16em] text-secondary-text">
            <tr>
              <th className="px-3 py-2">bar</th>
              <th className="px-3 py-2">标的</th>
              <th className="px-3 py-2">方向</th>
              <th className="px-3 py-2">数量</th>
              <th className="px-3 py-2">价格</th>
              <th className="px-3 py-2">佣金</th>
              <th className="px-3 py-2">印花税</th>
              <th className="px-3 py-2">滑点</th>
              <th className="px-3 py-2">质量</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((fill) => (
              <tr key={fill.fillId} className="border-t border-border/40">
                <td className="px-3 py-2 text-xs text-secondary-text">{fill.barEndAt || fill.session}</td>
                <td className="px-3 py-2 font-medium text-foreground">{fill.symbol}</td>
                <td className={cn('px-3 py-2', fill.side === 'buy' ? 'text-danger' : 'text-success')}>{fill.side === 'buy' ? '买入' : '卖出'}</td>
                <td className="px-3 py-2 tabular-nums">{fill.quantity}</td>
                <td className="px-3 py-2 tabular-nums">{fmtNum(fill.price, 3)}</td>
                <td className="px-3 py-2 tabular-nums">{fmtNum(fill.commission)}</td>
                <td className="px-3 py-2 tabular-nums">{fmtNum(fill.stampDuty)}</td>
                <td className="px-3 py-2 tabular-nums">{fmtNum(fill.slippageCost)}</td>
                <td className="px-3 py-2 text-xs"><Badge variant={fill.quality?.startsWith('partial') ? 'warning' : 'success'}>{fill.quality || 'full'}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination currentPage={data.page} totalPages={Math.max(1, Math.ceil(data.total / data.pageSize))} onPageChange={onPage} />
    </div>
  );
};

const TradesTable: React.FC<{ data: Paginated<TradeRow> | null; onPage: (page: number) => void }> = ({ data, onPage }) => {
  if (!data?.items.length) return <EmptyState title="暂无已平仓交易" description="未平仓交易不计入已实现胜率。" />;
  return (
    <div className="flex flex-col gap-3">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[980px] text-left text-sm">
          <thead className="text-xs uppercase tracking-[0.16em] text-secondary-text">
            <tr>
              <th className="px-3 py-2">标的</th>
              <th className="px-3 py-2">数量</th>
              <th className="px-3 py-2">入场</th>
              <th className="px-3 py-2">出场</th>
              <th className="px-3 py-2">持有</th>
              <th className="px-3 py-2">已实现</th>
              <th className="px-3 py-2">收益率</th>
              <th className="px-3 py-2">MFE / MAE</th>
              <th className="px-3 py-2">退出原因</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((trade) => (
              <tr key={trade.tradeId} className="border-t border-border/40">
                <td className="px-3 py-2 font-medium text-foreground">{trade.symbol}</td>
                <td className="px-3 py-2 tabular-nums">{trade.quantity}</td>
                <td className="px-3 py-2 text-xs text-secondary-text">{trade.entrySession} @ {fmtNum(trade.entryPrice, 3)}</td>
                <td className="px-3 py-2 text-xs text-secondary-text">{trade.exitSession} @ {fmtNum(trade.exitPrice, 3)}</td>
                <td className="px-3 py-2 tabular-nums">{trade.holdingDays}d</td>
                <td className={cn('px-3 py-2 tabular-nums', trade.realizedPnl >= 0 ? 'text-danger' : 'text-success')}>{fmtNum(trade.realizedPnl)}</td>
                <td className={cn('px-3 py-2 tabular-nums', trade.returnPct >= 0 ? 'text-danger' : 'text-success')}>{fmtPct(trade.returnPct)}</td>
                <td className="px-3 py-2 text-xs text-secondary-text">{fmtPct(trade.mfePct)} / {fmtPct(trade.maePct)}</td>
                <td className="px-3 py-2 text-xs">{trade.exitReason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination currentPage={data.page} totalPages={Math.max(1, Math.ceil(data.total / data.pageSize))} onPageChange={onPage} />
    </div>
  );
};

const InputsTable: React.FC<{ inputs: InputRow[] }> = ({ inputs }) => {
  if (!inputs.length) return <EmptyState title="暂无输入记录" description="运行前会保存纳入/排除计划清单。" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[980px] text-left text-sm">
        <thead className="text-xs uppercase tracking-[0.16em] text-secondary-text">
          <tr>
            <th className="px-3 py-2">标的</th>
            <th className="px-3 py-2">来源</th>
            <th className="px-3 py-2">计划/场景</th>
            <th className="px-3 py-2">选股链</th>
            <th className="px-3 py-2">纳入</th>
            <th className="px-3 py-2">排除原因</th>
          </tr>
        </thead>
        <tbody>
          {inputs.map((item) => (
            <tr key={item.inputId} className="border-t border-border/40">
              <td className="px-3 py-2 font-medium text-foreground">{item.symbol || '--'}</td>
              <td className="px-3 py-2 text-xs">{PIPELINE_LABELS[item.sourcePipeline || ''] || item.sourcePipeline || '--'}</td>
              <td className="px-3 py-2 text-xs text-secondary-text">{item.planId || '--'} / {item.scenarioId || '--'}</td>
              <td className="px-3 py-2 text-xs text-secondary-text">{item.selectionTraceRef || '--'}</td>
              <td className="px-3 py-2"><Badge variant={item.included ? 'success' : 'warning'}>{item.included ? '纳入' : '排除'}</Badge></td>
              <td className="px-3 py-2 text-xs text-secondary-text">{item.exclusionReason || '--'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const PositionsTable: React.FC<{ rows: PositionRow[] }> = ({ rows }) => {
  if (!rows.length) return <EmptyState title="期末无持仓" description="所有持仓已在期末前平仓，或实验尚未产生成交。" />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[620px] text-left text-sm">
        <thead className="text-xs uppercase tracking-[0.16em] text-secondary-text">
          <tr>
            <th className="px-3 py-2">标的</th>
            <th className="px-3 py-2">数量</th>
            <th className="px-3 py-2">平均成本</th>
            <th className="px-3 py-2">首次建仓</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.symbol} className="border-t border-border/40">
              <td className="px-3 py-2 font-medium text-foreground">{row.symbol}</td>
              <td className="px-3 py-2 tabular-nums">{row.quantity}</td>
              <td className="px-3 py-2 tabular-nums">{fmtNum(row.averageCost, 3)}</td>
              <td className="px-3 py-2 text-xs text-secondary-text">{row.firstAcquired || '--'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

export const BacktestRunsPage: React.FC = () => {
  const [form, setForm] = useState<FormState>(INITIAL_FORM);
  const [runs, setRuns] = useState<BacktestRunSummary[]>([]);
  const [runsTotal, setRunsTotal] = useState(0);
  const [runsPage, setRunsPage] = useState(1);
  const [runsLoading, setRunsLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [detail, setDetail] = useState<BacktestRunDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [tab, setTab] = useState<DetailTab>('overview');
  const [equity, setEquity] = useState<EquityPoint[]>([]);
  const [benchmarks, setBenchmarks] = useState<BenchmarkSeries[]>([]);
  const [benchmarkSummary, setBenchmarkSummary] = useState<BenchmarkPayload | null>(null);
  const [orders, setOrders] = useState<Paginated<OrderRow> | null>(null);
  const [fills, setFills] = useState<Paginated<FillRow> | null>(null);
  const [trades, setTrades] = useState<Paginated<TradeRow> | null>(null);
  const [positions, setPositions] = useState<PositionRow[]>([]);
  const [positionSummary, setPositionSummary] = useState<{ totalCost?: number; marketValue?: number; unrealizedPnl?: number }>({});
  const [inputs, setInputs] = useState<InputRow[]>([]);
  const [diagnostics, setDiagnostics] = useState<Record<string, unknown>>({});
  const [ordersPage, setOrdersPage] = useState(1);
  const [fillsPage, setFillsPage] = useState(1);
  const [tradesPage, setTradesPage] = useState(1);
  const [riskPolicies, setRiskPolicies] = useState<RiskPolicyPreset[]>([]);
  const [feePolicies, setFeePolicies] = useState<FeePolicy[]>([]);
  const [candidates, setCandidates] = useState<CandidatePlan[]>([]);
  const [precheckJob, setPrecheckJob] = useState<DataJob | null>(null);
  const [precheckResult, setPrecheckResult] = useState<PrecheckPayload | null>(null);
  const [prechecking, setPrechecking] = useState(false);
  const [syncJob, setSyncJob] = useState<DataJob | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [creating, setCreating] = useState(false);
  const [allowIncomplete, setAllowIncomplete] = useState(false);
  const [cancelling, setCancelling] = useState(false);

  const updateForm = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((previous) => ({ ...previous, [key]: value }));
  };

  const loadRuns = useCallback(async (page = 1) => {
    setRunsLoading(true);
    try {
      const payload = await backtestRunsApi.list({ page, pageSize: 20 });
      setRuns(payload.items);
      setRunsTotal(payload.total);
      setRunsPage(payload.page);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setRunsLoading(false);
    }
  }, []);

  const loadTables = useCallback(async (runId: string) => {
    const [equityPayload, ordersPayload, fillsPayload, tradesPayload, positionsPayload, inputsPayload, diagnosticsPayload] = await Promise.all([
      backtestRunsApi.equity(runId),
      backtestRunsApi.orders(runId, ordersPage, 100),
      backtestRunsApi.fills(runId, fillsPage, 100),
      backtestRunsApi.trades(runId, tradesPage, 100),
      backtestRunsApi.positions(runId),
      backtestRunsApi.inputs(runId),
      backtestRunsApi.diagnostics(runId),
    ]);
    const benchmarkPayload = await backtestRunsApi.benchmarks(runId).catch(() => null);
    setEquity(equityPayload.items);
    setBenchmarks(benchmarkPayload?.series || []);
    setBenchmarkSummary(benchmarkPayload);
    setOrders(ordersPayload);
    setFills(fillsPayload);
    setTrades(tradesPayload);
    setPositions(positionsPayload.items || []);
    setPositionSummary({
      totalCost: positionsPayload.totalCost,
      marketValue: positionsPayload.marketValue,
      unrealizedPnl: positionsPayload.unrealizedPnl,
    });
    setInputs(inputsPayload.items || []);
    setDiagnostics(diagnosticsPayload.items || {});
  }, [ordersPage, fillsPage, tradesPage]);

  useEffect(() => {
    void loadRuns(1);
    void (async () => {
      try {
        const [risk, fees, candidatePayload] = await Promise.all([
          backtestRunsApi.riskPolicies(),
          backtestRunsApi.feePolicies(),
          backtestRunsApi.candidates({ limit: 300 }),
        ]);
        setRiskPolicies(risk);
        setFeePolicies(fees);
        setCandidates(candidatePayload.items || []);
        if (fees.length && !fees.some((item) => item.version === INITIAL_FORM.feePolicy)) {
          setForm((previous) => ({ ...previous, feePolicy: fees[0].version }));
        }
      } catch (err) {
        setError(getParsedApiError(err));
      }
    })();
  }, [loadRuns]);

  useEffect(() => {
    if (!runs.some((run) => !isTerminal(run.status))) return;
    const timer = window.setInterval(() => {
      void loadRuns(runsPage);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [runs, runsPage, loadRuns]);

  useEffect(() => {
    if (!selectedRunId) {
      setDetail(null);
      setBenchmarks([]);
      setBenchmarkSummary(null);
      return;
    }
    let cancelled = false;
    let terminal = false;
    let timeout: number | null = null;
    const tick = async () => {
      setDetailLoading(true);
      try {
        const payload = await backtestRunsApi.get(selectedRunId);
        if (cancelled) return;
        setDetail(payload);
        if (isTerminal(payload.status)) {
          terminal = true;
          await loadTables(selectedRunId);
          if (!cancelled) setDetailLoading(false);
          return;
        }
      } catch (err) {
        if (!cancelled) setError(getParsedApiError(err));
      } finally {
        if (!cancelled && !terminal) {
          setDetailLoading(false);
          timeout = window.setTimeout(tick, 2000);
        }
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timeout != null) window.clearTimeout(timeout);
    };
  }, [selectedRunId, loadTables]);

  const selectedPolicy = useMemo(
    () => riskPolicies.find((item) => item.profile === form.riskProfile),
    [riskPolicies, form.riskProfile],
  );
  const isResearch = form.mode === 'historical_agent_research';
  const researchPrecheck = useMemo(() => {
    const diagnostics = precheckResult?.diagnostics;
    if (!diagnostics || typeof diagnostics !== 'object') return null;
    const research = (diagnostics as Record<string, unknown>).research;
    return research && typeof research === 'object' ? (research as Record<string, unknown>) : null;
  }, [precheckResult]);
  const detailResearch = detail?.research && typeof detail.research === 'object'
    ? (detail.research as Record<string, unknown>)
    : null;
  const researchDecisions = Array.isArray(detailResearch?.decisions)
    ? (detailResearch?.decisions as Array<Record<string, unknown>>)
    : [];

  const handlePrecheck = async () => {
    setError(null);
    setPrecheckResult(null);
    setPrechecking(true);
    try {
      const config = buildConfig(form);
      let job = await backtestRunsApi.precheck(config);
      for (let index = 0; index < 120 && (job.status === 'queued' || job.status === 'running'); index += 1) {
        await sleep(500);
        job = await backtestRunsApi.getJob(job.jobId);
      }
      setPrecheckJob(job);
      setPrecheckResult((job.result as PrecheckPayload | null) || null);
      if (job.status === 'failed') {
        throw new Error(job.errorMessage || '预检失败');
      }
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setPrechecking(false);
    }
  };

  const handleSync = async () => {
    setError(null);
    setSyncing(true);
    try {
      const config = buildConfig(form);
      let job = await backtestRunsApi.syncMinuteBars({
        symbols: [],
        start_date: form.startDate,
        end_date: form.endDate,
        frequency: '5',
        adjustflag: '3',
        config,
      });
      for (let index = 0; index < 120 && (job.status === 'queued' || job.status === 'running'); index += 1) {
        await sleep(1000);
        job = await backtestRunsApi.getJob(job.jobId);
      }
      setSyncJob(job);
      if (job.status === 'failed') throw new Error(job.errorMessage || '分钟线同步失败');
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setSyncing(false);
    }
  };

  const handleCreate = async () => {
    setError(null);
    setCreating(true);
    try {
      const config = buildConfig(form);
      const run = await backtestRunsApi.create(config, allowIncomplete);
      setSelectedRunId(run.runId);
      setDetail(run as BacktestRunDetail);
      setTab('overview');
      await loadRuns(1);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setCreating(false);
    }
  };

  const handleCancel = async () => {
    if (!selectedRunId) return;
    setCancelling(true);
    try {
      const run = await backtestRunsApi.cancel(selectedRunId);
      setDetail((previous) => (previous ? { ...previous, ...run } : previous));
      await loadRuns(runsPage);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setCancelling(false);
    }
  };

  const handleRerun = async () => {
    if (!selectedRunId) return;
    try {
      const run = await backtestRunsApi.rerun(selectedRunId);
      setSelectedRunId(run.runId);
      setDetail(run as BacktestRunDetail);
      await loadRuns(1);
    } catch (err) {
      setError(getParsedApiError(err));
    }
  };

  const handleExport = async (kind: string) => {
    if (!selectedRunId) return;
    try {
      await backtestRunsApi.downloadExport(selectedRunId, kind);
    } catch (err) {
      setError(getParsedApiError(err));
    }
  };

  const handleOrdersPage = async (page: number) => {
    setOrdersPage(page);
    if (selectedRunId) {
      const payload = await backtestRunsApi.orders(selectedRunId, page, 100);
      setOrders(payload);
    }
  };
  const handleFillsPage = async (page: number) => {
    setFillsPage(page);
    if (selectedRunId) {
      const payload = await backtestRunsApi.fills(selectedRunId, page, 100);
      setFills(payload);
    }
  };
  const handleTradesPage = async (page: number) => {
    setTradesPage(page);
    if (selectedRunId) {
      const payload = await backtestRunsApi.trades(selectedRunId, page, 100);
      setTrades(payload);
    }
  };

  const precheckBlocked = precheckResult?.status === 'blocked';
  const precheckWarnings = Array.isArray(precheckResult?.warnings) ? (precheckResult?.warnings as string[]) : [];
  const precheckBlocking = Array.isArray(precheckResult?.blockingReasons) ? (precheckResult?.blockingReasons as string[]) : [];

  return (
    <div className="space-y-6">
      {error ? <ApiErrorAlert error={error} onDismiss={() => setError(null)} /> : null}

      <div className="grid gap-6 xl:grid-cols-[420px_minmax(0,1fr)]">
        <div className="flex flex-col gap-6">
          <SectionCard
            title={isResearch ? '新建历史 Agent 研究' : '新建组合实验'}
            subtitle={isResearch ? 'P3 · 选股 → 单股确认 · 月度' : 'P0 · recorded plan replay'}
            actions={<Badge variant="info">多 run 并行</Badge>}
          >
            <div className="space-y-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <Input label="开始日期" type="date" value={form.startDate} onChange={(event) => updateForm('startDate', event.target.value)} />
                <Input label="结束日期" type="date" value={form.endDate} onChange={(event) => updateForm('endDate', event.target.value)} />
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <Input label="初始本金 (CNY)" type="number" min="1" value={form.initialCash} onChange={(event) => updateForm('initialCash', event.target.value)} />
                <Input label="实验硬上限 · 单票 %" type="number" min="1" max="100" value={form.maxSingle} onChange={(event) => updateForm('maxSingle', event.target.value)} />
              </div>

              {isResearch ? (
                <div className="space-y-3 rounded-2xl border border-purple/20 bg-purple/5 p-3">
                  <div className="flex items-center gap-2 text-sm font-semibold text-purple">
                    <Activity className="h-4 w-4" />
                    P3 历史 Agent 研究
                  </div>
                  <Select
                    label="入场模型"
                    value={form.researchEntryMode}
                    onChange={(value) => updateForm('researchEntryMode', value as FormState['researchEntryMode'])}
                    options={[
                      { value: 'adaptive', label: '自适应（突破 + 回踩）' },
                      { value: 'pullback', label: '仅回踩 MA20' },
                      { value: 'breakout', label: '仅 20 日突破' },
                    ]}
                  />
                  <Input label="订单有效期（交易日）" type="number" min="1" max="30" value={form.researchOrderDays} onChange={(event) => updateForm('researchOrderDays', event.target.value)} />
                  <Select
                    label="单股确认"
                    value={form.researchConfirmationMode}
                    onChange={(value) => updateForm('researchConfirmationMode', value as FormState['researchConfirmationMode'])}
                    options={[
                      { value: 'auto_accept', label: '自动接受（纯 AI 选股）' },
                      { value: 'llm', label: 'LLM 单股确认（可能 reject）' },
                    ]}
                  />
                  {form.researchConfirmationMode === 'auto_accept' ? (
                    <p className="text-xs text-secondary-text">
                      选中的标的直接生成确定性计划，仍受资金/风险硬门槛和限价单约束；结果标 auto_accept_selection，适合看纯选股表现。
                    </p>
                  ) : null}
                  <Select
                    label="候选范围"
                    value={form.researchUniverseMode}
                    onChange={(value) => updateForm('researchUniverseMode', value as FormState['researchUniverseMode'])}
                    options={[
                      { value: 'explicit', label: '显式标的池（≤50）' },
                      { value: 'auto_local_coverage', label: '本地覆盖自动发现' },
                    ]}
                  />
                  {form.researchUniverseMode === 'explicit' ? (
                    <div>
                      <label className="mb-2 block text-sm font-medium text-foreground">研究标的池（≤50，逗号分隔）</label>
                      <textarea
                        className={TEXTAREA_CLASS}
                        value={form.researchUniverse}
                        onChange={(event) => updateForm('researchUniverse', event.target.value)}
                        placeholder="600519, 600330, 002475"
                      />
                    </div>
                  ) : (
                    <div className="space-y-2">
                      <Input label="自动候选上限" type="number" min="1" max="500" value={form.researchAutoLimit} onChange={(event) => updateForm('researchAutoLimit', event.target.value)} />
                      <p className="text-xs text-secondary-text">
                        每个决策日从本地所有有足够 as-of 日线的 A 股中自动发现候选；不会读取当前股票名单。结果标 local_coverage_universe + survivor_bias，不是全市场 PIT。
                      </p>
                    </div>
                  )}
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Input label="选股 Top N" type="number" min="1" max="50" value={form.researchTopN} onChange={(event) => updateForm('researchTopN', event.target.value)} />
                    <Input label="每次确认上限" type="number" min="1" max="20" value={form.researchConfirmLimit} onChange={(event) => updateForm('researchConfirmLimit', event.target.value)} />
                    <Input label="LLM 调用上限" type="number" min="1" max="5000" value={form.researchMaxCalls} onChange={(event) => updateForm('researchMaxCalls', event.target.value)} />
                    <Input label="最少历史 bar 数" type="number" min="20" max="720" value={form.researchMinBars} onChange={(event) => updateForm('researchMinBars', event.target.value)} />
                  </div>
                  <Input label="模型版本标签（仅记录，不切换实际模型）" value={form.researchModel} onChange={(event) => updateForm('researchModel', event.target.value)} placeholder="留空或填写当前 Agent 模型" />
                  <p className="text-xs text-secondary-text">
                    计划价格由确定性研究计划生成；LLM 只负责选股与确认。结果固定标记 survivor_bias、model_knowledge_leakage_risk、llm_nondeterminism。
                  </p>
                </div>
              ) : null}

              <div className="grid gap-3 sm:grid-cols-2">
                <Select
                  label={isResearch ? '样本来源（P3 固定）' : '样本来源'}
                  value={isResearch ? 'research' : form.sourcePipeline}
                  onChange={(value) => updateForm('sourcePipeline', value as FormState['sourcePipeline'])}
                  options={
                    isResearch
                      ? [{ value: 'research', label: 'P3 选股 → 单股确认' }]
                      : [
                          { value: 'single_stock', label: '单股直连' },
                          { value: 'selection_confirmed', label: '选股 → 单股确认' },
                        ]
                  }
                  disabled={isResearch}
                />
                <Select
                  label="风险档位"
                  value={form.riskProfile}
                  onChange={(value) => updateForm('riskProfile', value as FormState['riskProfile'])}
                  options={[
                    { value: 'conservative', label: '保守' },
                    { value: 'balanced', label: '平衡' },
                    { value: 'aggressive', label: '激进' },
                  ]}
                />
              </div>

              {selectedPolicy ? (
                <div className="rounded-2xl border border-cyan/15 bg-cyan/5 p-3 text-xs text-secondary-text">
                  <div className="mb-2 flex items-center gap-2 font-medium text-cyan">
                    <ShieldCheck className="h-4 w-4" />
                    硬门槛 · {RISK_LABELS[selectedPolicy.profile] || selectedPolicy.profile}
                  </div>
                  <div className="grid grid-cols-2 gap-2">
                    <span>单票 ≤ {selectedPolicy.maxSinglePositionPct}%</span>
                    <span>总仓位 ≤ {selectedPolicy.maxTotalExposurePct}%</span>
                    <span>最多 {selectedPolicy.maxConcurrentPositions} 只</span>
                    <span>最低现金 {selectedPolicy.minCashPct}%</span>
                    <span>单笔风险预算 {selectedPolicy.riskPerTradePct}%</span>
                    <span>选股 Top {selectedPolicy.selectionTopN ?? '--'}</span>
                  </div>
                </div>
              ) : null}

              {isResearch ? (
                <div className="rounded-xl border border-border/50 bg-elevated/40 p-3 text-xs text-secondary-text">
                  P3 使用日线 as-of 快照与月度决策，不读取 5 分钟撮合数据；严格模式缺历史 bar 会阻断。
                </div>
              ) : (
                <div className="grid gap-3 sm:grid-cols-2">
                  <Select
                    label="撮合粒度"
                    value={form.granularity}
                    onChange={(value) => updateForm('granularity', value as FormState['granularity'])}
                    options={[
                      { value: '5m', label: '5 分钟（严格）' },
                      { value: '1d', label: '日线（显式降级）' },
                    ]}
                  />
                  <Select
                    label="缺失行情策略"
                    value={form.granularity === '1d' ? 'strict' : form.missingDataPolicy}
                    onChange={(value) => updateForm('missingDataPolicy', value as FormState['missingDataPolicy'])}
                    options={[
                      { value: 'strict', label: '严格 · 缺数据阻断' },
                      { value: 'daily_downgrade', label: '显式日线降级' },
                    ]}
                    disabled={form.granularity === '1d'}
                  />
                </div>
              )}

              <div className="grid gap-3 sm:grid-cols-2">
                <Select
                  label="费用策略"
                  value={form.feePolicy}
                  onChange={(value) => updateForm('feePolicy', value)}
                  options={feePolicies.map((item) => ({ value: item.version, label: item.version }))}
                />
                <Select
                  label="公司行为"
                  value={form.corporateActionPolicy}
                  onChange={(value) => updateForm('corporateActionPolicy', value as FormState['corporateActionPolicy'])}
                  options={[
                    { value: 'assume_none', label: '显式假设无已知行动' },
                    { value: 'strict', label: '严格 · 缺数据 partial' },
                  ]}
                />
              </div>

              <div className="grid gap-3 sm:grid-cols-3">
                <Input label="滑点 bps" type="number" min="0" value={form.slippageBps} onChange={(event) => updateForm('slippageBps', event.target.value)} />
                <Input label="成交量参与率" type="number" min="0.0001" max="1" step="0.0001" value={form.participationRate} onChange={(event) => updateForm('participationRate', event.target.value)} />
                <Input label="总仓位硬上限 %" type="number" min="1" max="100" value={form.maxTotal} onChange={(event) => updateForm('maxTotal', event.target.value)} />
              </div>

              <Input label="基准代码（可选）" placeholder="000300" value={form.benchmarkSymbol} onChange={(event) => updateForm('benchmarkSymbol', event.target.value)} />

              {!isResearch ? (
                <>
                  <div>
                    <label className="mb-2 block text-sm font-medium text-foreground">计划 ID（可选，逗号分隔）</label>
                    <textarea className={TEXTAREA_CLASS} value={form.planIdsText} onChange={(event) => updateForm('planIdsText', event.target.value)} placeholder="plan-xxxx, plan-yyyy" />
                  </div>
                  <div>
                    <label className="mb-2 block text-sm font-medium text-foreground">Trace ID（可选，逗号分隔）</label>
                    <textarea className={TEXTAREA_CLASS} value={form.traceIdsText} onChange={(event) => updateForm('traceIdsText', event.target.value)} placeholder="trace-xxxx" />
                  </div>
                </>
              ) : null}

              {!isResearch && form.sourcePipeline === 'selection_confirmed' ? (
                <div>
                  <label className="mb-2 block text-sm font-medium text-foreground">选股 → 确认显式父链（JSON 数组）</label>
                  <textarea
                    className={TEXTAREA_CLASS}
                    value={form.selectionLinksText}
                    onChange={(event) => updateForm('selectionLinksText', event.target.value)}
                    placeholder='[{"selection_trace_ref":"...","confirmation_trace_ref":"...","symbol":"600519"}]'
                  />
                  <p className="mt-1 text-xs text-secondary-text">缺少显式父链的计划会被排除，不会猜测关联。</p>
                </div>
              ) : null}

              <div>
                <label className="mb-2 block text-sm font-medium text-foreground">备注</label>
                <textarea className={TEXTAREA_CLASS} value={form.notes} onChange={(event) => updateForm('notes', event.target.value)} placeholder="实验假设、数据版本、对照说明…" />
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <Button variant="secondary" onClick={() => void handlePrecheck()} isLoading={prechecking}>
                  <RefreshCw className="h-4 w-4" />
                  数据预检
                </Button>
                <Button variant="outline" onClick={() => void handleSync()} isLoading={syncing}>
                  <CloudDownload className="h-4 w-4" />
                  同步 5 分钟线
                </Button>
                <Button variant="primary" onClick={() => void handleCreate()} isLoading={creating}>
                  <Play className="h-4 w-4" />
                  创建实验
                </Button>
                <label className="ml-auto flex items-center gap-2 text-xs text-secondary-text">
                  <input type="checkbox" checked={allowIncomplete} onChange={(event) => setAllowIncomplete(event.target.checked)} />
                  允许不完整数据运行（标 partial）
                </label>
              </div>

              {candidates.length ? (
                <p className="text-xs text-secondary-text">当前结构化候选计划：{candidates.length} 个（可留空计划 ID，按日期范围自动纳入）。</p>
              ) : null}

              {syncJob ? (
                <div className="rounded-xl border border-border/50 bg-elevated/40 p-3 text-xs text-secondary-text">
                  <div className="flex items-center gap-2">
                    <Badge variant={syncJob.status === 'failed' ? 'danger' : syncJob.status === 'partial' ? 'warning' : 'success'}>
                      sync · {syncJob.status}
                    </Badge>
                    <span>{syncJob.jobId}</span>
                  </div>
                  {syncJob.result ? (
                    <p className="mt-2">
                      写入 {String((syncJob.result as Record<string, unknown>).writtenRows ?? '--')} 行 /
                      失败 {String((syncJob.result as Record<string, unknown>).failedSymbols ?? '--')} 个标的
                    </p>
                  ) : null}
                </div>
              ) : null}

              {precheckJob ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2 text-xs text-secondary-text">
                    <Badge variant={precheckJob.status === 'failed' ? 'danger' : precheckJob.status === 'running' ? 'info' : 'success'}>
                      precheck · {precheckJob.status}
                    </Badge>
                    <span>{precheckJob.jobId}</span>
                  </div>
                  {precheckBlocked ? (
                    <InlineAlert variant="danger" title="预检阻断" message={precheckBlocking.join(' / ') || '数据不完整'} />
                  ) : precheckWarnings.length ? (
                    <InlineAlert variant="warning" title="预检提示" message={precheckWarnings.join(' / ')} />
                  ) : precheckResult ? (
                    <InlineAlert variant="success" title="预检通过" message={`纳入 ${String(precheckResult.includedPlans ?? 0)} 个计划，排除 ${String(precheckResult.excludedPlans ?? 0)} 个。`} />
                  ) : null}
                  {Array.isArray(precheckResult?.coverage) ? <CoverageTable rows={precheckResult?.coverage as CoverageRow[]} /> : null}
                  {Array.isArray(precheckResult?.inputs) && (precheckResult?.inputs as InputRow[]).length ? (
                    <div className="rounded-xl border border-border/50 bg-elevated/30 p-3">
                      <div className="mb-2 flex items-center justify-between gap-2">
                        <h3 className="text-sm font-semibold text-foreground">输入排除明细</h3>
                        <span className="text-xs text-secondary-text">
                          纳入 {(precheckResult?.inputs as InputRow[]).filter((item) => item.included).length} /
                          排除 {(precheckResult?.inputs as InputRow[]).filter((item) => !item.included).length}
                        </span>
                      </div>
                      <div className="max-h-56 overflow-auto">
                        <table className="w-full min-w-[680px] text-left text-xs">
                          <thead className="text-secondary-text">
                            <tr>
                              <th className="px-2 py-1">标的</th>
                              <th className="px-2 py-1">计划</th>
                              <th className="px-2 py-1">来源</th>
                              <th className="px-2 py-1">纳入</th>
                              <th className="px-2 py-1">排除原因</th>
                            </tr>
                          </thead>
                          <tbody>
                            {(precheckResult?.inputs as InputRow[]).map((item) => (
                              <tr key={item.inputId} className="border-t border-border/40">
                                <td className="px-2 py-1 text-foreground">{item.symbol || '--'}</td>
                                <td className="px-2 py-1 text-secondary-text">{item.planId || item.sourceId || '--'}</td>
                                <td className="px-2 py-1 text-secondary-text">{item.sourcePipeline || '--'}</td>
                                <td className="px-2 py-1">
                                  <Badge variant={item.included ? 'success' : 'warning'}>{item.included ? '是' : '否'}</Badge>
                                </td>
                                <td className="px-2 py-1 text-secondary-text">{item.exclusionReason || '--'}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    </div>
                  ) : null}
                  {researchPrecheck ? (
                    <div className="rounded-xl border border-purple/20 bg-purple/5 p-3 text-xs text-secondary-text">
                      <div className="mb-2 flex flex-wrap items-center gap-2">
                        <Badge variant="history">P3 研究预检</Badge>
                        <span>标的 {String(researchPrecheck.universeSize ?? '--')}</span>
                        <span>决策 {String(researchPrecheck.decisionCount ?? '--')} 次</span>
                        <span>数据 {String(researchPrecheck.dataSource ?? '--')}</span>
                        <span>预计 LLM 调用 {String(researchPrecheck.estimatedLlmCalls ?? '--')} / {String(researchPrecheck.maxLlmCalls ?? '--')}</span>
                        <span>模型 {String(researchPrecheck.modelRef ?? '--')}</span>
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {Array.isArray(researchPrecheck.labels) ? (researchPrecheck.labels as string[]).map((label) => (
                          <Badge key={label} variant="warning">{label}</Badge>
                        )) : null}
                      </div>
                      {researchPrecheck.toolGaps && typeof researchPrecheck.toolGaps === 'object' ? (
                        <p className="mt-2">工具缺口：{Object.keys(researchPrecheck.toolGaps as Record<string, unknown>).join(', ') || '无'}</p>
                      ) : null}
                    </div>
                  ) : null}
                  {precheckBlocked && precheckBlocking.includes('no_included_plans') ? (
                    <InlineAlert
                      variant="warning"
                      title="没有可执行计划"
                      message="请检查：计划/Trace 筛选是否为空或写错；artifact 是否为 generated；是否有最新 account decision；decision 是否为 actionable/waiting_confirmation；selection_confirmed 是否提供了匹配的 selection_links；计划有效期是否在实验区间内。"
                    />
                  ) : null}
                </div>
              ) : null}
            </div>
          </SectionCard>

          <SectionCard title="实验列表" subtitle={`${runsTotal} 个 run`} actions={<Button variant="ghost" size="sm" onClick={() => void loadRuns(runsPage)} isLoading={runsLoading}><RefreshCw className="h-4 w-4" />刷新</Button>}>
            {runs.length ? (
              <div className="space-y-3">
                <div className="divide-y divide-border/40">
                  {runs.map((run) => (
                    <button
                      key={run.runId}
                      type="button"
                      onClick={() => {
                        setSelectedRunId(run.runId);
                        setTab('overview');
                        setOrdersPage(1);
                        setFillsPage(1);
                        setTradesPage(1);
                      }}
                      className={cn(
                        'flex w-full items-center gap-3 py-3 text-left transition hover:bg-hover/40',
                        selectedRunId === run.runId ? 'bg-cyan/5' : '',
                      )}
                    >
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant={statusVariant(run.status)}>{STATUS_LABELS[run.status] || run.status}</Badge>
                          <span className="truncate font-mono text-xs text-foreground">{shortRunId(run.runId)}</span>
                          {run.reusedFromRunId ? <Badge variant="history">复用</Badge> : null}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-3 text-xs text-secondary-text">
                          <span>{PIPELINE_LABELS[run.sourcePipeline || ''] || run.sourcePipeline || '--'}</span>
                          <span>{RISK_LABELS[run.riskProfile || ''] || run.riskProfile || '--'}</span>
                          <span>{run.createdAt?.slice(0, 16).replace('T', ' ') || '--'}</span>
                          <span>{fmtPct(run.metrics?.totalReturnPct)}</span>
                        </div>
                        <div className="mt-2 h-1 bg-muted">
                          <div className="h-1 bg-cyan" style={{ width: `${Math.max(2, Math.round((run.progress || 0) * 100))}%` }} />
                        </div>
                      </div>
                      <ChevronRight className="h-4 w-4 shrink-0 text-secondary-text" />
                    </button>
                  ))}
                </div>
                <Pagination currentPage={runsPage} totalPages={Math.max(1, Math.ceil(runsTotal / 20))} onPageChange={(page) => void loadRuns(page)} />
              </div>
            ) : (
              <EmptyState title="还没有实验" description="先做一次数据预检，然后创建第一个组合回测。" />
            )}
          </SectionCard>
        </div>

        <div className="flex flex-col gap-6">
          {!detail ? (
            <SectionCard title="实验详情" subtitle="选择左侧 run 查看">
              <EmptyState title="未选择实验" description="创建或从左侧列表选择一个 run，查看净值、订单、成交和输入审计。" icon={<Table2 className="h-8 w-8" />} />
            </SectionCard>
          ) : (
            <>
              <SectionCard
                title="实验详情"
                subtitle={shortRunId(detail.runId)}
                actions={
                  <div className="flex flex-wrap items-center gap-2">
                    {!isTerminal(detail.status) ? (
                      <Button variant="danger-subtle" size="sm" onClick={() => void handleCancel()} isLoading={cancelling}>
                        <Square className="h-4 w-4" />取消
                      </Button>
                    ) : (
                      <Button variant="secondary" size="sm" onClick={() => void handleRerun()}>
                        <RefreshCw className="h-4 w-4" />重跑
                      </Button>
                    )}
                    <Button variant="ghost" size="sm" onClick={() => selectedRunId && void loadTables(selectedRunId)} isLoading={detailLoading}>
                      <RefreshCw className="h-4 w-4" />刷新
                    </Button>
                  </div>
                }
              >
                <div className="space-y-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant={statusVariant(detail.status)}>{STATUS_LABELS[detail.status] || detail.status}</Badge>
                    <Badge variant="info">{PIPELINE_LABELS[detail.sourcePipeline || ''] || detail.sourcePipeline || '--'}</Badge>
                    <Badge variant="history">{RISK_LABELS[detail.riskProfile || ''] || detail.riskProfile || '--'}</Badge>
                    {detail.dataQuality ? <Badge variant={detail.dataQuality === 'complete' ? 'success' : 'warning'}>{detail.dataQuality}</Badge> : null}
                    {detail.reusedFromRunId ? <Badge variant="history">复用自 {shortRunId(detail.reusedFromRunId)}</Badge> : null}
                  </div>

                  {!isTerminal(detail.status) ? (
                    <div>
                      <div className="mb-1 flex items-center justify-between text-xs text-secondary-text">
                        <span>{detail.phase || '处理中'}</span>
                        <span>{Math.round((detail.progress || 0) * 100)}%</span>
                      </div>
                      <div className="h-2 bg-muted">
                        <div className="h-2 bg-cyan transition-all" style={{ width: `${Math.max(2, Math.round((detail.progress || 0) * 100))}%` }} />
                      </div>
                    </div>
                  ) : null}

                  {detail.status === 'failed' || detail.status === 'interrupted' ? (
                    <InlineAlert variant="danger" title={detail.errorCode || '运行失败'} message={detail.errorMessage || '查看诊断信息。'} />
                  ) : detail.status === 'partial' ? (
                    <InlineAlert
                      variant="warning"
                      title="结果标记为 partial"
                      message={`数据质量 ${detail.dataQuality || 'partial'}；${(detail.qualityFlags || []).join(' / ') || '存在未完全验证的输入'}。`}
                    />
                  ) : null}

                  <MetricGrid
                    metrics={detail.metrics}
                    excessVsShanghai={benchmarkSummary?.excessVsShanghaiPct ?? null}
                    shanghaiReturn={benchmarkSummary?.shanghaiReturnPct ?? null}
                  />

                  <div className="flex flex-wrap gap-2">
                    {['config', 'manifest', 'metrics', 'ledger', 'fills', 'orders', 'equity', 'trades'].map((kind) => (
                      <Button key={kind} variant="outline" size="sm" onClick={() => void handleExport(kind)}>
                        <Download className="h-4 w-4" />{kind}
                      </Button>
                    ))}
                  </div>

                  <div className="flex flex-wrap gap-2 border-b border-border/50 pb-2">
                    {TAB_ITEMS.map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        onClick={() => setTab(item.key)}
                        className={cn(
                          'rounded-lg px-3 py-1.5 text-sm transition',
                          tab === item.key ? 'bg-cyan/10 font-medium text-cyan' : 'text-secondary-text hover:bg-hover',
                        )}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>

                  {tab === 'overview' ? (
                    <div className="space-y-4">
                      {detailResearch ? (
                        <div className="rounded-xl border border-purple/20 bg-purple/5 p-3">
                          <div className="mb-2 flex flex-wrap items-center gap-2">
                            <Badge variant="history">P3 历史 Agent 研究</Badge>
                            <span className="text-xs text-secondary-text">模型 {String(detailResearch.modelRef ?? '--')}</span>
                            <span className="text-xs text-secondary-text">Prompt {String(detailResearch.promptVersion ?? '--')}</span>
                            <span className="text-xs text-secondary-text">LLM {String(detailResearch.llmCalls ?? 0)} 次 / 缓存 {String(detailResearch.cacheHits ?? 0)} 次</span>
                            <span className="text-xs text-secondary-text">数据 {String(detailResearch.dataSource ?? '--')}</span>
                          </div>
                          <div className="mb-2 flex flex-wrap gap-1">
                            {(detail.labels || []).map((label) => <Badge key={label} variant="warning">{label}</Badge>)}
                          </div>
                          {researchDecisions.length ? (
                            <div className="max-h-48 overflow-auto">
                              <table className="w-full min-w-[640px] text-left text-xs">
                                <thead className="text-secondary-text">
                                  <tr>
                                    <th className="px-2 py-1">决策日</th>
                                    <th className="px-2 py-1">状态</th>
                                    <th className="px-2 py-1">选中</th>
                                    <th className="px-2 py-1">确认计划</th>
                                    <th className="px-2 py-1">备注</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {researchDecisions.map((decision, index) => (
                                    <tr key={`${String(decision.session)}-${index}`} className="border-t border-border/40">
                                      <td className="px-2 py-1 text-foreground">{String(decision.session ?? '--')}</td>
                                      <td className="px-2 py-1">{String(decision.status ?? '--')}</td>
                                      <td className="px-2 py-1">{Array.isArray(decision.selected) ? decision.selected.length : 0}</td>
                                      <td className="px-2 py-1">{Array.isArray(decision.plans) ? decision.plans.length : 0}</td>
                                      <td className="px-2 py-1 text-secondary-text">{String(decision.noTradeReason ?? decision.selectionGap ?? '--')}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          ) : null}
                        </div>
                      ) : null}
                      {equity.length ? <EquityChart points={equity} benchmarks={benchmarks} /> : <EmptyState title="暂无净值曲线" description="运行完成后会生成逐日权益点。" />}
                      <div className="grid gap-4 lg:grid-cols-2">
                        <div>
                          <h3 className="mb-2 text-sm font-semibold text-foreground">行情覆盖</h3>
                          <CoverageTable rows={detail.coverage} />
                        </div>
                        <div>
                          <h3 className="mb-2 text-sm font-semibold text-foreground">期末持仓</h3>
                          <div className="mb-2 flex flex-wrap gap-3 text-xs text-secondary-text">
                            <span>已实现 {detail.realizedPnl ?? '--'}</span>
                            <span>期末权益 {detail.finalEquity ?? '--'}</span>
                            <span>现金 {detail.cash ?? '--'}</span>
                            <span>成本 {positionSummary.totalCost != null ? positionSummary.totalCost.toFixed(2) : '--'}</span>
                            <span>市值 {positionSummary.marketValue != null ? positionSummary.marketValue.toFixed(2) : '--'}</span>
                            <span className={cn('font-medium', (positionSummary.unrealizedPnl ?? 0) >= 0 ? 'text-danger' : 'text-success')}>
                              未实现 {positionSummary.unrealizedPnl != null ? positionSummary.unrealizedPnl.toFixed(2) : '--'}
                            </span>
                          </div>
                          <PositionsTable rows={positions} />
                        </div>
                      </div>
                    </div>
                  ) : null}
                  {tab === 'orders' ? <OrdersTable data={orders} onPage={(page) => void handleOrdersPage(page)} /> : null}
                  {tab === 'fills' ? <FillsTable data={fills} onPage={(page) => void handleFillsPage(page)} /> : null}
                  {tab === 'trades' ? <TradesTable data={trades} onPage={(page) => void handleTradesPage(page)} /> : null}
                  {tab === 'inputs' ? <InputsTable inputs={inputs.length ? inputs : detail.inputs || []} /> : null}
                  {tab === 'diagnostics' ? (
                    <div className="space-y-3">
                      <div className="flex flex-wrap gap-2">
                        {(detail.qualityFlags || []).map((flag) => <Badge key={flag} variant="warning">{flag}</Badge>)}
                      </div>
                      <pre className="max-h-[520px] overflow-auto rounded-xl border border-border/50 bg-surface-2 p-4 text-xs text-secondary-text">
                        {JSON.stringify(diagnostics, null, 2)}
                      </pre>
                    </div>
                  ) : null}
                </div>
              </SectionCard>
            </>
          )}
        </div>
      </div>
    </div>
  );
};

export default BacktestRunsPage;
