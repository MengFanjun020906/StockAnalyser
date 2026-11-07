import apiClient from './index';
import { toCamelCase } from './utils';

export type SignalSchedulerState = {
  market: string;
  status: string;
  heartbeatAt?: string | null;
  lastStartedAt?: string | null;
  lastSuccessAt?: string | null;
  lastTradingDate?: string | null;
  nextRunAt?: string | null;
  watchCount?: number;
  l1Count?: number;
  error?: string | null;
  online?: boolean;
};

export type EntrySignal = {
  signalId: string;
  planId?: string | null;
  scenarioId?: string | null;
  stockCode: string;
  stockName?: string | null;
  market: string;
  signalType: string;
  status: string;
  effectiveStatus?: string;
  revision: number;
  entryLow?: number | null;
  entryHigh?: number | null;
  stopPrice?: number | null;
  triggerRule?: {
    type?: string;
    low?: number | null;
    high?: number | null;
    zones?: EntryZone[];
    stopTriggerMode?: string;
  };
  createdAsOf: string;
  lastCheckedAsOf: string;
  validUntil: string;
  evidenceQuality?: string;
  sourceTraceId?: string | null;
  metadata?: Record<string, unknown>;
};

export type PricePlanSummary = {
  planId: string;
  stockCode: string;
  stockName?: string | null;
  market: string;
  generationStatus: 'generated' | 'observation' | 'no_plan';
  revision: number;
  actualAsOfSession: string;
  entryLow?: number | null;
  entryHigh?: number | null;
  reasonCodes: string[];
  scenarioCount: number;
  traceRef?: string | null;
};

export type PlanReview = {
  conclusion?: string;
  reviewStatus?: string;
  validatedOutput?: Record<string, unknown>;
};

export type AccountPlanDecision = {
  riskProfile?: string;
  selectedScenarioId?: string | null;
  reviewPermission?: string;
  confirmationPolicy?: string;
  positionSizingRef?: string | null;
  targetPositionPct?: number | null;
  accountNetRr1?: number | null;
  plannedQuantity?: number | null;
  accountCostPolicyRef?: string | null;
  accountContextAbsent?: boolean;
  executionStatus?: string;
  reasonCodes?: string[];
};

export type PricePlanScenario = {
  scenarioId: string;
  role: 'primary' | 'secondary' | 'observation';
  baseRank?: number;
  sourceFamilies: string[];
  anchorRefs?: string[];
  entryLow: number;
  entryHigh: number;
  entryReference?: number;
  stopLoss?: number | null;
  target1?: number | null;
  target2?: number | null;
  referenceNetRr1?: number | null;
  reachabilityBand?: string;
  priceRelation?: string;
  validFrom?: string;
  confirmationDeadline?: string;
  scenarioReasonCodes: string[];
};

export type PricePlanArtifact = {
  planId: string;
  generationStatus: string;
  actualAsOfSession: string;
  reasonCodes: string[];
  scenarios: PricePlanScenario[];
};

export type EntryZone = {
  label: string;
  low: number;
  high: number;
  rationale?: string | null;
};

export type PositionWatch = {
  watchId: string;
  accountId: number;
  stockCode: string;
  stockName?: string | null;
  market: string;
  positionCycleId: number;
  status: string;
  quantity: number;
  averageCost: number;
  stopPrice?: number | null;
  lastCheckedAsOf: string;
  riskLevel: string;
  currentAction: string;
  latestScore?: number | null;
  latestExitWarningLevel?: number;
  metadata?: Record<string, unknown>;
};

export type SignalPreviewList = {
  entrySignals: EntrySignal[];
  pricePlans: PricePlanSummary[];
  positionWatches: PositionWatch[];
  scheduler: SignalSchedulerState[];
  counts: {
    entry: number;
    plans: number;
    activeEntry: number;
    positions: number;
    highRisk: number;
  };
};

export type SignalEvaluationHorizon = {
  signalReturnPct?: number | null;
  executionAdjustedReturnPct?: number | null;
  benchmarkReturnPct?: number | null;
  excessReturnPct?: number | null;
  sampleCount?: number;
};

export type SignalEvaluationSetup = {
  setupType: string;
  sampleCount: number;
  eligibleCount: number;
  triggeredCount: number;
  hitRatePct?: number | null;
  coverageDays: number;
  reviewStatus: string;
  calibrationApplied: boolean;
};

export type SignalEvaluationSummary = {
  sampleCount: number;
  eligibleCount: number;
  triggeredCount: number;
  untriggeredCount: number;
  ambiguousCount: number;
  hitRatePct?: number | null;
  averageWaitTradingDays?: number | null;
  executionAdjustedReturnPct?: number | null;
  opportunityCostPct?: number | null;
  avoidedLossPct?: number | null;
  horizons: Record<string, SignalEvaluationHorizon>;
  bySetupType: SignalEvaluationSetup[];
  reviewPolicy: {
    minSamples: number;
    minCoverageDays: number;
    mode: string;
  };
};

export type SignalChartBar = {
  date: string;
  open: number | null;
  high: number | null;
  low: number | null;
  close: number | null;
  volume: number | null;
  amount?: number | null;
  pctChange?: number | null;
  source?: string | null;
  guideLine?: number | null;
  ma57?: number | null;
  volumeMa5?: number | null;
  dif?: number | null;
  dea?: number | null;
  macdHistogram?: number | null;
};

export type ChanPen = {
  id?: number;
  direction?: string;
  start?: { index?: number; time?: string; price?: number };
  end?: { index?: number; time?: string; price?: number };
};

