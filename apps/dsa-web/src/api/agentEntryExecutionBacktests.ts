import apiClient from './index';
import { toCamelCase } from './utils';

export type EntryExecutionStrategyName =
  | 'strict_ai_entry'
  | 'next_open_baseline'
  | 'atr_elastic_entry'
  | 'breakout_fallback_entry'
  | string;

export type EntryExecutionStrategyResult = {
  status?: string;
  strategy?: string;
  entryDate?: string | null;
  entryPrice?: number | null;
  exitDate?: string | null;
  exitPrice?: number | null;
  exitReason?: string | null;
  holdingDays?: number | null;
  pnlPct?: number | null;
  ambiguousBar?: boolean;
  tradeRule?: string | null;
  sellableFrom?: string | null;
  entryReason?: string | null;
  limits?: string[];
};

export type EntryExecutionTradePlan = {
  decisionDate?: string;
  tsCode?: string;
  name?: string;
  rank?: number;
  entryRule?: string;
  entryZoneLow?: number | null;
  entryZoneHigh?: number | null;
  breakoutTrigger?: number | null;
  stopLossPrice?: number | null;
  takeProfitPrice?: number | null;
  entryExpiryDays?: number;
  signalValidDays?: number;
  signalValidUntil?: string | null;
  signalValidityLabel?: string | null;
  maxHoldDays?: number;
  executionMode?: string;
  finalAction?: string;
  parseStatus?: string;
};

export type EntryExecutionBacktestRow = {
  schemaVersion?: string;
  traceId?: string;
  traceDir?: string;
  decisionDate?: string;
  tsCode?: string;
  name?: string;
  rank?: number;
  tradePlan?: EntryExecutionTradePlan;
  evaluation?: EntryExecutionStrategyResult;
  strategies?: Record<EntryExecutionStrategyName, EntryExecutionStrategyResult>;
  warnings?: string[];
  parseStatus?: string;
  priceData?: EntryExecutionPriceData;
};

export type EntryExecutionPriceData = {
  granularity?: string;
  source?: string;
  status?: string;
  code?: string;
  barCount?: number;
  frequency?: string;
  adjustflag?: string;
  firstBarAt?: string | null;
  lastBarAt?: string | null;
  dailyBars?: EntryExecutionDailyBar[];
};

export type EntryExecutionDailyBar = {
  date?: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
};

export type EntryExecutionBacktestSummary = {
  total?: number;
  fillRatePct?: number;
  strategyCounts?: Record<string, number>;
  statusCounts?: Record<string, number>;
  avgPnlPct?: Record<string, number | null>;
  medianPnlPct?: Record<string, number | null>;
  strategyMetrics?: Record<string, EntryExecutionStrategyMetrics>;
  headlineMetrics?: EntryExecutionHeadlineMetrics;
};

export type EntryExecutionStrategyMetrics = {
  total?: number;
  filled?: number;
  notFilled?: number;
  skipped?: number;
  fillRatePct?: number;
  winCount?: number;
  lossCount?: number;
  flatCount?: number;
  winRatePct?: number;
  avgPnlPct?: number | null;
  medianPnlPct?: number | null;
  totalPnlPct?: number | null;
  compoundedPnlPct?: number | null;
  bestPnlPct?: number | null;
  worstPnlPct?: number | null;
  avgWinPct?: number | null;
  avgLossPct?: number | null;
  payoffRatio?: number | null;
};

export type EntryExecutionHeadlineMetrics = {
  bestStrategy?: string | null;
  bestCompoundedPnlPct?: number | null;
  bestTotalPnlPct?: number | null;
  bestWinRatePct?: number | null;
  bestFillRatePct?: number | null;
  bestFilled?: number;
  bestAvgPnlPct?: number | null;
};

export type EntryExecutionBacktestResponse = {
  sourcePath?: string;
  exists?: boolean;
  total?: number;
  page?: number;
  pageSize?: number;
  totalPages?: number;
  availableDates?: string[];
  items: EntryExecutionBacktestRow[];
  summary: EntryExecutionBacktestSummary;
  historySummary?: EntryExecutionBacktestSummary;
};

export type EntryExecutionBacktestBuildResult = {
  traceCount?: number;
  reviewCount?: number;
  outputPath?: string;
  skipped?: number;
};

export type EntryExecutionMinuteSyncItem = {
  symbol?: string;
  status?: string;
  startDate?: string;
  endDate?: string;
  fetchedRows?: number;
  writtenRows?: number;
  error?: string;
  reason?: string;
  coverage?: {
    count?: number;
    minDatetime?: string | null;
    maxDatetime?: string | null;
    frequency?: string;
    adjustflag?: string;
  };
  traceIds?: string[];
};

export type EntryExecutionMinuteSyncResult = {
  traceCount?: number;
  planCount?: number;
  symbolCount?: number;
  fetchedSymbols?: number;
  skippedSymbols?: number;
  failedSymbols?: number;
  fetchedRows?: number;
  writtenRows?: number;
  frequency?: string;
  adjustflag?: string;
  items?: EntryExecutionMinuteSyncItem[];
};

