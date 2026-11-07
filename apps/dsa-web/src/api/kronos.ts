import apiClient from './index';
import { toCamelCase } from './utils';

export const KRONOS_ANALYZE_TIMEOUT_MS = 300000;

export type KronosBar = {
  index?: number;
  time: string;
  open?: number | null;
  high?: number | null;
  low?: number | null;
  close?: number | null;
  volume?: number | null;
  amount?: number | null;
};

export type KronosRedBox = {
  x0: string;
  x1: string;
  y0: number;
  y1: number;
};

export type KronosPatternInstance = {
  patternId: string;
  patternType: string;
  label?: string;
  direction?: string;
  confidence?: number;
  startTime: string;
  endTime: string;
  priceLow?: number;
  priceHigh?: number;
  redBox?: KronosRedBox;
  summary?: string;
  evidence?: Record<string, unknown>;
};

export type KronosSignal = {
  bias?: 'bullish' | 'bearish' | 'neutral' | string;
  confidence?: number;
  expectedReturnPct?: number;
  riskReward?: number;
  horizonBars?: number;
  actionBias?: string;
};

export type KronosForecastSummary = {
  lastClose?: number;
  finalClose?: number;
  forecastHigh?: number;
  forecastLow?: number;
  expectedReturnPct?: number;
  maxUpsidePct?: number;
  maxDrawdownPct?: number;
  riskReward?: number;
  trendConsistency?: number;
  horizonBars?: number;
};

export type KronosAnalyzeResponse = {
  status: string;
  code?: string;
  market?: string;
  frequency?: string;
  reason?: string;
  runId?: string | null;
  asOf?: string | null;
  bars: KronosBar[];
  forecast: KronosBar[];
  signal: KronosSignal;
  forecastSummary: KronosForecastSummary;
  patternInstances: KronosPatternInstance[];
  structureContext?: Record<string, unknown>;
  dataQuality?: {
    status?: string;
    barCount?: number;
    requiredBars?: number;
    source?: string;
    flags?: string[];
    warnings?: string[];
    asOf?: string;
  };
  sourceChain?: string[];
  modelProfile?: Record<string, unknown>;
  diagnostics?: Record<string, unknown>;
  createdAt?: string;
};

export type KronosStatusResponse = {
  status: string;
  enabled: boolean;
  marketScope: string[];
  frequencyScope: string[];
  repoPath?: string;
  repoAvailable?: boolean;
  tokenizerName?: string;
  modelName?: string;
  device?: string;
  maxContext?: number;
  cacheEnabled?: boolean;
};

export type KronosAnalyzeRequest = {
  stockCode: string;
  frequency?: '1d';
  lookback?: number;
  predLen?: number;
  targetDate?: string;
  fallbackToNetwork?: boolean;
  persist?: boolean;
};

export const kronosApi = {
  getStatus: async (): Promise<KronosStatusResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/kronos/status');
    const data = toCamelCase<KronosStatusResponse>(response.data);
    return {
      status: data.status || 'ok',
      enabled: Boolean(data.enabled),
      marketScope: data.marketScope ?? ['cn'],
      frequencyScope: data.frequencyScope ?? ['1d'],
      repoPath: data.repoPath,
      repoAvailable: data.repoAvailable,
      tokenizerName: data.tokenizerName,
      modelName: data.modelName,
      device: data.device,
      maxContext: data.maxContext,
      cacheEnabled: data.cacheEnabled,
    };
  },

  analyze: async (request: KronosAnalyzeRequest): Promise<KronosAnalyzeResponse> => {
    const response = await apiClient.post<Record<string, unknown>>(
      '/api/v1/kronos/signals/analyze',
      {
        stock_code: request.stockCode,
        frequency: request.frequency ?? '1d',
        lookback: request.lookback,
        pred_len: request.predLen,
        target_date: request.targetDate || undefined,
        fallback_to_network: request.fallbackToNetwork ?? true,
        persist: request.persist,
      },
      { timeout: KRONOS_ANALYZE_TIMEOUT_MS },
    );
    const data = toCamelCase<KronosAnalyzeResponse>(response.data);
    return {
      status: data.status,
      code: data.code,
      market: data.market,
      frequency: data.frequency,
      reason: data.reason,
      runId: data.runId,
      asOf: data.asOf,
      bars: data.bars ?? [],
      forecast: data.forecast ?? [],
      signal: data.signal ?? {},
      forecastSummary: data.forecastSummary ?? {},
      patternInstances: data.patternInstances ?? [],
      structureContext: data.structureContext ?? {},
      dataQuality: data.dataQuality ?? {},
      sourceChain: data.sourceChain ?? [],
      modelProfile: data.modelProfile ?? {},
      diagnostics: data.diagnostics ?? {},
      createdAt: data.createdAt,
    };
  },
};
