import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  BrainCircuit,
  ChartCandlestick,
  Check,
  ChevronDown,
  Circle,
  Download,
  History,
  Loader2,
  Play,
  RotateCcw,
  Trash2,
  Wrench,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useNavigate, useParams } from 'react-router-dom';
import { agentApi, type AgentTraceRunResponse, type AgentTraceToolCall } from '../api/agent';
import { portfolioApi } from '../api/portfolio';
import { CandidateDecisionTable, type CandidateDecisionRow, type CandidateDecisionTone } from '../components/candidates/CandidateDecisionTable';
import { ApiErrorAlert, Button, Collapsible, JsonViewer } from '../components/common';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import type { PortfolioAccountItem } from '../types/portfolio';
import { cn } from '../utils/cn';

/* ═══════════════════════════════════════════════
   Constants & Types
   ═══════════════════════════════════════════════ */

const DEFAULT_PROMPT = '帮我选一下下周可以入手的股票，并说明候选池来源、入池理由、主要风险和等待确认条件。';
const DEFAULT_STOCK_CODE = '';
const HISTORY_CONTENT_PREVIEW_CHARS = 4000;
const TOOL_PREVIEW_RENDER_CHARS = 6000;
const RISK_OPTIONS = [
  { value: 'conservative', label: '保守' },
  { value: 'balanced', label: '均衡' },
  { value: 'aggressive', label: '进取' },
];
const HORIZON_OPTIONS = [
  { value: 'short_term', label: '短线' },
  { value: 'swing', label: '波段' },
  { value: 'medium_term', label: '中线' },
  { value: 'long_term', label: '长线' },
];
const REPORT_INTENT_OPTIONS = [
  { value: 'auto', label: '自动识别' },
  { value: 'watchlist_scan', label: '选股候选池' },
  { value: 'position_review', label: '持仓诊断' },
  { value: 'entry_analysis', label: '入场分析' },
  { value: 'risk_review', label: '账户风控' },
  { value: 'event_impact', label: '事件影响' },
];
const CANDIDATE_DISCOVERY_OPTIONS = [
  { value: 'thesis_desk_committee', label: '打法席位委员会 (P4)', desc: '当前调试阶段只允许四席位链路：召回层 → 低位启动/动量/质量修复/主题催化 → Regime 分配名额' },
] as const;
const CANDIDATE_DISCOVERY_STORAGE_KEY = 'dsa.candidateDiscoveryMode';
const TRACE_HISTORY_KEY = 'dsa.agentTrace.history.v1';
const TRACE_HISTORY_LIMIT = 10;
const TRACE_SESSION_PREFIX = 'trace-';

type TraceStatus = 'idle' | 'running' | 'done' | 'error';
type TraceStreamEvent = Record<string, unknown> & {
  type?: string;
  message?: string;
  tool?: string;
  display_name?: string;
  step?: number;
  success?: boolean;
  duration?: number;
  arguments?: Record<string, unknown>;
  result_preview?: string;
  result_length?: number;
};
type TraceHistoryItem = {
  id: string;
  createdAt: string;
  message: string;
  stockCode: string;
  accountId?: number;
  status: 'success' | 'error';
  result: AgentTraceRunResponse;
  isCompact?: boolean;
};

/* ═══════════════════════════════════════════════
   Helpers
   ═══════════════════════════════════════════════ */

const formatDuration = (duration?: number): string => {
  if (typeof duration !== 'number' || !Number.isFinite(duration)) return '-';
  return `${duration.toFixed(2)}s`;
};

const toFiniteNumber = (value: unknown): number | null => {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
};

const formatCount = (value: unknown): string => {
  const numberValue = toFiniteNumber(value);
  if (numberValue == null) return '-';
  return Math.round(numberValue).toLocaleString();
};

const formatLatencyMs = (value: unknown): string => {
  const numberValue = toFiniteNumber(value);
  if (numberValue == null) return '-';
  if (numberValue >= 1000) return `${(numberValue / 1000).toFixed(2)}s`;
  return `${numberValue.toFixed(0)}ms`;
};

const formatCost = (value: unknown): string => {
  const numberValue = toFiniteNumber(value);
  if (numberValue == null || numberValue <= 0) return '-';
  return `$${numberValue.toFixed(6)}`;
};

const formatPercent = (value: unknown): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return `${value.toLocaleString(undefined, { maximumFractionDigits: 2 })}%`;
};

const formatPercentInput = (value: unknown): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '';
  return String(value);
};

const parseOptionalPercent = (value: string): number | undefined => {
  const normalized = value.trim();
  if (!normalized) return undefined;
  const parsed = Number(normalized);
  if (!Number.isFinite(parsed)) return undefined;
  return Math.min(100, Math.max(0, parsed));
};

const shouldSendStockCode = (message: string, stockCode: string): boolean => {
  const code = stockCode.trim();
  if (!code) return false;
  if (code !== DEFAULT_STOCK_CODE) return true;
  if (message.includes(code)) return true;
  return false;
};

const toStringList = (value: unknown): string[] => {
  if (!value) return [];
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean);
  if (typeof value === 'string') return value ? [value] : [];
  return [String(value)];
};

const APPENDIX_SEPARATOR = '<!-- APPENDIX_SEPARATOR -->';
const APPENDIX_HEADING_RE = /\n##\s+附录[一二三四五六七八九十]+[、，]|$/;

const splitReportContent = (content: string): { main: string; appendix: string } => {
  if (!content) return { main: '', appendix: '' };
  if (content.includes(APPENDIX_SEPARATOR)) {
    const [main, ...rest] = content.split(APPENDIX_SEPARATOR);
    return { main: main.trimEnd(), appendix: rest.join(APPENDIX_SEPARATOR).trim() };
  }
  const match = APPENDIX_HEADING_RE.exec(content);
  if (match && match.index > 0 && match.index < content.length) {
    return { main: content.slice(0, match.index).trimEnd(), appendix: content.slice(match.index).trim() };
  }
  return { main: content, appendix: '' };
};

const sanitizeMarkdownFileNamePart = (value: string): string => value
  .trim()
  .replace(/\s+/g, '-')
  .replace(/[^a-zA-Z0-9._-]+/g, '-')
  .replace(/-+/g, '-')
  .replace(/^-+|-+$/g, '');

const buildTraceMarkdownFileName = (result: AgentTraceRunResponse): string => {
  const sessionPart = sanitizeMarkdownFileNamePart(result.session_id || 'trace');
  const datePart = new Date().toISOString().slice(0, 10);
  return `agent-trace-${sessionPart}-${datePart}.md`;
};

const downloadMarkdownReport = (result: AgentTraceRunResponse): void => {
  if (!result.content) return;
  const blob = new Blob([result.content], { type: 'text/markdown;charset=utf-8' });
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = buildTraceMarkdownFileName(result);
  anchor.rel = 'noopener noreferrer';
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.URL.revokeObjectURL(url);
};

const toRecordList = (value: unknown): Record<string, unknown>[] => {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object' && !Array.isArray(item));
};

const asRecord = (value: unknown): Record<string, unknown> | null => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
);

const normalizeSeedDateForUrl = (value?: string | null): string => {
  const text = String(value || '').trim();
  const compact = /^(\d{4})(\d{2})(\d{2})$/.exec(text);
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]}`;
  return text;
};

const createEmptyTraceResult = (): AgentTraceRunResponse => ({
  success: false, session_id: '', content: '', error: null,
  total_steps: 0, total_tokens: 0, provider: '', model: '', mode: 'planning_execute',
  events: [], tool_calls: [], planner: null, agent_user_context: null,
  context_summary: null, debate: null, stock_selection: null, risk_gate: null, artifact_dir: null,
  llm_telemetry: null, judge_sanity: null, runtime_config: null,
});

const eventToToolCall = (event: TraceStreamEvent): AgentTraceToolCall => ({
  step: typeof event.step === 'number' ? event.step : 0,
  tool: String(event.tool || ''),
  arguments: event.arguments || {},
  success: event.success === true,
  duration: typeof event.duration === 'number' ? event.duration : undefined,
  result_length: typeof event.result_length === 'number' ? event.result_length : undefined,
  result_preview: typeof event.result_preview === 'string' ? event.result_preview : undefined,
  result_json: event.result_json,
  cached: typeof event.cached === 'boolean' ? event.cached : undefined,
  timeout: typeof event.timeout === 'boolean' ? event.timeout : undefined,
});

const eventToTraceEvent = (event: TraceStreamEvent): AgentTraceRunResponse['events'][number] => ({
  ...event,
  type: event.type || 'unknown',
  success: event.success === true ? true : event.success === false ? false : undefined,
});

const mergeSelectionProgress = (
  stockSelection: Record<string, unknown> | null | undefined,
  event: TraceStreamEvent,
): Record<string, unknown> | null | undefined => {
  const type = String(event.type || '');
  if (type.startsWith('selection_') && type !== 'selection_expert_graph_done') {
    const payload = asRecord(event.payload);
    const existing = normalizeStockSelectionPayload(stockSelection) || {};
    const selectionContext = asRecord(existing.selection_context) || {};
    const stages = asRecord(selectionContext.stages) || {};
    const stageName = selectionStageNameFromEvent(type, payload);
    return {
      ...existing,
      enabled: existing.enabled ?? true,
      selection_context: {
        ...selectionContext,
        stages: stageName && payload ? { ...stages, [stageName]: payload } : stages,
      },
    };
  }
  if (type !== 'selection_expert_graph_done') return stockSelection;
  const payload = asRecord(event.payload) || {};
  const mode = String(payload.orchestration_mode || 'expert_graph');
  const existing = normalizeStockSelectionPayload(stockSelection) || {};
  const finalReport = asRecord(existing.final_report_json) || {};
  const selectionContext = asRecord(existing.selection_context) || {};
  return {
    ...existing,
    enabled: existing.enabled ?? true,
    final_report_json: {
      ...finalReport,
      orchestration_mode: finalReport.orchestration_mode || mode,
      expert_state: finalReport.expert_state || payload.expert_state || null,
    },
    selection_context: {
      ...selectionContext,
      orchestration_mode: selectionContext.orchestration_mode || mode,
      expert_state: selectionContext.expert_state || payload.expert_state || null,
    },
  };
};

const selectionStageNameFromEvent = (
  type: string,
  payload: Record<string, unknown> | null,
): string | null => {
  const explicitStage = typeof payload?.stage === 'string' ? payload.stage : '';
  if (explicitStage && !explicitStage.startsWith('selection_')) return explicitStage;
  const mapping: Record<string, string> = {
    selection_candidate_discovery_done: 'candidate_discovery',
    selection_balanced_candidate_evidence_done: 'balanced_candidate_evidence',
    selection_candidate_screening_done: 'candidate_screening',
    selection_deep_dive_done: 'single_stock_deep_dive',
    selection_meta_orchestrator_done: 'meta_orchestrator',
    selection_pricing_agent_done: 'pricing_agent',
    selection_allocation_done: 'portfolio_allocation',
    selection_adversarial_done: 'adversarial_review',
    selection_judge_done: 'judge_decision',
  };
  return mapping[type] || null;
};

const looksLikeFinalReport = (value: Record<string, unknown>): boolean => (
  Boolean(value.orchestration_mode || value.expert_state || value.selection_context || value.candidate_discovery)
);

const normalizeStockSelectionPayload = (
  value: Record<string, unknown> | null | undefined,
): Record<string, unknown> | null => {
  const record = asRecord(value);
  if (!record) return null;
  if (asRecord(record.final_report_json)) return record;
  if (!looksLikeFinalReport(record)) return record;
  return {
    enabled: record.enabled ?? true,
    success: record.success ?? true,
    ...record,
    final_report_json: record,
    selection_context: asRecord(record.selection_context) || {},
  };
};

const getStockSelectionMode = (stockSelection: Record<string, unknown> | null): string => {
  const normalized = normalizeStockSelectionPayload(stockSelection);
  const finalReport = asRecord(normalized?.final_report_json) || {};
  const selCtx = asRecord(normalized?.selection_context) || {};
  return String(finalReport.orchestration_mode || selCtx.orchestration_mode || 'legacy');
};

const mergeStockSelectionResult = (
  existingValue: Record<string, unknown> | null | undefined,
  incomingValue: Record<string, unknown> | null | undefined,
): Record<string, unknown> | null | undefined => {
  const existing = normalizeStockSelectionPayload(existingValue);
  const incoming = normalizeStockSelectionPayload(incomingValue);
  if (!incoming) return existing;
  if (!existing) return incoming;

  const existingMode = getStockSelectionMode(existing);
  const incomingMode = getStockSelectionMode(incoming);
  const existingExpertState = extractExpertState(existing);
  const incomingExpertState = extractExpertState(incoming);

  if (existingMode === 'expert_graph' && incomingMode !== 'expert_graph' && !incomingExpertState) {
    return {
      ...incoming,
      final_report_json: {
        ...(asRecord(incoming.final_report_json) || {}),
        orchestration_mode: 'expert_graph',
        expert_state: existingExpertState || (asRecord(incoming.final_report_json) || {}).expert_state || null,
      },
      selection_context: {
        ...(asRecord(incoming.selection_context) || {}),
        orchestration_mode: 'expert_graph',
        expert_state: existingExpertState || (asRecord(incoming.selection_context) || {}).expert_state || null,
      },
    };
  }

  if (existingMode === 'expert_graph' && incomingMode === 'expert_graph' && existingExpertState && !incomingExpertState) {
    return {
      ...incoming,
      final_report_json: {
        ...(asRecord(incoming.final_report_json) || {}),
        expert_state: existingExpertState,
      },
      selection_context: {
        ...(asRecord(incoming.selection_context) || {}),
        expert_state: existingExpertState,
      },
    };
  }

  return incoming;
};

const loadTraceHistory = (): TraceHistoryItem[] => {
  try {
    const raw = window.localStorage.getItem(TRACE_HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed)
      ? parsed.slice(0, TRACE_HISTORY_LIMIT).map(compactTraceHistoryItem) as TraceHistoryItem[]
      : [];
  } catch { return []; }
};

const saveTraceHistory = (items: TraceHistoryItem[]) => {
  const compactItems = items.slice(0, TRACE_HISTORY_LIMIT).map(compactTraceHistoryItem);
  try {
    window.localStorage.setItem(TRACE_HISTORY_KEY, JSON.stringify(compactItems));
  } catch (error) {
    try {
      window.localStorage.setItem(TRACE_HISTORY_KEY, JSON.stringify(compactItems.slice(0, 1)));
    } catch {
      // History is only a convenience index. Ignore quota/security errors so the trace page keeps rendering.
    }
    if (import.meta.env.DEV) {
      console.warn('Agent Trace history save skipped:', error);
    }
  }
};

const persistTraceHistory = (items: TraceHistoryItem[], next: TraceHistoryItem): TraceHistoryItem[] => {
  const compactNext = compactTraceHistoryItem(next);
  const merged = [compactNext, ...items.map(compactTraceHistoryItem).filter((item) => item.id !== compactNext.id)].slice(0, TRACE_HISTORY_LIMIT);
  saveTraceHistory(merged);
  return merged;
};

const compactTraceHistoryItem = (item: TraceHistoryItem): TraceHistoryItem => {
  const result = item.result || createEmptyTraceResult();
  const finalReport = asRecord(result.stock_selection?.final_report_json) || {};
  const selectionContext = asRecord(result.stock_selection?.selection_context) || {};
  const compactStockSelection = result.stock_selection ? {
    enabled: result.stock_selection.enabled ?? true,
    success: result.stock_selection.success ?? result.success,
    final_report_json: {
      orchestration_mode: finalReport.orchestration_mode,
    },
    selection_context: {
      orchestration_mode: selectionContext.orchestration_mode,
    },
  } : null;

  return {
    ...item,
    isCompact: true,
    result: {
      ...createEmptyTraceResult(),
      success: result.success,
      session_id: result.session_id || item.id,
      content: truncateText(result.content || '', HISTORY_CONTENT_PREVIEW_CHARS),
      error: result.error ?? null,
      total_steps: result.total_steps || 0,
      total_tokens: result.total_tokens || 0,
      provider: result.provider || '',
      model: result.model || '',
      mode: result.mode || 'planning_execute',
      events: [],
      tool_calls: [],
      planner: result.planner ? { intent: result.planner.intent, primary_symbol: result.planner.primary_symbol } : null,
      agent_user_context: null,
      context_summary: result.context_summary ? {
        account_count: result.context_summary.account_count,
        position_count: result.context_summary.position_count,
        investor: asRecord(result.context_summary.investor),
      } : null,
      debate: null,
      stock_selection: compactStockSelection,
      risk_gate: null,
      llm_telemetry: asRecord(result.llm_telemetry) || null,
      judge_sanity: asRecord(result.judge_sanity) || null,
      artifact_dir: result.artifact_dir || null,
      runtime_config: result.runtime_config || null,
    },
  };
};

const shouldLoadFullTraceFromBackend = (item: TraceHistoryItem | undefined): boolean => (
  Boolean(item?.isCompact && item.result?.artifact_dir && item.result?.session_id)
);

const truncateText = (value: string, maxChars: number): string => {
  if (!value || value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}\n\n...[已截断 ${value.length - maxChars} 字，完整内容请从后端 Trace artifact 读取]`;
};

const normalizeTraceSessionId = (value: string | undefined | null): string => {
  const raw = String(value || '').trim();
  if (!raw) return '';
  return raw.startsWith(TRACE_SESSION_PREFIX) ? raw : `${TRACE_SESSION_PREFIX}${raw}`;
};

const createTraceSessionId = (): string => {
  const random =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID().replace(/-/g, '')
      : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
  return `${TRACE_SESSION_PREFIX}${random}`;
};

const DIMENSION_LABELS: Record<string, string> = {
  account_risk: '账户风险', technical: '技术面', capital_flow: '资金面',
  news_event: '消息面', fundamental: '基本面', data_quality: '数据质量',
  chip_distribution: '筹码面', market_state: '市场状态',
};

type CandidateReasonDimension = {
  dimension: string;
  label: string;
  detail: string;
};

type DisplayCandidate = {
  code: string;
  name: string;
  source: string;
  recallSources: string[];
  candidateExperts: string[];
  candidateDimensions: string[];
  strategies: string[];
  tags: string[];
  reason: string;
  score?: number;
  scoreKind?: string;
  scoreLabel?: string;
  scoreNote?: string;
  latestDate: string;
  metrics: Record<string, unknown>;
  reasonDimensions: CandidateReasonDimension[];
  lifecycleStatus: string;
  consensusBonus?: number;
  mixedEvidence: boolean;
  setupType?: string;
  primaryDesk?: string;
  stance?: string;
  desks: string[];
  multiDeskConviction: boolean;
  conflictFlags: string[];
};

type CandidateDimensionGroup = {
  dimension: string;
  label: string;
  candidates: Array<{
    candidate: DisplayCandidate;
    details: string[];
  }>;
};

type CandidateExpertPacketDisplay = {
  expert: string;
  dimension: string;
  label: string;
  dimensionLabel: string;
  status: string;
  count: number;
  themeCount: number;
  freshness: string;
  asOf: string;
  sourceChain: Record<string, unknown>[];
  diagnostics: Record<string, unknown>[];
  warnings: string[];
  errors: string[];
  required: boolean;
  directStockCandidate: boolean;
  goal: string;
};

type SeedPoolPreviewItem = {
  code: string;
  name: string;
  source: string;
  hint: string;
  sourceDiagnostics?: Record<string, unknown>;
  freshness: string;
  triggerSignals: string[];
};

type SeedPoolDisplay = {
  seedDate?: string;
  seedCount: number;
  totalLimit?: number;
  sourceCounts: Record<string, number>;
  dimensionCounts: Record<string, number>;
  preview: SeedPoolPreviewItem[];
};

type ThesisDeskPacketDisplay = {
  expert: string;
  label: string;
  status: string;
  seedCount: number;
  acceptedCount: number;
  rejectedCount: number;
  elapsedMs?: number;
  candidates: Array<{
    code: string;
    name: string;
    stance: string;
    setupType: string;
    reason: string;
    confidence?: number;
  }>;
  rejected: Array<{ code: string; name: string; reason: string }>;
  diagnostics: string[];
  errors: string[];
  reason: string;
  toolCallCount: number;
  perSeedPackets: Array<{
    code: string;
    name: string;
    status: string;
    elapsedMs?: number;
    candidateCount: number;
    rejectedCount: number;
    toolCallCount: number;
    errors: string[];
    diagnostics: string[];
    reason: string;
  }>;
};

type CandidateThemeDisplay = {
  theme: string;
  eventTitle: string;
  status: string;
  reason: string;
  confidence?: number;
};

