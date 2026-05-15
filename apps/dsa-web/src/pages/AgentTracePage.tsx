import React, { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  BrainCircuit,
  Check,
  ChevronDown,
  Circle,
  History,
  Loader2,
  Play,
  Trash2,
  Wrench,
} from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { agentApi, type AgentTraceRunResponse, type AgentTraceToolCall } from '../api/agent';
import { portfolioApi } from '../api/portfolio';
import { ApiErrorAlert, Button, JsonViewer } from '../components/common';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import type { PortfolioAccountItem } from '../types/portfolio';
import { cn } from '../utils/cn';

/* ═══════════════════════════════════════════════
   Constants & Types
   ═══════════════════════════════════════════════ */

const DEFAULT_PROMPT = '我持有 600519，帮我分析未来走势，适合继续拿长线吗？如果要加仓或减仓，关键观察点是什么？';
const DEFAULT_STOCK_CODE = '600519';
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
  { value: 'position_review', label: '持仓诊断' },
  { value: 'entry_analysis', label: '入场分析' },
  { value: 'risk_review', label: '账户风控' },
  { value: 'event_impact', label: '事件影响' },
];
const TRACE_HISTORY_KEY = 'dsa.agentTrace.history.v1';
const TRACE_HISTORY_LIMIT = 10;

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
};

/* ═══════════════════════════════════════════════
   Helpers
   ═══════════════════════════════════════════════ */

const formatDuration = (duration?: number): string => {
  if (duration == null) return '-';
  return `${duration.toFixed(2)}s`;
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
  return !/(选股|筛选|推荐.*股|股票池|组合|配置|分配仓位|仓位分配|买什么|挑.*股)/.test(message);
};

