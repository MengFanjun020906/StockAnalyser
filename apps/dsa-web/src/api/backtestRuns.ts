import apiClient from './index';
import { toCamelCase } from './utils';

export type BacktestRunStatus =
  | 'queued'
  | 'validating'
  | 'running'
  | 'succeeded'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export type BacktestRunMetrics = {
  quality?: string;
  totalReturnPct?: number | null;
  annualizedReturnPct?: number | null;
  volatilityPct?: number | null;
  sharpe?: number | null;
  maxDrawdownPct?: number | null;
  maxDrawdownStart?: string | null;
  maxDrawdownTrough?: string | null;
  maxDrawdownRecovery?: string | null;
  benchmarkReturnPct?: number | null;
  excessReturnPct?: number | null;
  cashRatioPct?: number | null;
  averagePositionPct?: number | null;
  turnoverPct?: number | null;
  feeDragPct?: number | null;
  slippageDragPct?: number | null;
  tradingDays?: number;
  dailyReturnCount?: number;
  notApplicable?: Record<string, string>;
  singleTrade?: Record<string, number | string | null>;
};

export type BacktestRunSummary = {
  runId: string;
  mode: string;
  sourcePipeline?: string;
  riskProfile?: string;
  status: BacktestRunStatus;
  attempt: number;
  requestFingerprint?: string | null;
  experimentFingerprint?: string | null;
  reusedFromRunId?: string | null;
  dataQuality?: string;
  realizedPnl?: string | null;
  unrealizedPnl?: string | null;
  finalEquity?: string | null;
  cash?: string | null;
  research?: Record<string, unknown> | null;
  labels?: string[];
  llmCalls?: number;
  cacheHits?: number;
  qualityFlags?: string[];
  progress?: number;
  phase?: string;
  metrics?: BacktestRunMetrics | null;
  counts?: Record<string, number | string | null>;
  errorCode?: string | null;
  errorMessage?: string | null;
  createdAt?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
  cancelRequested?: boolean;
  resultAvailable?: boolean;
  config?: Record<string, unknown> | null;
  deduplicated?: boolean;
  precheckStatus?: string | null;
  precheckWarnings?: string[];
};

export type CoverageRow = {
  symbol: string;
  sessionsExpected: number;
  sessionsFull: number;
  sessionsPartial: number;
  sessionsMissing: number;
  sessionsSuspended: number;
  sessionsPreListing: number;
  missingBars: number;
  expectedBarsPerSession: number;
  quality: string;
  flags: string[];
};

export type InputRow = {
  inputId: string;
  sourceId: string;
  planId?: string | null;
  scenarioId?: string | null;
  decisionId?: string | null;
  symbol?: string | null;
  included: boolean;
  exclusionReason?: string | null;
  sourcePipeline?: string | null;
  selectionTraceRef?: string | null;
  confirmationTraceRef?: string | null;
  linkQuality?: string | null;
  traceRefs?: string[];
  qualityFlags?: string[];
};

export type BacktestRunDetail = BacktestRunSummary & {
  manifest?: Record<string, unknown> | null;
  coverage?: CoverageRow[];
  inputs?: InputRow[];
  diagnostics?: Record<string, unknown>;
};

export type EquityPoint = {
  session: string;
  timestamp?: string | null;
  cash: number;
  reservedCash: number;
  availableCash: number;
  marketValue: number;
  equity: number;
  peakEquity: number;
  drawdownPct: number;
  stale: boolean;
  benchmarkValue?: number | null;
  granularity?: string;
  quality?: string;
};

export type BenchmarkPoint = {
  session: string;
  value: number | null;
};

export type BenchmarkSeries = {
  symbol: string;
  name: string;
  status: string;
  points: BenchmarkPoint[];
  firstSession?: string;
  lastSession?: string;
};

export type BenchmarkPayload = {
  runId: string;
  initialEquity: number;
  series: BenchmarkSeries[];
  warnings: string[];
  benchmarkReturns?: Record<string, number>;
  strategyReturnPct?: number | null;
  shanghaiReturnPct?: number | null;
  excessVsShanghaiPct?: number | null;
  source?: string;
  note?: string;
};

export type OrderRow = {
  orderId: string;
  planKey?: string | null;
  symbol: string;
  side: string;
  orderType: string;
  quantity: number;
  remaining: number;
  filledQuantity: number;
  avgFillPrice?: number | null;
  limitPrice?: number | null;
  stopPrice?: number | null;
  status: string;
  reason?: string | null;
  preRegisteredExit?: boolean;
  exitReason?: string | null;
  submittedAt?: string | null;
  activeFrom?: string | null;
  expiresOnSession?: string | null;
};

export type FillRow = {
  fillId: string;
  orderId: string;
  symbol: string;
  side: string;
  quantity: number;
  price: number;
  grossAmount: number;
  commission: number;
  transferFee: number;
  stampDuty: number;
  slippageCost: number;
  session: string;
  barEndAt?: string | null;
  quality?: string | null;
  reason?: string | null;
};

export type TradeRow = {
  tradeId: string;
  symbol: string;
  entryOrderId: string;
  exitOrderId: string;
  quantity: number;
  entryPrice: number;
  exitPrice: number;
  costBasis: number;
  netProceeds: number;
  realizedPnl: number;
  returnPct: number;
  entrySession: string;
  exitSession: string;
  holdingDays: number;
  exitReason: string;
  mfePct?: number | null;
  maePct?: number | null;
};

export type PositionRow = {
  symbol: string;
  quantity: number;
  costTotal: number;
  averageCost?: number | null;
  firstAcquired?: string | null;
};

export type DataJob = {
  jobId: string;
  jobType: string;
  status: string;
  request?: Record<string, unknown> | null;
  result?: Record<string, unknown> | null;
  progress?: number;
  errorCode?: string | null;
  errorMessage?: string | null;
  createdAt?: string | null;
  startedAt?: string | null;
  finishedAt?: string | null;
};