type CandidateCapacityDisplay = {
  maxCandidatesToDeepDive?: number;
  minPerExpert?: number;
  maxPerExpert?: number;
  maxThemeWatchItems?: number;
  softQuotas?: Record<string, { min?: number; max?: number }>;
};

type CandidateQualityDisplay = {
  candidateCount: number;
  hardStrategyTrunkMissing: boolean;
  hardExclusionCount: number;
  fallbackCount: number;
  multiSourceCount: number;
  dimensionCounts: Record<string, number>;
  sourceCounts: Record<string, number>;
  lifecycleCounts: Record<string, number>;
};

type CandidateHardExclusionDisplay = {
  excludedCount: number;
  reasonCounts: Record<string, number>;
  examples: Array<{ code: string; name: string; reason: string; source?: string }>;
  policy: Record<string, unknown>;
};

type CandidateActionSuggestion = {
  key: string;
  label: string;
  tone?: CandidateDecisionTone;
  strength?: string;
  reason?: string;
};

type EventImpactWatch = {
  eventId: string;
  title: string;
  snippet: string;
  eventType: string;
  maturity: string;
  impactVariables: string[];
  watchThemes: string[];
  validationWindowDays?: number;
  source: string;
  url: string;
  publishedDate: string;
  validationMatches: Array<{
    theme: string;
    status: string;
    resultCount: number;
    titles: string[];
  }>;
};

const STRATEGY_LABELS: Record<string, string> = {
  ma_volume: '均线放量突破',
  turtle_trade: '海龟突破',
  high_tight_flag: '高窄旗形',
  limit_up_shakeout: '涨停洗盘',
  uptrend_limit_down: '上升趋势跌停错杀',
  rps_breakout: 'RPS 强势突破',
  volume_breakout: '放量突破',
  capital_heat: '资金热度',
  quality_value: '质量价值',
  shrink_pullback: '缩量回踩',
  balanced_alpha: '均衡 Alpha',
  dual_low: '双低价值',
  momentum_quality: '动量质量',
  oversold_reversal: '超跌反转',
  hot_sector: '强势板块',
  breakout: '突破',
  rps: 'RPS 强势',
  momentum: '动量',
  relative_strength: '相对强势',
  liquidity: '流动性',
  consolidation: '平台整理',
  volume_shrink: '缩量',
};

const SOURCE_LABEL_PREFIXES: Array<[string, string]> = [
  ['alphasift:', 'AlphaSift 多因子'],
  ['sequoia:', 'Sequoia 形态'],
  ['event_impact:', '事件影响链'],
  ['news_momentum:', '消息面动量'],
  ['news_sentiment:', '新闻情绪热点'],
  ['akshare:industry:', '强势行业板块'],
  ['akshare:concept:', '强势概念板块'],
  ['fallback_seed_pool', '固定兜底观察池'],
  ['user_seed', '用户输入'],
];

const DESK_LABELS: Record<string, string> = {
  early_turn_desk: '低位启动席',
  momentum_desk: '动量席',
  quality_repair_desk: '质量修复席',
  theme_catalyst_desk: '主题催化席',
};

const SETUP_TYPE_LABELS: Record<string, string> = {
  trend_continuation: '趋势延续',
  early_turn: '低位启动',
  theme_follow: '题材跟随',
  theme_catalyst: '主题催化',
  quality_repair: '质量修复',
  capital_momentum: '资金动量',
  unknown: '未分类',
};

const STANCE_LABELS: Record<string, string> = {
  support: '支持',
  watch: '观察',
  neutral: '中性',
  oppose: '反对',
  invalid: '无效',
};

const deskLabel = (key: string): string => DESK_LABELS[key] || key;
const setupTypeLabel = (key: string): string => SETUP_TYPE_LABELS[key] || key;
const stanceLabel = (key: string): string => STANCE_LABELS[key] || key;

const CANDIDATE_EXPERT_LABELS: Record<string, string> = {
  strategy_factor_expert: 'AlphaSift 策略多因子专家',
  technical_candidate_expert: 'Sequoia 技术形态专家',
  sector_theme_expert: '板块主题专家',
  capital_flow_expert: '资金发现专家',
  news_event_expert: '消息事件专家',
  sentiment_theme_expert: '情绪/宏观专家',
  fundamental_expert: '基本面发现专家',
  early_turn_desk: '低位启动席',
  momentum_desk: '动量席',
  quality_repair_desk: '质量修复席',
  theme_catalyst_desk: '主题催化席',
};

const CANDIDATE_EXPERT_META: Record<string, { goal: string; required: boolean; directStockCandidate: boolean }> = {
  strategy_factor_expert: { goal: 'YAML 多策略硬筛', required: true, directStockCandidate: true },
  technical_candidate_expert: { goal: '技术形态/突破/RPS', required: true, directStockCandidate: true },
  capital_flow_expert: { goal: '主力、龙虎榜、两融、板块资金', required: true, directStockCandidate: true },
  fundamental_expert: { goal: '成长、质量、估值、安全边际', required: true, directStockCandidate: true },
  sector_theme_expert: { goal: '强势板块扩散到个股', required: false, directStockCandidate: true },
  news_event_expert: { goal: '公司级硬事件', required: false, directStockCandidate: true },
  sentiment_theme_expert: { goal: '主题观察和验证线索', required: false, directStockCandidate: false },
  early_turn_desk: { goal: '低位区间、资金回补、拐点启动', required: true, directStockCandidate: true },
  momentum_desk: { goal: '趋势延续、放量突破、强势动量', required: true, directStockCandidate: true },
  quality_repair_desk: { goal: '基本面质量、低估修复、困境反转', required: true, directStockCandidate: true },
  theme_catalyst_desk: { goal: '日报主题、业务匹配、板块资金验证', required: true, directStockCandidate: true },
};

const DIMENSION_GROUP_LABELS: Record<string, string> = {
  strategy: '策略候选',
  technical: '技术面候选',
  capital: '资金面候选',
  sentiment: '情绪/热点候选',
  message: '消息面候选',
  fundamental: '基本面候选',
  market_regime: '市场环境约束',
  portfolio_risk: '组合风控约束',
  fallback: '兜底观察池',
  other: '其他候选',
};

const DIMENSION_GROUP_ORDER = ['strategy', 'technical', 'capital', 'sentiment', 'message', 'fundamental', 'market_regime', 'portfolio_risk', 'fallback', 'other'];

const displayStrategyName = (name: string): string => STRATEGY_LABELS[name] || name;

const displayCandidateExpertName = (expert: string): string => (
  CANDIDATE_EXPERT_LABELS[expert] || expert.replace(/_/g, ' ')
);

const displaySourceName = (source: string): string => {
  if (source === 'alphasift:multi_strategy') return 'AlphaSift 多策略共振';
  if (source === 'sequoia:multi_strategy') return 'Sequoia 多策略共振';
  if (source === 'expert_graph_discovery') return '多专家候选发现';
  if (source === 'multi_expert_recall') return '多专家候选共振';
  if (source.startsWith('candidate_expert:')) {
    return displayCandidateExpertName(source.slice('candidate_expert:'.length));
  }
  for (const [prefix, label] of SOURCE_LABEL_PREFIXES) {
    if (source === prefix || source.startsWith(prefix)) {
      const suffix = source.slice(prefix.length);
      if (suffix === 'multi_strategy') return `${label}：多策略共振`;
      return suffix ? `${label}：${suffix}` : label;
    }
  }
  if (source === 'multi_recall') return '多路召回';
  return source;
};

const displayReasonText = (reason: string): string => {
  let text = reason;
  Object.entries(STRATEGY_LABELS).forEach(([raw, label]) => {
    text = text.replaceAll(raw, label);
  });
  text = text.replaceAll('market_regime', '市场环境');
  text = text.replaceAll('technical', '技术结构');
  text = text.replaceAll('capital_chip', '资金筹码');
  text = text.replaceAll('news_sentiment', '消息情绪');
  text = text.replaceAll('fundamental', '基本面');
  text = text.replaceAll('portfolio_risk', '组合风控');
  text = text.replaceAll('detect_market_regime/get_sector_rankings', '市场状态/板块排行工具');
  text = text.replaceAll('analyze_trend', '趋势分析工具');
  text = text.replaceAll('get_capital_flow', '资金流工具');
  return text;
};

const STRATEGY_ONLY_TAGS = new Set(['breakout', 'rps', 'momentum', 'relative_strength', 'liquidity', 'ma_cross']);

const isStrategySummaryReason = (reason: string): boolean => {
  const text = reason.trim();
  return text.includes('多策略共振') || text.includes('多路召回共振') || text.includes('策略入池');
};

const strategySourceDetail = (sources: string[], labels: string[]): string => {
  const suffix = labels.length ? `：${labels.join('、')}` : '';
  if (sources.some((s) => s.startsWith('alphasift:'))) return `AlphaSift YAML 多因子策略入池${suffix}`;
  if (sources.some((s) => s.startsWith('sequoia:'))) return `Sequoia 形态/动量策略入池${suffix}`;
  return labels.length ? `命中策略：${labels.join('、')}` : '';
};

const formatMetricValue = (value: unknown): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return String(value ?? '');
  if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(2)}亿`;
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(2)}万`;
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
};