export type EntryExecutionMinuteSyncResponse = {
  sync?: EntryExecutionMinuteSyncResult;
  rebuild?: EntryExecutionBacktestBuildResult;
};

export type EntryExecutionBacktestFilters = {
  strategy?: string;
  symbol?: string;
  decisionDate?: string;
  page?: number;
  pageSize?: number;
  limit?: number;
};

export type EntryExecutionBacktestRebuildOptions = {
  limit?: number;
};

export type EntryExecutionMinuteSyncOptions = {
  limit?: number;
  decisionDate?: string;
  symbol?: string;
  frequency?: '5' | '15' | '30' | '60';
  adjustflag?: '1' | '2' | '3';
  rebuild?: boolean;
};

const strategyKeyAliases: Record<string, EntryExecutionStrategyName> = {
  strictAiEntry: 'strict_ai_entry',
  strict_ai_entry: 'strict_ai_entry',
  nextOpenBaseline: 'next_open_baseline',
  next_open_baseline: 'next_open_baseline',
  atrElasticEntry: 'atr_elastic_entry',
  atr_elastic_entry: 'atr_elastic_entry',
  breakoutFallbackEntry: 'breakout_fallback_entry',
  breakout_fallback_entry: 'breakout_fallback_entry',
};

const statusKeyAliases: Record<string, string> = {
  notFilled: 'not_filled',
  not_filled: 'not_filled',
  strategySkipped: 'strategy_skipped',
  strategy_skipped: 'strategy_skipped',
  insufficientStartPrice: 'insufficient_start_price',
  insufficient_start_price: 'insufficient_start_price',
  insufficientForwardBars: 'insufficient_forward_bars',
  insufficient_forward_bars: 'insufficient_forward_bars',
  invalidTradePlan: 'invalid_trade_plan',
  invalid_trade_plan: 'invalid_trade_plan',
};

function normalizeKeyedRecord<T>(record: Record<string, T> | undefined, aliases: Record<string, string>): Record<string, T> {
  if (!record) return {};
  return Object.fromEntries(
    Object.entries(record).map(([key, value]) => [aliases[key] || key, value]),
  );
}

function normalizeStrategyRecord<T>(record: Record<string, T> | undefined): Record<EntryExecutionStrategyName, T> {
  return normalizeKeyedRecord(record, strategyKeyAliases) as Record<EntryExecutionStrategyName, T>;
}

function normalizeSummary(summary: EntryExecutionBacktestSummary | undefined): EntryExecutionBacktestSummary {
  return {
    ...(summary ?? {}),
    strategyCounts: normalizeStrategyRecord(summary?.strategyCounts),
    statusCounts: normalizeKeyedRecord(summary?.statusCounts, statusKeyAliases),
    avgPnlPct: normalizeStrategyRecord(summary?.avgPnlPct),
    medianPnlPct: normalizeStrategyRecord(summary?.medianPnlPct),
    strategyMetrics: normalizeStrategyRecord(summary?.strategyMetrics),
  };
}

function normalizeRow(row: EntryExecutionBacktestRow): EntryExecutionBacktestRow {
  return {
    ...row,
    strategies: normalizeStrategyRecord(row.strategies),
  };
}

export const agentEntryExecutionBacktestsApi = {
  list: async (filters: EntryExecutionBacktestFilters = {}): Promise<EntryExecutionBacktestResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/agent-entry-execution-backtests', {
      params: {
        strategy: filters.strategy || undefined,
        symbol: filters.symbol || undefined,
        decision_date: filters.decisionDate || undefined,
        page: filters.page ?? 1,
        page_size: filters.pageSize ?? 20,
        limit: filters.limit ?? 300,
      },
    });
    const data = toCamelCase<EntryExecutionBacktestResponse>(response.data);
    return {
      sourcePath: data.sourcePath,
      exists: data.exists,
      total: data.total ?? 0,
      page: data.page ?? 1,
      pageSize: data.pageSize ?? 20,
      totalPages: data.totalPages ?? 0,
      availableDates: data.availableDates ?? [],
      items: (data.items ?? []).map(normalizeRow),
      summary: normalizeSummary(data.summary),
      historySummary: normalizeSummary(data.historySummary),
    };
  },

  rebuild: async (options: EntryExecutionBacktestRebuildOptions = {}): Promise<EntryExecutionBacktestBuildResult> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/agent-entry-execution-backtests/rebuild', undefined, {
      params: {
        limit: options.limit ?? 300,
      },
    });
    return toCamelCase<EntryExecutionBacktestBuildResult>(response.data);
  },

  syncMinuteBars: async (options: EntryExecutionMinuteSyncOptions = {}): Promise<EntryExecutionMinuteSyncResponse> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/agent-entry-execution-backtests/minute-bars/sync', undefined, {
      params: {
        limit: options.limit,
        decision_date: options.decisionDate || undefined,
        symbol: options.symbol || undefined,
        frequency: options.frequency ?? '5',
        adjustflag: options.adjustflag ?? '3',
        rebuild: options.rebuild ?? true,
      },
      timeout: 600000,
    });
    return toCamelCase<EntryExecutionMinuteSyncResponse>(response.data);
  },
};
