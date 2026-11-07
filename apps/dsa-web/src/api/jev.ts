import apiClient from './index';

export interface JevAnswer {
  type: 'choice';
  choice: string;
  confidence: number;
  probabilities: Record<string, number>;
}

export interface JevStatus {
  enabled: boolean;
  configured: boolean;
  shadow_mode: true;
  model: string;
  markets: string[];
  horizons: number[];
}

export interface JevEvaluation {
  evaluation_id?: string;
  stock_code?: string;
  market?: string;
  as_of?: string;
  purpose?: 'entry' | 'position';
  status: string;
  reason?: string;
  shadow_mode: true;
  model?: string;
  created_at?: string;
  source?: string;
  source_ref?: string;
  evaluation_mode?: 'historical_replay' | 'forward_shadow';
  latency_ms?: number;
  cached?: boolean;
  answers?: Record<string, JevAnswer>;
  usage?: { input_tokens?: number; output_tokens?: number };
  state?: {
    bar_count: number;
    history_source: string;
    missing_evidence: string[];
    metrics: { last_close: number; trend: string; atr14_pct: number };
    price_plan: { status: string; plan_id?: string };
    position: { status: string; unrealized_price_return_pct?: number };
    as_of?: string;
    history_fingerprint?: string;
    agent_evidence?: Record<string, Array<{ tool: string; quality: string; summary: string }>>;
  };
  questions?: Record<string, unknown>;
  outcomes?: Array<{ horizon: number; target_date?: string; status: string; price_return_pct?: number | null }>;
}

export interface JevTraceReview {
  status: string;
  reason?: string | null;
  shadow_mode: true;
  session_id?: string;
  capture_id?: string;
  shared_snapshot?: boolean;
  created_at?: string;
  completed_at?: string;
  expires_at?: string;
  baseline?: { action?: string | null; source?: string; judge_action?: string | null;
    summary?: string; reasons?: string[]; trading_horizon?: string | null };
  evaluation?: JevEvaluation | null;
  comparison?: { status: string; reason?: string | null;
    rows: Array<{ horizon: number; status: string; flags: string[]; reason?: string | null }> };
}

export const jevApi = {
  traceReview: async (sessionId: string): Promise<JevTraceReview> => (
    await apiClient.get(`/api/v1/agent/trace/sessions/${encodeURIComponent(sessionId)}/jev`)
  ).data,
  status: async (): Promise<JevStatus> => (await apiClient.get('/api/v1/jev/status')).data,
  list: async (stockCode?: string): Promise<{ items: JevEvaluation[] }> => (
    await apiClient.get('/api/v1/jev/evaluations', { params: { stock_code: stockCode || undefined } })
  ).data,
  detail: async (id: string): Promise<JevEvaluation> => (
    await apiClient.get(`/api/v1/jev/evaluations/${encodeURIComponent(id)}`)
  ).data,
  evaluate: async (request: { stock_code: string; account_id?: number; fallback_to_network: boolean }): Promise<JevEvaluation> => (
    await apiClient.post('/api/v1/jev/evaluate', request, { timeout: 180000 })
  ).data,
};