export type RiskPolicyPreset = {
  profile: string;
  policyVersion: string;
  maxSinglePositionPct: number;
  maxTotalExposurePct: number;
  maxConcurrentPositions: number;
  minCashPct: number;
  riskPerTradePct: number;
  requireStopLoss: boolean;
  selectionTopN?: number | null;
};

export type FeePolicy = {
  version: string;
  description: string;
  rules: Array<Record<string, unknown>>;
};

export type CandidatePlan = {
  planId: string;
  revision: number;
  symbol: string;
  actualAsOfSession: string;
  generationStatus: string;
  dataQuality: string;
  scenarioCount: number;
  formalScenarios: string[];
  selectedScenarioId?: string | null;
  decisionStatus?: string | null;
  targetPositionPct?: number | null;
  traceRefs: string[];
  planFingerprint: string;
};

export type Paginated<T> = {
  total: number;
  page: number;
  pageSize: number;
  items: T[];
};

const unwrap = <T>(data: unknown): T => toCamelCase<T>(data);

export const backtestRunsApi = {
  list: async (params: { status?: string; page?: number; pageSize?: number } = {}): Promise<{ total: number; page: number; pageSize: number; items: BacktestRunSummary[] }> => {
    const response = await apiClient.get('/api/v1/backtest/runs', {
      params: {
        status: params.status || undefined,
        page: params.page ?? 1,
        page_size: params.pageSize ?? 20,
      },
    });
    return unwrap(response.data);
  },

  get: async (runId: string): Promise<BacktestRunDetail> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}`);
    return unwrap(response.data);
  },

  create: async (config: Record<string, unknown>, allowIncomplete = false): Promise<BacktestRunSummary> => {
    const response = await apiClient.post('/api/v1/backtest/runs', {
      config,
      allow_incomplete: allowIncomplete,
    });
    return unwrap(response.data);
  },

  precheck: async (config: Record<string, unknown>): Promise<DataJob> => {
    const response = await apiClient.post('/api/v1/backtest/prechecks', { config });
    return unwrap(response.data);
  },

  getJob: async (jobId: string): Promise<DataJob> => {
    const response = await apiClient.get(`/api/v1/backtest/prechecks/${jobId}`);
    return unwrap(response.data);
  },

  syncMinuteBars: async (payload: Record<string, unknown>): Promise<DataJob> => {
    const response = await apiClient.post('/api/v1/backtest/minute-bars/sync', payload);
    return unwrap(response.data);
  },

  equity: async (runId: string): Promise<Paginated<EquityPoint>> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/equity`, {
      params: { page: 1, page_size: 500 },
    });
    return unwrap(response.data);
  },

  benchmarks: async (runId: string): Promise<BenchmarkPayload> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/benchmarks`);
    return unwrap(response.data);
  },

  orders: async (runId: string, page = 1, pageSize = 100): Promise<Paginated<OrderRow>> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/orders`, {
      params: { page, page_size: pageSize },
    });
    return unwrap(response.data);
  },

  fills: async (runId: string, page = 1, pageSize = 100): Promise<Paginated<FillRow>> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/fills`, {
      params: { page, page_size: pageSize },
    });
    return unwrap(response.data);
  },

  trades: async (runId: string, page = 1, pageSize = 100): Promise<Paginated<TradeRow>> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/trades`, {
      params: { page, page_size: pageSize },
    });
    return unwrap(response.data);
  },

  positions: async (runId: string): Promise<{ items: PositionRow[]; totalCost?: number; marketValue?: number; unrealizedPnl?: number }> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/positions`);
    return unwrap(response.data);
  },

  inputs: async (runId: string, included?: boolean): Promise<{ items: InputRow[] }> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/inputs`, {
      params: included === undefined ? {} : { included },
    });
    return unwrap(response.data);
  },

  diagnostics: async (runId: string): Promise<{ items: Record<string, unknown> }> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/diagnostics`);
    return unwrap(response.data);
  },

  cancel: async (runId: string): Promise<BacktestRunSummary> => {
    const response = await apiClient.post(`/api/v1/backtest/runs/${runId}/cancel`);
    return unwrap(response.data);
  },

  rerun: async (runId: string): Promise<BacktestRunSummary> => {
    const response = await apiClient.post(`/api/v1/backtest/runs/${runId}/rerun`);
    return unwrap(response.data);
  },

  riskPolicies: async (): Promise<RiskPolicyPreset[]> => {
    const response = await apiClient.get('/api/v1/backtest/risk-policies');
    return unwrap(response.data);
  },

  feePolicies: async (): Promise<FeePolicy[]> => {
    const response = await apiClient.get('/api/v1/backtest/fee-policies');
    return unwrap(response.data);
  },

  candidates: async (params: { startDate?: string; endDate?: string; symbol?: string; limit?: number } = {}): Promise<{ total: number; items: CandidatePlan[] }> => {
    const response = await apiClient.get('/api/v1/backtest/candidates', {
      params: {
        start_date: params.startDate || undefined,
        end_date: params.endDate || undefined,
        symbol: params.symbol || undefined,
        limit: params.limit ?? 300,
      },
    });
    return unwrap(response.data);
  },

  downloadExport: async (runId: string, kind: string): Promise<void> => {
    const response = await apiClient.get(`/api/v1/backtest/runs/${runId}/export`, {
      params: { kind },
      responseType: 'blob',
    });
    const blob = response.data as Blob;
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${runId}-${kind}.${kind === 'config' || kind === 'manifest' || kind === 'metrics' || kind === 'inputs' || kind === 'diagnostics' ? 'json' : 'csv'}`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  },
};
