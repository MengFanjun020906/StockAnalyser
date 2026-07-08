import apiClient from './index';
import { toCamelCase } from './utils';

export type NewsSignalCompanyImpact = {
  symbol?: string;
  name?: string;
  direction?: string;
  confidence?: number;
  mappingStatus?: string;
  role?: string;
  rationale?: string;
};

export type NewsSignalIndustryImpact = {
  industry?: string;
  direction?: string;
  strength?: string;
  rationale?: string;
};

export type NewsSignalTransmissionPath = {
  source?: string;
  eventCategory?: string;
  eventScore?: number;
  mechanism?: string;
  target?: string;
  affectedIndustries?: string[];
  affectedSymbols?: string[];
  inferenceLevel?: string;
  evidenceGrade?: string;
  chainSteps?: Array<{ label?: string; text?: string; score?: number }>;
  scoreBreakdown?: Record<string, number>;
  evidenceSnippets?: string[];
  conclusion?: string;
  rationale?: string;
};

export type RawNewsEpisode = {
  episodeId?: string;
  source?: string;
  provider?: string;
  sourceId?: string;
  url?: string;
  title?: string;
  summary?: string;
  content?: string;
  normalizedContent?: string;
  qualityScore?: number;
  qualityGrade?: string;
  qualityFlags?: string[];
  publishedAt?: string;
  signalDate?: string;
  session?: string;
  status?: string;
  subjects?: unknown[];
  stocks?: unknown[];
  errors?: string[];
};

export type NewsExtractedEvent = {
  eventId?: string;
  rawEpisodeId?: string;
  cardId?: string;
  signalDate?: string;
  eventTime?: string;
  eventType?: string;
  trigger?: string;
  subject?: string;
  object?: string;
  direction?: string;
  metricValue?: string;
  evidenceSentence?: string;
  sourceUrl?: string;
  source?: string;
  extractor?: string;
  confidence?: number;
  verificationStatus?: string;
  verificationSources?: Array<Record<string, unknown>>;
  entityLinks?: Array<Record<string, unknown>>;
  diagnostics?: Record<string, unknown>;
  status?: string;
};

export type NewsSignalCard = {
  id?: number;
  cardId: string;
  signalDate?: string;
  session?: string;
  signalLayer?: string;
  summaryShort?: string;
  newsTone?: string;
  marketImpact?: string;
  impactHorizon?: string;
  validFrom?: string;
  validUntil?: string;
  decayRule?: string;
  refreshTrigger?: string;
  stalenessScore?: number;
  evidenceGrade?: string;
  inferenceLevel?: string;
  mappingStatus?: string;
  mappingConfidence?: number;
  signalScore?: number;
  adjustedSignalScore?: number;
  status?: string;
  primaryIndustries?: string[];
  secondaryIndustries?: string[];
  explicitEntities?: string[];
  industryImpacts?: NewsSignalIndustryImpact[];
  companyImpacts?: NewsSignalCompanyImpact[];
  transmissionPaths?: NewsSignalTransmissionPath[];
  rawEpisodeIds?: string[];
  sourceChain?: unknown[];
  diagnostics?: Record<string, unknown>;
  sourceCount?: number;
  graphSyncStatus?: string;
  feedbackCounts?: Record<string, number>;
  rawEpisodes?: RawNewsEpisode[];
  extractedEvents?: NewsExtractedEvent[];
};

export type NewsSignalSummary = {
  total?: number;
  active?: number;
  suppressed?: number;
  mappingCounts?: Record<string, number>;
  horizonCounts?: Record<string, number>;
  layerCounts?: Record<string, number>;
  topIndustries?: Array<{ key: string; count: number }>;
};

export type NewsSignalListResponse = {
  schemaVersion?: string;
  total?: number;
  items: NewsSignalCard[];
  summary: NewsSignalSummary;
};

export type NewsSignalMetrics = {
  totalCards?: number;
  activeCards?: number;
  suppressedCards?: number;
  avgSignalScore?: number | null;
  mappingCounts?: Record<string, number>;
  horizonCounts?: Record<string, number>;
  layerCounts?: Record<string, number>;
  graphSyncCounts?: Record<string, number>;
  feedbackCounts?: Record<string, number>;
};

export type NewsSignalRebuildResult = {
  status?: string;
  targetDate?: string;
  rawEpisodesUpserted?: number;
  cardsUpserted?: number;
  edgeSync?: Record<string, unknown>;
  graphSync?: Record<string, unknown>;
  errors?: Array<Record<string, unknown>>;
  cards?: NewsSignalCard[];
};