const formatMetricWithSuffix = (value: unknown, suffix = ''): string => {
  if (value == null || value === '') return '';
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value);
  return `${num.toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
};

const fundamentalMetricBadges = (candidate: DisplayCandidate): string[] => {
  const metrics = candidate.metrics || {};
  return [
    ['ROE', metrics.roe, '%'],
    ['营收增速', metrics.revenue_growth ?? metrics.revenueGrowth, '%'],
    ['利润增速', metrics.profit_growth ?? metrics.profitGrowth, '%'],
    ['现金流/利润', metrics.operating_cashflow_ratio ?? metrics.operatingCashflowRatio, '%'],
    ['PE', metrics.pe_ttm ?? metrics.peTtm, ''],
    ['PB', metrics.pb, ''],
  ]
    .map(([label, value, suffix]) => {
      const formatted = formatMetricWithSuffix(value, String(suffix));
      return formatted ? `${label} ${formatted}` : '';
    })
    .filter(Boolean);
};

const technicalMetricBadges = (candidate: DisplayCandidate): string[] => {
  const metrics = candidate.metrics || {};
  const macdRaw = String(metrics.macd_status || '').trim();
  const macd = macdRaw
    ? ({ bullish: 'MACD 多头', bearish: 'MACD 空头', neutral: 'MACD 中性' } as Record<string, string>)[macdRaw] || `MACD ${macdRaw}`
    : '';
  const rsiValue = formatMetricWithSuffix(metrics.rsi_value, '');
  const rsiRaw = String(metrics.rsi_status || '').trim();
  const rsi = rsiValue
    ? `RSI ${rsiValue}${rsiRaw === 'overbought' ? ' / 超买' : rsiRaw === 'oversold' ? ' / 超卖' : ''}`
    : '';
  const bollRaw = String(metrics.boll_position || '').trim();
  const boll = bollRaw
    ? ({
      above_upper: '布林上轨外',
      upper_half: '布林中上轨',
      lower_half: '布林中下轨',
      below_lower: '布林下轨外',
    } as Record<string, string>)[bollRaw] || `布林 ${bollRaw}`
    : '';
  return [
    ['MA5', metrics.ma5, ''],
    ['MA20', metrics.ma20, ''],
    ['MA60', metrics.ma60, ''],
    ['量比', metrics.volume_ratio, ''],
    ['RPS', metrics.rps, ''],
  ]
    .map(([label, value, suffix]) => {
      const formatted = formatMetricWithSuffix(value, String(suffix));
      return formatted ? `${label} ${formatted}` : '';
    })
    .concat([macd, rsi, boll].filter(Boolean))
    .filter(Boolean);
};

const dimensionTone = (dimension: string): string => {
  if (dimension === 'strategy') return 'border-cyan/25 bg-cyan/10 text-cyan';
  if (dimension === 'technical') return 'border-success/25 bg-success/10 text-success';
  if (dimension === 'capital') return 'border-warning/25 bg-warning/10 text-warning';
  if (dimension === 'sentiment') return 'border-purple/25 bg-purple/10 text-purple';
  if (dimension === 'message') return 'border-cyan/25 bg-cyan/10 text-cyan';
  if (dimension === 'fundamental') return 'border-border/60 bg-surface-2 text-secondary-text';
  if (dimension === 'market_regime') return 'border-danger/25 bg-danger/10 text-danger';
  if (dimension === 'portfolio_risk') return 'border-border/60 bg-surface-2 text-secondary-text';
  return 'border-border/60 bg-surface-2 text-muted-text';
};

const displayEventMaturity = (maturity: string): string => {
  const mapping: Record<string, string> = {
    breaking: '突发观察',
    developing: '等待验证',
    confirmed: '已验证',
  };
  return mapping[maturity] || maturity || '未知';
};

const displayLifecycleStatus = (status: string): string => {
  const mapping: Record<string, string> = {
    new: '新进入',
    active: '持续有效',
    watching: '观察中',
    decayed: '已降权',
    removed: '已移出',
  };
  return mapping[status] || status || '未知';
};

const displayCandidateAction = (action: string): { label: string; tone: CandidateDecisionTone } => {
  const mapping: Record<string, { label: string; tone: CandidateDecisionTone }> = {
    open: { label: '可小仓试探', tone: 'success' },
    add: { label: '可加仓', tone: 'success' },
    hold: { label: '继续持有', tone: 'info' },
    wait: { label: '等待确认', tone: 'warning' },
    monitor: { label: '观察跟踪', tone: 'info' },
    reject: { label: '暂不纳入', tone: 'danger' },
    reduce: { label: '减仓', tone: 'warning' },
    take_profit: { label: '止盈观察', tone: 'warning' },
    stop_loss: { label: '止损', tone: 'danger' },
    insufficient_data: { label: '证据不足', tone: 'warning' },
    deep_dive: { label: '进入深挖', tone: 'success' },
  };
  return mapping[action] || { label: action || '观察跟踪', tone: 'default' };
};

const actionStrengthLabel = (strength: string): string => {
  const mapping: Record<string, string> = {
    strong: '强',
    medium: '中',
    weak: '弱',
    none: '无',
  };
  return mapping[strength] || strength;
};

const inferCandidateActionFromScore = (candidate: DisplayCandidate): CandidateActionSuggestion => {
  const isFallback = candidate.source === 'fallback_seed_pool' || candidate.recallSources.includes('fallback_seed_pool');
  if (isFallback) {
    return {
      key: 'monitor',
      label: '观察跟踪',
      tone: 'info',
      reason: '固定种子池只用于维持取证链路，不作为真实推荐。',
    };
  }
  if (candidate.mixedEvidence) {
    return {
      key: 'wait',
      label: '等待确认',
      tone: 'warning',
      reason: '候选存在反证，需等待后续技术、资金或消息证据确认。',
    };
  }
  if (candidate.multiDeskConviction || candidate.recallSources.length > 1) {
    return {
      key: 'monitor',
      label: '重点观察',
      tone: 'info',
      reason: '候选具备多来源或多席位证据，但尚未完成最终 Judge 裁决。',
    };
  }
  return {
    key: 'monitor',
    label: '观察跟踪',
    tone: 'info',
    reason: '候选池阶段只代表值得继续取证，不代表直接买入。',
  };
};

const displayExclusionReason = (reason: string): string => {
  const mapping: Record<string, string> = {
    missing_code: '缺少代码',
    blacklisted: '黑名单',
    st_or_special_treatment: 'ST/特殊处理',
    suspended: '停牌',
    delist_risk: '退市风险',
    untradable_limit_locked: '极端不可成交',
    new_listing_risk: '上市时间过短',
    insufficient_liquidity: '流动性不足',
    name_code_mismatch: '名称代码不一致',
  };
  return mapping[reason] || reason;
};

const displayQualityKey = (key: string): string => (
  DIMENSION_GROUP_LABELS[key]
  || CANDIDATE_EXPERT_LABELS[key]
  || displaySourceName(key)
  || key
);

const formatConfidence = (value: unknown): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return `${Math.round(value * 100)}%`;
};

const normalizeReasonDimensions = (item: Record<string, unknown>): CandidateReasonDimension[] => {
  const result: CandidateReasonDimension[] = [];
  const add = (dimension: string, label: string, detail: string) => {
    const text = detail.trim();
    if (!text) return;
    if (result.some((entry) => entry.dimension === dimension && entry.detail === text)) return;
    result.push({ dimension, label, detail: text });
  };

  const source = String(item.source || item.candidate_source || '');
  const recallSources = toStringList(item.recall_sources);
  const sources = recallSources.length ? recallSources : source ? [source] : [];
  const strategies = toStringList(item.matched_strategies || item.strategies).map(displayStrategyName);
  const metrics = asRecord(item.metrics) || {};
  const labels = strategies.filter((label, index, arr) => label && arr.indexOf(label) === index);

  const explicit = toRecordList(item.reason_dimensions).map((entry) => {
    const dimension = String(entry.dimension || 'other');
    const label = String(entry.label || entry.dimension || '理由');
    const rawDetail = String(entry.detail || entry.reason || entry.text || '');
    if (dimension === 'strategy') {
      const detail = strategySourceDetail(sources, labels) || displayReasonText(rawDetail);
      return { dimension, label, detail };
    }
    if (dimension === 'technical' && isStrategySummaryReason(rawDetail)) {
      return { dimension, label, detail: '' };
    }
    if (dimension === 'capital' && rawDetail && !rawDetail.includes('流动性代理')) {
      return { dimension, label, detail: `流动性代理：${displayReasonText(rawDetail)}` };
    }
    return { dimension, label, detail: displayReasonText(rawDetail) };
  }).filter((entry) => entry.detail);
  if (explicit.length) return explicit;

  if (sources.some((s) => s.startsWith('alphasift:'))) {
    add('strategy', '策略', strategySourceDetail(sources, labels));
  } else if (sources.some((s) => s.startsWith('sequoia:'))) {
    add('strategy', '策略', strategySourceDetail(sources, labels));
  } else if (labels.length) {
    add('strategy', '策略', strategySourceDetail(sources, labels));
  }

  sources.filter((s) => s.startsWith('akshare:')).forEach((s) => {
    const sector = s.split(':').pop() || '';
    add('sentiment', '情绪/热点', sector ? `来自强势板块「${sector}」成分股` : '来自强势板块成分股');
  });
  if (sources.some((s) => s.startsWith('news_sentiment:') || s.startsWith('news_momentum:'))) {
    const topic = String(item.news_topic || '');
    const title = String(item.news_title || item.headline || '');
    const sourceName = String(item.news_source || '');
    add(
      sources.some((s) => s.startsWith('news_momentum:')) ? 'message' : 'sentiment',
      sources.some((s) => s.startsWith('news_momentum:')) ? '消息面' : '情绪/热点',
      [
        topic ? `热点主题：${topic}` : '',
        title ? `新闻：${title}` : '',
        sourceName ? `来源：${sourceName}` : '',
      ].filter(Boolean).join('；') || '被近期科技/商业热点新闻提及',
    );
  }
  if (sources.some((s) => s.startsWith('event_impact:'))) {
    const eventTitle = String(item.event_title || '');
    const theme = String(item.validated_theme || '');
    const validationTitle = String(item.validation_title || '');
    add(
      'sentiment',
      '情绪/事件',
      [
        eventTitle ? `事件：${eventTitle}` : '',
        theme ? `验证主题：${theme}` : '',
        validationTitle ? `后续事实：${validationTitle}` : '',
      ].filter(Boolean).join('；') || '事件传导验证后的主题成分候选',
    );
  }

  const reason = String(item.reason || item.candidate_reason || item.entry_reason || '');
  if (reason && !reason.includes('多路召回') && !isStrategySummaryReason(reason)) add('technical', '技术面', displayReasonText(reason));
  if (reason && !result.length) add('strategy', '策略', displayReasonText(reason));

  const technicalBits: string[] = [];
  if (toStringList(item.strategy_tags).some((tag) => ['breakout', 'rps', 'momentum', 'relative_strength', 'ma_cross'].includes(tag))) {
    technicalBits.push('形态/趋势信号满足候选条件');
  }
  [
    ['breakout_20d_pct', '20日突破'],
    ['range_20d_pct', '20日区间'],
    ['pullback_to_ma20_pct', '回踩MA20'],
    ['consolidation_days_20d', '收敛天数'],
    ['rps', 'RPS'],
  ].forEach(([key, label]) => {
    const value = metrics[key];
    if (value != null) technicalBits.push(`${label}=${formatMetricValue(value)}`);
  });
  if (technicalBits.length) add('technical', '技术面', technicalBits.slice(0, 3).join('；'));

  const capitalBits: string[] = [];
  [
    ['amount', '成交额'],
    ['turnover', '成交额'],
    ['turnover_rate', '换手率'],
    ['volume_ratio', '量比'],
    ['volume_ratio_20d', '20日量比'],
  ].forEach(([key, label]) => {
    const value = item[key] ?? metrics[key];
    if (value != null) capitalBits.push(`${label}=${formatMetricValue(value)}`);
  });
  if (capitalBits.length) add('capital', '资金面', `流动性代理：${capitalBits.slice(0, 4).join('；')}`);

  if (source === 'user_seed') add('message', '消息/输入', '用户或上下文提供，优先进入候选池');
  if (source === 'fallback_seed_pool') {
    return [{ dimension: 'fallback', label: '兜底观察', detail: '固定种子池兜底，仅用于保证后续取证链路可运行，不代表策略筛选或推荐' }];
  }
  return result.slice(0, 5);
};

const normalizeCandidate = (item: Record<string, unknown>): DisplayCandidate | null => {
  const code = String(item.code || item.stock_code || item.symbol || '').trim();
  if (!code) return null;
  const metrics = asRecord(item.metrics) || {};
  const scoreValue = item.signal_score ?? item.score;
  const score = typeof scoreValue === 'number' ? scoreValue : Number(scoreValue);
  const source = String(item.source || item.candidate_source || '');
  const recallSources = toStringList(item.recall_sources);
  const isFallbackSeed = source === 'fallback_seed_pool' || recallSources.includes('fallback_seed_pool');
  const fallbackReasonDimensions: CandidateReasonDimension[] = [
    { dimension: 'fallback', label: '兜底观察', detail: '固定种子池兜底，仅用于保证后续取证链路可运行，不代表策略筛选或推荐' },
  ];
  return {
    code,
    name: String(item.name || item.stock_name || ''),
    source,
    recallSources,
    candidateExperts: toStringList(item.candidate_experts),
    candidateDimensions: toStringList(item.candidate_dimensions),
    strategies: isFallbackSeed ? [] : toStringList(item.matched_strategies || item.strategies).map(displayStrategyName),
    tags: isFallbackSeed ? [] : toStringList(item.strategy_tags).filter((tag) => !STRATEGY_ONLY_TAGS.has(tag)).map(displayStrategyName),
    reason: displayReasonText(String(item.reason || item.candidate_reason || item.entry_reason || '')),
    score: Number.isFinite(score) ? score : undefined,
    scoreKind: item.score_kind ? String(item.score_kind) : undefined,
    scoreLabel: item.score_label ? String(item.score_label) : undefined,
    scoreNote: item.score_note ? String(item.score_note) : undefined,
    latestDate: String(item.latest_date || item.date || ''),
    metrics,
    reasonDimensions: isFallbackSeed ? fallbackReasonDimensions : normalizeReasonDimensions(item),
    lifecycleStatus: String(item.lifecycle_status || 'new'),
    consensusBonus: typeof item.consensus_bonus === 'number' ? item.consensus_bonus : undefined,
    mixedEvidence: Boolean(item.mixed_evidence),
    setupType: item.setup_type ? String(item.setup_type) : undefined,
    primaryDesk: item.primary_desk ? String(item.primary_desk) : undefined,
    stance: item.stance ? String(item.stance) : undefined,
    desks: toStringList(item.desks),
    multiDeskConviction: Boolean(item.multi_desk_conviction),
    conflictFlags: toStringList(item.conflict_flags),
  };
};

const mergeDisplayCandidates = (groups: DisplayCandidate[][]): DisplayCandidate[] => {
  const byCode = new Map<string, DisplayCandidate>();
  groups.flat().forEach((candidate) => {
    const current = byCode.get(candidate.code);
    if (!current) {
      byCode.set(candidate.code, { ...candidate });
      return;
    }
    current.name = current.name || candidate.name;
    current.source = current.source || candidate.source;
    current.recallSources = Array.from(new Set([...current.recallSources, ...candidate.recallSources]));
    current.candidateExperts = Array.from(new Set([...current.candidateExperts, ...candidate.candidateExperts]));
    current.candidateDimensions = Array.from(new Set([...current.candidateDimensions, ...candidate.candidateDimensions]));
    current.strategies = Array.from(new Set([...current.strategies, ...candidate.strategies]));
    current.tags = Array.from(new Set([...current.tags, ...candidate.tags]));
    current.reason = current.reason || candidate.reason;
    if (candidate.score != null && (current.score == null || candidate.score > current.score)) {
      current.score = candidate.score;
      current.scoreKind = candidate.scoreKind || current.scoreKind;
      current.scoreLabel = candidate.scoreLabel || current.scoreLabel;
      current.scoreNote = candidate.scoreNote || current.scoreNote;
    }
    current.latestDate = current.latestDate || candidate.latestDate;
    current.metrics = { ...candidate.metrics, ...current.metrics };
    current.lifecycleStatus = current.lifecycleStatus || candidate.lifecycleStatus;
    current.consensusBonus = Math.max(current.consensusBonus ?? 0, candidate.consensusBonus ?? 0) || current.consensusBonus || candidate.consensusBonus;
    current.mixedEvidence = current.mixedEvidence || candidate.mixedEvidence;
    current.reasonDimensions = [...current.reasonDimensions];
    candidate.reasonDimensions.forEach((entry) => {
      if (!current.reasonDimensions.some((item) => item.dimension === entry.dimension && item.detail === entry.detail)) {
        current.reasonDimensions.push(entry);
      }
    });
  });
  return Array.from(byCode.values());
};

type DeskCommitteeStatus = {
  mode: string;
  status: string;
  degraded: boolean;
  fallbackUsed: boolean;
  error: string;
  dimensionsCovered: string[];
  deskDiagnostics: Array<{ desk: string; status: string; picks: number }>;
};

const findCandidateDiscoveryPayload = (
  stockSelection: Record<string, unknown> | null,
): { discovery: Record<string, unknown>; full: Record<string, unknown> } => {
  const stockSel = stockSelection || {};
  const finalReport = asRecord(stockSel.final_report_json) || {};
  const stages = asRecord(asRecord(stockSel.selection_context)?.stages) || {};
  const discovery = asRecord(finalReport.candidate_discovery) || asRecord(stages.candidate_discovery) || {};
  const full = asRecord(discovery.full) || {};
  return { discovery, full };
};

const extractSeedPoolDisplay = (
  result: AgentTraceRunResponse,
  stockSelection: Record<string, unknown> | null,
): SeedPoolDisplay | null => {
  const { discovery, full } = findCandidateDiscoveryPayload(stockSelection);
  const eventSummary = result.events
    .map((event) => asRecord(asRecord(event)?.payload)?.seed_pool_summary)
    .map(asRecord)
    .find(Boolean);
  const summary = (
    asRecord(discovery.seed_pool_summary)
    || asRecord(full.seed_pool_summary)
    || eventSummary
  );
  if (!summary) return null;
  const preview = toRecordList(summary.preview).map((item) => ({
    code: String(item.code || ''),
    name: String(item.name || ''),
    source: String(item.source || ''),
    hint: String(item.hint || ''),
    sourceDiagnostics: asRecord(item.source_diagnostics) || undefined,
    freshness: String(item.freshness || ''),
    triggerSignals: toRecordList(item.trigger_signals)
      .map((signal) => String(signal.summary || signal.kind || signal.dimension || ''))
      .filter(Boolean),
  })).filter((item) => item.code);
  return {
    seedDate: String(summary.seed_date || summary.trade_date || '').trim() || undefined,
    seedCount: Number(summary.seed_count || preview.length || 0),
    totalLimit: typeof summary.total_limit === 'number' ? summary.total_limit : undefined,
    sourceCounts: Object.fromEntries(Object.entries(asRecord(summary.seed_sources) || {}).map(([key, value]) => [key, Number(value || 0)])),
    dimensionCounts: Object.fromEntries(Object.entries(asRecord(summary.signal_dimensions) || {}).map(([key, value]) => [key, Number(value || 0)])),
    preview,
  };
};

const extractThesisDeskPackets = (
  stockSelection: Record<string, unknown> | null,
): ThesisDeskPacketDisplay[] => {
  const { discovery, full } = findCandidateDiscoveryPayload(stockSelection);
  const packets = toRecordList(discovery.thesis_desk_packets).length
    ? toRecordList(discovery.thesis_desk_packets)
    : toRecordList(full.thesis_desk_packets);
  return packets.map((packet) => {
    const expert = String(packet.expert || '');
    const seedSummary = asRecord(packet.seed_summary) || {};
    const perSeedPackets = toRecordList(packet.per_seed_packets).map((seedPacket) => {
      const seedCandidates = toRecordList(seedPacket.candidates);
      const seedRejected = toRecordList(seedPacket.rejected);
      const seedDiagnostics = toRecordList(seedPacket.diagnostics);
      const diagnosticCode = seedDiagnostics
        .map((item) => String(item.code || item.expected_code || ''))
        .find(Boolean) || '';
      const firstCandidate = asRecord(seedCandidates[0]) || {};
      const firstRejected = asRecord(seedRejected[0]) || {};
      const seedReason = [
        ...toStringList(seedPacket.errors),
        ...seedDiagnostics
          .map((item) => [item.reason, item.error, item.note, item.source, item.status].filter(Boolean).join(' · '))
          .filter(Boolean),
      ].find(Boolean) || '';
      return {
        code: String(firstCandidate.code || firstRejected.code || diagnosticCode || ''),
        name: String(firstCandidate.name || firstRejected.name || ''),
        status: String(seedPacket.status || 'unknown'),
        elapsedMs: typeof seedPacket.elapsed_ms === 'number' ? seedPacket.elapsed_ms : undefined,
        candidateCount: seedCandidates.length,
        rejectedCount: seedRejected.length,
        toolCallCount: toRecordList(seedPacket.tool_calls).length,
        errors: toStringList(seedPacket.errors),
        diagnostics: seedDiagnostics
          .map((item) => [item.source, item.status, item.reason, item.error].filter(Boolean).join(' · '))
          .filter(Boolean),
        reason: displayReasonText(seedReason),
      };
    });
    const packetDiagnostics = toRecordList(packet.diagnostics)
      .map((item) => [item.source, item.status, item.reason, item.note, item.error].filter(Boolean).join(' · '))
      .filter(Boolean);
    const packetErrors = toStringList(packet.errors);
    const packetReason = [...packetErrors, ...packetDiagnostics].find(Boolean) || '';
    return {
      expert,
      label: displayCandidateExpertName(expert || String(packet.dimension || '')),
      status: String(packet.status || 'unknown'),
      seedCount: Number(seedSummary.seed_count || 0),
      acceptedCount: Number(seedSummary.accepted_count ?? packet.candidate_count ?? toRecordList(packet.candidates).length),
      rejectedCount: Number(seedSummary.rejected_count ?? packet.rejected_count ?? toRecordList(packet.rejected).length),
      elapsedMs: typeof packet.elapsed_ms === 'number' ? packet.elapsed_ms : undefined,
      candidates: toRecordList(packet.candidates).map((candidate) => ({
        code: String(candidate.code || ''),
        name: String(candidate.name || ''),
        stance: String(candidate.stance || ''),
        setupType: String(candidate.setup_type || ''),
        reason: String(candidate.reason || ''),
        confidence: typeof candidate.confidence === 'number' ? candidate.confidence : undefined,
      })).filter((candidate) => candidate.code),
      rejected: toRecordList(packet.rejected).map((item) => ({
        code: String(item.code || ''),
        name: String(item.name || ''),
        reason: String(item.reason || item.summary || ''),
      })).filter((item) => item.code || item.reason),
      diagnostics: packetDiagnostics,
      errors: packetErrors,
      reason: displayReasonText(packetReason),
      toolCallCount: toRecordList(packet.tool_calls).length,
      perSeedPackets,
    };
  }).filter((packet) => packet.expert);
};

const extractDeskCommitteeStatus = (
  stockSelection: Record<string, unknown> | null,
): DeskCommitteeStatus | null => {
  const { discovery, full } = findCandidateDiscoveryPayload(stockSelection);
  let mode = 'thesis_desk_committee';
  let desk = asRecord(discovery.thesis_desk_committee) || asRecord(full.thesis_desk_committee);
  if (!desk) {
    mode = 'llm_expert_committee';
    desk = asRecord(discovery.llm_expert_committee) || asRecord(full.llm_expert_committee);
  }
  if (!desk) return null;
  const diags = toRecordList(desk.thesis_desk_diagnostics).length
    ? toRecordList(desk.thesis_desk_diagnostics)
    : toRecordList(desk.diagnostics);
  return {
    mode,
    status: String(desk.status || ''),
    degraded: Boolean(desk.degraded) || Boolean(discovery.degraded),
    fallbackUsed: Boolean(full.fallback_used || discovery.fallback_used)
      || String(full.candidate_source || discovery.candidate_source || '') === 'fallback',
    error: String(desk.error || ''),
    dimensionsCovered: toStringList(desk.dimensions_covered),
    deskDiagnostics: diags
      .map((d) => ({ desk: String(d.desk || ''), status: String(d.status || ''), picks: Number(d.picks || 0) }))
      .filter((d) => d.desk),
  };
};

const extractDiscoveryCandidates = (
  result: AgentTraceRunResponse,
  stockSelection: Record<string, unknown> | null,
): DisplayCandidate[] => {
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  const stages = asRecord(selCtx.stages) || {};
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  const discoveryFull = asRecord(asRecord(finalReport.candidate_discovery)?.full) || asRecord(asRecord(stages.candidate_discovery)?.full) || {};
  const stageCandidates = toRecordList(discoveryFull.candidates).map(normalizeCandidate).filter((item): item is DisplayCandidate => Boolean(item));
  const toolCandidates = result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .flatMap((call) => toRecordList(asRecord(call.result_json)?.candidates))
    .map(normalizeCandidate)
    .filter((item): item is DisplayCandidate => Boolean(item));
  return mergeDisplayCandidates([toolCandidates, stageCandidates]);
};

const extractDiscoverySteps = (result: AgentTraceRunResponse): Record<string, unknown>[] => (
  result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .flatMap((call) => toRecordList(asRecord(call.result_json)?.discovery_steps))
);

const extractCandidateExpertPackets = (result: AgentTraceRunResponse): CandidateExpertPacketDisplay[] => {
  const seen = new Set<string>();
  return result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .flatMap((call) => toRecordList(asRecord(call.result_json)?.expert_packets))
    .map((packet) => {
      const expert = String(packet.expert || '');
      const dimension = String(packet.dimension || '');
      const dataQuality = asRecord(packet.data_quality) || {};
      const meta = CANDIDATE_EXPERT_META[expert] || { goal: '', required: false, directStockCandidate: true };
      return {
        expert,
        dimension,
        label: displayCandidateExpertName(expert),
        dimensionLabel: DIMENSION_GROUP_LABELS[dimension] || dimension || '候选维度',
        status: String(packet.status || 'unknown'),
        count: toRecordList(packet.candidates).length,
        themeCount: toRecordList(packet.themes).length,
        freshness: String(dataQuality.freshness || 'unknown'),
        asOf: String(dataQuality.as_of || ''),
        sourceChain: toRecordList(dataQuality.source_chain),
        diagnostics: toRecordList(packet.diagnostics),
        warnings: [...toStringList(dataQuality.warnings), ...toStringList(packet.warnings)],
        errors: toStringList(packet.errors),
        required: meta.required,
        directStockCandidate: meta.directStockCandidate,
        goal: meta.goal,
      };
    })
    .filter((packet) => {
      if (!packet.expert || seen.has(packet.expert)) return false;
      seen.add(packet.expert);
      return true;
    });
};

const extractExpertGroupedCandidates = (result: AgentTraceRunResponse): CandidateDimensionGroup[] => {
  const byDimension = new Map<string, CandidateDimensionGroup>();
  const packetByExpert = new Map<string, CandidateExpertPacketDisplay>();
  extractCandidateExpertPackets(result).forEach((packet) => {
    packetByExpert.set(packet.expert, packet);
  });

  const packetCandidatesByDimension = new Map<string, Array<{ candidate: DisplayCandidate; details: string[] }>>();
  result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .flatMap((call) => toRecordList(asRecord(call.result_json)?.expert_packets))
    .forEach((packet) => {
      const expert = String(packet.expert || '');
      const dimension = String(packet.dimension || '') || 'other';
      const normalizedCandidates = toRecordList(packet.candidates)
        .map(normalizeCandidate)
        .filter((item): item is DisplayCandidate => Boolean(item));
      if (!expert || !normalizedCandidates.length) return;

      const key = dimension || 'other';
      const existing = packetCandidatesByDimension.get(key) || [];
      normalizedCandidates.forEach((candidate) => {
        const current = existing.find((item) => item.candidate.code === candidate.code);
        const details = candidate.reasonDimensions
          .filter((entry) => entry.dimension === key)
          .map((entry) => displayReasonText(entry.detail || candidate.reason || '-'))
          .filter(Boolean);
        const fallbackDetails = details.length ? details : [candidate.reason || candidate.source || '-'];
        if (current) {
          fallbackDetails.forEach((detail) => {
            if (detail && !current.details.includes(detail)) current.details.push(detail);
          });
        } else {
          existing.push({ candidate, details: fallbackDetails });
        }
      });
      packetCandidatesByDimension.set(key, existing);
    });

  const assignedCodes = new Set<string>();
  DIMENSION_GROUP_ORDER.forEach((dimension) => {
    const items = packetCandidatesByDimension.get(dimension) || [];
    const uniqueItems = items.filter((item) => {
      if (assignedCodes.has(item.candidate.code)) return false;
      assignedCodes.add(item.candidate.code);
      return true;
    });
    if (!uniqueItems.length) return;
    const packetMeta = Array.from(packetByExpert.values()).find((packet) => packet.dimension === dimension);
    byDimension.set(dimension, {
      dimension,
      label: packetMeta?.dimensionLabel || DIMENSION_GROUP_LABELS[dimension] || dimension,
      candidates: uniqueItems,
    });
  });

  packetCandidatesByDimension.forEach((items, dimension) => {
    if (byDimension.has(dimension)) return;
    const uniqueItems = items.filter((item) => {
      if (assignedCodes.has(item.candidate.code)) return false;
      assignedCodes.add(item.candidate.code);
      return true;
    });
    if (!uniqueItems.length) return;
    const packetMeta = Array.from(packetByExpert.values()).find((packet) => packet.dimension === dimension);
    byDimension.set(dimension, {
      dimension,
      label: packetMeta?.dimensionLabel || DIMENSION_GROUP_LABELS[dimension] || dimension,
      candidates: uniqueItems,
    });
  });

  return Array.from(byDimension.values()).sort((a, b) => {
    const ai = DIMENSION_GROUP_ORDER.indexOf(a.dimension);
    const bi = DIMENSION_GROUP_ORDER.indexOf(b.dimension);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
};

const extractCandidateExpertThemes = (result: AgentTraceRunResponse): CandidateThemeDisplay[] => {
  const seen = new Set<string>();
  return result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .flatMap((call) => toRecordList(asRecord(call.result_json)?.themes))
    .map((theme) => ({
      theme: String(theme.theme || ''),
      eventTitle: String(theme.event_title || ''),
      status: String(theme.status || 'watch'),
      reason: String(theme.reason || ''),
      confidence: typeof theme.confidence === 'number' ? theme.confidence : undefined,
    }))
    .filter((theme) => {
      const key = `${theme.theme}|${theme.eventTitle}`;
      if (!theme.theme || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
};

const extractCandidateCapacity = (result: AgentTraceRunResponse): CandidateCapacityDisplay | null => {
  const capacity = result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .map((call) => asRecord(call.result_json)?.capacity)
    .map(asRecord)
    .find(Boolean);
  if (!capacity) return null;
  return {
    maxCandidatesToDeepDive: typeof capacity.max_candidates_to_deep_dive === 'number' ? capacity.max_candidates_to_deep_dive : undefined,
    minPerExpert: typeof capacity.min_per_expert === 'number' ? capacity.min_per_expert : undefined,
    maxPerExpert: typeof capacity.max_per_expert === 'number' ? capacity.max_per_expert : undefined,
    maxThemeWatchItems: typeof capacity.max_theme_watch_items === 'number' ? capacity.max_theme_watch_items : undefined,
    softQuotas: asRecord(capacity.soft_quotas) as CandidateCapacityDisplay['softQuotas'],
  };
};

const extractCandidateQuality = (result: AgentTraceRunResponse): CandidateQualityDisplay | null => {
  const quality = result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .map((call) => asRecord(call.result_json)?.quality)
    .map(asRecord)
    .find(Boolean);
  if (!quality) return null;
  return {
    candidateCount: Number(quality.candidate_count || 0),
    hardStrategyTrunkMissing: Boolean(quality.hard_strategy_trunk_missing),
    hardExclusionCount: Number(quality.hard_exclusion_count || 0),
    fallbackCount: Number(quality.fallback_count || 0),
    multiSourceCount: Number(quality.multi_source_count || 0),
    dimensionCounts: Object.fromEntries(Object.entries(asRecord(quality.dimension_counts) || {}).map(([key, value]) => [key, Number(value || 0)])),
    sourceCounts: Object.fromEntries(Object.entries(asRecord(quality.source_counts) || asRecord(quality.expert_counts) || {}).map(([key, value]) => [key, Number(value || 0)])),
    lifecycleCounts: Object.fromEntries(Object.entries(asRecord(quality.lifecycle_counts) || {}).map(([key, value]) => [key, Number(value || 0)])),
  };
};

const extractCandidateHardExclusion = (result: AgentTraceRunResponse): CandidateHardExclusionDisplay | null => {
  const hardExclusion = result.tool_calls
    .filter((call) => call.tool === 'discover_watchlist_candidates')
    .map((call) => asRecord(call.result_json)?.hard_exclusion)
    .map(asRecord)
    .find(Boolean);
  if (!hardExclusion) return null;
  return {
    excludedCount: Number(hardExclusion.excluded_count || 0),
    reasonCounts: Object.fromEntries(Object.entries(asRecord(hardExclusion.reason_counts) || {}).map(([key, value]) => [key, Number(value || 0)])),
    examples: toRecordList(hardExclusion.examples).map((item) => ({
      code: String(item.code || ''),
      name: String(item.name || ''),
      reason: String(item.reason || ''),
      source: item.source ? String(item.source) : undefined,
    })),
    policy: asRecord(hardExclusion.policy) || {},
  };
};

const findScreeningFull = (stockSelection: Record<string, unknown> | null): Record<string, unknown> => {
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  const stages = asRecord(selCtx.stages) || {};
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  return asRecord(asRecord(finalReport.candidate_screening)?.full) || asRecord(asRecord(stages.candidate_screening)?.full) || {};
};

const findScreeningSummary = (stockSelection: Record<string, unknown> | null): Record<string, unknown> => {
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  const stages = asRecord(selCtx.stages) || {};
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  return asRecord(asRecord(finalReport.candidate_screening)?.summary) || asRecord(asRecord(stages.candidate_screening)?.summary) || {};
};

const findDeepDiveFull = (stockSelection: Record<string, unknown> | null): Record<string, unknown> => {
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  const stages = asRecord(selCtx.stages) || {};
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  return asRecord(asRecord(finalReport.single_stock_deep_dive)?.full) || asRecord(asRecord(stages.single_stock_deep_dive)?.full) || {};
};

const extractDeepDiveSummariesByCode = (stockSelection: Record<string, unknown> | null): Map<string, Record<string, unknown>> => {
  const byCode = new Map<string, Record<string, unknown>>();
  const full = findDeepDiveFull(stockSelection);
  const results = toRecordList(full.results);
  results.forEach((item) => {
    const summary = asRecord(item.summary) || {};
    const stock = asRecord(asRecord(item.full)?.stock) || {};
    const code = String(summary.code || stock.code || '').trim();
    if (!code) return;
    byCode.set(code, summary);
  });
  return byCode;
};

const extractScreeningByCode = (stockSelection: Record<string, unknown> | null): Map<string, Record<string, unknown>> => {
  const byCode = new Map<string, Record<string, unknown>>();
  toRecordList(findScreeningFull(stockSelection).shortlist).forEach((item) => {
    const code = String(item.code || '').trim();
    if (code) byCode.set(code, item);
  });
  return byCode;
};

const suggestTraceCandidateAction = (
  candidate: DisplayCandidate,
  stockSelection: Record<string, unknown> | null,
): CandidateActionSuggestion => {
  const screeningSummary = findScreeningSummary(stockSelection);
  const deepTargets = new Set(toStringList(screeningSummary.deep_dive_targets));
  const monitorTargets = new Set(toStringList(screeningSummary.monitor_targets));
  const rejectedTargets = new Set(toStringList(screeningSummary.rejected_targets));
  const screeningByCode = extractScreeningByCode(stockSelection);
  const deepByCode = extractDeepDiveSummariesByCode(stockSelection);
  const deepSummary = deepByCode.get(candidate.code);
  const screening = screeningByCode.get(candidate.code);
  const deepAction = String(deepSummary?.action_bias || '').trim();
  if (deepAction) {
    const display = displayCandidateAction(deepAction);
    const strength = String(deepSummary?.action_strength || '');
    return {
      key: deepAction,
      label: display.label,
      tone: display.tone,
      strength: actionStrengthLabel(strength),
      reason: String(deepSummary?.key_reason || toStringList(deepSummary?.main_supporting_evidence)[0] || '动作来自单股深度取证结果。'),
    };
  }
  if (rejectedTargets.has(candidate.code) || String(screening?.screening_result || '') === 'reject') {
    return { key: 'reject', label: '暂不纳入', tone: 'danger', reason: String(screening?.primary_reason || '初筛阶段已淘汰。') };
  }
  if (deepTargets.has(candidate.code) || String(screening?.screening_result || '') === 'deep_dive') {
    return { key: 'deep_dive', label: '进入深挖', tone: 'success', reason: String(screening?.primary_reason || '初筛认为值得进入单股深度取证。') };
  }
  if (monitorTargets.has(candidate.code) || String(screening?.screening_result || '') === 'monitor') {
    return { key: 'monitor', label: '观察跟踪', tone: 'info', reason: String(screening?.primary_reason || '初筛建议继续观察。') };
  }
  return inferCandidateActionFromScore(candidate);
};

const candidateEvidenceTone = (dimension: string): CandidateDecisionTone => {
  if (dimension === 'strategy' || dimension === 'technical' || dimension === 'fundamental') return 'info';
  if (dimension === 'capital' || dimension === 'sentiment' || dimension === 'message') return 'warning';
  if (dimension === 'fallback') return 'warning';
  return 'default';
};

const candidateMetricHighlights = (candidate: DisplayCandidate): string[] => (
  [...fundamentalMetricBadges(candidate), ...technicalMetricBadges(candidate)].slice(0, 10)
);

const candidateDecisionRank = (actionKey: string): number => {
  if (['buy', 'open', 'add', 'conditional_buy', 'deep_dive'].includes(actionKey)) return 0;
  if (['monitor', 'wait'].includes(actionKey)) return 1;
  if (['hold', 'reduce'].includes(actionKey)) return 2;
  if (['reject', 'avoid'].includes(actionKey)) return 4;
  return 3;
};

const finiteNumber = (value: unknown): number | undefined => {
  const parsed = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
};

const toTraceCandidateDecisionRows = (
  candidates: DisplayCandidate[],
  stockSelection: Record<string, unknown> | null,
): CandidateDecisionRow[] => {
  const screeningByCode = extractScreeningByCode(stockSelection);
  const deepByCode = extractDeepDiveSummariesByCode(stockSelection);
  return candidates
    .map((candidate) => {
      const action = suggestTraceCandidateAction(candidate, stockSelection);
      const screening = screeningByCode.get(candidate.code);
      const hasDeepSummary = deepByCode.has(candidate.code);
      const screeningScore = finiteNumber(screening?.score);
      const decisionScore = screeningScore;
      const sources = [candidate.source, ...candidate.recallSources].filter((source, index, arr) => source && arr.indexOf(source) === index);
      const screeningReason = String(screening?.primary_reason || '').trim();
      const reason = hasDeepSummary
        ? (action.reason || screeningReason || candidate.reasonDimensions[0]?.detail || candidate.reason || '')
        : (screeningReason || candidate.reasonDimensions[0]?.detail || candidate.reason || action.reason || '');
      const scoreNote = screeningScore != null
        ? 'candidate_screening 阶段的取证分；seed pool 召回分只保留为来源诊断。'
        : undefined;
      const evidence = [
        ...(screening ? [{
          label: `初筛：${displayCandidateAction(String(screening.screening_result || action.key)).label}`,
          detail: displayReasonText(screeningReason || action.reason || ''),
          tone: action.tone,
        }] : []),
        ...candidate.reasonDimensions.map((entry) => ({
          label: entry.label || DIMENSION_GROUP_LABELS[entry.dimension] || entry.dimension,
          detail: displayReasonText(entry.detail),
          tone: candidateEvidenceTone(entry.dimension),
        })),
      ];
      return {
        id: candidate.code,
        code: candidate.code,
        name: candidate.name,
        score: decisionScore,
        scoreLabel: screeningScore != null ? '初筛分' : undefined,
        scoreNote,
        secondaryScores: [],
        action,
        primaryReason: displayReasonText(reason),
        dimensionLabels: candidate.reasonDimensions.map((item) => item.label || DIMENSION_GROUP_LABELS[item.dimension] || item.dimension),
        expertLabels: candidate.candidateExperts.map(displayCandidateExpertName),
        sourceLabels: sources.map(displaySourceName),
        lifecycleLabel: displayLifecycleStatus(candidate.lifecycleStatus),
        dateLabel: candidate.latestDate,
        badges: [
          ...(candidate.primaryDesk ? [{ label: deskLabel(candidate.primaryDesk), tone: 'history' as CandidateDecisionTone }] : []),
          ...(candidate.setupType ? [{ label: setupTypeLabel(candidate.setupType), tone: 'info' as CandidateDecisionTone }] : []),
          ...(candidate.stance && candidate.stance !== 'support' ? [{ label: `席位:${stanceLabel(candidate.stance)}`, tone: 'default' as CandidateDecisionTone }] : []),
          ...(candidate.multiDeskConviction ? [{ label: '多席共振', tone: 'history' as CandidateDecisionTone }] : []),
          ...candidate.conflictFlags.map((flag) => ({ label: `冲突:${flag}`, tone: 'warning' as CandidateDecisionTone })),
          ...(candidate.candidateExperts.length > 1 || candidate.recallSources.length > 1
            ? [{ label: '多专家候选共振', tone: 'history' as CandidateDecisionTone }]
            : []),
          ...(candidate.consensusBonus != null && candidate.consensusBonus > 0 ? [{ label: `共振 +${formatMetricValue(candidate.consensusBonus)}`, tone: 'success' as CandidateDecisionTone }] : []),
          ...(candidate.mixedEvidence ? [{ label: '存在反证', tone: 'warning' as CandidateDecisionTone }] : []),
        ],
        evidence,
        metricHighlights: candidateMetricHighlights(candidate),
        riskFlags: [
          ...(candidate.mixedEvidence ? ['存在反证，需要后续取证确认'] : []),
          ...candidate.reasonDimensions
            .filter((entry) => entry.detail.includes('超买') || entry.detail.includes('追高') || entry.dimension === 'fallback')
            .map((entry) => displayReasonText(entry.detail)),
        ],
        detailNote: '候选池阶段只说明入池价值，最终买卖动作以后续深挖、组合配置和风控为准。',
      };
    })
    .sort((a, b) => {
      const actionDiff = candidateDecisionRank(a.action.key) - candidateDecisionRank(b.action.key);
      if (actionDiff !== 0) return actionDiff;
      return String(a.code).localeCompare(String(b.code));
    });
};

const extractEventImpactWatches = (result: AgentTraceRunResponse): EventImpactWatch[] => {
  const seen = new Set<string>();
  const events: EventImpactWatch[] = [];
  extractDiscoverySteps(result)
    .filter((step) => String(step.source || '') === 'event_impact')
    .flatMap((step) => toRecordList(step.events))
    .forEach((event) => {
      const title = String(event.title || '');
      const eventId = String(event.event_id || title);
      const key = eventId || title;
      if (!key || seen.has(key)) return;
      seen.add(key);
      events.push({
        eventId,
        title,
        snippet: String(event.snippet || ''),
        eventType: String(event.event_type || ''),
        maturity: String(event.maturity || 'breaking'),
        impactVariables: toStringList(event.impact_variables),
        watchThemes: toStringList(event.watch_themes),
        validationWindowDays: typeof event.validation_window_days === 'number' ? event.validation_window_days : undefined,
        source: String(event.source || ''),
        url: String(event.url || ''),
        publishedDate: String(event.published_date || ''),
        validationMatches: toRecordList(event.validation_matches).map((match) => ({
          theme: String(match.theme || ''),
          status: String(match.status || ''),
          resultCount: toRecordList(match.results).length,
          titles: toRecordList(match.results).map((item) => String(item.title || '')).filter(Boolean).slice(0, 2),
        })),
      });
    });
  return events;
};

const extractExpertState = (stockSelection: Record<string, unknown> | null): Record<string, unknown> | null => {
  const normalized = normalizeStockSelectionPayload(stockSelection);
  const finalReport = asRecord(normalized?.final_report_json) || {};
  const selCtx = asRecord(normalized?.selection_context) || {};
  return asRecord(finalReport.expert_state) || asRecord(selCtx.expert_state);
};

const SELECTION_STAGE_LABELS: Record<string, string> = {
  selection_start: '选股流水线启动',
  selection_resume_loaded: '断点信息已加载',
  selection_resume_reused_stage: '历史阶段已复用',
  selection_candidate_discovery_done: '候选发现完成',
  selection_market_regime_done: '市场环境识别完成',
  selection_candidate_screening_done: '候选筛选完成',
  selection_deep_dive_done: '单股深度取证完成',
  selection_allocation_done: '组合配置完成',
  selection_adversarial_done: '反方审查完成',
  selection_judge_done: 'Judge 裁决完成',
  selection_expert_graph_done: '多专家图谱生成完成',
  selection_done: '选股流水线完成',
  selection_error: '选股流水线失败',
};

const getLatestSelectionStage = (result: AgentTraceRunResponse): { type: string; label: string } | null => {
  for (let index = result.events.length - 1; index >= 0; index -= 1) {
    const type = String(result.events[index]?.type || '');
    if (type.startsWith('selection_')) {
      return { type, label: SELECTION_STAGE_LABELS[type] || type };
    }
  }
  return null;
};

const getSelectionOrchestrationMode = (stockSelection: Record<string, unknown> | null): string => {
  const normalized = normalizeStockSelectionPayload(stockSelection);
  const finalReport = asRecord(normalized?.final_report_json) || {};
  const selCtx = asRecord(normalized?.selection_context) || {};
  return String(finalReport.orchestration_mode || selCtx.orchestration_mode || 'legacy');
};

const getRuntimeOrchestrationMode = (result: AgentTraceRunResponse): string => {
  const runtimeConfig = asRecord(result.runtime_config) || {};
  return String(runtimeConfig.agent_orchestration_mode || '');
};

const getTraceReportIntent = (result: AgentTraceRunResponse): string => {
  const contextReport = asRecord(asRecord(result.agent_user_context)?.report);
  const plannerIntent = asRecord(result.planner)?.intent;
  const intentResolution = asRecord(asRecord(result.context_summary)?.intent_resolution);
  return String(
    contextReport?.intent
    || intentResolution?.intent
    || plannerIntent
    || '',
  );
};

const getIntentResolution = (result: AgentTraceRunResponse): Record<string, unknown> | null => (
  asRecord(asRecord(result.context_summary)?.intent_resolution)
);

const hasCandidateDimension = (groups: CandidateDimensionGroup[], dimensions: string[]): boolean => (
  groups.some((group) => dimensions.includes(group.dimension) && group.candidates.length > 0)
);


/* ═══════════════════════════════════════════════
   Timeline Step UI Primitives
   ═══════════════════════════════════════════════ */

type StepStatus = 'pending' | 'active' | 'done' | 'error';

const StepIcon: React.FC<{ status: StepStatus }> = ({ status }) => {
  if (status === 'done') return <Check className="h-3.5 w-3.5 text-white" />;
  if (status === 'active') return <Loader2 className="h-3.5 w-3.5 animate-spin text-background" />;
  if (status === 'error') return <AlertTriangle className="h-3.5 w-3.5 text-white" />;
  return <Circle className="h-2.5 w-2.5 text-muted-text" />;
};

const StepNode: React.FC<{ status: StepStatus }> = ({ status }) => (
  <div className={cn(
    'relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2',
    status === 'done' && 'border-success bg-success',
    status === 'active' && 'border-foreground bg-foreground',
    status === 'error' && 'border-danger bg-danger',
    status === 'pending' && 'border-border bg-card',
  )}>
    <StepIcon status={status} />
  </div>
);

const TimelineStep: React.FC<{
  label: string;
  title: string;
  status: StepStatus;
  narrative?: string;
  isLast?: boolean;
  children?: React.ReactNode;
  defaultOpen?: boolean;
}> = ({ label, title, status, narrative, isLast = false, children, defaultOpen = false }) => {
  const [manualOpen, setManualOpen] = useState<boolean | null>(null);
  const open = manualOpen ?? defaultOpen;
  const hasContent = Boolean(children);

  return (
    <div className="relative flex gap-4">
      {/* Vertical line */}
      <div className="flex flex-col items-center">
        <StepNode status={status} />
        {!isLast && <div className="w-px flex-1 bg-border" />}
      </div>

      {/* Content */}
      <div className={cn('min-w-0 flex-1 pb-8', isLast && 'pb-0')}>
        <div className="flex items-baseline gap-2">
          <span className="text-label font-semibold uppercase tracking-wider text-muted-text">{label}</span>
          <h3 className="text-sm font-medium text-foreground">{title}</h3>
        </div>

        {narrative ? (
          <p className="mt-1.5 text-sm leading-relaxed text-secondary-text">{narrative}</p>
        ) : null}

        {hasContent ? (
          <>
            <button
              type="button"
              onClick={() => setManualOpen(!open)}
              className="mt-2 flex items-center gap-1 text-xs text-muted-text transition-colors hover:text-secondary-text"
            >
              <ChevronDown className={cn('h-3 w-3 transition-transform', !open && '-rotate-90')} />
              {open ? '收起详情' : '展开详情'}
            </button>
            {open ? <div className="mt-3">{children}</div> : null}
          </>
        ) : null}
      </div>
    </div>
  );
};

const inputClass = 'h-9 w-full rounded-xl border border-border bg-card px-3 text-sm text-foreground outline-none transition-all placeholder:text-muted-text focus:border-ring focus:ring-2 focus:ring-ring/20';
const selectClass = inputClass;
const textareaClass = 'min-h-20 w-full resize-y rounded-xl border border-border bg-card px-3 py-2 text-sm text-foreground outline-none transition-all placeholder:text-muted-text focus:border-ring focus:ring-2 focus:ring-ring/20';
const markdownProseClass = 'max-w-none text-sm leading-relaxed text-foreground [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:text-foreground [&_h2]:text-base [&_h2]:font-semibold [&_h2]:text-foreground [&_h3]:text-sm [&_h3]:font-semibold [&_h3]:text-foreground [&_h4]:text-sm [&_h4]:font-medium [&_h4]:text-foreground [&_strong]:font-semibold [&_strong]:text-foreground [&_a]:text-cyan [&_a]:underline [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-1 [&_p]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-4 [&_blockquote]:text-secondary-text [&_code]:rounded [&_code]:bg-surface-2 [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs [&_pre]:rounded-lg [&_pre]:bg-surface-2 [&_pre]:p-3 [&_table]:w-full [&_th]:border [&_th]:border-border [&_th]:bg-surface-2 [&_th]:px-3 [&_th]:py-1.5 [&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_td]:border [&_td]:border-border [&_td]:px-3 [&_td]:py-1.5 [&_td]:text-xs';

/* ═══════════════════════════════════════════════
   Main Page Component
   ═══════════════════════════════════════════════ */

const AgentTracePage: React.FC = () => {
  const navigate = useNavigate();
  const { sessionId: routeSessionId } = useParams<{ sessionId?: string }>();
  const [message, setMessage] = useState(DEFAULT_PROMPT);
  const [stockCode, setStockCode] = useState(DEFAULT_STOCK_CODE);
  const [accounts, setAccounts] = useState<PortfolioAccountItem[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [reportIntent, setReportIntent] = useState('auto');
  const [candidateDiscoveryMode, setCandidateDiscoveryMode] = useState<'thesis_desk_committee'>('thesis_desk_committee');
  const [riskPreference, setRiskPreference] = useState('balanced');
  const [tradingHorizon, setTradingHorizon] = useState('long_term');
  const [maxSinglePositionPct, setMaxSinglePositionPct] = useState('20');
  const [maxTotalEquityExposurePct, setMaxTotalEquityExposurePct] = useState('80');
  const [maxAcceptableDrawdownPct, setMaxAcceptableDrawdownPct] = useState('15');
  const [defaultStopLossPct, setDefaultStopLossPct] = useState('8');
  const [investorNotes, setInvestorNotes] = useState('偏长期持有，关注回撤控制和分批操作。');
  const [injectPortfolioContext, setInjectPortfolioContext] = useState(true);
  const [result, setResult] = useState<AgentTraceRunResponse | null>(null);
  const [selectedToolIndex, setSelectedToolIndex] = useState(0);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const [running, setRunning] = useState(false);
  const [traceStatus, setTraceStatus] = useState<TraceStatus>('idle');
  const [statusMessage, setStatusMessage] = useState('等待运行');
  const [historyItems, setHistoryItems] = useState<TraceHistoryItem[]>([]);
  const [showConfig, setShowConfig] = useState(false);
  const [runtimeConfig, setRuntimeConfig] = useState<Record<string, unknown> | null>(null);
  const activeRunSessionIdRef = useRef<string | null>(null);
  const runtimeConfigRef = useRef<Record<string, unknown> | null>(null);
  const activeRunMetaRef = useRef<{
    message: string;
    stockCode: string;
    accountId?: number;
  }>({ message: DEFAULT_PROMPT, stockCode: DEFAULT_STOCK_CODE });

  const hydrateTraceSessionFromBackend = useCallback((sessionId: string, fallback?: TraceHistoryItem) => {
    const placeholder = fallback?.result || createEmptyTraceResult();
    setResult({
      ...placeholder,
      session_id: sessionId,
      runtime_config: runtimeConfigRef.current || placeholder.runtime_config || runtimeConfig,
    });
    setTraceStatus('running');
    setStatusMessage(fallback ? '正在从后端加载完整 Trace 记录...' : '正在从后端加载 Trace 记录...');
    agentApi.getTraceSession(sessionId)
      .then((item) => {
        const next: TraceHistoryItem = {
          ...item,
          accountId: item.accountId ?? undefined,
          result: { ...item.result, runtime_config: runtimeConfigRef.current || item.result.runtime_config },
        };
        setResult(next.result);
        setSelectedToolIndex(0);
        setError(null);
        setTraceStatus(next.status === 'success' ? 'done' : 'error');
        setStatusMessage(next.status === 'success' ? '已从后端加载 Trace' : '已从后端加载失败记录');
        setMessage(next.message);
        setStockCode(next.stockCode);
        setSelectedAccountId(next.accountId ? String(next.accountId) : '');
        setHistoryItems((items) => persistTraceHistory(items, next));
      })
      .catch((err) => {
        if (fallback) {
          setResult({ ...fallback.result, runtime_config: runtimeConfigRef.current || fallback.result.runtime_config });
          setTraceStatus(fallback.status === 'success' ? 'done' : 'error');
          setStatusMessage('后端完整 Trace 加载失败，当前显示本地轻量历史摘要');
          setMessage(fallback.message);
          setStockCode(fallback.stockCode);
          setSelectedAccountId(fallback.accountId ? String(fallback.accountId) : '');
        } else {
          setError(getParsedApiError(err));
          setTraceStatus('error');
          setStatusMessage('未找到该 Trace 记录');
        }
      });
  }, [runtimeConfig]);

  useEffect(() => {
    document.title = 'Agent Trace';
    setHistoryItems(loadTraceHistory());
  }, []);

  useEffect(() => {
    const sessionId = normalizeTraceSessionId(routeSessionId);
    if (!sessionId) return;
    if (activeRunSessionIdRef.current === sessionId) return;
    const historyItem = loadTraceHistory().find((item) => item.id === sessionId || item.result.session_id === sessionId);
    if (!historyItem || shouldLoadFullTraceFromBackend(historyItem)) {
      hydrateTraceSessionFromBackend(sessionId, historyItem);
      return;
    }
    setResult({ ...historyItem.result, runtime_config: runtimeConfigRef.current || historyItem.result.runtime_config });
    setSelectedToolIndex(0);
    setError(null);
    setTraceStatus(historyItem.status === 'success' ? 'done' : 'error');
    setStatusMessage(historyItem.status === 'success' ? '已加载历史' : '已加载失败记录');
    setMessage(historyItem.message);
    setStockCode(historyItem.stockCode);
    setSelectedAccountId(historyItem.accountId ? String(historyItem.accountId) : '');
  }, [routeSessionId, hydrateTraceSessionFromBackend]);

  useEffect(() => {
    let alive = true;
    portfolioApi.getAccounts()
      .then((response) => { if (alive) { setAccounts(response.accounts); if (response.accounts.length === 1) setSelectedAccountId(String(response.accounts[0].id)); } })
      .catch(() => { if (alive) setAccounts([]); });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    let alive = true;
    agentApi.getRuntimeConfig()
      .then((response) => {
        if (alive) {
          const next = asRecord(response.runtime_config);
          runtimeConfigRef.current = next;
          setRuntimeConfig(next);
        }
      })
      .catch(() => {
        if (alive) {
          runtimeConfigRef.current = null;
          setRuntimeConfig(null);
        }
      });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    if (!runtimeConfig) return;
    setResult((prev) => (prev ? { ...prev, runtime_config: runtimeConfig } : prev));
  }, [runtimeConfig, result?.session_id]);

  const selectedTool = result?.tool_calls[selectedToolIndex] ?? null;

  const handleRun = async (options?: {
    resumeFromSessionId?: string;
    messageOverride?: string;
    stockCodeOverride?: string;
    accountIdOverride?: number | null;
  }) => {
    const sessionId = createTraceSessionId();
    activeRunSessionIdRef.current = sessionId;
    const runMessage = options?.messageOverride ?? message;
    const runStockCode = options?.stockCodeOverride ?? stockCode;
    const selectedAccountNumber = options?.accountIdOverride !== undefined
      ? (options.accountIdOverride ?? undefined)
      : (selectedAccountId ? Number(selectedAccountId) : undefined);
    const stockCodeToSend = shouldSendStockCode(runMessage, runStockCode) ? runStockCode.trim() : undefined;
    activeRunMetaRef.current = { message: runMessage, stockCode: runStockCode, accountId: selectedAccountNumber };
    const shouldInjectPortfolioContext = injectPortfolioContext || selectedAccountNumber != null;
    navigate(`/agent-trace/${encodeURIComponent(sessionId)}`, { replace: false });
    setRunning(true);
    setTraceStatus('running');
    setStatusMessage('正在准备上下文...');
    setError(null);
    setSelectedToolIndex(0);
    setResult({ ...createEmptyTraceResult(), session_id: sessionId, runtime_config: runtimeConfig });
    try {
      const response = await agentApi.traceStream({
        session_id: sessionId,
        message: runMessage,
        account_id: selectedAccountNumber,
        stock_code: stockCodeToSend,
        inject_portfolio_context: shouldInjectPortfolioContext,
        analysis_mode: 'planning_execute',
        report_intent: reportIntent === 'auto' ? undefined : reportIntent,
        risk_preference: riskPreference,
        trading_horizon: tradingHorizon,
        max_single_position_pct: parseOptionalPercent(maxSinglePositionPct),
        max_total_equity_exposure_pct: parseOptionalPercent(maxTotalEquityExposurePct),
        max_acceptable_drawdown_pct: parseOptionalPercent(maxAcceptableDrawdownPct),
        default_stop_loss_pct: parseOptionalPercent(defaultStopLossPct),
        investor_notes: investorNotes.trim() || undefined,
        candidate_discovery_mode: candidateDiscoveryMode,
        resume_from_session_id: options?.resumeFromSessionId,
      });
      await consumeTraceStream(response);
    } catch (err) {
      setError(getParsedApiError(err));
      setTraceStatus('error');
      setStatusMessage(err instanceof Error ? err.message : 'Trace 运行失败');
    } finally {
      activeRunSessionIdRef.current = null;
      setRunning(false);
    }
  };

  const handleResumeCurrentTrace = async () => {
    const sourceSessionId = result?.session_id || normalizeTraceSessionId(routeSessionId);
    if (!sourceSessionId || running) return;
    await handleRun({ resumeFromSessionId: sourceSessionId });
  };

  const handleResumeHistory = async (item: TraceHistoryItem) => {
    const sourceSessionId = item.result.session_id || item.id;
    if (!sourceSessionId || running) return;
    await handleRun({
      resumeFromSessionId: sourceSessionId,
      messageOverride: item.message,
      stockCodeOverride: item.stockCode,
      accountIdOverride: item.accountId ?? null,
    });
  };

  const handleSelectHistory = (item: TraceHistoryItem) => {
    const sessionId = item.result.session_id || item.id;
    if (sessionId) {
      navigate(`/agent-trace/${encodeURIComponent(sessionId)}`, { replace: false });
    }
    if (shouldLoadFullTraceFromBackend(item) && sessionId) {
      hydrateTraceSessionFromBackend(sessionId, item);
      return;
    }
    setResult({ ...item.result, runtime_config: runtimeConfigRef.current || runtimeConfig || item.result.runtime_config });
    setSelectedToolIndex(0);
    setError(null);
    setTraceStatus(item.status === 'success' ? 'done' : 'error');
    setStatusMessage(item.status === 'success' ? '已加载历史' : '已加载失败记录');
    setMessage(item.message);
    setStockCode(item.stockCode);
    setSelectedAccountId(item.accountId ? String(item.accountId) : '');
    const investor = (item.result.context_summary?.investor || {}) as Record<string, unknown>;
    const plannerIntent = item.result.planner?.intent;
    setReportIntent(typeof plannerIntent === 'string' ? plannerIntent : 'auto');
    setRiskPreference(typeof investor.risk_preference === 'string' ? investor.risk_preference : 'balanced');
    setTradingHorizon(typeof investor.trading_horizon === 'string' ? investor.trading_horizon : 'long_term');
    setMaxSinglePositionPct(formatPercentInput(investor.max_single_position_pct));
    setMaxTotalEquityExposurePct(formatPercentInput(investor.max_total_equity_exposure_pct));
    setMaxAcceptableDrawdownPct(formatPercentInput(investor.max_acceptable_drawdown_pct));
    setDefaultStopLossPct(formatPercentInput(investor.default_stop_loss_pct));
  };

  const consumeTraceStream = async (response: Response) => {
    const reader = response.body?.getReader();
    if (!reader) throw new Error('No response body');
    const decoder = new TextDecoder();
    let buffer = '';
    const processLine = (line: string) => {
      if (!line.startsWith('data: ')) return;
      const payload = line.slice(6).trim();
      if (!payload) return;
      const event = JSON.parse(payload) as TraceStreamEvent;
      applyTraceEvent(event);
    };
    while (true) {
      const { value, done } = await reader.read();
      if (value) {
        buffer += decoder.decode(value, { stream: !done });
        let idx = buffer.indexOf('\n');
        while (idx >= 0) {
          processLine(buffer.slice(0, idx).trimEnd());
          buffer = buffer.slice(idx + 1);
          idx = buffer.indexOf('\n');
        }
      }
      if (done) break;
    }
    if (buffer.trim()) processLine(buffer.trim());
  };

  const applyTraceEvent = (event: TraceStreamEvent) => {
    const type = event.type || 'unknown';
    if (type === 'context_ready') {
      setStatusMessage('账户与持仓上下文已就绪');
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), session_id: String(event.session_id || prev?.session_id || ''), context_summary: asRecord(event.context_summary), agent_user_context: asRecord(event.agent_user_context), runtime_config: asRecord(event.runtime_config) || prev?.runtime_config || runtimeConfig }));
      return;
    }
    if (type === 'planner_ready') {
      setStatusMessage('执行计划已生成');
      setResult((prev) => ({
        ...(prev || createEmptyTraceResult()),
        session_id: String(event.session_id || prev?.session_id || ''),
        planner: asRecord(event.planner),
        context_summary: asRecord(event.context_summary) || prev?.context_summary || null,
        runtime_config: asRecord(event.runtime_config) || prev?.runtime_config || runtimeConfig,
      }));
      return;
    }
    if (type === 'tool_start') {
      setStatusMessage(`正在调用 ${event.display_name || event.tool || '工具'}...`);
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), events: [...(prev?.events || []), eventToTraceEvent(event)] }));
      return;
    }
    if (type === 'tool_done') {
      setStatusMessage(`${event.display_name || event.tool || '工具'} 完成`);
      setResult((prev) => {
        const cur = prev || createEmptyTraceResult();
        return { ...cur, events: [...cur.events, eventToTraceEvent(event)], tool_calls: [...cur.tool_calls, eventToToolCall(event)] };
      });
      return;
    }
    if (type === 'thinking' || type === 'generating') {
      setStatusMessage(event.message || (type === 'generating' ? '生成最终输出...' : '分析中...'));
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), events: [...(prev?.events || []), eventToTraceEvent(event)] }));
      return;
    }
    if (type === 'heartbeat') {
      setStatusMessage('分析仍在运行，等待工具或模型返回...');
      return;
    }
    if (type.startsWith('debate_')) {
      setStatusMessage(event.message || 'Debate 阶段更新');
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), events: [...(prev?.events || []), eventToTraceEvent(event)] }));
      return;
    }
    if (type.startsWith('selection_')) {
      setStatusMessage(type === 'selection_expert_graph_done' ? '多专家图谱已生成' : (event.message || '选股阶段更新'));
      setResult((prev) => {
        const cur = prev || createEmptyTraceResult();
        return {
          ...cur,
          events: [...cur.events, eventToTraceEvent(event)],
          stock_selection: mergeSelectionProgress(asRecord(cur.stock_selection), event) || cur.stock_selection,
        };
      });
      return;
    }
    if (type === 'done') {
      setTraceStatus(event.success ? 'done' : 'error');
      setStatusMessage(event.success ? '分析完成' : String(event.error || '失败'));
      setError(event.success ? null : getParsedApiError(String(event.error || '失败')));
      setResult((prev) => {
        const cur = prev || createEmptyTraceResult();
        const next = {
          ...cur, success: Boolean(event.success),
          session_id: String(event.session_id || cur.session_id || ''),
          content: String(event.content || ''), error: typeof event.error === 'string' ? event.error : null,
          total_steps: typeof event.total_steps === 'number' ? event.total_steps : 0,
          total_tokens: typeof event.total_tokens === 'number' ? event.total_tokens : 0,
          provider: String(event.provider || ''), model: String(event.model || ''),
          mode: String(event.mode || 'planning_execute'),
          tool_calls: Array.isArray(event.tool_calls) ? event.tool_calls as AgentTraceToolCall[] : cur.tool_calls,
          planner: asRecord(event.planner) || cur.planner,
          agent_user_context: asRecord(event.agent_user_context) || cur.agent_user_context,
          context_summary: asRecord(event.context_summary) || cur.context_summary,
          debate: asRecord(event.debate) || cur.debate,
          stock_selection: mergeStockSelectionResult(asRecord(cur.stock_selection), asRecord(event.stock_selection)) || cur.stock_selection,
          risk_gate: asRecord(event.risk_gate) || cur.risk_gate,
          llm_telemetry: asRecord(event.llm_telemetry) || cur.llm_telemetry || null,
          judge_sanity: asRecord(event.judge_sanity) || cur.judge_sanity || null,
          artifact_dir: typeof event.artifact_dir === 'string' ? event.artifact_dir : cur.artifact_dir,
          runtime_config: asRecord(event.runtime_config) || cur.runtime_config || runtimeConfig,
        };
        setHistoryItems((items) => persistTraceHistory(items, {
          id: next.session_id || `${Date.now()}`, createdAt: new Date().toISOString(),
          message: activeRunMetaRef.current.message,
          stockCode: shouldSendStockCode(activeRunMetaRef.current.message, activeRunMetaRef.current.stockCode)
            ? activeRunMetaRef.current.stockCode.trim()
            : '',
          accountId: activeRunMetaRef.current.accountId,
          status: next.success ? 'success' : 'error', result: next,
        }));
        return next;
      });
      return;
    }
    if (type === 'error') {
      setTraceStatus('error');
      setStatusMessage(event.message || 'Trace 运行失败');
      setResult((prev) => {
        const cur = prev || createEmptyTraceResult();
        const next = { ...cur, success: false, error: event.message || 'Trace 运行失败', events: [...cur.events, eventToTraceEvent(event)] };
        setHistoryItems((items) => persistTraceHistory(items, {
          id: next.session_id || `${Date.now()}`, createdAt: new Date().toISOString(),
          message: activeRunMetaRef.current.message,
          stockCode: shouldSendStockCode(activeRunMetaRef.current.message, activeRunMetaRef.current.stockCode)
            ? activeRunMetaRef.current.stockCode.trim()
            : '',
          accountId: activeRunMetaRef.current.accountId,
          status: 'error',
          result: next,
        }));
        return next;
      });
    }
  };

  /* ─── Derived data for timeline ─── */
  const planner = useMemo(() => asRecord(result?.planner), [result?.planner]);
  const debate = useMemo(() => asRecord(result?.debate), [result?.debate]);
  const riskPayload = useMemo(() => asRecord(result?.risk_gate), [result?.risk_gate]);
  const llmTelemetry = useMemo(() => asRecord(result?.llm_telemetry), [result?.llm_telemetry]);
  const judgeSanity = useMemo(() => asRecord(result?.judge_sanity), [result?.judge_sanity]);
  const stockSelection = useMemo(() => normalizeStockSelectionPayload(asRecord(result?.stock_selection)), [result?.stock_selection]);
  const failedToolCount = useMemo(() => result?.tool_calls.filter((t) => !t.success).length ?? 0, [result]);
  const hasDiscoveryCandidates = useMemo(
    () => Boolean(result && extractDiscoveryCandidates(result, stockSelection).length),
    [result, stockSelection],
  );

  const getLayerStatus = (hasData: boolean): StepStatus => {
    if (hasData) return 'done';
    if (traceStatus === 'running') return 'active';
    if (traceStatus === 'error') return 'error';
    return 'pending';
  };

  /* ─── Render ─── */
  return (
    <div className="min-h-screen bg-surface-2">
      <div className="mx-auto max-w-[1100px] px-6 py-10">
        {/* Header */}
        <header className="mb-8">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div>
              <h1 className="text-lg font-semibold text-foreground">Agent Trace</h1>
              <p className="mt-1 text-sm text-muted-text">从用户问题出发，观察 Agent 如何逐层取证、辩论和裁决。</p>
            </div>
            <button
              type="button"
              onClick={() => {
                const rawSeedDate = result ? extractSeedPoolDisplay(result, stockSelection)?.seedDate : undefined;
                const seedDate = normalizeSeedDateForUrl(rawSeedDate);
                navigate(seedDate ? `/seed-pool-quality?seed_date=${encodeURIComponent(seedDate)}` : '/seed-pool-quality');
              }}
              className="inline-flex h-9 items-center gap-2 border border-border bg-card px-3 text-sm text-secondary-text transition-colors hover:bg-hover hover:text-foreground"
            >
              <ChartCandlestick className="h-4 w-4" />
              种子池质量
            </button>
          </div>
        </header>

        {/* Input area */}
        <div className="mb-8 rounded-xl border border-border bg-card p-5 shadow-sm">
          <div className="flex gap-3">
            <textarea
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              className={textareaClass + ' flex-1'}
              placeholder="输入你的问题..."
            />
            <div className="flex flex-col gap-2">
              <Button onClick={() => void handleRun()} isLoading={running} loadingText="运行中" size="sm">
                <Play className="h-3.5 w-3.5" />
                运行
              </Button>
              {result?.session_id && traceStatus !== 'running' ? (
                <Button
                  type="button"
                  onClick={() => void handleResumeCurrentTrace()}
                  isLoading={running}
                  loadingText="继续中"
                  size="sm"
                  variant="secondary"
                >
                  <RotateCcw className="h-3.5 w-3.5" />
                  继续此 Trace
                </Button>
              ) : null}
              <button
                type="button"
                onClick={() => setShowConfig(!showConfig)}
                className="text-label text-muted-text hover:text-secondary-text"
              >
                {showConfig ? '收起配置' : '展开配置'}
              </button>
            </div>
          </div>

          {showConfig ? (
            <div className="mt-4 border-t border-border pt-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">股票代码</span>
                  <input value={stockCode} onChange={(e) => setStockCode(e.target.value)} className={inputClass} placeholder="可选，输入后才会随请求发送" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">账户</span>
                  <select value={selectedAccountId} onChange={(e) => setSelectedAccountId(e.target.value)} className={selectClass}>
                    <option value="">全部</option>
                    {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">报告意图</span>
                  <select value={reportIntent} onChange={(e) => setReportIntent(e.target.value)} className={selectClass}>
                    {REPORT_INTENT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block" title={CANDIDATE_DISCOVERY_OPTIONS.find((o) => o.value === candidateDiscoveryMode)?.desc || ''}>
                  <span className="mb-1 block text-label text-muted-text">候选发现</span>
                  <select
                    aria-label="候选发现模式"
                    value={candidateDiscoveryMode}
                    onChange={(e) => {
                      const next = e.target.value === 'thesis_desk_committee' ? 'thesis_desk_committee' : 'thesis_desk_committee';
                      setCandidateDiscoveryMode(next);
                      if (typeof window !== 'undefined') {
                        window.localStorage.setItem(CANDIDATE_DISCOVERY_STORAGE_KEY, next);
                      }
                    }}
                    className={selectClass}
                  >
                    {CANDIDATE_DISCOVERY_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">风险偏好</span>
                  <select value={riskPreference} onChange={(e) => setRiskPreference(e.target.value)} className={selectClass}>
                    {RISK_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">持有周期</span>
                  <select value={tradingHorizon} onChange={(e) => setTradingHorizon(e.target.value)} className={selectClass}>
                    {HORIZON_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">单票上限%</span>
                  <input value={maxSinglePositionPct} onChange={(e) => setMaxSinglePositionPct(e.target.value)} className={inputClass} placeholder="20" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">总权益上限%</span>
                  <input value={maxTotalEquityExposurePct} onChange={(e) => setMaxTotalEquityExposurePct(e.target.value)} className={inputClass} placeholder="80" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">最大回撤%</span>
                  <input value={maxAcceptableDrawdownPct} onChange={(e) => setMaxAcceptableDrawdownPct(e.target.value)} className={inputClass} placeholder="15" />
                </label>
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <label className="block">
                  <span className="mb-1 block text-label text-muted-text">默认止损%</span>
                  <input value={defaultStopLossPct} onChange={(e) => setDefaultStopLossPct(e.target.value)} className={inputClass} placeholder="8" />
                </label>
                <label className="col-span-2 block">
                  <span className="mb-1 block text-label text-muted-text">画像备注</span>
                  <input value={investorNotes} onChange={(e) => setInvestorNotes(e.target.value)} className={inputClass} placeholder="偏长期持有" />
                </label>
                <label className="flex items-end gap-2 pb-1">
                  <input type="checkbox" checked={injectPortfolioContext} onChange={(e) => setInjectPortfolioContext(e.target.checked)} className="h-4 w-4 rounded border-border" />
                  <span className="text-xs text-muted-text">注入持仓</span>
                </label>
              </div>
            </div>
          ) : null}

          {stockCode.trim() && !shouldSendStockCode(message, stockCode) ? (
            <p className="mt-2 text-xs text-warning">默认股票代码未在问题中出现，本次不发送该代码；意图由后端模型识别。</p>
          ) : null}
        </div>

        {error ? <div className="mb-6"><ApiErrorAlert error={error} /></div> : null}

        {/* History pills */}
        {historyItems.length ? (
          <div className="mb-8">
            <div className="mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-xs text-muted-text"><History className="h-3 w-3" /> 历史</span>
              <button type="button" onClick={() => { saveTraceHistory([]); setHistoryItems([]); }} className="flex items-center gap-1 text-label text-muted-text hover:text-danger">
                <Trash2 className="h-3 w-3" /> 清空
              </button>
            </div>
            <div className="flex max-h-64 flex-col gap-2 overflow-y-auto pr-1">
              {historyItems.map((item) => (
                <div
                  key={`${item.id}-${item.createdAt}`}
                  className="flex w-full items-start gap-2 rounded-lg border border-border bg-card px-3 py-2 text-xs transition-all hover:border-border hover:shadow-sm"
                >
                  <button
                    type="button"
                    onClick={() => handleSelectHistory(item)}
                    className="flex min-w-0 flex-1 items-start gap-2 text-left"
                  >
                    <span className={cn('mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full', item.status === 'success' ? 'bg-success' : 'bg-danger')} />
                    <span className="min-w-0 flex-1">
                      <span className="block font-mono text-foreground">{item.stockCode || '选股'}</span>
                      <span className="mt-0.5 line-clamp-2 block break-words leading-5 text-muted-text">{item.message}</span>
                    </span>
                  </button>
                  <button
                    type="button"
                    disabled={running}
                    onClick={() => void handleResumeHistory(item)}
                    className="inline-flex h-7 shrink-0 items-center gap-1 border border-border px-2 text-label text-secondary-text transition-colors hover:bg-hover hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                    title={`从 ${item.result.session_id || item.id} 继续运行`}
                  >
                    <RotateCcw className="h-3 w-3" />
                    继续
                  </button>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {/* Status */}
        {traceStatus !== 'idle' ? (
          <div className="mb-6 flex items-center gap-2 text-sm text-muted-text">
            {traceStatus === 'running' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            {traceStatus === 'done' ? <Check className="h-3.5 w-3.5 text-success" /> : null}
            {traceStatus === 'error' ? <AlertTriangle className="h-3.5 w-3.5 text-danger" /> : null}
            <span>{statusMessage}</span>
            {result?.session_id ? <span className="ml-auto font-mono text-label text-muted-text">{result.session_id}</span> : null}
          </div>
        ) : null}

        {/* Timeline */}
        {result ? (
          <div className="rounded-xl border border-border bg-card px-6 py-8 shadow-sm">
            <TimelineStep
              label="L1"
              title="数据与候选池"
              status={getLayerStatus(Boolean(result.tool_calls.length || stockSelection))}
              narrative={buildL1Narrative(result, stockSelection)}
              defaultOpen={Boolean(stockSelection || hasDiscoveryCandidates || getIntentResolution(result))}
            >
              <L1Detail result={result} stockSelection={stockSelection} traceStatus={traceStatus} />
            </TimelineStep>

            <TimelineStep
              label="L2"
              title="证据取证"
              status={getLayerStatus(result.tool_calls.length > 0)}
              narrative={`已完成 ${result.tool_calls.length} 次工具调用${failedToolCount ? `，其中 ${failedToolCount} 次失败` : ''}。`}
              defaultOpen={true}
            >
              <L2Detail toolCalls={result.tool_calls} selectedIndex={selectedToolIndex} onSelect={setSelectedToolIndex} selectedTool={selectedTool} />
            </TimelineStep>

            <TimelineStep
              label="L3"
              title="信号层"
              status={getLayerStatus(Boolean(debate || result.content))}
              narrative="将证据压缩为方向、置信度和冲突点，形成结构化信号。"
            >
              <L3Detail result={result} debate={debate} />
            </TimelineStep>

            <TimelineStep
              label="L4"
              title="决策层"
              status={getLayerStatus(Boolean(debate?.judge_decision || planner))}
              narrative={buildL4Narrative(debate, planner)}
              defaultOpen={Boolean(debate?.judge_decision)}
            >
              <L4Detail debate={debate} planner={planner} />
            </TimelineStep>

            <TimelineStep
              label="L5"
              title="风控闸门"
              status={getLayerStatus(Boolean(riskPayload))}
              narrative={buildL5Narrative(riskPayload)}
            >
              <L5Detail riskPayload={riskPayload} />
            </TimelineStep>

            <TimelineStep
              label="L6"
              title="方案层"
              status={getLayerStatus(Boolean(asRecord(riskPayload?.trade_plan)))}
              narrative={buildL6Narrative(riskPayload)}
            >
              {asRecord(riskPayload?.trade_plan) ? (
                <JsonViewer data={asRecord(riskPayload?.trade_plan) as Record<string, unknown>} maxHeight="300px" />
              ) : null}
            </TimelineStep>

            <TimelineStep
              label="L7"
              title="托管跟踪"
              status="pending"
              narrative="模拟盘托管尚未接入，后续将展示方案执行状态和偏离原因。"
            />

            <TimelineStep
              label="L8"
              title="可观测性"
              status={getLayerStatus(Boolean(llmTelemetry || judgeSanity))}
              narrative={buildObservabilityNarrative(llmTelemetry, judgeSanity)}
              defaultOpen={Boolean(llmTelemetry || judgeSanity)}
            >
              <ObservabilityDetail llmTelemetry={llmTelemetry} judgeSanity={judgeSanity} />
            </TimelineStep>

            <TimelineStep
              label="L9"
              title="复盘进化"
              status={getLayerStatus(Boolean(result.artifact_dir))}
              narrative={result.artifact_dir ? `Trace 已落盘：${result.artifact_dir}` : '等待 Trace 完成后落盘。'}
              isLast={true}
            />

            {/* Final Output */}
            {result.content ? (
              <div className="mt-8 border-t border-border pt-6">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
                  <h3 className="text-sm font-medium text-foreground">最终报告</h3>
                  <Button
                    type="button"
                    variant="secondary"
                    size="sm"
                    className="border-border bg-card text-foreground hover:bg-surface-2"
                    onClick={() => downloadMarkdownReport(result)}
                    title="导出 Markdown 文件"
                    aria-label="导出 MD"
                  >
                    <Download className="h-4 w-4" aria-hidden="true" />
                    导出 MD
                  </Button>
                </div>
                {result.error ? (
                  <div className="mb-3 rounded-lg bg-danger/10 p-3 text-sm text-danger">{result.error}</div>
                ) : null}
                <div className="max-h-[500px] overflow-auto rounded-lg border border-border bg-card p-5">
                  <div className={markdownProseClass}>
                    <Markdown remarkPlugins={[remarkGfm]}>{splitReportContent(result.content).main}</Markdown>
                  </div>
                </div>
                {splitReportContent(result.content).appendix ? (
                  <Collapsible title="附录：候选池来源、逐股维度证据与风控条件" defaultOpen={false}>
                    <div className={markdownProseClass}>
                      <Markdown remarkPlugins={[remarkGfm]}>{splitReportContent(result.content).appendix}</Markdown>
                    </div>
                  </Collapsible>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed border-border bg-card px-6 py-16 text-center">
            <p className="text-sm text-muted-text">输入问题并点击「运行」，观察 Agent 的完整推理链路。</p>
          </div>
        )}
      </div>
    </div>
  );
};


/* ═══════════════════════════════════════════════
   Layer Detail Components
   ═══════════════════════════════════════════════ */

const L1Detail: React.FC<{
  result: AgentTraceRunResponse;
  stockSelection: Record<string, unknown> | null;
  traceStatus: TraceStatus;
}> = ({ result, stockSelection, traceStatus }) => {
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  const stages = asRecord(selCtx.stages) || {};
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  const screeningSummary = asRecord(asRecord(finalReport.candidate_screening)?.summary) || asRecord(asRecord(stages.candidate_screening)?.summary) || {};
  const deepTargets = toStringList(screeningSummary.deep_dive_targets);
  const candidates = extractDiscoveryCandidates(result, stockSelection);
  const orchestrationMode = getSelectionOrchestrationMode(stockSelection);
  const runtimeOrchestrationMode = getRuntimeOrchestrationMode(result);
  const reportIntent = getTraceReportIntent(result);
  const isWatchlistScan = reportIntent === 'watchlist_scan' || Boolean(stockSelection);
  const intentResolution = getIntentResolution(result);
  const classifierConfigured = intentResolution?.classifier_configured === true;
  const classifierSuccess = intentResolution?.classifier_success === true;
  const classifierError = typeof intentResolution?.classifier_error === 'string' ? intentResolution.classifier_error : '';
  const classifierModel = typeof intentResolution?.classifier_model === 'string' ? intentResolution.classifier_model : '';
  const displayOrchestrationMode = runtimeOrchestrationMode || orchestrationMode;
  const latestSelectionStage = getLatestSelectionStage(result);
  const dimensionGroups = extractExpertGroupedCandidates(result);
  const hasSentimentCandidates = hasCandidateDimension(dimensionGroups, ['sentiment', 'message']);
  const fallbackCandidates = candidates.filter((candidate) => (
    candidate.source === 'fallback_seed_pool' || candidate.recallSources.includes('fallback_seed_pool')
  ));
  const discoverySteps = extractDiscoverySteps(result);
  const candidateExpertPackets = extractCandidateExpertPackets(result);
  const isWaitingForCandidateExperts = (
    isWatchlistScan
    && displayOrchestrationMode === 'expert_graph'
    && !candidateExpertPackets.length
    && traceStatus === 'running'
  );
  const candidateThemes = extractCandidateExpertThemes(result);
  const candidateCapacity = extractCandidateCapacity(result);
  const candidateQuality = extractCandidateQuality(result);
  const candidateHardExclusion = extractCandidateHardExclusion(result);
  const candidateDecisionRows = toTraceCandidateDecisionRows(candidates, stockSelection);
  const deskStatus = extractDeskCommitteeStatus(stockSelection);
  const seedPool = extractSeedPoolDisplay(result, stockSelection);
  const thesisDeskPackets = extractThesisDeskPackets(stockSelection);
  const fundamentalPacket = candidateExpertPackets.find((packet) => packet.expert === 'fundamental_expert');
  const fundamentalCandidates = candidates.filter((candidate) => (
    candidate.candidateExperts.includes('fundamental_expert')
    || candidate.candidateDimensions.includes('fundamental')
    || candidate.source.startsWith('fundamental:')
  ));
  const fundamentalSource = fundamentalPacket?.sourceChain.find(Boolean) || {};
  const fundamentalSnapshotDiag = (fundamentalPacket?.diagnostics || []).find((item) => String(item.source || '').startsWith('fundamental_candidate')) || {};
  const eventWatches = extractEventImpactWatches(result);

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-border bg-surface-2 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <BrainCircuit className="h-4 w-4 text-muted-text" />
          <span className="text-sm font-semibold text-foreground">多专家选股状态</span>
          <span className={cn(
            'rounded-full px-2 py-0.5 text-xxs font-semibold uppercase tracking-wide',
            displayOrchestrationMode === 'expert_graph' ? 'bg-foreground text-background' : 'bg-surface-2 text-muted-text',
          )}>
            {displayOrchestrationMode}
          </span>
          {stockSelection && runtimeOrchestrationMode && runtimeOrchestrationMode !== orchestrationMode ? (
            <span className="text-label text-warning">本次选股结果仍为 {orchestrationMode}，后端配置为 {runtimeOrchestrationMode}</span>
          ) : null}
        </div>
        {candidateExpertPackets.length ? (
          <div className="mt-3 space-y-3">
            <div className="rounded-lg border border-border bg-card p-3 shadow-sm shadow-black/5">
              <div className="mb-3 flex flex-wrap items-baseline gap-2">
                <span className="text-xs font-semibold text-foreground">1. L1 候选发现专家</span>
                <span className="text-label text-muted-text">这些专家只负责 discover 候选池，不负责后续验证、买入裁决或组合风控。</span>
                {candidateCapacity ? (
                  <div className="ml-auto flex flex-wrap gap-1.5 text-xxs text-muted-text">
                    {candidateCapacity.maxCandidatesToDeepDive != null ? (
                      <span className="rounded-full bg-surface-2 px-2 py-0.5">深挖上限 {candidateCapacity.maxCandidatesToDeepDive}</span>
                    ) : null}
                    {candidateCapacity.minPerExpert != null ? (
                      <span className="rounded-full bg-surface-2 px-2 py-0.5">专家保底 {candidateCapacity.minPerExpert}</span>
                    ) : null}
                    {candidateCapacity.maxPerExpert != null ? (
                      <span className="rounded-full bg-surface-2 px-2 py-0.5">单专家最多 {candidateCapacity.maxPerExpert}</span>
                    ) : null}
                  </div>
                ) : null}
              </div>
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {candidateExpertPackets.map((packet) => (
                  <div key={packet.expert} className={cn('rounded-lg border p-3', dimensionTone(packet.dimension))}>
                    <div className="mb-2 flex items-center gap-2">
                      <span className="text-xs font-semibold">{packet.label}</span>
                      <span className="ml-auto rounded-full bg-card/75 px-2 py-0.5 text-xxs font-medium">{packet.status}</span>
                    </div>
                    <p className="text-label leading-relaxed opacity-85">{packet.goal || '候选发现'}</p>
                    <div className="mt-2 flex flex-wrap gap-1.5 text-xxs">
                      <span className="rounded-md bg-card/75 px-2 py-0.5">候选 {packet.count}</span>
                      {packet.themeCount ? <span className="rounded-md bg-card/75 px-2 py-0.5">主题 {packet.themeCount}</span> : null}
                      <span className="rounded-md bg-card/75 px-2 py-0.5">{packet.required ? '必须出候选' : '可选'}</span>
                      <span className="rounded-md bg-card/75 px-2 py-0.5">{packet.directStockCandidate ? '可直接推个股' : '默认只观察主题'}</span>
                    </div>
                    {packet.warnings.length || packet.errors.length ? (
                      <p className="mt-2 line-clamp-2 text-xxs leading-relaxed opacity-80">
                        {[...packet.errors, ...packet.warnings].slice(0, 2).join('；')}
                      </p>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
            {candidates.length ? (
              <div className="rounded-lg border border-border bg-card p-3 shadow-sm shadow-black/5">
                <div className="mb-3 flex flex-wrap items-baseline gap-2">
                  <span className="text-xs font-semibold text-foreground">2. 合并后的候选池</span>
                  <span className="text-label text-muted-text">{candidates.length} 只；候选进入后续阶段才会做技术/资金/消息/基本面验证和 Judge 裁决。</span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {candidates.slice(0, 12).map((candidate) => (
                    <span key={`expert-summary-candidate-${candidate.code}`} className="rounded-full border border-border bg-surface-2 px-2.5 py-1 text-label text-secondary-text">
                      <span className="font-mono font-semibold text-foreground">{candidate.code}</span>
                      {candidate.name ? ` ${candidate.name}` : ''}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
            {fundamentalPacket ? (
              <div className="rounded-lg border border-border/60 bg-surface-2 p-3 text-foreground">
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold">P2 基本面发现闭环</span>
                  <span className="rounded-full bg-card/80 px-2 py-0.5 text-xxs font-medium">{fundamentalPacket.status}</span>
                  <span className="rounded-full bg-card/80 px-2 py-0.5 text-xxs">候选 {fundamentalCandidates.length}</span>
                  {fundamentalPacket.asOf ? <span className="rounded-full bg-card/80 px-2 py-0.5 text-xxs">报告期 {fundamentalPacket.asOf}</span> : null}
                  {fundamentalSnapshotDiag.row_count != null ? (
                    <span className="rounded-full bg-card/80 px-2 py-0.5 text-xxs">快照 {String(fundamentalSnapshotDiag.row_count)} 行</span>
                  ) : null}
                </div>
                <p className="text-label leading-relaxed">
                  基本面专家读取本地预计算表，不在本轮 Trace 里实时全市场拉财报；如果这里为 0 只，优先检查快照表是否为空或筛选阈值是否未命中。
                </p>
                <div className="mt-2 grid gap-2 text-xxs md:grid-cols-2">
                  <div className="rounded-md bg-card/70 px-2 py-1.5">
                    <span className="text-secondary-text">数据源：</span>
                    <span className="break-all">{[fundamentalSource.table, fundamentalSource.db_path].filter(Boolean).join(' · ') || '-'}</span>
                  </div>
                  <div className="rounded-md bg-card/70 px-2 py-1.5">
                    <span className="text-secondary-text">诊断：</span>
                    <span>{[...fundamentalPacket.errors, ...fundamentalPacket.warnings].slice(0, 2).join('；') || '基本面专家已参与本轮候选发现。'}</span>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        ) : isWatchlistScan ? (
          <p className="mt-2 text-xs text-muted-text">
            {displayOrchestrationMode === 'expert_graph' && isWaitingForCandidateExperts
              ? `多专家候选发现正在运行，当前尚未返回 discover 专家包。${latestSelectionStage ? `最新阶段：${latestSelectionStage.label}；` : ''}`
              : displayOrchestrationMode === 'expert_graph'
                ? `本轮没有返回 L1 候选发现专家包。${latestSelectionStage ? `最后阶段：${latestSelectionStage.label}；` : ''}请检查 discover_watchlist_candidates 是否完成。`
                : '当前为 legacy 选股链路，尚未输出 L1 候选发现专家包。设置 AGENT_ORCHESTRATION_MODE=expert_graph 并重启后端后会显示。'}
          </p>
        ) : (
          <div className="mt-2 rounded-lg border border-warning/25 bg-warning/10 px-3 py-2 text-xs leading-relaxed text-warning">
            <p>本次请求未进入选股链路，当前识别意图为 {reportIntent || '未知'}，所以不会生成 expert_state。</p>
            {classifierConfigured ? (
              <p className="mt-1">
                MiMo 意图分类{classifierSuccess ? '已成功' : '未成功'}
                {classifierModel ? `，模型 ${classifierModel}` : ''}
                {classifierError ? `：${classifierError}` : '。'}
              </p>
            ) : (
              <p className="mt-1">MiMo 意图分类器未配置；可显式选择“选股候选池”运行。</p>
            )}
          </div>
        )}
      </div>

      {(seedPool || thesisDeskPackets.length) ? (
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <p className="text-label font-medium uppercase tracking-wider text-muted-text">P4 四席位可观察性</p>
            {seedPool ? (
              <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xxs text-secondary-text">
                Seed {seedPool.seedCount}{seedPool.totalLimit != null ? ` / ${seedPool.totalLimit}` : ''}
              </span>
            ) : null}
            {thesisDeskPackets.length ? (
              <span className="rounded-full bg-surface-2 px-2 py-0.5 text-xxs text-secondary-text">席位 {thesisDeskPackets.length}</span>
            ) : null}
          </div>

          {seedPool ? (
            <div className="space-y-3">
              <div className="grid gap-2 md:grid-cols-3">
                <div className="rounded-md bg-surface-2 p-3">
                  <p className="text-xxs uppercase tracking-wider text-muted-text">种子数量</p>
                  <p className="mt-1 text-xl font-semibold text-foreground">{seedPool.seedCount}</p>
                </div>
                <div className="rounded-md bg-surface-2 p-3">
                  <p className="text-xxs uppercase tracking-wider text-muted-text">来源分布</p>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-secondary-text">
                    {Object.entries(seedPool.sourceCounts).map(([key, count]) => `${displaySourceName(key)} ${count}`).join('；') || '-'}
                  </p>
                </div>
                <div className="rounded-md bg-surface-2 p-3">
                  <p className="text-xxs uppercase tracking-wider text-muted-text">信号维度</p>
                  <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-secondary-text">
                    {Object.entries(seedPool.dimensionCounts).map(([key, count]) => `${displayQualityKey(key)} ${count}`).join('；') || '-'}
                  </p>
                </div>
              </div>
              {seedPool.preview.length ? (
                <div>
                  <p className="mb-2 text-xxs uppercase tracking-wider text-muted-text">Seed Preview ({seedPool.preview.length})</p>
                  <div className="grid max-h-[360px] gap-2 overflow-y-auto pr-1 sm:grid-cols-2 xl:grid-cols-4">
                    {seedPool.preview.slice(0, 20).map((seed) => (
                      <div key={`seed-${seed.code}-${seed.source}`} className="min-h-[92px] rounded-md border border-border/70 bg-surface-2 px-3 py-2">
                        <div className="flex items-baseline gap-2">
                          <span className="font-mono text-xs font-semibold text-foreground">{seed.code}</span>
                          {seed.name ? <span className="truncate text-xs text-foreground">{seed.name}</span> : null}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-1">
                          <span className="rounded bg-card px-1.5 py-0.5 text-xxs text-secondary-text">{displaySourceName(seed.source)}</span>
                          {seed.sourceDiagnostics ? <span className="rounded bg-card px-1.5 py-0.5 text-xxs text-secondary-text">来源诊断</span> : null}
                        </div>
                        <p className="mt-1 line-clamp-2 text-xxs leading-relaxed text-muted-text">
                          {seed.triggerSignals.slice(0, 2).join('；') || seed.hint || seed.freshness || '-'}
                        </p>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}

          {thesisDeskPackets.length ? (
            <div className={cn('grid gap-3', seedPool ? 'mt-4' : '', 'lg:grid-cols-3')}>
              {thesisDeskPackets.map((packet) => (
                <div key={`thesis-desk-${packet.expert}`} className="rounded-lg border border-border/70 bg-surface-2 p-3">
                  <div className="mb-2 flex items-center gap-2">
                    <span className="text-xs font-semibold text-foreground">{packet.label}</span>
                    <span className="ml-auto rounded-full bg-card px-2 py-0.5 text-xxs text-secondary-text">{packet.status}</span>
                  </div>
                  <div className="mb-2 flex flex-wrap gap-1.5 text-xxs text-muted-text">
                    <span className="rounded-md bg-card px-2 py-0.5">看 {packet.seedCount}</span>
                    <span className="rounded-md bg-card px-2 py-0.5">输出 {packet.acceptedCount}</span>
                    <span className="rounded-md bg-card px-2 py-0.5">剔除 {packet.rejectedCount}</span>
                    <span className="rounded-md bg-card px-2 py-0.5">工具 {packet.toolCallCount}</span>
                    {packet.elapsedMs != null ? <span className="rounded-md bg-card px-2 py-0.5">{packet.elapsedMs}ms</span> : null}
                  </div>
                  {packet.candidates.length ? (
                    <div className="space-y-2">
                      {packet.candidates.slice(0, 5).map((candidate) => (
                        <div key={`${packet.expert}-${candidate.code}`} className="rounded-md bg-card/85 px-2.5 py-2">
                          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                            <span className="font-mono text-xs font-semibold text-foreground">{candidate.code}</span>
                            {candidate.name ? <span className="text-xs text-foreground">{candidate.name}</span> : null}
                            {candidate.stance ? <span className="rounded bg-surface-2 px-1.5 py-0.5 text-xxs text-secondary-text">{stanceLabel(candidate.stance)}</span> : null}
                            {candidate.setupType ? <span className="rounded bg-surface-2 px-1.5 py-0.5 text-xxs text-secondary-text">{setupTypeLabel(candidate.setupType)}</span> : null}
                          </div>
                          {candidate.reason ? <p className="mt-1 line-clamp-2 text-xxs leading-relaxed text-secondary-text">{displayReasonText(candidate.reason)}</p> : null}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="rounded-md bg-card/80 px-2.5 py-2 text-xxs leading-relaxed text-muted-text">
                      <p>本席位未输出候选。</p>
                      {packet.reason ? <p className="mt-1 text-secondary-text">{packet.reason}</p> : null}
                    </div>
                  )}
                  {packet.errors.length || packet.diagnostics.length ? (
                    <p className="mt-2 line-clamp-2 text-xxs leading-relaxed text-muted-text">
                      {[...packet.errors, ...packet.diagnostics].slice(0, 2).join('；')}
                    </p>
                  ) : null}
                  {packet.perSeedPackets.length ? (
                    <div className="mt-3 max-h-44 space-y-1.5 overflow-y-auto pr-1">
                      {packet.perSeedPackets.slice(0, 20).map((seed, idx) => (
                        <div key={`${packet.expert}-seed-${seed.code || idx}`} className="rounded-md bg-card/70 px-2 py-1.5">
                          <div className="flex flex-wrap items-center gap-1.5 text-xxs">
                            <span className="font-mono font-semibold text-foreground">{seed.code || `seed-${idx + 1}`}</span>
                            {seed.name ? <span className="text-secondary-text">{seed.name}</span> : null}
                            <span className="rounded bg-surface-2 px-1.5 py-0.5 text-muted-text">{seed.status}</span>
                            <span className="rounded bg-surface-2 px-1.5 py-0.5 text-muted-text">出 {seed.candidateCount}</span>
                            <span className="rounded bg-surface-2 px-1.5 py-0.5 text-muted-text">剔 {seed.rejectedCount}</span>
                            <span className="rounded bg-surface-2 px-1.5 py-0.5 text-muted-text">工具 {seed.toolCallCount}</span>
                            {seed.elapsedMs != null ? <span className="rounded bg-surface-2 px-1.5 py-0.5 text-muted-text">{seed.elapsedMs}ms</span> : null}
                          </div>
                          {seed.errors.length || seed.diagnostics.length ? (
                            <p className="mt-1 line-clamp-1 text-xxs leading-relaxed text-muted-text">
                              {seed.reason || [...seed.errors, ...seed.diagnostics].slice(0, 1).join('；')}
                            </p>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {candidateQuality ? (
        <div className="rounded-lg border border-border bg-card p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <p className="text-label font-medium uppercase tracking-wider text-muted-text">候选池质量与门禁</p>
            {candidateQuality.hardStrategyTrunkMissing ? (
              <span className="rounded-full bg-danger/10 px-2 py-0.5 text-xxs font-medium text-danger">硬策略主干缺失</span>
            ) : (
              <span className="rounded-full bg-success/10 px-2 py-0.5 text-xxs font-medium text-success">硬策略主干可用</span>
            )}
            {candidateQuality.hardExclusionCount ? (
              <span className="rounded-full bg-warning/10 px-2 py-0.5 text-xxs font-medium text-warning">硬排除 {candidateQuality.hardExclusionCount}</span>
            ) : null}
          </div>
          <div className="grid gap-2 md:grid-cols-4">
            <div className="rounded-lg bg-surface-2 p-3">
              <p className="text-xxs uppercase tracking-wider text-muted-text">候选数量</p>
              <p className="mt-1 text-lg font-semibold text-foreground">{candidateQuality.candidateCount}</p>
            </div>
            <div className="rounded-lg bg-surface-2 p-3">
              <p className="text-xxs uppercase tracking-wider text-muted-text">多源共振</p>
              <p className="mt-1 text-lg font-semibold text-foreground">{candidateQuality.multiSourceCount}</p>
            </div>
            <div className="rounded-lg bg-surface-2 p-3">
              <p className="text-xxs uppercase tracking-wider text-muted-text">兜底观察</p>
              <p className="mt-1 text-lg font-semibold text-foreground">{candidateQuality.fallbackCount}</p>
            </div>
            <div className="rounded-lg bg-surface-2 p-3">
              <p className="text-xxs uppercase tracking-wider text-muted-text">生命周期</p>
              <p className="mt-1 text-xs leading-relaxed text-secondary-text">
                {Object.entries(candidateQuality.lifecycleCounts).map(([key, count]) => `${displayLifecycleStatus(key)} ${count}`).join('；') || '-'}
              </p>
            </div>
          </div>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div>
              <p className="mb-1.5 text-xxs uppercase tracking-wider text-muted-text">维度分布</p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(candidateQuality.dimensionCounts).map(([key, count]) => (
                  <span key={key} className="rounded-full bg-surface-2 px-2 py-0.5 text-xxs text-secondary-text">{displayQualityKey(key)} {count}</span>
                ))}
              </div>
            </div>
            <div>
              <p className="mb-1.5 text-xxs uppercase tracking-wider text-muted-text">来源分布</p>
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(candidateQuality.sourceCounts).map(([key, count]) => (
                  <span key={key} className="rounded-full bg-surface-2 px-2 py-0.5 text-xxs text-secondary-text">{displayQualityKey(key)} {count}</span>
                ))}
              </div>
            </div>
          </div>
          {candidateHardExclusion?.excludedCount ? (
            <div className="mt-3 rounded-lg border border-warning/25 bg-warning/10 px-3 py-2 text-warning">
              <p className="text-label font-semibold">硬排除明细</p>
              <p className="mt-1 text-xxs leading-relaxed">
                {Object.entries(candidateHardExclusion.reasonCounts).map(([key, count]) => `${displayExclusionReason(key)} ${count}`).join('；')}
              </p>
              {candidateHardExclusion.examples.length ? (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {candidateHardExclusion.examples.slice(0, 6).map((item) => (
                    <span key={`${item.code}-${item.reason}`} className="rounded-md bg-card/80 px-2 py-0.5 text-xxs">
                      {item.code}{item.name ? ` ${item.name}` : ''} · {displayExclusionReason(item.reason)}
                    </span>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {candidateThemes.length ? (
        <div className="rounded-lg border border-border bg-card p-4">
          <p className="mb-2 text-label font-medium uppercase tracking-wider text-muted-text">主题观察 ({candidateThemes.length})</p>
          <div className="grid gap-2 md:grid-cols-2">
            {candidateThemes.slice(0, 6).map((theme) => (
              <div key={`${theme.theme}-${theme.eventTitle}`} className="rounded-lg border border-purple/25 bg-purple/10 px-3 py-2 text-purple">
                <div className="mb-1 flex flex-wrap items-center gap-2">
                  <span className="text-xs font-semibold">{theme.theme}</span>
                  <span className="rounded-full bg-card/75 px-2 py-0.5 text-xxs">{displayEventMaturity(theme.status)}</span>
                  {theme.confidence != null ? <span className="text-xxs opacity-80">置信 {formatConfidence(theme.confidence)}</span> : null}
                </div>
                {theme.eventTitle ? <p className="text-label leading-relaxed">{theme.eventTitle}</p> : null}
                {theme.reason ? <p className="mt-1 line-clamp-2 text-xxs leading-relaxed opacity-80">{theme.reason}</p> : null}
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {discoverySteps.length ? (
        <div>
          <p className="mb-2 text-label font-medium uppercase tracking-wider text-muted-text">候选来源审计</p>
          <div className="flex flex-wrap gap-1.5">
            {discoverySteps.map((step, i) => {
              const source = String(step.source || '-');
              const status = String(step.status || '-');
              const count = typeof step.count === 'number' ? step.count : undefined;
              return (
                <span key={`${source}-${i}`} className="rounded-full border border-border bg-card px-2.5 py-1 text-label text-secondary-text">
                  {displaySourceName(source)} · {status}{count != null ? ` · ${count}` : ''}
                </span>
              );
            })}
          </div>
        </div>
      ) : null}

      {eventWatches.length ? (
        <div>
          <p className="mb-3 text-label font-medium uppercase tracking-wider text-muted-text">消息/事件观察 ({eventWatches.length})</p>
          <div className="grid gap-3 lg:grid-cols-2">
            {eventWatches.map((event) => {
              const confirmedCount = event.validationMatches.filter((match) => match.status === 'confirmed').length;
              return (
                <div key={event.eventId || event.title} className={cn('rounded-lg border p-3', dimensionTone(event.maturity === 'confirmed' ? 'sentiment' : 'message'))}>
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-card/80 px-2 py-0.5 text-xxs font-semibold">{displayEventMaturity(event.maturity)}</span>
                    {event.eventType ? <span className="rounded-full bg-card/60 px-2 py-0.5 text-xxs">{event.eventType}</span> : null}
                    {event.validationWindowDays ? <span className="text-xxs text-muted-text">{event.validationWindowDays} 日验证窗口</span> : null}
                    {confirmedCount ? <span className="ml-auto rounded-full bg-success/10 px-2 py-0.5 text-xxs text-success">验证 {confirmedCount}</span> : null}
                  </div>
                  <div className="space-y-1.5">
                    <p className="text-xs font-semibold leading-relaxed text-foreground">{event.title}</p>
                    {event.snippet ? <p className="line-clamp-2 text-label leading-relaxed text-secondary-text">{event.snippet}</p> : null}
                    {event.watchThemes.length ? (
                      <div className="flex flex-wrap gap-1">
                        {event.watchThemes.slice(0, 6).map((theme) => (
                          <span key={theme} className="rounded-md bg-card/75 px-2 py-0.5 text-xxs text-secondary-text">{theme}</span>
                        ))}
                      </div>
                    ) : null}
                    {event.impactVariables.length ? (
                      <p className="text-xxs leading-relaxed text-muted-text">影响变量：{event.impactVariables.slice(0, 5).join('、')}</p>
                    ) : null}
                    {event.validationMatches.length ? (
                      <div className="mt-2 space-y-1 rounded-md bg-card/70 p-2">
                        {event.validationMatches.slice(0, 4).map((match) => (
                          <div key={`${event.eventId}-${match.theme}`} className="text-xxs leading-relaxed text-secondary-text">
                            <span className="font-semibold">{match.theme || '主题'}</span>
                            <span className="mx-1">·</span>
                            <span>{match.status === 'confirmed' ? `已验证 ${match.resultCount} 条` : '观察中，未形成个股候选'}</span>
                            {match.titles.length ? <span> · {match.titles.join('；')}</span> : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {(event.source || event.publishedDate) ? (
                      <p className="text-xxs text-muted-text">{[event.source, event.publishedDate].filter(Boolean).join(' · ')}</p>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {deskStatus ? (
        <div className={cn(
          'rounded-lg border p-3 text-xs',
          deskStatus.degraded || deskStatus.error ? 'border-amber-500/50 bg-amber-500/10' : 'border-emerald-500/40 bg-emerald-500/10',
        )}>
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-semibold text-foreground">
              {deskStatus.mode === 'thesis_desk_committee' ? '打法席位委员会' : 'LLM 专家委员会'}
            </span>
            <span className={cn(
              'rounded-full px-2 py-0.5 text-xxs',
              deskStatus.degraded || deskStatus.error ? 'bg-amber-500/20 text-amber-200' : 'bg-emerald-500/20 text-emerald-200',
            )}>
              {deskStatus.error ? '运行异常' : deskStatus.degraded ? '降级运行' : deskStatus.status ? `状态 ${deskStatus.status}` : '运行中'}
            </span>
            {deskStatus.deskDiagnostics.map((d) => (
              <span key={d.desk} className="rounded-md bg-card/70 px-2 py-0.5 text-xxs text-secondary-text">
                {deskLabel(d.desk)}: {d.status}{d.picks ? ` ${d.picks}只` : ''}
              </span>
            ))}
          </div>
          {deskStatus.error ? (
            <p className="mt-1.5 text-xxs leading-relaxed text-amber-300">席位运行异常：{deskStatus.error}</p>
          ) : deskStatus.degraded && deskStatus.fallbackUsed ? (
            <p className="mt-1.5 text-xxs leading-relaxed text-amber-300">席位收敛降级，候选池回退到召回结果，请核对 trace candidate_discovery。</p>
          ) : deskStatus.degraded ? (
            <p className="mt-1.5 text-xxs leading-relaxed text-amber-300">席位收敛降级，已保留可用席位候选；请查看 trace candidate_discovery 的 partial_errors。</p>
          ) : null}
        </div>
      ) : null}

      {candidates.length ? (
        <CandidateDecisionTable
          title="候选入池榜"
          description="优先展示初筛和深挖结论；seed pool 召回分只作为来源内诊断，不做跨来源评分比较。"
          items={candidateDecisionRows}
          scoreColumnLabel="评估口径"
          emptyTitle="本轮没有候选"
          emptyDescription="候选发现阶段没有返回可展示股票。"
        />
      ) : null}

      {dimensionGroups.length ? (
        <Collapsible title="专家维度与原始候选分组" defaultOpen={false} icon={<BrainCircuit className="h-4 w-4" />}>
          <div className="grid gap-3 lg:grid-cols-2">
            {dimensionGroups.map((group) => (
              <div key={group.dimension} className={cn('rounded-lg border p-3', dimensionTone(group.dimension))}>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold">{group.label}</span>
                  <span className="rounded-full bg-card/70 px-2 py-0.5 text-xxs">{group.candidates.length} 只</span>
                </div>
                <div className="space-y-2">
                  {group.candidates.slice(0, 6).map(({ candidate, details }) => (
                    <div key={`${group.dimension}-${candidate.code}`} className="rounded-md bg-card/75 px-2.5 py-2">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                        <span className="font-mono text-xs font-semibold text-foreground">{candidate.code}</span>
                        {candidate.name ? <span className="text-xs font-medium text-foreground">{candidate.name}</span> : null}
                      </div>
                      {details.length ? (
                        <p className="mt-1 text-label leading-relaxed text-secondary-text">{details.slice(0, 2).join('；')}</p>
                      ) : null}
                    </div>
                  ))}
                </div>
              </div>
            ))}
            {!hasSentimentCandidates ? (
              <div className={cn('rounded-lg border p-3', dimensionTone('sentiment'))}>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold">情绪/热点候选</span>
                  <span className="rounded-full bg-card/70 px-2 py-0.5 text-xxs">0 只</span>
                </div>
                <div className="rounded-md bg-card/75 px-2.5 py-2">
                  <p className="text-label leading-relaxed text-secondary-text">
                    本次候选召回没有命中消息/情绪来源；当前只会在强势板块、用户输入或后续情绪工具接入后生成这类候选。
                  </p>
                </div>
              </div>
            ) : null}
          </div>
        </Collapsible>
      ) : null}

      {fallbackCandidates.length ? (
        <div>
          <p className="mb-3 text-label font-medium uppercase tracking-wider text-warning">兜底观察池 ({fallbackCandidates.length})</p>
          <div className="rounded-lg border border-warning/25 bg-warning/10 p-3">
            <p className="mb-2 text-label leading-relaxed text-warning">
              这些股票来自固定种子池，只用于真实候选召回失败时维持后续取证链路；它们不是策略、资金或消息面筛选结果，不能作为推荐依据。
            </p>
            <div className="grid gap-2 sm:grid-cols-2">
              {fallbackCandidates.slice(0, 8).map((candidate) => (
                <div key={`fallback-${candidate.code}`} className="rounded-md bg-card/80 px-2.5 py-2">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="font-mono text-xs font-semibold text-foreground">{candidate.code}</span>
                    {candidate.name ? <span className="text-xs font-medium text-foreground">{candidate.name}</span> : null}
                  </div>
                  <p className="mt-1 text-label leading-relaxed text-secondary-text">{candidate.reason || '固定兜底观察样本'}</p>
                </div>
              ))}
            </div>
          </div>
        </div>
      ) : null}

      {deepTargets.length ? (
        <div>
          <p className="mb-1.5 text-label font-medium uppercase tracking-wider text-muted-text">深挖标的</p>
          <div className="flex flex-wrap gap-1.5">
            {deepTargets.map((code) => (
              <span key={code} className="rounded-md bg-success/10 px-2 py-0.5 font-mono text-xs text-success">{code}</span>
            ))}
          </div>
        </div>
      ) : null}
      {/* Stage status */}
      {Object.keys(stages).length ? (
        <div>
          <p className="mb-1.5 text-label font-medium uppercase tracking-wider text-muted-text">流水线阶段</p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(stages).map(([key, val]) => {
              const s = asRecord(val) || {};
              const status = String(s.status || '-');
              return (
                <span key={key} className="flex items-center gap-1.5 rounded-full border border-border px-2.5 py-1 text-label">
                  <span className={cn('h-1.5 w-1.5 rounded-full', status === 'ok' ? 'bg-success' : status === 'partial' ? 'bg-warning/100' : 'bg-muted-text')} />
                  <span className="text-secondary-text">{key.replace(/_/g, ' ')}</span>
                </span>
              );
            })}
          </div>
        </div>
      ) : null}
      {/* Data tools used */}
      {result.tool_calls.filter((t) => t.tool.startsWith('get_') || t.tool.includes('quote')).length ? (
        <div>
          <p className="mb-1.5 text-label font-medium uppercase tracking-wider text-muted-text">数据工具</p>
          <div className="flex flex-wrap gap-1.5">
            {result.tool_calls.filter((t) => t.tool.startsWith('get_') || t.tool.includes('quote')).map((t, i) => (
              <span key={`${t.tool}-${i}`} className={cn('rounded-md px-2 py-0.5 text-label', t.success ? 'bg-surface-2 text-secondary-text' : 'bg-danger/10 text-danger')}>
                {t.tool}
              </span>
            ))}
          </div>
        </div>
      ) : null}
    </div>
  );
};

const L2Detail: React.FC<{
  toolCalls: AgentTraceToolCall[];
  selectedIndex: number;
  onSelect: (i: number) => void;
  selectedTool: AgentTraceToolCall | null;
}> = ({ toolCalls, selectedIndex, onSelect, selectedTool }) => {
  const preview = selectedTool?.result_preview ? truncateText(String(selectedTool.result_preview), TOOL_PREVIEW_RENDER_CHARS) : '';
  return (
    <div className="space-y-3">
      {/* Tool list */}
      <div className="max-h-[300px] overflow-y-auto rounded-lg border border-border">
        {toolCalls.length ? toolCalls.map((call, i) => (
          <button
            key={`${call.step}-${call.tool}-${i}`}
            type="button"
            onClick={() => onSelect(i)}
            className={cn(
              'flex w-full items-center gap-3 border-b border-border px-3 py-2 text-left text-xs transition-colors last:border-0 hover:bg-surface-2',
              selectedIndex === i && 'bg-surface-2',
            )}
          >
            <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', call.success ? 'bg-success' : 'bg-danger')} />
            <span className="min-w-0 flex-1 truncate font-medium text-foreground">{String(call.tool || '-')}</span>
            <span className="shrink-0 font-mono text-muted-text">{formatDuration(call.duration)}</span>
          </button>
        )) : (
          <p className="p-3 text-xs text-muted-text">暂无工具调用</p>
        )}
      </div>
      {/* Selected tool detail */}
      {selectedTool ? (
        <div className="rounded-lg border border-border p-4">
          <div className="mb-3 flex items-center gap-3 text-xs">
            <Wrench className="h-3.5 w-3.5 text-muted-text" />
            <span className="font-medium text-foreground">{String(selectedTool.tool || '-')}</span>
            <span className={cn('rounded-full px-2 py-0.5 text-xxs font-medium', selectedTool.success ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger')}>
              {selectedTool.success ? 'OK' : 'FAIL'}
            </span>
            <span className="ml-auto font-mono text-muted-text">step {String(selectedTool.step ?? '-')} · {formatDuration(selectedTool.duration)}</span>
          </div>
          <JsonViewer data={(selectedTool.arguments || {}) as Record<string, unknown>} maxHeight="160px" />
          {preview ? (
            <pre className="mt-3 max-h-[180px] overflow-auto rounded-lg bg-surface-2 p-3 font-mono text-label leading-5 text-secondary-text whitespace-pre-wrap">
              {preview}
            </pre>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};

const L3Detail: React.FC<{ result: AgentTraceRunResponse; debate: Record<string, unknown> | null }> = ({ result, debate }) => {
  const judge = asRecord(debate?.judge_decision);
  const dims = toRecordList(judge?.dimension_assessments);

  if (dims.length) {
    return (
      <div className="space-y-2">
        {dims.map((item, i) => {
          const dim = String(item.dimension || '-');
          const verdict = String(item.verdict || '-');
          return (
            <div key={`${dim}-${i}`} className="flex items-center justify-between rounded-lg border border-border px-3 py-2">
              <span className="text-xs text-foreground">{DIMENSION_LABELS[dim] || dim}</span>
              <span className={cn(
                'rounded-full px-2 py-0.5 text-xxs font-medium',
                verdict === 'supports_primary' && 'bg-success/10 text-success',
                verdict === 'supports_opposing' && 'bg-danger/10 text-danger',
                verdict === 'mixed' && 'bg-warning/10 text-warning',
                verdict === 'insufficient_data' && 'bg-surface-2 text-muted-text',
              )}>
                {verdict.replace(/_/g, ' ')}
              </span>
            </div>
          );
        })}
      </div>
    );
  }

  // Fallback: show tool evidence summary
  if (result.tool_calls.length) {
    return (
      <div className="space-y-1">
        {result.tool_calls.slice(0, 8).map((t, i) => (
          <div key={`${t.tool}-${i}`} className="flex items-center gap-2 text-xs">
            <span className={cn('h-1.5 w-1.5 rounded-full', t.success ? 'bg-success' : 'bg-danger')} />
            <span className="text-secondary-text">{t.tool}</span>
            <span className="text-muted-text">{t.success ? '证据可用' : '取证失败'}</span>
          </div>
        ))}
      </div>
    );
  }

  return <p className="text-xs text-muted-text">等待证据进入信号层。</p>;
};

const L4Detail: React.FC<{ debate: Record<string, unknown> | null; planner: Record<string, unknown> | null }> = ({ debate, planner }) => {
  const primary = asRecord(debate?.primary_thesis) || {};
  const opposing = asRecord(debate?.opposing_thesis) || {};
  const judge = asRecord(debate?.judge_decision) || {};
  const reasonPoints = toStringList(judge.reason_points);

  return (
    <div className="space-y-4">
      {/* Planner intent */}
      {planner ? (
        <div className="rounded-lg bg-surface-2 p-3">
          <p className="text-label font-medium uppercase tracking-wider text-muted-text">Planner</p>
          <p className="mt-1 text-xs text-foreground">
            意图: {String(planner.intent || '-')} · 目标: {String(planner.primary_symbol || '-')} · {planner.has_position ? '持仓命中' : '未命中持仓'}
          </p>
        </div>
      ) : null}

      {/* Debate thesis comparison */}
      {(primary.summary || opposing.summary) ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-lg border border-border p-4">
            <p className="mb-2 text-label font-semibold uppercase tracking-wider text-muted-text">主观点</p>
            <p className="text-xs leading-relaxed text-foreground">{String(primary.summary || '-')}</p>
            {toStringList(primary.evidence).length ? (
              <ul className="mt-2 space-y-0.5 text-label text-muted-text">
                {toStringList(primary.evidence).slice(0, 4).map((e, i) => <li key={i}>· {e}</li>)}
              </ul>
            ) : null}
          </div>
          <div className="rounded-lg border border-border p-4">
            <p className="mb-2 text-label font-semibold uppercase tracking-wider text-muted-text">反方</p>
            <p className="text-xs leading-relaxed text-foreground">{String(opposing.summary || '-')}</p>
            {toStringList(opposing.evidence).length ? (
              <ul className="mt-2 space-y-0.5 text-label text-muted-text">
                {toStringList(opposing.evidence).slice(0, 4).map((e, i) => <li key={i}>· {e}</li>)}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* Judge */}
      {judge.final_action ? (
        <div className="rounded-lg bg-foreground p-4 text-background">
          <div className="flex items-center gap-2">
            <BrainCircuit className="h-4 w-4 text-background/60" />
            <span className="text-label font-semibold uppercase tracking-wider text-background/60">Judge 裁决</span>
            <span className="ml-auto rounded-full bg-card/10 px-2.5 py-0.5 text-xs font-medium">{String(judge.final_action)}</span>
          </div>
          <p className="mt-2 text-sm leading-relaxed text-background/85">{String(judge.decision_summary || judge.reason || '-')}</p>
          {reasonPoints.length ? (
            <ul className="mt-3 space-y-1 text-xs text-background/60">
              {reasonPoints.map((p, i) => <li key={i}>· {p}</li>)}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};

const L5Detail: React.FC<{ riskPayload: Record<string, unknown> | null }> = ({ riskPayload }) => {
  if (!riskPayload) return <p className="text-xs text-muted-text">尚未生成风控结果。</p>;
  const gate = asRecord(riskPayload.risk_gate) || {};
  const checks = toRecordList(gate.checks);
  const blockedReasons = toStringList(gate.blocked_reasons);

  return (
    <div className="space-y-3">
      {checks.length ? (
        <div className="space-y-1.5">
          {checks.map((check, i) => {
            const passed = check.passed === true;
            return (
              <div key={`${String(check.rule_id)}-${i}`} className="flex items-start gap-2 text-xs">
                <span className={cn('mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full', passed ? 'bg-success' : 'bg-danger')} />
                <span className="font-mono text-muted-text">{String(check.rule_id || '-')}</span>
                <span className="flex-1 text-secondary-text">{String(check.message || '-')}</span>
              </div>
            );
          })}
        </div>
      ) : null}
      {blockedReasons.length ? (
        <div className="rounded-lg bg-danger/10 p-3">
          <p className="text-label font-medium text-danger">阻断原因</p>
          <ul className="mt-1 space-y-0.5 text-xs text-danger">
            {blockedReasons.map((r, i) => <li key={i}>· {r}</li>)}
          </ul>
        </div>
      ) : null}
    </div>
  );
};

const ObservabilityDetail: React.FC<{
  llmTelemetry: Record<string, unknown> | null;
  judgeSanity: Record<string, unknown> | null;
}> = ({ llmTelemetry, judgeSanity }) => {
  const stages = toRecordList(llmTelemetry?.by_stage);
  const sanityChecks = toRecordList(judgeSanity?.sanity_checks);
  const requiredChanges = toRecordList(judgeSanity?.required_plan_changes);
  const totalCalls = toFiniteNumber(llmTelemetry?.total_calls) || 0;
  const finalAction = String(judgeSanity?.final_action || '-');
  const primaryPlanVerdict = String(judgeSanity?.primary_plan_verdict || '-');

  if (!llmTelemetry && !judgeSanity) {
    return <p className="text-xs text-muted-text">本次 Trace 尚未返回 LLM telemetry 或 Judge sanity 汇总。</p>;
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-4">
        <div className="rounded-lg bg-surface-2 p-3">
          <p className="text-xxs uppercase tracking-wider text-muted-text">LLM 调用</p>
          <p className="mt-1 text-lg font-semibold text-foreground">{formatCount(llmTelemetry?.total_calls)}</p>
        </div>
        <div className="rounded-lg bg-surface-2 p-3">
          <p className="text-xxs uppercase tracking-wider text-muted-text">Token</p>
          <p className="mt-1 text-lg font-semibold text-foreground">{formatCount(llmTelemetry?.total_tokens)}</p>
        </div>
        <div className="rounded-lg bg-surface-2 p-3">
          <p className="text-xxs uppercase tracking-wider text-muted-text">延迟</p>
          <p className="mt-1 text-lg font-semibold text-foreground">{formatLatencyMs(llmTelemetry?.total_latency_ms)}</p>
        </div>
        <div className="rounded-lg bg-surface-2 p-3">
          <p className="text-xxs uppercase tracking-wider text-muted-text">估算成本</p>
          <p className="mt-1 text-lg font-semibold text-foreground">{formatCost(llmTelemetry?.estimated_cost)}</p>
        </div>
      </div>

      {stages.length ? (
        <div className="rounded-lg border border-border p-4">
          <p className="mb-3 text-label font-semibold uppercase tracking-wider text-muted-text">按阶段统计</p>
          <div className="space-y-2">
            {stages.map((stage, index) => (
              <div key={`${String(stage.stage || 'stage')}-${index}`} className="grid gap-2 rounded-md bg-surface-2 px-3 py-2 text-xs sm:grid-cols-[1fr_auto_auto_auto] sm:items-center">
                <span className="min-w-0 truncate font-medium text-foreground">{String(stage.stage || 'unknown')}</span>
                <span className="text-secondary-text">调用 {formatCount(stage.calls)}</span>
                <span className="text-secondary-text">Token {formatCount(stage.total_tokens)}</span>
                <span className={cn('text-secondary-text', toFiniteNumber(stage.failed_calls) ? 'text-danger' : '')}>
                  失败 {formatCount(stage.failed_calls)}
                </span>
              </div>
            ))}
          </div>
        </div>
      ) : totalCalls ? null : (
        <div className="rounded-lg border border-border p-4 text-xs text-muted-text">本次运行没有记录到 LLM 调用。</div>
      )}

      {judgeSanity ? (
        <div className="rounded-lg border border-border p-4">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            <p className="text-label font-semibold uppercase tracking-wider text-muted-text">Judge Sanity</p>
            <span className="rounded-full bg-surface-2 px-2.5 py-0.5 text-xxs text-secondary-text">动作 {finalAction}</span>
            <span className={cn(
              'rounded-full px-2.5 py-0.5 text-xxs font-medium',
              primaryPlanVerdict === 'accepted' && 'bg-success/10 text-success',
              primaryPlanVerdict === 'downgraded' && 'bg-warning/10 text-warning',
              primaryPlanVerdict === 'rejected' && 'bg-danger/10 text-danger',
              !['accepted', 'downgraded', 'rejected'].includes(primaryPlanVerdict) && 'bg-surface-2 text-secondary-text',
            )}>
              {primaryPlanVerdict}
            </span>
          </div>
          <p className="text-xs leading-relaxed text-foreground">
            {String(judgeSanity.decision_summary || judgeSanity.reason || 'Judge sanity 未返回摘要。')}
          </p>
          {sanityChecks.length ? (
            <div className="mt-3 space-y-2">
              {sanityChecks.map((check, index) => (
                <div key={`${String(check.rule_id || 'rule')}-${index}`} className="rounded-md bg-surface-2 px-3 py-2 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-xxs font-semibold text-foreground">{String(check.rule_id || '-')}</span>
                    {check.action ? <span className="rounded-full bg-card px-2 py-0.5 text-xxs text-secondary-text">{String(check.action)}</span> : null}
                    {(check.from_action || check.to_action) ? (
                      <span className="text-xxs text-muted-text">{String(check.from_action || '-')} → {String(check.to_action || '-')}</span>
                    ) : null}
                  </div>
                  {check.reason ? <p className="mt-1 text-secondary-text">{String(check.reason)}</p> : null}
                </div>
              ))}
            </div>
          ) : null}
          {requiredChanges.length ? (
            <Collapsible title={`要求修正 (${requiredChanges.length})`} defaultOpen={false} className="mt-3">
              <JsonViewer data={{ required_plan_changes: requiredChanges }} maxHeight="220px" />
            </Collapsible>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};

/* ═══════════════════════════════════════════════
   Narrative Builders
   ═══════════════════════════════════════════════ */

function buildL1Narrative(result: AgentTraceRunResponse, stockSelection: Record<string, unknown> | null): string {
  const candidates = extractDiscoveryCandidates(result, stockSelection);
  const dataTools = result.tool_calls.filter((t) => t.tool.startsWith('get_') || t.tool.includes('quote'));
  const candidateExpertPackets = extractCandidateExpertPackets(result);
  const orchestrationMode = getRuntimeOrchestrationMode(result) || getSelectionOrchestrationMode(stockSelection);
  const reportIntent = getTraceReportIntent(result);

  if (candidates.length) {
    const sourceLabels = Array.from(new Set(candidates.flatMap((item) => (
      item.recallSources.length ? item.recallSources : item.source ? [item.source] : []
    )).map(displaySourceName))).slice(0, 3);
    const expertText = candidateExpertPackets.length
      ? `L1 已输出 ${candidateExpertPackets.length} 个候选发现专家包；`
      : orchestrationMode === 'expert_graph'
        ? 'L1 多专家候选发现已开启；'
        : '';
    return `${expertText}第一阶段已生成 ${candidates.length} 只候选股票，来源包括${sourceLabels.length ? sourceLabels.join('、') : '多路召回'}；候选会汇总成决策榜，并按策略、技术、资金、消息/情绪和基本面拆解证据。`;
  }
  if (orchestrationMode === 'expert_graph' && reportIntent && reportIntent !== 'watchlist_scan') {
    return `后端多专家模式已开启，但本次识别为 ${reportIntent}，未进入选股候选池链路。`;
  }
  if (dataTools.length) {
    return `调用了 ${dataTools.length} 个数据工具获取行情和基础数据。`;
  }
  return '等待数据层工具调用...';
}

function buildL4Narrative(debate: Record<string, unknown> | null, planner: Record<string, unknown> | null): string {
  const judge = asRecord(debate?.judge_decision);
  if (judge?.final_action) {
    return `Judge 裁决：${String(judge.final_action)}。${String(judge.decision_summary || '')}`;
  }
  if (planner?.intent) {
    return `Planner 已识别意图为「${String(planner.intent)}」，等待 Debate 完成裁决。`;
  }
  return '等待决策层生成裁决。';
}

function buildL5Narrative(riskPayload: Record<string, unknown> | null): string {
  if (!riskPayload) return '等待风控闸门校验。';
  const gate = asRecord(riskPayload.risk_gate) || {};
  const status = String(gate.status || '-');
  const original = String(gate.original_action || '-');
  const allowed = String(gate.allowed_action || '-');
  if (status === 'passed') return `风控通过，允许执行「${allowed}」。`;
  if (status === 'blocked') return `风控阻断：原动作「${original}」被改为「${allowed}」。`;
  if (status === 'downgraded') return `动作已降级：「${original}」调整为「${allowed}」。`;
  return `风控状态：${status}`;
}

function buildL6Narrative(riskPayload: Record<string, unknown> | null): string {
  const tradePlan = asRecord(riskPayload?.trade_plan);
  if (!tradePlan) return '等待风控通过后生成交易方案。';
  return `已生成 ${String(tradePlan.symbol || '-')} 的「${String(tradePlan.action || '-')}」方案，目标仓位 ${formatPercent(tradePlan.target_position_pct)}。`;
}

function buildObservabilityNarrative(
  llmTelemetry: Record<string, unknown> | null,
  judgeSanity: Record<string, unknown> | null,
): string {
  const totalCalls = toFiniteNumber(llmTelemetry?.total_calls) || 0;
  const totalTokens = toFiniteNumber(llmTelemetry?.total_tokens) || 0;
  const failedCalls = toFiniteNumber(llmTelemetry?.failed_calls) || 0;
  const checkCount = toFiniteNumber(judgeSanity?.check_count) || 0;
  const requiredChangeCount = toFiniteNumber(judgeSanity?.required_change_count) || 0;
  if (totalCalls || judgeSanity) {
    const llmText = totalCalls
      ? `LLM ${formatCount(totalCalls)} 次，Token ${formatCount(totalTokens)}${failedCalls ? `，失败 ${formatCount(failedCalls)} 次` : ''}`
      : '未记录 LLM 调用';
    const sanityText = judgeSanity
      ? `Judge sanity ${formatCount(checkCount)} 条校验，要求修正 ${formatCount(requiredChangeCount)} 项`
      : '未返回 Judge sanity';
    return `${llmText}；${sanityText}。`;
  }
  return '等待 Trace 汇总 LLM 调用、成本、延迟和 Judge sanity 修正。';
}

export default AgentTracePage;