const toStringList = (value: unknown): string[] => {
  if (!value) return [];
  if (Array.isArray(value)) return value.map((item) => String(item)).filter(Boolean);
  if (typeof value === 'string') return value ? [value] : [];
  return [String(value)];
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

const createEmptyTraceResult = (): AgentTraceRunResponse => ({
  success: false, session_id: '', content: '', error: null,
  total_steps: 0, total_tokens: 0, provider: '', model: '', mode: 'planning_execute',
  events: [], tool_calls: [], planner: null, agent_user_context: null,
  context_summary: null, debate: null, stock_selection: null, risk_gate: null, artifact_dir: null,
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

const loadTraceHistory = (): TraceHistoryItem[] => {
  try {
    const raw = window.localStorage.getItem(TRACE_HISTORY_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.slice(0, TRACE_HISTORY_LIMIT) as TraceHistoryItem[] : [];
  } catch { return []; }
};

const saveTraceHistory = (items: TraceHistoryItem[]) => {
  window.localStorage.setItem(TRACE_HISTORY_KEY, JSON.stringify(items.slice(0, TRACE_HISTORY_LIMIT)));
};

const persistTraceHistory = (items: TraceHistoryItem[], next: TraceHistoryItem): TraceHistoryItem[] => {
  const merged = [next, ...items.filter((item) => item.id !== next.id)].slice(0, TRACE_HISTORY_LIMIT);
  saveTraceHistory(merged);
  return merged;
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
  strategies: string[];
  tags: string[];
  reason: string;
  score?: number;
  latestDate: string;
  metrics: Record<string, unknown>;
  reasonDimensions: CandidateReasonDimension[];
};

type ExpertOpinionDisplay = {
  expertName: string;
  dimension: string;
  label: string;
  verdict: string;
  confidence?: number;
  summary: string;
  supportingEvidence: string[];
  missingEvidence: string[];
  riskFlags: string[];
};

type CandidateDimensionGroup = {
  dimension: string;
  label: string;
  candidates: Array<{
    candidate: DisplayCandidate;
    details: string[];
  }>;
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
  ['fallback_seed_pool', '固定种子池'],
  ['user_seed', '用户输入'],
];

const EXPERT_LABELS: Record<string, string> = {
  market_regime_expert: '市场环境专家',
  candidate_discovery_expert: '候选发现专家',
  technical_expert: '技术结构专家',
  capital_chip_expert: '资金筹码专家',
  news_sentiment_expert: '消息情绪专家',
  fundamental_expert: '基本面专家',
  portfolio_risk_expert: '组合风控专家',
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
  other: '其他候选',
};

const DIMENSION_GROUP_ORDER = ['strategy', 'technical', 'capital', 'sentiment', 'message', 'fundamental', 'market_regime', 'portfolio_risk', 'other'];

const displayStrategyName = (name: string): string => STRATEGY_LABELS[name] || name;

const displaySourceName = (source: string): string => {
  if (source === 'alphasift:multi_strategy') return 'AlphaSift 多策略共振';
  if (source === 'sequoia:multi_strategy') return 'Sequoia 多策略共振';
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

const dimensionTone = (dimension: string): string => {
  if (dimension === 'strategy') return 'border-blue-100 bg-blue-50 text-blue-700';
  if (dimension === 'technical') return 'border-emerald-100 bg-emerald-50 text-emerald-700';
  if (dimension === 'capital') return 'border-amber-100 bg-amber-50 text-amber-700';
  if (dimension === 'sentiment') return 'border-purple-100 bg-purple-50 text-purple-700';
  if (dimension === 'message') return 'border-sky-100 bg-sky-50 text-sky-700';
  if (dimension === 'fundamental') return 'border-stone-200 bg-stone-50 text-stone-700';
  if (dimension === 'market_regime') return 'border-red-100 bg-red-50 text-red-700';
  if (dimension === 'portfolio_risk') return 'border-slate-200 bg-slate-50 text-slate-700';
  return 'border-[#e8e8e3] bg-[#f5f5f0] text-[#666]';
};

const verdictTone = (verdict: string): string => {
  if (['support', 'supports_primary', 'open'].includes(verdict)) return 'bg-emerald-50 text-emerald-700';
  if (['caution', 'mixed', 'wait', 'monitor'].includes(verdict)) return 'bg-amber-50 text-amber-700';
  if (['oppose', 'supports_opposing', 'reject'].includes(verdict)) return 'bg-red-50 text-red-700';
  return 'bg-[#f0f0ec] text-[#777]';
};

const displayVerdict = (verdict: string): string => {
  const mapping: Record<string, string> = {
    support: '支持',
    neutral: '中性',
    caution: '谨慎',
    oppose: '反对',
    insufficient_data: '证据不足',
    open: '可开仓',
    wait: '等待',
    monitor: '监控',
    reject: '否决',
  };
  return mapping[verdict] || verdict.replace(/_/g, ' ');
};

const displayEventMaturity = (maturity: string): string => {
  const mapping: Record<string, string> = {
    breaking: '突发观察',
    developing: '等待验证',
    confirmed: '已验证',
  };
  return mapping[maturity] || maturity || '未知';
};

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
  if (source === 'fallback_seed_pool') add('strategy', '策略', '固定种子池兜底，仅用于保证后续取证链路可运行');
  return result.slice(0, 5);
};

const normalizeCandidate = (item: Record<string, unknown>): DisplayCandidate | null => {
  const code = String(item.code || item.stock_code || item.symbol || '').trim();
  if (!code) return null;
  const metrics = asRecord(item.metrics) || {};
  const score = typeof item.signal_score === 'number' ? item.signal_score : Number(item.signal_score);
  return {
    code,
    name: String(item.name || item.stock_name || ''),
    source: String(item.source || item.candidate_source || ''),
    recallSources: toStringList(item.recall_sources),
    strategies: toStringList(item.matched_strategies || item.strategies).map(displayStrategyName),
    tags: toStringList(item.strategy_tags).filter((tag) => !STRATEGY_ONLY_TAGS.has(tag)).map(displayStrategyName),
    reason: displayReasonText(String(item.reason || item.candidate_reason || item.entry_reason || '')),
    score: Number.isFinite(score) ? score : undefined,
    latestDate: String(item.latest_date || item.date || ''),
    metrics,
    reasonDimensions: normalizeReasonDimensions(item),
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
    current.strategies = Array.from(new Set([...current.strategies, ...candidate.strategies]));
    current.tags = Array.from(new Set([...current.tags, ...candidate.tags]));
    current.reason = current.reason || candidate.reason;
    current.score = Math.max(current.score ?? 0, candidate.score ?? 0) || current.score || candidate.score;
    current.latestDate = current.latestDate || candidate.latestDate;
    current.metrics = { ...candidate.metrics, ...current.metrics };
    current.reasonDimensions = [...current.reasonDimensions];
    candidate.reasonDimensions.forEach((entry) => {
      if (!current.reasonDimensions.some((item) => item.dimension === entry.dimension && item.detail === entry.detail)) {
        current.reasonDimensions.push(entry);
      }
    });
  });
  return Array.from(byCode.values());
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
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  return asRecord(finalReport.expert_state) || asRecord(selCtx.expert_state);
};

const extractExpertOpinions = (stockSelection: Record<string, unknown> | null): ExpertOpinionDisplay[] => {
  const expertState = extractExpertState(stockSelection);
  const opinions = asRecord(expertState?.expert_opinions);
  if (!opinions) return [];
  return Object.entries(opinions).map(([expertName, raw]) => {
    const item = asRecord(raw) || {};
    const dimension = String(item.dimension || expertName);
    return {
      expertName,
      dimension,
      label: EXPERT_LABELS[expertName] || String(item.expert_name || expertName).replace(/_/g, ' '),
      verdict: String(item.verdict || 'insufficient_data'),
      confidence: typeof item.confidence === 'number' ? item.confidence : undefined,
      summary: String(item.summary || ''),
      supportingEvidence: toStringList(item.supporting_evidence),
      missingEvidence: toStringList(item.missing_evidence),
      riskFlags: toStringList(item.risk_flags),
    };
  });
};

const getSelectionOrchestrationMode = (stockSelection: Record<string, unknown> | null): string => {
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  return String(finalReport.orchestration_mode || selCtx.orchestration_mode || 'legacy');
};

const buildCandidateDimensionGroups = (candidates: DisplayCandidate[]): CandidateDimensionGroup[] => {
  const byDimension = new Map<string, CandidateDimensionGroup>();
  const ensure = (dimension: string, label?: string): CandidateDimensionGroup => {
    const key = dimension || 'other';
    const existing = byDimension.get(key);
    if (existing) return existing;
    const group = {
      dimension: key,
      label: label || DIMENSION_GROUP_LABELS[key] || key,
      candidates: [],
    };
    byDimension.set(key, group);
    return group;
  };

  candidates.forEach((candidate) => {
    const entries = candidate.reasonDimensions.length
      ? candidate.reasonDimensions
      : [{ dimension: 'other', label: '其他', detail: candidate.reason || candidate.source || '-' }];
    entries.forEach((entry) => {
      const group = ensure(entry.dimension, DIMENSION_GROUP_LABELS[entry.dimension] || `${entry.label}候选`);
      const current = group.candidates.find((item) => item.candidate.code === candidate.code);
      const detail = displayReasonText(entry.detail || candidate.reason || '-');
      if (current) {
        if (detail && !current.details.includes(detail)) current.details.push(detail);
      } else {
        group.candidates.push({ candidate, details: detail ? [detail] : [] });
      }
    });
  });

  return Array.from(byDimension.values()).sort((a, b) => {
    const ai = DIMENSION_GROUP_ORDER.indexOf(a.dimension);
    const bi = DIMENSION_GROUP_ORDER.indexOf(b.dimension);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
};

const hasCandidateDimension = (groups: CandidateDimensionGroup[], dimensions: string[]): boolean => (
  groups.some((group) => dimensions.includes(group.dimension) && group.candidates.length > 0)
);


/* ═══════════════════════════════════════════════
   Timeline Step UI Primitives
   ═══════════════════════════════════════════════ */

type StepStatus = 'pending' | 'active' | 'done' | 'error';

const StepIcon: React.FC<{ status: StepStatus }> = ({ status }) => {
  if (status === 'done') return <Check className="h-3.5 w-3.5 text-white" />;
  if (status === 'active') return <Loader2 className="h-3.5 w-3.5 animate-spin text-white" />;
  if (status === 'error') return <AlertTriangle className="h-3.5 w-3.5 text-white" />;
  return <Circle className="h-2.5 w-2.5 text-[#999]" />;
};

const StepNode: React.FC<{ status: StepStatus }> = ({ status }) => (
  <div className={cn(
    'relative z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2',
    status === 'done' && 'border-emerald-500 bg-emerald-500',
    status === 'active' && 'border-[#1a1a1a] bg-[#1a1a1a]',
    status === 'error' && 'border-red-500 bg-red-500',
    status === 'pending' && 'border-[#e8e8e3] bg-white',
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
  const [open, setOpen] = useState(defaultOpen);
  const hasContent = Boolean(children);

  useEffect(() => {
    if (defaultOpen) setOpen(true);
  }, [defaultOpen]);

  return (
    <div className="relative flex gap-4">
      {/* Vertical line */}
      <div className="flex flex-col items-center">
        <StepNode status={status} />
        {!isLast && <div className="w-px flex-1 bg-[#e8e8e3]" />}
      </div>

      {/* Content */}
      <div className={cn('min-w-0 flex-1 pb-8', isLast && 'pb-0')}>
        <div className="flex items-baseline gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-[#999]">{label}</span>
          <h3 className="text-sm font-medium text-[#1a1a1a]">{title}</h3>
        </div>

        {narrative ? (
          <p className="mt-1.5 text-[13px] leading-relaxed text-[#555]">{narrative}</p>
        ) : null}

        {hasContent ? (
          <>
            <button
              type="button"
              onClick={() => setOpen(!open)}
              className="mt-2 flex items-center gap-1 text-xs text-[#999] transition-colors hover:text-[#555]"
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

const inputClass = 'h-9 w-full rounded-xl border border-[#e8e8e3] bg-white px-3 text-sm text-[#1a1a1a] outline-none transition-all placeholder:text-[#999] focus:border-[#ccc] focus:ring-2 focus:ring-[#f0f0ec]';
const selectClass = inputClass;
const textareaClass = 'min-h-20 w-full resize-y rounded-xl border border-[#e8e8e3] bg-white px-3 py-2 text-sm text-[#1a1a1a] outline-none transition-all placeholder:text-[#999] focus:border-[#ccc] focus:ring-2 focus:ring-[#f0f0ec]';

/* ═══════════════════════════════════════════════
   Main Page Component
   ═══════════════════════════════════════════════ */

const AgentTracePage: React.FC = () => {
  const [message, setMessage] = useState(DEFAULT_PROMPT);
  const [stockCode, setStockCode] = useState(DEFAULT_STOCK_CODE);
  const [accounts, setAccounts] = useState<PortfolioAccountItem[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [reportIntent, setReportIntent] = useState('auto');
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

  useEffect(() => {
    document.title = 'Agent Trace';
    setHistoryItems(loadTraceHistory());
  }, []);

  useEffect(() => {
    let alive = true;
    portfolioApi.getAccounts()
      .then((response) => { if (alive) { setAccounts(response.accounts); if (response.accounts.length === 1) setSelectedAccountId(String(response.accounts[0].id)); } })
      .catch(() => { if (alive) setAccounts([]); });
    return () => { alive = false; };
  }, []);

  const selectedTool = result?.tool_calls[selectedToolIndex] ?? null;

  const handleRun = async () => {
    const stockCodeToSend = shouldSendStockCode(message, stockCode) ? stockCode.trim() : undefined;
    setRunning(true);
    setTraceStatus('running');
    setStatusMessage('正在准备上下文...');
    setError(null);
    setSelectedToolIndex(0);
    setResult(createEmptyTraceResult());
    try {
      const response = await agentApi.traceStream({
        message,
        account_id: selectedAccountId ? Number(selectedAccountId) : undefined,
        stock_code: stockCodeToSend,
        inject_portfolio_context: injectPortfolioContext,
        analysis_mode: 'planning_execute',
        report_intent: reportIntent === 'auto' ? undefined : reportIntent,
        risk_preference: riskPreference,
        trading_horizon: tradingHorizon,
        max_single_position_pct: parseOptionalPercent(maxSinglePositionPct),
        max_total_equity_exposure_pct: parseOptionalPercent(maxTotalEquityExposurePct),
        max_acceptable_drawdown_pct: parseOptionalPercent(maxAcceptableDrawdownPct),
        default_stop_loss_pct: parseOptionalPercent(defaultStopLossPct),
        investor_notes: investorNotes.trim() || undefined,
      });
      await consumeTraceStream(response);
    } catch (err) {
      setError(getParsedApiError(err));
      setTraceStatus('error');
      setStatusMessage(err instanceof Error ? err.message : 'Trace 运行失败');
    } finally {
      setRunning(false);
    }
  };

  const handleSelectHistory = (item: TraceHistoryItem) => {
    setResult(item.result);
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
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), session_id: String(event.session_id || prev?.session_id || ''), context_summary: asRecord(event.context_summary), agent_user_context: asRecord(event.agent_user_context) }));
      return;
    }
    if (type === 'planner_ready') {
      setStatusMessage('执行计划已生成');
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), session_id: String(event.session_id || prev?.session_id || ''), planner: asRecord(event.planner) }));
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
      setResult((prev) => ({ ...(prev || createEmptyTraceResult()), events: [...(prev?.events || []), eventToTraceEvent(event)] }));
      return;
    }
    if (type === 'done') {
      setTraceStatus(event.success ? 'done' : 'error');
      setStatusMessage(event.success ? '分析完成' : String(event.error || '失败'));
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
          stock_selection: asRecord(event.stock_selection) || cur.stock_selection,
          risk_gate: asRecord(event.risk_gate) || cur.risk_gate,
          artifact_dir: typeof event.artifact_dir === 'string' ? event.artifact_dir : cur.artifact_dir,
        };
        setHistoryItems((items) => persistTraceHistory(items, {
          id: next.session_id || `${Date.now()}`, createdAt: new Date().toISOString(),
          message, stockCode: shouldSendStockCode(message, stockCode) ? stockCode.trim() : '',
          accountId: selectedAccountId ? Number(selectedAccountId) : undefined,
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
          message, stockCode: shouldSendStockCode(message, stockCode) ? stockCode.trim() : '',
          accountId: selectedAccountId ? Number(selectedAccountId) : undefined, status: 'error', result: next,
        }));
        return next;
      });
    }
  };

  /* ─── Derived data for timeline ─── */
  const planner = useMemo(() => asRecord(result?.planner), [result?.planner]);
  const debate = useMemo(() => asRecord(result?.debate), [result?.debate]);
  const riskPayload = useMemo(() => asRecord(result?.risk_gate), [result?.risk_gate]);
  const stockSelection = useMemo(() => asRecord(result?.stock_selection), [result?.stock_selection]);
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
    <div className="min-h-screen bg-[#f5f5f0]" style={{ fontFamily: '"Noto Serif SC", "Source Han Serif SC", Georgia, "Times New Roman", serif' }}>
      <div className="mx-auto max-w-[1100px] px-6 py-10">
        {/* Header */}
        <header className="mb-8">
          <h1 className="text-lg font-semibold text-[#1a1a1a]">Agent Trace</h1>
          <p className="mt-1 text-sm text-[#777]">从用户问题出发，观察 Agent 如何逐层取证、辩论和裁决。</p>
        </header>

        {/* Input area */}
        <div className="mb-8 rounded-xl border border-[#e8e8e3] bg-white p-5 shadow-sm">
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
              <button
                type="button"
                onClick={() => setShowConfig(!showConfig)}
                className="text-[11px] text-[#999] hover:text-[#555]"
              >
                {showConfig ? '收起配置' : '展开配置'}
              </button>
            </div>
          </div>

          {showConfig ? (
            <div className="mt-4 border-t border-[#eeeee9] pt-4">
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">股票代码</span>
                  <input value={stockCode} onChange={(e) => setStockCode(e.target.value)} className={inputClass} placeholder="600519" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">账户</span>
                  <select value={selectedAccountId} onChange={(e) => setSelectedAccountId(e.target.value)} className={selectClass}>
                    <option value="">全部</option>
                    {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">报告意图</span>
                  <select value={reportIntent} onChange={(e) => setReportIntent(e.target.value)} className={selectClass}>
                    {REPORT_INTENT_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">风险偏好</span>
                  <select value={riskPreference} onChange={(e) => setRiskPreference(e.target.value)} className={selectClass}>
                    {RISK_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">持有周期</span>
                  <select value={tradingHorizon} onChange={(e) => setTradingHorizon(e.target.value)} className={selectClass}>
                    {HORIZON_OPTIONS.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">单票上限%</span>
                  <input value={maxSinglePositionPct} onChange={(e) => setMaxSinglePositionPct(e.target.value)} className={inputClass} placeholder="20" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">总权益上限%</span>
                  <input value={maxTotalEquityExposurePct} onChange={(e) => setMaxTotalEquityExposurePct(e.target.value)} className={inputClass} placeholder="80" />
                </label>
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">最大回撤%</span>
                  <input value={maxAcceptableDrawdownPct} onChange={(e) => setMaxAcceptableDrawdownPct(e.target.value)} className={inputClass} placeholder="15" />
                </label>
              </div>
              <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
                <label className="block">
                  <span className="mb-1 block text-[11px] text-[#999]">默认止损%</span>
                  <input value={defaultStopLossPct} onChange={(e) => setDefaultStopLossPct(e.target.value)} className={inputClass} placeholder="8" />
                </label>
                <label className="col-span-2 block">
                  <span className="mb-1 block text-[11px] text-[#999]">画像备注</span>
                  <input value={investorNotes} onChange={(e) => setInvestorNotes(e.target.value)} className={inputClass} placeholder="偏长期持有" />
                </label>
                <label className="flex items-end gap-2 pb-1">
                  <input type="checkbox" checked={injectPortfolioContext} onChange={(e) => setInjectPortfolioContext(e.target.checked)} className="h-4 w-4 rounded border-[#e8e8e3]" />
                  <span className="text-xs text-[#777]">注入持仓</span>
                </label>
              </div>
            </div>
          ) : null}

          {stockCode.trim() && !shouldSendStockCode(message, stockCode) ? (
            <p className="mt-2 text-xs text-amber-600">当前问题像选股/组合配置，将不会发送该股票代码。</p>
          ) : null}
        </div>

        {error ? <div className="mb-6"><ApiErrorAlert error={error} /></div> : null}

        {/* History pills */}
        {historyItems.length ? (
          <div className="mb-8">
            <div className="mb-2 flex items-center justify-between">
              <span className="flex items-center gap-1.5 text-xs text-[#999]"><History className="h-3 w-3" /> 历史</span>
              <button type="button" onClick={() => { saveTraceHistory([]); setHistoryItems([]); }} className="flex items-center gap-1 text-[11px] text-[#999] hover:text-red-500">
                <Trash2 className="h-3 w-3" /> 清空
              </button>
            </div>
            <div className="flex gap-2 overflow-x-auto pb-1">
              {historyItems.map((item) => (
                <button key={`${item.id}-${item.createdAt}`} type="button" onClick={() => handleSelectHistory(item)}
                  className="flex shrink-0 items-center gap-2 rounded-full border border-[#e8e8e3] bg-white px-3 py-1.5 text-xs transition-all hover:border-[#ddd] hover:shadow-sm">
                  <span className={cn('h-1.5 w-1.5 rounded-full', item.status === 'success' ? 'bg-emerald-500' : 'bg-red-500')} />
                  <span className="font-mono text-[#333]">{item.stockCode || '选股'}</span>
                  <span className="max-w-[140px] truncate text-[#999]">{item.message}</span>
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {/* Status */}
        {traceStatus !== 'idle' ? (
          <div className="mb-6 flex items-center gap-2 text-sm text-[#777]">
            {traceStatus === 'running' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            {traceStatus === 'done' ? <Check className="h-3.5 w-3.5 text-emerald-500" /> : null}
            {traceStatus === 'error' ? <AlertTriangle className="h-3.5 w-3.5 text-red-500" /> : null}
            <span>{statusMessage}</span>
            {result?.session_id ? <span className="ml-auto font-mono text-[11px] text-[#bbb]">{result.session_id}</span> : null}
          </div>
        ) : null}

        {/* Timeline */}
        {result ? (
          <div className="rounded-xl border border-[#e8e8e3] bg-white px-6 py-8 shadow-sm">
            <TimelineStep
              label="L1"
              title="数据与候选池"
              status={getLayerStatus(Boolean(result.tool_calls.length || stockSelection))}
              narrative={buildL1Narrative(result, stockSelection)}
              defaultOpen={Boolean(stockSelection || hasDiscoveryCandidates)}
            >
              <L1Detail result={result} stockSelection={stockSelection} />
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
              title="复盘进化"
              status={getLayerStatus(Boolean(result.artifact_dir))}
              narrative={result.artifact_dir ? `Trace 已落盘：${result.artifact_dir}` : '等待 Trace 完成后落盘。'}
              isLast={true}
            />

            {/* Final Output */}
            {result.content ? (
              <div className="mt-8 border-t border-[#eeeee9] pt-6">
                <h3 className="mb-3 text-sm font-medium text-[#1a1a1a]">最终报告</h3>
                {result.error ? (
                  <div className="mb-3 rounded-lg bg-red-50 p-3 text-sm text-red-700">{result.error}</div>
                ) : null}
                <div className="max-h-[500px] overflow-auto rounded-lg border border-[#eeeee9] bg-white p-5">
                  <div className="max-w-none text-sm leading-relaxed text-[#333] [&_h1]:text-lg [&_h1]:font-semibold [&_h1]:text-[#1a1a1a] [&_h2]:text-base [&_h2]:font-semibold [&_h2]:text-[#1a1a1a] [&_h3]:text-sm [&_h3]:font-semibold [&_h3]:text-[#1a1a1a] [&_h4]:text-sm [&_h4]:font-medium [&_h4]:text-[#333] [&_strong]:font-semibold [&_strong]:text-[#1a1a1a] [&_a]:text-blue-600 [&_a]:underline [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_li]:my-1 [&_p]:my-2 [&_blockquote]:border-l-2 [&_blockquote]:border-[#e8e8e3] [&_blockquote]:pl-4 [&_blockquote]:text-[#555] [&_code]:rounded [&_code]:bg-[#f0f0ec] [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-xs [&_pre]:rounded-lg [&_pre]:bg-[#f5f5f0] [&_pre]:p-3 [&_table]:w-full [&_th]:border [&_th]:border-[#e8e8e3] [&_th]:bg-[#f5f5f0] [&_th]:px-3 [&_th]:py-1.5 [&_th]:text-left [&_th]:text-xs [&_th]:font-medium [&_td]:border [&_td]:border-[#e8e8e3] [&_td]:px-3 [&_td]:py-1.5 [&_td]:text-xs">
                    <Markdown remarkPlugins={[remarkGfm]}>{result.content}</Markdown>
                  </div>
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <div className="rounded-xl border border-dashed border-[#e8e8e3] bg-white px-6 py-16 text-center">
            <p className="text-sm text-[#999]">输入问题并点击「运行」，观察 Agent 的完整推理链路。</p>
          </div>
        )}
      </div>
    </div>
  );
};


/* ═══════════════════════════════════════════════
   Layer Detail Components
   ═══════════════════════════════════════════════ */

const L1Detail: React.FC<{ result: AgentTraceRunResponse; stockSelection: Record<string, unknown> | null }> = ({ result, stockSelection }) => {
  const selCtx = asRecord(stockSelection?.selection_context) || {};
  const stages = asRecord(selCtx.stages) || {};
  const finalReport = asRecord(stockSelection?.final_report_json) || {};
  const discoverySummary = asRecord(asRecord(finalReport.candidate_discovery)?.summary) || asRecord(asRecord(stages.candidate_discovery)?.summary) || {};
  const candidateCodes = toStringList(discoverySummary.candidate_codes);
  const screeningSummary = asRecord(asRecord(finalReport.candidate_screening)?.summary) || asRecord(asRecord(stages.candidate_screening)?.summary) || {};
  const deepTargets = toStringList(screeningSummary.deep_dive_targets);
  const candidates = extractDiscoveryCandidates(result, stockSelection);
  const expertState = extractExpertState(stockSelection);
  const expertOpinions = extractExpertOpinions(stockSelection);
  const orchestrationMode = getSelectionOrchestrationMode(stockSelection);
  const dimensionGroups = buildCandidateDimensionGroups(candidates);
  const hasSentimentCandidates = hasCandidateDimension(dimensionGroups, ['sentiment', 'message']);
  const discoverySteps = extractDiscoverySteps(result);
  const eventWatches = extractEventImpactWatches(result);

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-[#eeeee9] bg-[#fbfbf8] p-4">
        <div className="flex flex-wrap items-center gap-2">
          <BrainCircuit className="h-4 w-4 text-[#777]" />
          <span className="text-sm font-semibold text-[#1a1a1a]">多专家选股状态</span>
          <span className={cn(
            'rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
            orchestrationMode === 'expert_graph' ? 'bg-[#1a1a1a] text-white' : 'bg-[#f0f0ec] text-[#777]',
          )}>
            {orchestrationMode}
          </span>
          {expertState?.status ? <span className="text-[11px] text-[#999]">status: {String(expertState.status)}</span> : null}
        </div>
        {expertOpinions.length ? (
          <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {expertOpinions.map((opinion) => (
              <div key={opinion.expertName} className="rounded-lg border border-white bg-white p-3 shadow-[0_1px_0_rgba(0,0,0,0.03)]">
                <div className="mb-2 flex items-center gap-2">
                  <span className="text-xs font-semibold text-[#333]">{opinion.label}</span>
                  <span className={cn('ml-auto rounded-full px-2 py-0.5 text-[10px] font-medium', verdictTone(opinion.verdict))}>
                    {displayVerdict(opinion.verdict)}
                  </span>
                </div>
                <p className="line-clamp-2 text-[12px] leading-relaxed text-[#555]">{opinion.summary || '-'}</p>
                <div className="mt-2 flex flex-wrap gap-1.5 text-[10px]">
                  <span className="rounded-md bg-[#f5f5f0] px-2 py-0.5 text-[#777]">置信 {formatConfidence(opinion.confidence)}</span>
                  {opinion.supportingEvidence.length ? <span className="rounded-md bg-emerald-50 px-2 py-0.5 text-emerald-700">支持 {opinion.supportingEvidence.length}</span> : null}
                  {opinion.missingEvidence.length ? <span className="rounded-md bg-amber-50 px-2 py-0.5 text-amber-700">缺口 {opinion.missingEvidence.length}</span> : null}
                  {opinion.riskFlags.length ? <span className="rounded-md bg-red-50 px-2 py-0.5 text-red-700">风险 {opinion.riskFlags.length}</span> : null}
                </div>
              </div>
            ))}
          </div>
        ) : (
          <p className="mt-2 text-xs text-[#777]">
            {orchestrationMode === 'expert_graph'
              ? '多专家模式已开启，但本次 Trace 没有返回 expert_state。请重新运行选股链路，或检查后端是否已重启并生成 selection_expert_graph_done 事件。'
              : '当前为 legacy 选股链路，尚未输出专家图谱。设置 AGENT_ORCHESTRATION_MODE=expert_graph 并重启后端后会显示专家意见。'}
          </p>
        )}
      </div>

      {discoverySteps.length ? (
        <div>
          <p className="mb-2 text-[11px] font-medium uppercase tracking-wider text-[#999]">候选来源审计</p>
          <div className="flex flex-wrap gap-1.5">
            {discoverySteps.map((step, i) => {
              const source = String(step.source || '-');
              const status = String(step.status || '-');
              const count = typeof step.count === 'number' ? step.count : undefined;
              return (
                <span key={`${source}-${i}`} className="rounded-full border border-[#e8e8e3] bg-white px-2.5 py-1 text-[11px] text-[#666]">
                  {displaySourceName(source)} · {status}{count != null ? ` · ${count}` : ''}
                </span>
              );
            })}
          </div>
        </div>
      ) : null}

      {eventWatches.length ? (
        <div>
          <p className="mb-3 text-[11px] font-medium uppercase tracking-wider text-[#999]">消息/事件观察 ({eventWatches.length})</p>
          <div className="grid gap-3 lg:grid-cols-2">
            {eventWatches.map((event) => {
              const confirmedCount = event.validationMatches.filter((match) => match.status === 'confirmed').length;
              return (
                <div key={event.eventId || event.title} className={cn('rounded-lg border p-3', dimensionTone(event.maturity === 'confirmed' ? 'sentiment' : 'message'))}>
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <span className="rounded-full bg-white/80 px-2 py-0.5 text-[10px] font-semibold">{displayEventMaturity(event.maturity)}</span>
                    {event.eventType ? <span className="rounded-full bg-white/60 px-2 py-0.5 text-[10px]">{event.eventType}</span> : null}
                    {event.validationWindowDays ? <span className="text-[10px] text-[#777]">{event.validationWindowDays} 日验证窗口</span> : null}
                    {confirmedCount ? <span className="ml-auto rounded-full bg-emerald-50 px-2 py-0.5 text-[10px] text-emerald-700">验证 {confirmedCount}</span> : null}
                  </div>
                  <div className="space-y-1.5">
                    <p className="text-xs font-semibold leading-relaxed text-[#1a1a1a]">{event.title}</p>
                    {event.snippet ? <p className="line-clamp-2 text-[11px] leading-relaxed text-[#555]">{event.snippet}</p> : null}
                    {event.watchThemes.length ? (
                      <div className="flex flex-wrap gap-1">
                        {event.watchThemes.slice(0, 6).map((theme) => (
                          <span key={theme} className="rounded-md bg-white/75 px-2 py-0.5 text-[10px] text-[#666]">{theme}</span>
                        ))}
                      </div>
                    ) : null}
                    {event.impactVariables.length ? (
                      <p className="text-[10px] leading-relaxed text-[#777]">影响变量：{event.impactVariables.slice(0, 5).join('、')}</p>
                    ) : null}
                    {event.validationMatches.length ? (
                      <div className="mt-2 space-y-1 rounded-md bg-white/70 p-2">
                        {event.validationMatches.slice(0, 4).map((match) => (
                          <div key={`${event.eventId}-${match.theme}`} className="text-[10px] leading-relaxed text-[#666]">
                            <span className="font-semibold">{match.theme || '主题'}</span>
                            <span className="mx-1">·</span>
                            <span>{match.status === 'confirmed' ? `已验证 ${match.resultCount} 条` : '观察中，未形成个股候选'}</span>
                            {match.titles.length ? <span> · {match.titles.join('；')}</span> : null}
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {(event.source || event.publishedDate) ? (
                      <p className="text-[10px] text-[#999]">{[event.source, event.publishedDate].filter(Boolean).join(' · ')}</p>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {dimensionGroups.length ? (
        <div>
          <p className="mb-3 text-[11px] font-medium uppercase tracking-wider text-[#999]">按专家维度分组的候选</p>
          <div className="grid gap-3 lg:grid-cols-2">
            {dimensionGroups.map((group) => (
              <div key={group.dimension} className={cn('rounded-lg border p-3', dimensionTone(group.dimension))}>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-xs font-semibold">{group.label}</span>
                  <span className="rounded-full bg-white/70 px-2 py-0.5 text-[10px]">{group.candidates.length} 只</span>
                </div>
                <div className="space-y-2">
                  {group.candidates.slice(0, 6).map(({ candidate, details }) => (
                    <div key={`${group.dimension}-${candidate.code}`} className="rounded-md bg-white/75 px-2.5 py-2">
                      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                        <span className="font-mono text-xs font-semibold text-[#1a1a1a]">{candidate.code}</span>
                        {candidate.name ? <span className="text-xs font-medium text-[#333]">{candidate.name}</span> : null}
                        {candidate.score != null ? <span className="text-[10px] text-[#777]">评分 {formatMetricValue(candidate.score)}</span> : null}
                      </div>
                      {details.length ? (
                        <p className="mt-1 text-[11px] leading-relaxed text-[#555]">{details.slice(0, 2).join('；')}</p>
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
                  <span className="rounded-full bg-white/70 px-2 py-0.5 text-[10px]">0 只</span>
                </div>
                <div className="rounded-md bg-white/75 px-2.5 py-2">
                  <p className="text-[11px] leading-relaxed text-[#555]">
                    本次候选召回没有命中消息/情绪来源；当前只会在强势板块、用户输入或后续情绪工具接入后生成这类候选。
                  </p>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      {candidates.length ? (
        <div>
          <p className="mb-3 text-[11px] font-medium uppercase tracking-wider text-[#999]">候选池列表 ({candidates.length})</p>
          <div className="grid gap-3">
            {candidates.map((c) => {
              const sources = c.recallSources.length ? c.recallSources : c.source ? [c.source] : [];
              const labels = [...c.strategies, ...c.tags].filter((label, index, arr) => label && arr.indexOf(label) === index);
              return (
                <div key={c.code} className="rounded-lg border border-[#eeeee9] bg-[#fbfbf8] p-4">
                  <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                    <span className="font-mono text-sm font-semibold text-[#1a1a1a]">{c.code}</span>
                    {c.name ? <span className="text-sm font-medium text-[#333]">{c.name}</span> : null}
                    {c.score != null ? <span className="rounded-full bg-[#1a1a1a] px-2 py-0.5 text-[10px] font-medium text-white">评分 {formatMetricValue(c.score)}</span> : null}
                    {c.latestDate ? <span className="text-[11px] text-[#999]">{c.latestDate}</span> : null}
                  </div>

                  {sources.length ? (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {sources.map((source) => (
                        <span key={source} className="rounded-md bg-white px-2 py-0.5 text-[11px] text-[#666]">{displaySourceName(source)}</span>
                      ))}
                    </div>
                  ) : null}

                  {labels.length ? (
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {labels.map((label) => (
                        <span key={label} className="rounded-md bg-[#f0f0ec] px-2 py-0.5 text-[11px] text-[#555]">{label}</span>
                      ))}
                    </div>
                  ) : null}

                  {c.reasonDimensions.length ? (
                    <div className="mt-3 space-y-1.5">
                      {c.reasonDimensions.map((item, i) => (
                        <div key={`${c.code}-${item.dimension}-${i}`} className={cn('rounded-md border px-2.5 py-1.5 text-[12px] leading-relaxed', dimensionTone(item.dimension))}>
                          <span className="mr-2 font-semibold">{item.label}</span>
                          <span>{displayReasonText(item.detail)}</span>
                        </div>
                      ))}
                    </div>
                  ) : c.reason ? (
                    <p className="mt-3 text-[12px] leading-relaxed text-[#666]">{c.reason}</p>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      ) : candidateCodes.length ? (
        <div>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-[#999]">候选池 ({candidateCodes.length})</p>
          <div className="flex flex-wrap gap-1.5">
            {candidateCodes.map((code) => (
              <span key={code} className="rounded-md bg-[#f0f0ec] px-2 py-0.5 font-mono text-xs text-[#333]">{code}</span>
            ))}
          </div>
        </div>
      ) : null}

      {deepTargets.length ? (
        <div>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-[#999]">深挖标的</p>
          <div className="flex flex-wrap gap-1.5">
            {deepTargets.map((code) => (
              <span key={code} className="rounded-md bg-emerald-50 px-2 py-0.5 font-mono text-xs text-emerald-700">{code}</span>
            ))}
          </div>
        </div>
      ) : null}
      {/* Stage status */}
      {Object.keys(stages).length ? (
        <div>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-[#999]">流水线阶段</p>
          <div className="flex flex-wrap gap-2">
            {Object.entries(stages).map(([key, val]) => {
              const s = asRecord(val) || {};
              const status = String(s.status || '-');
              return (
                <span key={key} className="flex items-center gap-1.5 rounded-full border border-[#e8e8e3] px-2.5 py-1 text-[11px]">
                  <span className={cn('h-1.5 w-1.5 rounded-full', status === 'ok' ? 'bg-emerald-500' : status === 'partial' ? 'bg-amber-500' : 'bg-[#ccc]')} />
                  <span className="text-[#555]">{key.replace(/_/g, ' ')}</span>
                </span>
              );
            })}
          </div>
        </div>
      ) : null}
      {/* Data tools used */}
      {result.tool_calls.filter((t) => t.tool.startsWith('get_') || t.tool.includes('quote')).length ? (
        <div>
          <p className="mb-1.5 text-[11px] font-medium uppercase tracking-wider text-[#999]">数据工具</p>
          <div className="flex flex-wrap gap-1.5">
            {result.tool_calls.filter((t) => t.tool.startsWith('get_') || t.tool.includes('quote')).map((t, i) => (
              <span key={`${t.tool}-${i}`} className={cn('rounded-md px-2 py-0.5 text-[11px]', t.success ? 'bg-[#f0f0ec] text-[#555]' : 'bg-red-50 text-red-600')}>
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
}> = ({ toolCalls, selectedIndex, onSelect, selectedTool }) => (
  <div className="space-y-3">
    {/* Tool list */}
    <div className="max-h-[300px] overflow-y-auto rounded-lg border border-[#eeeee9]">
      {toolCalls.length ? toolCalls.map((call, i) => (
        <button
          key={`${call.step}-${call.tool}-${i}`}
          type="button"
          onClick={() => onSelect(i)}
          className={cn(
            'flex w-full items-center gap-3 border-b border-[#f2f2ed] px-3 py-2 text-left text-xs transition-colors last:border-0 hover:bg-[#f5f5f0]',
            selectedIndex === i && 'bg-[#f5f5f0]',
          )}
        >
          <span className={cn('h-1.5 w-1.5 shrink-0 rounded-full', call.success ? 'bg-emerald-500' : 'bg-red-500')} />
          <span className="min-w-0 flex-1 truncate font-medium text-[#333]">{call.tool}</span>
          <span className="shrink-0 font-mono text-[#999]">{formatDuration(call.duration)}</span>
        </button>
      )) : (
        <p className="p-3 text-xs text-[#999]">暂无工具调用</p>
      )}
    </div>
    {/* Selected tool detail */}
    {selectedTool ? (
      <div className="rounded-lg border border-[#eeeee9] p-4">
        <div className="mb-3 flex items-center gap-3 text-xs">
          <Wrench className="h-3.5 w-3.5 text-[#999]" />
          <span className="font-medium text-[#333]">{selectedTool.tool}</span>
          <span className={cn('rounded-full px-2 py-0.5 text-[10px] font-medium', selectedTool.success ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700')}>
            {selectedTool.success ? 'OK' : 'FAIL'}
          </span>
          <span className="ml-auto font-mono text-[#999]">step {selectedTool.step} · {formatDuration(selectedTool.duration)}</span>
        </div>
        <JsonViewer data={(selectedTool.arguments || {}) as Record<string, unknown>} maxHeight="160px" />
        {selectedTool.result_preview ? (
          <pre className="mt-3 max-h-[180px] overflow-auto rounded-lg bg-[#f5f5f0] p-3 font-mono text-[11px] leading-5 text-[#555] whitespace-pre-wrap">
            {selectedTool.result_preview}
          </pre>
        ) : null}
      </div>
    ) : null}
  </div>
);

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
            <div key={`${dim}-${i}`} className="flex items-center justify-between rounded-lg border border-[#eeeee9] px-3 py-2">
              <span className="text-xs text-[#333]">{DIMENSION_LABELS[dim] || dim}</span>
              <span className={cn(
                'rounded-full px-2 py-0.5 text-[10px] font-medium',
                verdict === 'supports_primary' && 'bg-emerald-50 text-emerald-700',
                verdict === 'supports_opposing' && 'bg-red-50 text-red-700',
                verdict === 'mixed' && 'bg-amber-50 text-amber-700',
                verdict === 'insufficient_data' && 'bg-[#f0f0ec] text-[#777]',
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
            <span className={cn('h-1.5 w-1.5 rounded-full', t.success ? 'bg-emerald-500' : 'bg-red-500')} />
            <span className="text-[#555]">{t.tool}</span>
            <span className="text-[#999]">{t.success ? '证据可用' : '取证失败'}</span>
          </div>
        ))}
      </div>
    );
  }

  return <p className="text-xs text-[#999]">等待证据进入信号层。</p>;
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
        <div className="rounded-lg bg-[#f5f5f0] p-3">
          <p className="text-[11px] font-medium uppercase tracking-wider text-[#999]">Planner</p>
          <p className="mt-1 text-xs text-[#333]">
            意图: {String(planner.intent || '-')} · 目标: {String(planner.primary_symbol || '-')} · {planner.has_position ? '持仓命中' : '未命中持仓'}
          </p>
        </div>
      ) : null}

      {/* Debate thesis comparison */}
      {(primary.summary || opposing.summary) ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-lg border border-[#eeeee9] p-4">
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-[#999]">主观点</p>
            <p className="text-xs leading-relaxed text-[#333]">{String(primary.summary || '-')}</p>
            {toStringList(primary.evidence).length ? (
              <ul className="mt-2 space-y-0.5 text-[11px] text-[#777]">
                {toStringList(primary.evidence).slice(0, 4).map((e, i) => <li key={i}>· {e}</li>)}
              </ul>
            ) : null}
          </div>
          <div className="rounded-lg border border-[#eeeee9] p-4">
            <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-[#999]">反方</p>
            <p className="text-xs leading-relaxed text-[#333]">{String(opposing.summary || '-')}</p>
            {toStringList(opposing.evidence).length ? (
              <ul className="mt-2 space-y-0.5 text-[11px] text-[#777]">
                {toStringList(opposing.evidence).slice(0, 4).map((e, i) => <li key={i}>· {e}</li>)}
              </ul>
            ) : null}
          </div>
        </div>
      ) : null}

      {/* Judge */}
      {judge.final_action ? (
        <div className="rounded-lg bg-[#1a1a1a] p-4 text-white">
          <div className="flex items-center gap-2">
            <BrainCircuit className="h-4 w-4 text-[#999]" />
            <span className="text-[11px] font-semibold uppercase tracking-wider text-[#999]">Judge 裁决</span>
            <span className="ml-auto rounded-full bg-white/10 px-2.5 py-0.5 text-xs font-medium">{String(judge.final_action)}</span>
          </div>
          <p className="mt-2 text-sm leading-relaxed text-white/85">{String(judge.decision_summary || judge.reason || '-')}</p>
          {reasonPoints.length ? (
            <ul className="mt-3 space-y-1 text-xs text-[#999]">
              {reasonPoints.map((p, i) => <li key={i}>· {p}</li>)}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
};

const L5Detail: React.FC<{ riskPayload: Record<string, unknown> | null }> = ({ riskPayload }) => {
  if (!riskPayload) return <p className="text-xs text-[#999]">尚未生成风控结果。</p>;
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
                <span className={cn('mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full', passed ? 'bg-emerald-500' : 'bg-red-500')} />
                <span className="font-mono text-[#777]">{String(check.rule_id || '-')}</span>
                <span className="flex-1 text-[#555]">{String(check.message || '-')}</span>
              </div>
            );
          })}
        </div>
      ) : null}
      {blockedReasons.length ? (
        <div className="rounded-lg bg-red-50 p-3">
          <p className="text-[11px] font-medium text-red-700">阻断原因</p>
          <ul className="mt-1 space-y-0.5 text-xs text-red-600">
            {blockedReasons.map((r, i) => <li key={i}>· {r}</li>)}
          </ul>
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
  const expertOpinions = extractExpertOpinions(stockSelection);
  const orchestrationMode = getSelectionOrchestrationMode(stockSelection);

  if (candidates.length) {
    const sourceLabels = Array.from(new Set(candidates.flatMap((item) => (
      item.recallSources.length ? item.recallSources : item.source ? [item.source] : []
    )).map(displaySourceName))).slice(0, 3);
    const expertText = expertOpinions.length
      ? `多专家模式已输出 ${expertOpinions.length} 个专家意见；`
      : orchestrationMode === 'expert_graph'
        ? '多专家模式已开启；'
        : '';
    return `${expertText}第一阶段已生成 ${candidates.length} 只候选股票，来源包括${sourceLabels.length ? sourceLabels.join('、') : '多路召回'}；候选会按策略、技术、资金、消息/情绪等维度分组展示。`;
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

export default AgentTracePage;
