import apiClient from './index';
import { toCamelCase } from './utils';

export type VerdictReviewWindow = {
  evalStatus?: string;
  futureReturnPct?: number | null;
  simulatedReturnPct?: number | null;
  directionExpected?: string | null;
  directionCorrect?: boolean | null;
  outcome?: string | null;
  endClose?: number | null;
  maxHigh?: number | null;
  minLow?: number | null;
};

export type VerdictReviewRow = {
  schemaVersion?: string;
  chainType?: 'stock_selection' | 'single_stock_analysis' | string;
  traceId?: string;
  traceDir?: string;
  decisionDate?: string;
  symbol?: string;
  name?: string;
  intent?: string | null;
  finalAction?: string | null;
  primaryPlanVerdict?: string | null;
  symbolAction?: string | null;
  operationAdvice?: string | null;
  decisionType?: string | null;
  confidence?: number | null;
  regime?: string | null;
  riskLevel?: string | null;
  dataQuality?: string | null;
  startPrice?: number | null;
  startDate?: string | null;
  windows?: Record<string, VerdictReviewWindow>;
  reviewLabel?: string;
  limits?: string[];
};

export type VerdictReviewSummary = {
  total?: number;
  completedCount?: number;
  completionRatePct?: number;
  avgFutureReturnPct?: number | null;
  chainCounts?: Record<string, number>;
  labelCounts?: Record<string, number>;
};

export type VerdictReviewResponse = {
  sourcePath?: string;
  exists?: boolean;
  total?: number;
  items: VerdictReviewRow[];
  summary: VerdictReviewSummary;
};

export type VerdictReviewBuildResult = {
  traceCount?: number;
  reviewCount?: number;
  outputPath?: string;
  skipped?: number;
  evalWindows?: number[];
};

export type VerdictReviewFilters = {
  chainType?: string;
  reviewLabel?: string;
  symbol?: string;
  limit?: number;
};

export type VerdictReviewRebuildOptions = {
  windows?: string;
  limit?: number;
};

export const agentVerdictReviewsApi = {
  list: async (filters: VerdictReviewFilters = {}): Promise<VerdictReviewResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/agent-verdict-reviews', {
      params: {
        chain_type: filters.chainType || undefined,
        review_label: filters.reviewLabel || undefined,
        symbol: filters.symbol || undefined,
        limit: filters.limit ?? 200,
      },
    });
    const data = toCamelCase<VerdictReviewResponse>(response.data);
    return {
      sourcePath: data.sourcePath,
      exists: data.exists,
      total: data.total ?? 0,
      items: data.items ?? [],
      summary: data.summary ?? {},
    };
  },

  rebuild: async (options: VerdictReviewRebuildOptions = {}): Promise<VerdictReviewBuildResult> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/agent-verdict-reviews/rebuild', undefined, {
      params: {
        windows: options.windows || '7,30',
        limit: options.limit ?? 300,
      },
    });
    return toCamelCase<VerdictReviewBuildResult>(response.data);
  },
};
