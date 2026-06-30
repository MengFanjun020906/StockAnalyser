import apiClient from './index';
import { API_BASE_URL } from '../utils/constants';
import { createApiError, isApiRequestError, parseApiError } from './error';

export interface ChatStreamOptions {
  signal?: AbortSignal;
}

export interface ChatRequest {
  message: string;
  skills?: string[];
}

export interface ChatStreamRequest extends ChatRequest {
  session_id?: string;
  context?: unknown;
}

export interface ChatResponse {
  success: boolean;
  content: string;
  session_id: string;
  error?: string;
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
  debate?: Record<string, unknown> | null;
  stock_selection?: Record<string, unknown> | null;
  risk_gate?: Record<string, unknown> | null;
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

export interface SkillInfo {
  id: string;
  name: string;
  description: string;
}

export interface SkillsResponse {
  skills: SkillInfo[];
  default_skill_id: string;
}

export interface ChatSessionItem {
  session_id: string;
  title: string;
  message_count: number;
  created_at: string | null;
  last_active: string | null;
}

export interface ChatSessionMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string | null;
}

export const agentApi = {
  async chat(payload: ChatRequest): Promise<ChatResponse> {
    const response = await apiClient.post<ChatResponse>('/api/v1/agent/chat', payload, {
      timeout: 120000,
    });
    return response.data;
  },
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
    options?: ChatStreamOptions,
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
  async getSkills(): Promise<SkillsResponse> {
    const response = await apiClient.get<SkillsResponse>('/api/v1/agent/skills');
    return response.data;
  },
  async getChatSessions(limit = 50): Promise<ChatSessionItem[]> {
    const response = await apiClient.get<{ sessions: ChatSessionItem[] }>('/api/v1/agent/chat/sessions', { params: { limit } });
    return response.data.sessions;
  },
  async getChatSessionMessages(sessionId: string): Promise<ChatSessionMessage[]> {
    const response = await apiClient.get<{ messages: ChatSessionMessage[] }>(`/api/v1/agent/chat/sessions/${sessionId}`);
    return response.data.messages;
  },
  async deleteChatSession(sessionId: string): Promise<void> {
    await apiClient.delete(`/api/v1/agent/chat/sessions/${sessionId}`);
  },
  async sendChat(content: string): Promise<{ success: boolean }> {
    const response = await apiClient.post<{
      success: boolean;
      error?: string;
      message?: string;
    }>('/api/v1/agent/chat/send', { content });
    const data = response.data;
    if (data.success === false) {
      throw new Error(data.message || '发送失败');
    }
    return { success: true };
  },
  async chatStream(
    payload: ChatStreamRequest,
    options?: ChatStreamOptions,
  ): Promise<Response> {
    const base = API_BASE_URL || '';
    const url = `${base}/api/v1/agent/chat/stream`;
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        credentials: 'include',
        signal: options?.signal,
      });

      if (response.ok) {
        return response;
      }

      const contentType = response.headers.get('content-type') || '';
      let responseData: unknown = null;
      if (contentType.includes('application/json')) {
        responseData = await response.json().catch(() => null);
      } else {
        responseData = await response.text().catch(() => null);
      }

      const parsed = parseApiError({
        response: {
          status: response.status,
          statusText: response.statusText,
          data: responseData,
        },
      });
      throw createApiError(parsed, {
        response: {
          status: response.status,
          statusText: response.statusText,
          data: responseData,
        },
      });
    } catch (error: unknown) {
      if (isApiRequestError(error)) {
        throw error;
      }
      if (error instanceof Error && error.name === 'AbortError') {
        throw error;
      }

      const parsed = parseApiError(error);
      throw createApiError(parsed, { cause: error });
    }
  },
};
