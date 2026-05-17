import apiClient from './index';
import { toCamelCase } from './utils';

export type CandidatePoolRun = {
  runId: string;
  sessionId?: string | null;
  createdAt: string;
  market?: string;
  candidateSource?: string;
  candidateCount: number;
  fallbackUsed: boolean;
  status?: string;
  quality?: Record<string, unknown>;
  hardExclusion?: Record<string, unknown>;
  discoverySteps?: Array<Record<string, unknown>>;
  expertPackets?: Array<Record<string, unknown>>;
  themes?: Array<Record<string, unknown>>;
  capacity?: Record<string, unknown>;
  note?: string;
};

export type CandidatePoolItem = {
  id: number;
  runId: string;
  code: string;
  name: string;
  market?: string;
  source?: string;
  signalScore?: number | null;
  candidateExperts?: string[];
  candidateDimensions?: string[];
  recallSources?: string[];
  reason?: string;
  reasonDimensions?: Array<{ dimension?: string; label?: string; detail?: string }>;
  metrics?: Record<string, unknown>;
  lifecycleStatus?: string;
  validUntil?: string;
  recurrenceCount?: number;
  createdAt?: string;
};

export type CandidatePoolSummary = {
  candidateCount?: number;
  dimensionCounts?: Record<string, number>;
  sourceCounts?: Record<string, number>;
  lifecycleCounts?: Record<string, number>;
  recurringCount?: number;
  multiSourceCount?: number;
  fallbackCount?: number;
  hardExclusionCount?: number;
  hardStrategyTrunkMissing?: boolean;
};

export type FundamentalCandidateStatus = {
  enabled?: boolean;
  expert?: string;
  status?: string;
  candidateCount?: number;
  latestPeriod?: string | null;
  updatedAt?: string | null;
  dbPath?: string | null;
  table?: string | null;
  rowCount?: number | null;
  eventCount?: number | null;
  warnings?: string[];
  errors?: string[];
  diagnostics?: Array<Record<string, unknown>>;
};

export type CandidatePoolDetail = {
  run: CandidatePoolRun | null;
  items: CandidatePoolItem[];
  quality: Record<string, unknown>;
  hardExclusion: Record<string, unknown>;
  summary: CandidatePoolSummary;
  fundamentalStatus?: FundamentalCandidateStatus;
};

export type CandidatePoolRunsResponse = {
  runs: CandidatePoolRun[];
};

export const candidatePoolApi = {
  getLatest: async (): Promise<CandidatePoolDetail> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/candidate-pool/latest');
    const data = toCamelCase<CandidatePoolDetail>(response.data);
    return {
      run: data.run ?? null,
      items: data.items ?? [],
      quality: data.quality ?? {},
      hardExclusion: data.hardExclusion ?? {},
      summary: data.summary ?? {},
      fundamentalStatus: data.fundamentalStatus ?? {},
    };
  },

  getRuns: async (limit = 20): Promise<CandidatePoolRunsResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/candidate-pool/runs', { params: { limit } });
    return toCamelCase<CandidatePoolRunsResponse>(response.data);
  },

  getRun: async (runId: string): Promise<CandidatePoolDetail> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/candidate-pool/runs/${encodeURIComponent(runId)}`);
    const data = toCamelCase<CandidatePoolDetail>(response.data);
    return {
      run: data.run ?? null,
      items: data.items ?? [],
      quality: data.quality ?? {},
      hardExclusion: data.hardExclusion ?? {},
      summary: data.summary ?? {},
      fundamentalStatus: data.fundamentalStatus ?? {},
    };
  },
};