export type NewsSignalEdge = {
  edgeId?: string;
  sourceCardId?: string;
  targetCardId?: string;
  targetType?: string;
  targetId?: string;
  edgeClass?: string;
  edgeType?: string;
  weight?: number;
  edgeQuality?: number;
  qualityGrade?: string;
  qualityFlags?: string[];
  method?: string;
  rationale?: string;
  evidence?: Record<string, unknown>;
};

export type NewsSignalGraphNode = {
  id: string;
  type?: string;
  label?: string;
  signalDate?: string;
  signalLayer?: string;
  signalScore?: number;
};

export type NewsSignalGraph = {
  centerCardId?: string;
  nodes: NewsSignalGraphNode[];
  edges: NewsSignalEdge[];
  summary?: {
    nodeCount?: number;
    edgeCount?: number;
    edgeClassCounts?: Record<string, number>;
    edgeQualityCounts?: Record<string, number>;
    avgEdgeQuality?: number | null;
  };
};

export type NewsSignalFilters = {
  signalDate?: string;
  signalLayer?: string;
  industry?: string;
  horizon?: string;
  status?: string;
  limit?: number;
};

export const newsSignalsApi = {
  list: async (filters: NewsSignalFilters = {}): Promise<NewsSignalListResponse> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/news-signals', {
      params: {
        signal_date: filters.signalDate || undefined,
        signal_layer: filters.signalLayer || undefined,
        industry: filters.industry || undefined,
        horizon: filters.horizon || undefined,
        status: filters.status || undefined,
        limit: filters.limit ?? 120,
      },
    });
    const data = toCamelCase<NewsSignalListResponse>(response.data);
    return {
      schemaVersion: data.schemaVersion,
      total: data.total ?? 0,
      items: data.items ?? [],
      summary: data.summary ?? {},
    };
  },

  get: async (cardId: string): Promise<NewsSignalCard> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/news-signals/${encodeURIComponent(cardId)}`);
    return toCamelCase<NewsSignalCard>(response.data);
  },

  metrics: async (signalDate?: string): Promise<NewsSignalMetrics> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/news-signals/metrics', {
      params: { signal_date: signalDate || undefined },
    });
    return toCamelCase<NewsSignalMetrics>(response.data);
  },

  rebuild: async (options: {
    targetDate?: string;
    includeCjzc?: boolean;
    includeCls?: boolean;
    includeXueqiu?: boolean;
    includeMacroFinance?: boolean;
    clsLimit?: number;
    xueqiuLimit?: number;
    macroFinanceLimit?: number;
    syncGraphiti?: boolean;
    includeSemanticEdges?: boolean;
  } = {}): Promise<NewsSignalRebuildResult> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/news-signals/rebuild', undefined, {
      params: {
        target_date: options.targetDate || undefined,
        include_cjzc: options.includeCjzc ?? true,
        include_cls: options.includeCls ?? true,
        include_xueqiu: options.includeXueqiu ?? true,
        include_macro_finance: options.includeMacroFinance ?? true,
        cls_limit: options.clsLimit ?? 50,
        xueqiu_limit: options.xueqiuLimit ?? 30,
        macro_finance_limit: options.macroFinanceLimit ?? 30,
        sync_graphiti: options.syncGraphiti ?? false,
        include_semantic_edges: options.includeSemanticEdges ?? false,
      },
      timeout: 120000,
    });
    return toCamelCase<NewsSignalRebuildResult>(response.data);
  },

  graphSync: async (options: { signalDate?: string; limit?: number; includeSemanticEdges?: boolean } = {}): Promise<Record<string, unknown>> => {
    const response = await apiClient.post<Record<string, unknown>>('/api/v1/news-signals/graph-sync', undefined, {
      params: {
        signal_date: options.signalDate || undefined,
        limit: options.limit ?? 100,
        include_semantic_edges: options.includeSemanticEdges ?? false,
      },
      timeout: 300000,
    });
    return toCamelCase<Record<string, unknown>>(response.data);
  },

  graph: async (cardId: string, limit = 200): Promise<NewsSignalGraph> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/news-signals/${encodeURIComponent(cardId)}/graph`, {
      params: { limit },
    });
    const data = toCamelCase<NewsSignalGraph>(response.data);
    return {
      centerCardId: data.centerCardId,
      nodes: data.nodes ?? [],
      edges: data.edges ?? [],
      summary: data.summary ?? {},
    };
  },

  feedback: async (cardId: string, feedbackType: string, note = ''): Promise<Record<string, unknown>> => {
    const response = await apiClient.post<Record<string, unknown>>(`/api/v1/news-signals/${encodeURIComponent(cardId)}/feedback`, {
      feedback_type: feedbackType,
      note,
      payload: {},
    });
    return toCamelCase<Record<string, unknown>>(response.data);
  },
};
