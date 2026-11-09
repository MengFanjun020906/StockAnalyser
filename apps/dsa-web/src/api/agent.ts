import apiClient from './index';
import { API_BASE_URL } from '../utils/constants';

export interface AgentStreamOptions {
  signal?: AbortSignal;
}

export interface AgentTraceRunRequest {
  message: string;
  session_id?: string;
  account_id?: number;
  stock_code?: string;
  stock_name?: string;
  skills?: string[];
  context?: unknown;
  inject_portfolio_context?: boolean;
  analysis_mode?: string;
  report_intent?: string;
  risk_preference?: string;
  trading_horizon?: string;
  max_single_position_pct?: number;
  max_total_equity_exposure_pct?: number;
  max_acceptable_drawdown_pct?: number;
  default_stop_loss_pct?: number;
  investor_notes?: string;
  candidate_discovery_mode?: 'deterministic' | 'llm_expert_committee' | 'thesis_desk_committee';
  resume_from_session_id?: string;
}

export interface AgentTraceEvent {
  type: string;
  step?: number;
  tool?: string;
  display_name?: string;
  success?: boolean;
  duration?: number;
  message?: string;
  [key: string]: unknown;
}

export interface AgentTraceToolCall {
  step: number;
  tool: string;
  arguments?: Record<string, unknown>;
  success: boolean;
  duration?: number;
  result_length?: number;
  result_preview?: string;
  result_json?: unknown;
  cached?: boolean;
  timeout?: boolean;
  [key: string]: unknown;
}

export interface AgentTraceRunResponse {
  success: boolean;
  session_id: string;
  content: string;
  error?: string | null;
  total_steps: number;
  total_tokens: number;
  provider: string;
  model: string;
  mode: string;
  events: AgentTraceEvent[];
  tool_calls: AgentTraceToolCall[];
  planner?: Record<string, unknown> | null;
  agent_user_context?: Record<string, unknown> | null;
  context_summary?: Record<string, unknown> | null;
  dimension_evidence?: Record<string, unknown> | null;
  report_quality?: Record<string, unknown> | null;
  debate?: Record<string, unknown> | null;
  stock_selection?: Record<string, unknown> | null;
  composite_verdict?: Record<string, unknown> | null;
  risk_gate?: Record<string, unknown> | null;
  price_plan?: Record<string, unknown> | null;
  trade_plan_result?: Record<string, unknown> | null;
  llm_telemetry?: Record<string, unknown> | null;
  judge_sanity?: Record<string, unknown> | null;
  artifact_dir?: string | null;
  runtime_config?: Record<string, unknown> | null;
}

export interface AgentTraceHistoryItemResponse {
  id: string;
  createdAt: string;
  message: string;
  stockCode: string;
  accountId?: number | null;
  status: 'success' | 'error';
  result: AgentTraceRunResponse;
}

export interface AgentRuntimeConfigResponse {
  runtime_config: Record<string, unknown>;
}

export const agentApi = {
  async runTrace(payload: AgentTraceRunRequest): Promise<AgentTraceRunResponse> {
    const response = await apiClient.post<AgentTraceRunResponse>('/api/v1/agent/trace/run', payload, {
      timeout: 180000,
    });
    return response.data;
  },
  async getRuntimeConfig(): Promise<AgentRuntimeConfigResponse> {
    const response = await apiClient.get<AgentRuntimeConfigResponse>('/api/v1/agent/runtime-config');
    return response.data;
  },
  async getTraceSession(sessionId: string): Promise<AgentTraceHistoryItemResponse> {
    const response = await apiClient.get<AgentTraceHistoryItemResponse>(`/api/v1/agent/trace/sessions/${encodeURIComponent(sessionId)}`);
    return response.data;
  },
  async traceStream(
    payload: AgentTraceRunRequest,
    options?: AgentStreamOptions,
  ): Promise<Response> {
    const base = API_BASE_URL || '';
    const response = await fetch(`${base}/api/v1/agent/trace/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      credentials: 'include',
      signal: options?.signal,
    });
    if (!response.ok) {
      throw new Error(`Trace stream failed: HTTP ${response.status}`);
    }
    return response;
  },
};