export type ChanCenter = {
  id?: number;
  zg?: number;
  zd?: number;
  /** Compatibility for chart data built before API key normalization. */
  ZG?: number;
  ZD?: number;
  startIndex?: number;
  endIndex?: number;
  startDate?: string;
  endDate?: string;
};

export type ChanSignal = {
  name?: string;
  timeframe?: string;
  triggered?: boolean;
  metrics?: Record<string, unknown>;
};

export type CompositePayload = {
  snapshot?: {
    asOf?: string;
    dataQuality?: string;
    s4?: {
      status?: string;
      regionPass?: boolean | null;
      resonanceCount?: number | null;
      volumeRatio5D?: number | null;
      volatilityPct?: number | null;
      gain10dPct?: number | null;
      goldenCrossToday?: boolean | null;
      allOk?: boolean | null;
      missingFeatures?: string[];
    };
    chan?: { status?: string; signals?: ChanSignal[] };
  };
  compositeVerdict?: {
    score?: number | null;
    scaleMax?: number;
    signalLevel?: string;
    verdictStatus?: string;
    entryPermission?: string;
    requiredHoldingAction?: string | null;
    components?: {
      s4?: { score?: number | null; status?: string };
      chan?: { score?: number | null; status?: string };
    };
    exitWarning?: { level?: number; items?: string[] };
    missingFeatures?: string[];
  };
};

export type LifecycleEvent = {
  eventId: string;
  eventType: string;
  fromStatus?: string | null;
  toStatus?: string | null;
  occurredAt: string;
  asOf: string;
  source: string;
  sourceTraceId?: string | null;
  payload?: Record<string, unknown>;
};

export type PricePlanLifecycleState = {
  planId: string;
  scenarioId: string;
  stateVersion: number;
  state: 'waiting_touch' | 'touched' | 'confirmed' | 'invalidated' | 'expired' | 'ambiguous' | 'closed';
  firstTouchedAt?: string | null;
  confirmedAt?: string | null;
  invalidatedAt?: string | null;
  expiredAt?: string | null;
  lastEvaluatedAsOf: string;
  lastEventId: string;
};

export type SignalPreviewDetail = {
  kind: 'entry_signal' | 'position_watch' | 'price_plan';
  subject: EntrySignal | PositionWatch | PricePlanSummary;
  pricePlan?: PricePlanArtifact;
  review?: PlanReview | null;
  accountDecision?: AccountPlanDecision | null;
  lifecycleStates?: PricePlanLifecycleState[];
  events: LifecycleEvent[];
  chart: {
    stockCode: string;
    market: string;
    status: string;
    bars: SignalChartBar[];
    structure?: {
      status?: string;
      dataQuality?: string;
      pens?: ChanPen[];
      centers?: ChanCenter[];
      signals?: ChanSignal[];
    };
    composite?: CompositePayload | null;
    corporateActions?: Array<{
      date: string;
      actionType: string;
      note?: string | null;
    }>;
    dataWindow?: {
      start?: string | null;
      end?: string | null;
      barCount?: number;
      targetDate?: string;
      sources?: string[];
      priceBasis?: string;
      refresh?: { attempted?: boolean; source?: string | null; savedCount?: number; error?: string | null };
    };
  };
};

type StockIdentified = { stockCode: string; market: string };

export function dedupeLatestByStock<T extends StockIdentified>(rows: T[]): T[] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    const key = `${String(row.market || 'cn').toLowerCase()}:${String(row.stockCode || '').toUpperCase()}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export const signalPreviewApi = {
  list: async (): Promise<SignalPreviewList> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/signal-preview');
    const data = toCamelCase<SignalPreviewList>(response.data);
    const pricePlans = dedupeLatestByStock(data.pricePlans ?? []);
    const plannedStocks = new Set(pricePlans.map((item) => `${item.market.toLowerCase()}:${item.stockCode.toUpperCase()}`));
    const entrySignals = dedupeLatestByStock(data.entrySignals ?? []).filter((item) => (
      Boolean(item.planId)
      || !plannedStocks.has(`${item.market.toLowerCase()}:${item.stockCode.toUpperCase()}`)
    ));
    return {
      entrySignals,
      pricePlans,
      positionWatches: dedupeLatestByStock(data.positionWatches ?? []),
      scheduler: data.scheduler ?? [],
      counts: data.counts ?? { entry: 0, plans: 0, activeEntry: 0, positions: 0, highRisk: 0 },
    };
  },

  entry: async (signalId: string, refreshTail = true): Promise<SignalPreviewDetail> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/signal-preview/entries/${encodeURIComponent(signalId)}`, {
      params: { refresh_tail: refreshTail },
      timeout: 120000,
    });
    return toCamelCase<SignalPreviewDetail>(response.data);
  },

  plan: async (planId: string, refreshTail = true): Promise<SignalPreviewDetail> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/signal-preview/plans/${encodeURIComponent(planId)}`, {
      params: { refresh_tail: refreshTail },
      timeout: 120000,
    });
    return toCamelCase<SignalPreviewDetail>(response.data);
  },

  position: async (watchId: string, refreshTail = true): Promise<SignalPreviewDetail> => {
    const response = await apiClient.get<Record<string, unknown>>(`/api/v1/signal-preview/positions/${encodeURIComponent(watchId)}`, {
      params: { refresh_tail: refreshTail },
      timeout: 120000,
    });
    return toCamelCase<SignalPreviewDetail>(response.data);
  },

  evaluation: async (limit = 300): Promise<SignalEvaluationSummary> => {
    const response = await apiClient.get<Record<string, unknown>>('/api/v1/signal-preview/evaluation', {
      params: { limit },
      timeout: 120000,
    });
    return toCamelCase<SignalEvaluationSummary>(response.data);
  },
};
