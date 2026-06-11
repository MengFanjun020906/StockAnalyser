import apiClient from './index';
import { toCamelCase } from './utils';

export type SeedPoolEvaluation = {
  evaluationDate?: string;
  seedClose?: number;
  evaluationOpen?: number;
  evaluationHigh?: number;
  evaluationLow?: number;
  evaluationClose?: number;
  nextCloseReturnPct?: number;
  benchmarkCode?: string;
  benchmarkReturnPct?: number;
  alphaReturnPct?: number;
  mfePct?: number;
  maePct?: number;
  liquidityStatus?: string;
  dataStatus?: string;
  error?: string;
  updatedAt?: string;
};

export type SeedPoolDeskOutcome = {
  desk: string;
  status?: string;
  stance?: string;
  decision?: string;
  reason?: string;
  risks?: unknown[];
  evidence?: Array<Record<string, unknown>>;
  metrics?: Record<string, unknown>;
  errors?: unknown[];
};

export type SeedPoolQualityItem = {
  id: number;
  snapshotId: number;
  code: string;
  name: string;
  market?: string;
  source?: string;
  catalystTags?: string[];
  catalystTier?: number;
  entryReason?: string;
  freshness?: string;
  seedOrder?: number;
  enteredDeepDive?: boolean;
  enteredFinalReport?: boolean;
  triggerSignals?: Array<Record<string, unknown>>;
  evaluation?: SeedPoolEvaluation | null;
  deskOutcomes?: SeedPoolDeskOutcome[];
};

export type SeedPoolSnapshot = {
  id: number;
  runId: string;
  traceId: string;
  seedDate: string;
  generatedAt?: string;
  market?: string;
  seedCount?: number;
  status?: string;
};

export type SeedPoolQualitySummary = {
  seedCount?: number;
  evaluatedCount?: number;
  tradableCount?: number;
  limitUpUnableBuyCount?: number;
  missingPriceCount?: number;
  upCount?: number;
  downCount?: number;
  winRatePct?: number;
  avgReturnPct?: number;
  medianReturnPct?: number;
  avgAlphaReturnPct?: number;
  medianAlphaReturnPct?: number;
  avgMfePct?: number;
  avgMaePct?: number;
};

export type SeedPoolQualityGroupStat = SeedPoolQualitySummary & {
  key: string;
  supportWatchCount?: number;
  opposeInvalidCount?: number;
};

export type SeedPoolQualityResponse = {
  snapshot: SeedPoolSnapshot | null;
  summary: SeedPoolQualitySummary;
  sourceStats: SeedPoolQualityGroupStat[];
  deskStats: SeedPoolQualityGroupStat[];
  catalystTierStats: SeedPoolQualityGroupStat[];
  items: SeedPoolQualityItem[];
};

export type SeedPoolQualityDate = {
  seedDate: string;
  snapshotCount: number;
  latestGeneratedAt?: string;
};

export type SeedPoolChartBar = {
  tradeDate: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  amount?: number;
  source?: string;
};

export type SeedPoolPriceLine = {
  desk: string;
  key: string;
  price: number;
  label: string;
  color: string;
};

export type SeedPoolChartData = {
  item: SeedPoolQualityItem;
  bars: SeedPoolChartBar[];
  evaluation: SeedPoolEvaluation;
  catalyst: {
    catalystTags?: string[];
    catalystTier?: number;
    triggerSignals?: Array<Record<string, unknown>>;
  };
  deskOutcomes: SeedPoolDeskOutcome[];
  priceLines: SeedPoolPriceLine[];
};

export const seedPoolQualityApi = {
  getDates: async (limit = 60): Promise<SeedPoolQualityDate[]> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/seed-pool-quality/dates', { params: { limit } });
    return toCamelCase<{ dates: SeedPoolQualityDate[] }>(response.data).dates ?? [];
  },

  getByDate: async (seedDate: string): Promise<SeedPoolQualityResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/seed-pool-quality', { params: { seed_date: seedDate } });
    const data = toCamelCase<SeedPoolQualityResponse>(response.data);
    return {
      snapshot: data.snapshot ?? null,
      summary: data.summary ?? {},
      sourceStats: data.sourceStats ?? [],
      deskStats: data.deskStats ?? [],
      catalystTierStats: data.catalystTierStats ?? [],
      items: data.items ?? [],
    };
  },

  evaluate: async (seedDate: string): Promise<Record<string, unknown>> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/seed-pool-quality/evaluate', undefined, {
      params: { seed_date: seedDate },
    });
    return toCamelCase<Record<string, unknown>>(response.data);
  },

  getChartData: async (itemId: number): Promise<SeedPoolChartData> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/seed-pool-quality/items/${itemId}/chart-data`);
    const data = toCamelCase<SeedPoolChartData>(response.data);
    return {
      item: data.item,
      bars: data.bars ?? [],
      evaluation: data.evaluation ?? {},
      catalyst: data.catalyst ?? {},
      deskOutcomes: data.deskOutcomes ?? [],
      priceLines: data.priceLines ?? [],
    };
  },
};
