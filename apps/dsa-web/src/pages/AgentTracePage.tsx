import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, Braces, ClipboardList, History, Play, Route, Trash2, Wrench } from 'lucide-react';
import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { agentApi, type AgentTraceRunResponse, type AgentTraceToolCall } from '../api/agent';
import { portfolioApi } from '../api/portfolio';
import { ApiErrorAlert, AppPage, Badge, Button, Card, JsonViewer, PageHeader } from '../components/common';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import type { PortfolioAccountItem } from '../types/portfolio';
import { cn } from '../utils/cn';

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

const formatDuration = (duration?: number): string => {
  if (duration == null) return '-';
  return `${duration.toFixed(2)}s`;
};

const getStatusVariant = (success?: boolean): 'success' | 'danger' | 'warning' => {
  if (success === true) return 'success';
  if (success === false) return 'danger';
  return 'warning';
};

const getToolCallKey = (call: AgentTraceToolCall, index: number): string =>
  `${call.step}-${call.tool}-${index}`;

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
  const [statusMessage, setStatusMessage] = useState('未运行');
  const [historyItems, setHistoryItems] = useState<TraceHistoryItem[]>([]);
  const [selectedEventIndex, setSelectedEventIndex] = useState(0);

  useEffect(() => {
    document.title = 'Agent Trace - DSA';
    setHistoryItems(loadTraceHistory());
  }, []);

  useEffect(() => {
    let alive = true;
    portfolioApi.getAccounts()
      .then((response) => {
        if (!alive) return;
        setAccounts(response.accounts);
        if (response.accounts.length === 1) {
          setSelectedAccountId(String(response.accounts[0].id));
        }
      })
      .catch(() => {
        if (!alive) return;
        setAccounts([]);
      });
    return () => {
      alive = false;
    };
  }, []);

  const selectedTool = result?.tool_calls[selectedToolIndex] ?? null;
  const selectedEvent = result?.events[selectedEventIndex] ?? null;
  const plannerSummary = useMemo(() => buildPlannerSummary(result?.planner), [result?.planner]);
  const failedToolCount = useMemo(
    () => result?.tool_calls.filter((tool) => tool.success === false).length ?? 0,
    [result],
  );

  const handleRun = async () => {
    const stockCodeToSend = shouldSendStockCode(message, stockCode) ? stockCode.trim() : undefined;
    setRunning(true);
    setTraceStatus('running');
    setStatusMessage('正在准备上下文...');
    setError(null);
    setSelectedToolIndex(0);
    setSelectedEventIndex(0);
    setResult({
      success: false,
      session_id: '',
      content: '',
      error: null,
      total_steps: 0,
      total_tokens: 0,
      provider: '',
      model: '',
      mode: 'planning_execute',
      events: [],
      tool_calls: [],
      planner: null,
      agent_user_context: null,
      context_summary: null,
      debate: null,
      stock_selection: null,
      artifact_dir: null,
    });
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
    setSelectedEventIndex(0);
    setError(null);
    setTraceStatus(item.status === 'success' ? 'done' : 'error');
    setStatusMessage(item.status === 'success' ? '已加载历史 Trace' : '已加载失败 Trace');
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

  const handleClearHistory = () => {
    saveTraceHistory([]);
    setHistoryItems([]);
  };

  const consumeTraceStream = async (response: Response) => {
    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error('Trace stream has no response body');
    }
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
        let index = buffer.indexOf('\n');
        while (index >= 0) {
          const line = buffer.slice(0, index).trimEnd();
          buffer = buffer.slice(index + 1);
          processLine(line);
          index = buffer.indexOf('\n');
        }
      }
      if (done) break;
    }
    if (buffer.trim()) {
      processLine(buffer.trim());
    }
  };

  const applyTraceEvent = (event: TraceStreamEvent) => {
    const type = event.type || 'unknown';
    if (type === 'context_ready') {
      setStatusMessage('账户和持仓上下文已生成');
      setResult((prev) => ({
        ...(prev || createEmptyTraceResult()),
        session_id: String(event.session_id || prev?.session_id || ''),
        context_summary: asRecord(event.context_summary),
        agent_user_context: asRecord(event.agent_user_context),
      }));
      return;
    }
    if (type === 'planner_ready') {
      setStatusMessage('Planner 已生成执行计划');
      setResult((prev) => ({
        ...(prev || createEmptyTraceResult()),
        session_id: String(event.session_id || prev?.session_id || ''),
        planner: asRecord(event.planner),
      }));
      return;
    }
    if (type === 'tool_start') {
      setStatusMessage(`正在调用 ${event.display_name || event.tool || '工具'}...`);
      setResult((prev) => ({
        ...(prev || createEmptyTraceResult()),
        events: [...(prev?.events || []), eventToTraceEvent(event)],
      }));
      return;
    }
    if (type === 'tool_done') {
      const nextTool = eventToToolCall(event);
      setStatusMessage(`${event.display_name || event.tool || '工具'} 已返回`);
      setResult((prev) => {
        const current = prev || createEmptyTraceResult();
        const toolCalls = [...current.tool_calls, nextTool];
        return {
          ...current,
          events: [...current.events, eventToTraceEvent(event)],
          tool_calls: toolCalls,
        };
      });
      setSelectedToolIndex((index) => index);
      return;
    }
    if (type === 'thinking' || type === 'generating') {
      setStatusMessage(event.message || (type === 'generating' ? '正在生成最终输出...' : '正在分析...'));
      setResult((prev) => ({
        ...(prev || createEmptyTraceResult()),
        events: [...(prev?.events || []), eventToTraceEvent(event)],
      }));
      return;
    }
    if (type.startsWith('debate_')) {
      const message = event.message
        || (type === 'debate_start' ? 'Debate 已开始' : type === 'debate_judge_done' ? 'Judge 裁决已生成' : 'Debate 阶段已更新');
      setStatusMessage(message);
      setResult((prev) => ({
        ...(prev || createEmptyTraceResult()),
        events: [...(prev?.events || []), eventToTraceEvent(event)],
      }));
      return;
    }
    if (type === 'done') {
      setTraceStatus(event.success ? 'done' : 'error');
      setStatusMessage(event.success ? 'Trace 已完成' : String(event.error || 'Trace 失败'));
      setResult((prev) => {
        const current = prev || createEmptyTraceResult();
        const nextResult = {
          ...current,
          success: Boolean(event.success),
          session_id: String(event.session_id || current.session_id || ''),
          content: String(event.content || ''),
          error: typeof event.error === 'string' ? event.error : null,
          total_steps: typeof event.total_steps === 'number' ? event.total_steps : 0,
          total_tokens: typeof event.total_tokens === 'number' ? event.total_tokens : 0,
          provider: String(event.provider || ''),
          model: String(event.model || ''),
          mode: String(event.mode || 'planning_execute'),
          tool_calls: Array.isArray(event.tool_calls) ? event.tool_calls as AgentTraceToolCall[] : current.tool_calls,
          planner: asRecord(event.planner) || current.planner,
          agent_user_context: asRecord(event.agent_user_context) || current.agent_user_context,
          context_summary: asRecord(event.context_summary) || current.context_summary,
          debate: asRecord(event.debate) || current.debate,
          stock_selection: asRecord(event.stock_selection) || current.stock_selection,
          artifact_dir: typeof event.artifact_dir === 'string' ? event.artifact_dir : current.artifact_dir,
        };
        setHistoryItems((items) => persistTraceHistory(items, {
          id: nextResult.session_id || `${Date.now()}`,
          createdAt: new Date().toISOString(),
          message,
          stockCode: shouldSendStockCode(message, stockCode) ? stockCode.trim() : '',
          accountId: selectedAccountId ? Number(selectedAccountId) : undefined,
          status: nextResult.success ? 'success' : 'error',
          result: nextResult,
        }));
        return nextResult;
      });
      return;
    }
    if (type === 'error') {
      setTraceStatus('error');
      setStatusMessage(event.message || 'Trace 运行失败');
      setResult((prev) => {
        const current = prev || createEmptyTraceResult();
        const nextResult = {
          ...current,
          success: false,
          error: event.message || 'Trace 运行失败',
          events: [...current.events, eventToTraceEvent(event)],
        };
        setHistoryItems((items) => persistTraceHistory(items, {
          id: nextResult.session_id || `${Date.now()}`,
          createdAt: new Date().toISOString(),
          message,
          stockCode: shouldSendStockCode(message, stockCode) ? stockCode.trim() : '',
          accountId: selectedAccountId ? Number(selectedAccountId) : undefined,
          status: 'error',
          result: nextResult,
        }));
        return nextResult;
      });
    }
  };

  return (
    <AppPage className="max-w-[1600px] space-y-5">
      <PageHeader
        eyebrow="Developer"
        title="Agent Trace"
        description="用真实用户问题触发 planning_execute，并核对本次是否使用了正确账户、持仓和用户画像。"
        actions={(
          <Button onClick={() => void handleRun()} isLoading={running} loadingText="运行中">
            <Play className="h-4 w-4" />
            运行 Trace
          </Button>
        )}
      />

      {error ? <ApiErrorAlert error={error} /> : null}

      <Card padding="sm" className="rounded-lg">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2">
            <span className={cn(
              'h-2.5 w-2.5 rounded-full',
              traceStatus === 'running' ? 'animate-pulse bg-warning' : traceStatus === 'done' ? 'bg-success' : traceStatus === 'error' ? 'bg-danger' : 'bg-muted-text',
            )}
            />
            <span className="text-sm font-medium text-foreground">{statusMessage}</span>
          </div>
          <span className="text-xs text-secondary-text">
            {result?.session_id ? `session ${result.session_id}` : '等待运行'}
          </span>
        </div>
        {result?.artifact_dir ? (
          <div className="mt-2 truncate border-t border-border/50 pt-2 text-xs text-secondary-text">
            Artifact: <span className="font-mono text-foreground">{result.artifact_dir}</span>
          </div>
        ) : null}
      </Card>

      <Card title="Trace History" subtitle="Local" padding="md" className="rounded-lg">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm text-secondary-text">
            <History className="h-4 w-4 text-cyan" />
            最近 {historyItems.length} 次运行
          </div>
          <Button size="sm" variant="ghost" onClick={handleClearHistory} disabled={!historyItems.length || running}>
            <Trash2 className="h-4 w-4" />
            清空
          </Button>
        </div>
        {historyItems.length ? (
          <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
            {historyItems.map((item) => (
              <button
                key={`${item.id}-${item.createdAt}`}
                type="button"
                onClick={() => handleSelectHistory(item)}
                className="rounded-lg border border-border/60 bg-base/70 p-3 text-left hover:bg-hover"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-sm font-medium text-foreground">{item.stockCode || '-'}</span>
                  <Badge variant={item.status === 'success' ? 'success' : 'danger'}>{item.status}</Badge>
                </div>
                <p className="mt-1 truncate text-xs text-secondary-text">{item.message}</p>
                <p className="mt-2 text-[11px] text-muted-text">{formatDateTime(item.createdAt)}</p>
              </button>
            ))}
          </div>
        ) : (
          <p className="text-sm text-secondary-text">暂无历史。Trace 完成或失败后会保存在当前浏览器。</p>
        )}
      </Card>

      <Card padding="md" className="rounded-lg">
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_180px_220px_180px_180px_180px]">
          <label className="block">
            <span className="mb-2 block text-xs font-medium text-secondary-text">调试 Prompt</span>
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              className="min-h-24 w-full resize-y rounded-lg border border-border/70 bg-base px-3 py-2 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
            />
          </label>
          <label className="block">
            <span className="mb-2 block text-xs font-medium text-secondary-text">股票代码</span>
            <input
              value={stockCode}
              onChange={(event) => setStockCode(event.target.value)}
              className="h-10 w-full rounded-lg border border-border/70 bg-base px-3 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
              placeholder="600519"
            />
            {stockCode.trim() && !shouldSendStockCode(message, stockCode) ? (
              <span className="mt-1 block text-xs text-warning">当前问题像选股/组合配置，将不会发送该股票代码。</span>
            ) : null}
          </label>
          <label className="block">
            <span className="mb-2 block text-xs font-medium text-secondary-text">持仓账户</span>
            <select
              value={selectedAccountId}
              onChange={(event) => setSelectedAccountId(event.target.value)}
              className="h-10 w-full rounded-lg border border-border/70 bg-base px-3 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
            >
              <option value="">全部账户</option>
              {accounts.map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name} · {account.market.toUpperCase()} · {account.baseCurrency}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="mb-2 block text-xs font-medium text-secondary-text">报告意图</span>
            <select
              value={reportIntent}
              onChange={(event) => setReportIntent(event.target.value)}
              className="h-10 w-full rounded-lg border border-border/70 bg-base px-3 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
            >
              {REPORT_INTENT_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="block">
            <span className="mb-2 block text-xs font-medium text-secondary-text">风险偏好</span>
            <select
              value={riskPreference}
              onChange={(event) => setRiskPreference(event.target.value)}
              className="h-10 w-full rounded-lg border border-border/70 bg-base px-3 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
            >
              {RISK_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="block">
            <span className="mb-2 block text-xs font-medium text-secondary-text">持有周期</span>
            <select
              value={tradingHorizon}
              onChange={(event) => setTradingHorizon(event.target.value)}
              className="h-10 w-full rounded-lg border border-border/70 bg-base px-3 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
            >
              {HORIZON_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
          </label>
          <label className="flex items-center gap-2 self-end rounded-lg border border-border/70 bg-base px-3 py-2 text-sm text-secondary-text">
            <input
              type="checkbox"
              checked={injectPortfolioContext}
              onChange={(event) => setInjectPortfolioContext(event.target.checked)}
              className="h-4 w-4 accent-cyan"
            />
            注入持仓上下文
          </label>
        </div>
        <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          <PercentInput label="单票上限" value={maxSinglePositionPct} onChange={setMaxSinglePositionPct} placeholder="20" />
          <PercentInput label="总权益上限" value={maxTotalEquityExposurePct} onChange={setMaxTotalEquityExposurePct} placeholder="80" />
          <PercentInput label="最大回撤" value={maxAcceptableDrawdownPct} onChange={setMaxAcceptableDrawdownPct} placeholder="15" />
          <PercentInput label="默认止损" value={defaultStopLossPct} onChange={setDefaultStopLossPct} placeholder="8" />
        </div>
        <label className="mt-4 block">
          <span className="mb-2 block text-xs font-medium text-secondary-text">用户画像备注</span>
          <input
            value={investorNotes}
            onChange={(event) => setInvestorNotes(event.target.value)}
            className="h-10 w-full rounded-lg border border-border/70 bg-base px-3 text-sm text-foreground outline-none focus:border-cyan/50 focus:ring-4 focus:ring-cyan/10"
            placeholder="例如：长期持有、不能承受大回撤、偏分批操作"
          />
        </label>
      </Card>

      {result ? (
        <>
          <ContextSummaryPanel result={result} />

          <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
            <TraceStat label="状态" value={result.success ? '成功' : '失败'} tone={result.success ? 'success' : 'danger'} />
            <TraceStat label="步骤" value={String(result.total_steps)} />
            <TraceStat label="工具调用" value={String(result.tool_calls.length)} />
            <TraceStat label="失败工具" value={String(failedToolCount)} tone={failedToolCount ? 'danger' : 'success'} />
            <TraceStat label="模型" value={result.model || result.provider || '-'} />
          </section>

          {plannerSummary.length ? (
            <Card title="Execution Thesis" subtitle="Auditable Reasoning" padding="md" className="rounded-lg">
              <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
                {plannerSummary.map((item) => (
                  <TraceMeta key={item.label} label={item.label} value={item.value} />
                ))}
              </div>
            </Card>
          ) : null}

          <StockSelectionPanel result={result} />

          <DebatePanel result={result} />

          <Card title="Final Output" subtitle="Markdown Report" padding="md" className="rounded-lg">
            {result.error ? (
              <div className="mb-3 flex items-start gap-2 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>{result.error}</span>
              </div>
            ) : null}
            <div className="min-h-[720px] max-h-[calc(100vh-180px)] overflow-auto rounded-lg border border-border/60 bg-base p-6">
              {result.content ? (
                <div className="chat-prose max-w-none">
                  <Markdown remarkPlugins={[remarkGfm]}>
                    {result.content}
                  </Markdown>
                </div>
              ) : (
                <p className="text-sm text-secondary-text">等待最终输出...</p>
              )}
            </div>
          </Card>

          <Card title="Evidence Timeline" subtitle="Click To Inspect" padding="md" className="rounded-lg">
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1.05fr)_minmax(360px,0.95fr)]">
              <div className="max-h-[620px] space-y-2 overflow-y-auto pr-1">
                {result.events.length ? result.events.map((event, index) => (
                  <button
                    key={`${event.type}-${index}`}
                    type="button"
                    onClick={() => setSelectedEventIndex(index)}
                    className={cn(
                      'w-full rounded-lg border border-border/60 bg-base/70 p-3 text-left hover:bg-hover',
                      selectedEventIndex === index ? 'border-cyan/50 bg-cyan/10' : '',
                    )}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-foreground">{event.display_name || event.tool || event.type}</span>
                      <Badge variant={getStatusVariant(event.success)}>{event.type}</Badge>
                    </div>
                    <p className="mt-1 text-xs text-secondary-text">
                      step {event.step ?? '-'} · {formatDuration(event.duration)}
                    </p>
                    {event.message ? <p className="mt-2 truncate text-xs text-muted-text">{event.message}</p> : null}
                  </button>
                )) : (
                  <p className="text-sm text-secondary-text">暂无事件。</p>
                )}
              </div>
              <div className="rounded-lg border border-border/60 bg-base/70 p-3">
                <TracePanelTitle icon={Route} title="Event Detail" />
                {selectedEvent ? (
                  <JsonViewer data={selectedEvent} maxHeight="560px" />
                ) : (
                  <p className="mt-3 text-sm text-secondary-text">选择一个事件查看完整载荷。</p>
                )}
              </div>
            </div>
          </Card>

          <Card title="Tool Calls" subtitle="Execute" padding="md" className="rounded-lg">
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(420px,0.72fr)]">
              <div className="overflow-hidden rounded-lg border border-border/60">
                <div className="grid grid-cols-[72px_minmax(180px,1fr)_100px_100px] border-b border-border/60 bg-base/70 px-3 py-2 text-xs font-medium text-secondary-text">
                  <span>Step</span>
                  <span>Tool</span>
                  <span>Status</span>
                  <span>Duration</span>
                </div>
                <div className="max-h-[620px] overflow-y-auto">
                  {result.tool_calls.map((call, index) => (
                    <button
                      key={getToolCallKey(call, index)}
                      type="button"
                      onClick={() => setSelectedToolIndex(index)}
                      className={cn(
                        'grid w-full grid-cols-[72px_minmax(180px,1fr)_100px_100px] items-center border-b border-border/40 px-3 py-2 text-left text-sm hover:bg-hover',
                        selectedToolIndex === index ? 'bg-cyan/10' : '',
                      )}
                    >
                      <span className="text-secondary-text">{call.step}</span>
                      <span className="truncate font-medium text-foreground">{call.tool}</span>
                      <span>
                        <Badge variant={call.success ? 'success' : 'danger'}>{call.success ? 'OK' : 'FAIL'}</Badge>
                      </span>
                      <span className="text-secondary-text">{formatDuration(call.duration)}</span>
                    </button>
                  ))}
                  {!result.tool_calls.length ? <p className="p-4 text-sm text-secondary-text">没有工具调用。</p> : null}
                </div>
              </div>

              <div className="space-y-3">
                <TracePanelTitle icon={Wrench} title="Selected Tool" />
                {selectedTool ? (
                  <>
                    <div className="grid grid-cols-2 gap-2 text-xs">
                      <TraceMeta label="Tool" value={selectedTool.tool} />
                      <TraceMeta label="Step" value={String(selectedTool.step)} />
                      <TraceMeta label="Duration" value={formatDuration(selectedTool.duration)} />
                      <TraceMeta label="Result Length" value={String(selectedTool.result_length ?? '-')} />
                    </div>
                    <JsonViewer data={(selectedTool.arguments || {}) as Record<string, unknown>} maxHeight="260px" />
                    <pre className="max-h-[360px] overflow-auto rounded-lg border border-border/60 bg-base p-3 text-xs leading-5 text-foreground whitespace-pre-wrap">
                      {selectedTool.result_preview || '无结果预览'}
                    </pre>
                  </>
                ) : (
                  <p className="text-sm text-secondary-text">选择一个工具调用查看参数和结果预览。</p>
                )}
              </div>
            </div>
          </Card>

        </>
      ) : (
        <Card padding="lg" className="rounded-lg">
          <div className="grid gap-4 md:grid-cols-3">
            <TraceEmpty icon={ClipboardList} title="Plan" text="展示 capability 到 tools 的展开结果、缺失工具和风险检查。" />
            <TraceEmpty icon={Route} title="Execute" text="展示每一步工具调用、参数、耗时、成功状态和结果预览。" />
            <TraceEmpty icon={Braces} title="Raw" text="保留 planner、工具日志和阶段输出 JSON，方便定位链路问题。" />
          </div>
        </Card>
      )}
    </AppPage>
  );
};

const TraceStat: React.FC<{ label: string; value: string; tone?: 'success' | 'danger' | 'default' }> = ({ label, value, tone = 'default' }) => (
  <Card padding="sm" className="rounded-lg">
    <p className="text-xs text-secondary-text">{label}</p>
    <p className={cn('mt-1 truncate text-lg font-semibold', tone === 'success' ? 'text-success' : tone === 'danger' ? 'text-danger' : 'text-foreground')}>
      {value}
    </p>
  </Card>
);

const TraceMeta: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="rounded-lg border border-border/60 bg-base p-2">
    <p className="text-[11px] text-muted-text">{label}</p>
    <p className="mt-1 truncate text-xs font-medium text-foreground">{value}</p>
  </div>
);

const PercentInput: React.FC<{
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder: string;
}> = ({ label, value, onChange, placeholder }) => (
  <label className="block">
    <span className="mb-2 block text-xs font-medium text-secondary-text">{label}</span>
    <div className="flex h-10 items-center rounded-lg border border-border/70 bg-base px-3 focus-within:border-cyan/50 focus-within:ring-4 focus-within:ring-cyan/10">
      <input
        value={value}
        onChange={(event) => onChange(event.target.value)}
        inputMode="decimal"
        className="min-w-0 flex-1 bg-transparent text-sm text-foreground outline-none"
        placeholder={placeholder}
      />
      <span className="ml-2 text-xs text-secondary-text">%</span>
    </div>
  </label>
);

const TracePanelTitle: React.FC<{ icon: React.ComponentType<{ className?: string }>; title: string }> = ({ icon: Icon, title }) => (
  <div className="flex items-center gap-2 text-sm font-medium text-foreground">
    <Icon className="h-4 w-4 text-cyan" />
    {title}
  </div>
);

const TraceEmpty: React.FC<{ icon: React.ComponentType<{ className?: string }>; title: string; text: string }> = ({ icon: Icon, title, text }) => (
  <div className="rounded-lg border border-border/60 bg-base/70 p-4">
    <Icon className="h-5 w-5 text-cyan" />
    <h2 className="mt-3 text-sm font-semibold text-foreground">{title}</h2>
    <p className="mt-1 text-sm text-secondary-text">{text}</p>
  </div>
);

const ContextSummaryPanel: React.FC<{ result: AgentTraceRunResponse }> = ({ result }) => {
  const summary = (result.context_summary || {}) as {
    context_error?: unknown;
    account_count?: unknown;
    position_count?: unknown;
    accounts?: Array<Record<string, unknown>>;
    investor?: Record<string, unknown> | null;
    target_position?: Record<string, unknown> | null;
    metadata?: Record<string, unknown>;
  };
  const accounts = Array.isArray(summary.accounts) ? summary.accounts : [];
  const investor = summary.investor || {};
  const targetPosition = summary.target_position || null;

  return (
    <Card title="Context In Use" subtitle="Account & Profile" padding="md" className="rounded-lg">
      {summary.context_error ? (
        <div className="mb-3 rounded-lg border border-danger/30 bg-danger/10 p-3 text-sm text-danger">
          {String(summary.context_error)}
        </div>
      ) : null}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <TraceMeta label="账户数" value={String(summary.account_count ?? 0)} />
        <TraceMeta label="持仓数" value={String(summary.position_count ?? 0)} />
        <TraceMeta label="风险偏好" value={String(investor.risk_preference ?? '-')} />
        <TraceMeta label="持有周期" value={String(investor.trading_horizon ?? '-')} />
        <TraceMeta label="单票上限" value={formatPercent(investor.max_single_position_pct)} />
        <TraceMeta label="总权益上限" value={formatPercent(investor.max_total_equity_exposure_pct)} />
        <TraceMeta label="最大回撤" value={formatPercent(investor.max_acceptable_drawdown_pct)} />
        <TraceMeta label="默认止损" value={formatPercent(investor.default_stop_loss_pct)} />
      </div>
      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(280px,0.8fr)]">
        <div className="overflow-hidden rounded-lg border border-border/60">
          <div className="grid grid-cols-[80px_minmax(140px,1fr)_90px_100px_120px] border-b border-border/60 bg-base/70 px-3 py-2 text-xs font-medium text-secondary-text">
            <span>ID</span>
            <span>账户</span>
            <span>市场</span>
            <span>现金</span>
            <span>权益</span>
          </div>
          {accounts.length ? accounts.map((account, index) => (
            <div key={`${account.account_id}-${index}`} className="grid grid-cols-[80px_minmax(140px,1fr)_90px_100px_120px] border-b border-border/40 px-3 py-2 text-sm">
              <span className="text-secondary-text">{String(account.account_id ?? '-')}</span>
              <span className="truncate font-medium text-foreground">{String(account.account_name ?? '-')}</span>
              <span className="text-secondary-text">{String(account.market ?? '-')}</span>
              <span className="text-secondary-text">{formatNumber(account.available_cash)}</span>
              <span className="text-secondary-text">{formatNumber(account.total_equity)}</span>
            </div>
          )) : (
            <p className="p-4 text-sm text-secondary-text">没有注入账户上下文。</p>
          )}
        </div>
        <div className="rounded-lg border border-border/60 bg-base/70 p-3">
          <TracePanelTitle icon={ClipboardList} title="Target Position" />
          {targetPosition ? (
            <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <TraceMeta label="Symbol" value={String(targetPosition.symbol ?? '-')} />
              <TraceMeta label="Quantity" value={formatNumber(targetPosition.quantity)} />
              <TraceMeta label="Avg Cost" value={formatNumber(targetPosition.avg_cost)} />
              <TraceMeta label="Last Price" value={formatNumber(targetPosition.last_price)} />
              <TraceMeta label="PnL" value={formatNumber(targetPosition.unrealized_pnl)} />
              <TraceMeta label="Weight" value={`${formatNumber(targetPosition.position_pct)}%`} />
            </div>
          ) : (
            <p className="mt-3 text-sm text-secondary-text">目标股票没有命中当前持仓。</p>
          )}
          {investor.notes ? <p className="mt-3 text-xs text-muted-text">{String(investor.notes)}</p> : null}
        </div>
      </div>
    </Card>
  );
};

const DebatePanel: React.FC<{ result: AgentTraceRunResponse }> = ({ result }) => {
  const debate = result.debate;
  if (!debate) return null;
  const enabled = debate.enabled === true;
  const success = debate.success === true;
  if (!enabled) return null;

  const primary = asRecord(debate.primary_thesis) || {};
  const opposing = asRecord(debate.opposing_thesis) || {};
  const judge = asRecord(debate.judge_decision) || {};
  const unresolved = toStringList(debate.unresolved_conflicts);
  const dimensionAssessments = toRecordList(judge.dimension_assessments);
  const reasonPoints = toStringList(judge.reason_points);
  const decisionSummary = String(judge.decision_summary || judge.reason || '-');

  return (
    <Card title="Debate Judge" subtitle="Adversarial Review" padding="md" className="rounded-lg">
      {!success ? (
        <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
          Debate 未完成：{String(debate.error || debate.skipped_reason || 'unknown')}
        </div>
      ) : null}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <TraceMeta label="模式" value={String(debate.mode || '-')} />
        <TraceMeta label="意图" value={String(debate.intent || '-')} />
        <TraceMeta label="Winner" value={String(judge.winner || '-')} />
        <TraceMeta label="Final Action" value={String(judge.final_action || '-')} />
      </div>
      <div className="mt-4 grid gap-4 xl:grid-cols-3">
        <DebateThesisCard title="主观点" thesis={primary} />
        <DebateThesisCard title="反方" thesis={opposing} />
        <div className="rounded-lg border border-border/60 bg-base/70 p-3">
          <TracePanelTitle icon={ClipboardList} title="Judge 裁决" />
          <div className="mt-3 rounded-lg border border-border/60 bg-surface p-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-md bg-foreground px-2 py-1 text-xs font-semibold text-base">
                {String(judge.final_action || '-')}
              </span>
              <span className="rounded-md border border-border/70 px-2 py-1 text-xs font-medium text-secondary-text">
                winner: {String(judge.winner || '-')}
              </span>
            </div>
            <p className="mt-3 text-sm leading-6 text-foreground">{decisionSummary}</p>
          </div>
          {dimensionAssessments.length ? <DimensionAssessmentGrid items={dimensionAssessments} /> : null}
          <KeyValueList label="裁决理由" items={reasonPoints.length ? reasonPoints : toStringList(judge.reason)} />
          <KeyValueList label="采纳" items={toStringList(judge.accepted_arguments)} />
          <KeyValueList label="驳回" items={toStringList(judge.rejected_arguments)} />
          <KeyValueList label="风控" items={toStringList(judge.risk_controls)} />
          {unresolved.length ? <KeyValueList label="未决冲突" items={unresolved} /> : null}
        </div>
      </div>
      <SessionOutputsPanel debate={debate} finalContent={result.content} />
    </Card>
  );
};

const STOCK_SELECTION_STAGE_LABELS: Array<{ key: string; label: string }> = [
  { key: 'candidate_discovery', label: '候选发现' },
  { key: 'candidate_screening', label: '初筛' },
  { key: 'single_stock_deep_dive', label: '深度分析' },
  { key: 'portfolio_allocation', label: '组合配置' },
  { key: 'adversarial_review', label: '反方审查' },
  { key: 'judge_decision', label: 'Judge' },
];

const StockSelectionPanel: React.FC<{ result: AgentTraceRunResponse }> = ({ result }) => {
  const selection = asRecord(result.stock_selection);
  if (!selection) return null;

  const success = selection.success === true;
  const selectionContext = asRecord(selection.selection_context) || {};
  const stages = asRecord(selectionContext.stages) || {};
  const finalReport = asRecord(selection.final_report_json) || {};
  const candidateDiscovery = asRecord(finalReport.candidate_discovery) || {};
  const screening = asRecord(finalReport.candidate_screening) || {};
  const deepDive = asRecord(finalReport.single_stock_deep_dive) || {};
  const allocation = asRecord(finalReport.portfolio_allocation) || {};
  const adversarial = asRecord(finalReport.adversarial_review) || {};
  const judge = asRecord(finalReport.judge_decision) || {};

  const discoverySummary = asRecord(candidateDiscovery.summary) || asRecord(asRecord(stages.candidate_discovery)?.summary) || {};
  const screeningSummary = asRecord(screening.summary) || asRecord(asRecord(stages.candidate_screening)?.summary) || {};
  const deepSummary = asRecord(deepDive.summary) || asRecord(asRecord(stages.single_stock_deep_dive)?.summary) || {};
  const allocationSummary = asRecord(allocation.summary) || asRecord(asRecord(stages.portfolio_allocation)?.summary) || {};
  const allocationFull = asRecord(allocation.full) || {};
  const adversarialSummary = asRecord(adversarial.summary) || asRecord(asRecord(stages.adversarial_review)?.summary) || {};
  const judgeSummary = asRecord(judge.summary) || asRecord(asRecord(stages.judge_decision)?.summary) || {};
  const judgeFull = asRecord(judge.full) || {};

  const candidateCodes = toStringList(discoverySummary.candidate_codes);
  const deepTargets = toStringList(screeningSummary.deep_dive_targets);
  const waitTargets = toStringList(deepSummary.wait_targets);
  const openTargets = toStringList(deepSummary.open_targets);
  const rejectedTargets = toStringList(deepSummary.reject_targets);
  const planRows = toRecordList(allocationFull.positions_plan).slice(0, 4);
  const riskControls = toStringList(judgeFull.risk_controls);

  return (
    <Card title="Stock Selection Pipeline" subtitle="watchlist_scan" padding="md" className="rounded-lg">
      {!success ? (
        <div className="mb-3 rounded-lg border border-warning/30 bg-warning/10 p-3 text-sm text-warning">
          选股流水线未完成：{String(selection.error || selection.skipped_reason || 'unknown')}
        </div>
      ) : null}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
        <TraceMeta label="策略" value={String(selectionContext.candidate_strategy || '-')} />
        <TraceMeta label="最终动作" value={String(judgeSummary.final_action || allocationSummary.portfolio_action || '-')} />
        <TraceMeta label="裁决" value={String(judgeSummary.primary_plan_verdict || '-')} />
        <TraceMeta label="候选数" value={String(candidateCodes.length || '-')} />
        <TraceMeta label="Next Step" value={String(judgeSummary.next_step || selectionContext.next_step || '-')} />
      </div>

      <div className="mt-4 grid gap-2 md:grid-cols-3 xl:grid-cols-6">
        {STOCK_SELECTION_STAGE_LABELS.map((stage) => {
          const stagePayload = asRecord(stages[stage.key]) || {};
          const status = String(stagePayload.status || '-');
          return (
            <div key={stage.key} className="rounded-lg border border-border/60 bg-base/70 p-3">
              <p className="text-xs font-medium text-secondary-text">{stage.label}</p>
              <span className={cn(
                'mt-2 inline-flex rounded-md border px-2 py-1 text-xs font-medium',
                status === 'ok' ? 'border-success/30 bg-success/10 text-success'
                  : status === 'partial' ? 'border-warning/30 bg-warning/10 text-warning'
                    : status === '-' ? 'border-border bg-surface text-muted-text'
                      : 'border-danger/30 bg-danger/10 text-danger',
              )}
              >
                {status}
              </span>
              {stagePayload.full_ref ? (
                <p className="mt-2 truncate font-mono text-[11px] text-muted-text">{String(stagePayload.full_ref)}</p>
              ) : null}
            </div>
          );
        })}
      </div>

      <div className="mt-4 grid gap-4 xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.2fr)_minmax(0,1fr)]">
        <div className="rounded-lg border border-border/60 bg-base/70 p-3">
          <TracePanelTitle icon={ClipboardList} title="候选与初筛" />
          <ChipList label="候选池" items={candidateCodes.slice(0, 10)} />
          <ChipList label="深挖标的" items={deepTargets.slice(0, 8)} />
          <KeyValueList label="发现限制" items={toStringList(discoverySummary.main_limitations)} />
          <KeyValueList label="初筛限制" items={toStringList(screeningSummary.main_limitations)} />
        </div>

        <div className="rounded-lg border border-border/60 bg-base/70 p-3">
          <TracePanelTitle icon={Braces} title="深度分析与配置" />
          <div className="grid gap-2 sm:grid-cols-3">
            <TraceMeta label="Open" value={openTargets.length ? openTargets.join(', ') : '-'} />
            <TraceMeta label="Wait" value={waitTargets.length ? waitTargets.join(', ') : '-'} />
            <TraceMeta label="Reject" value={rejectedTargets.length ? rejectedTargets.join(', ') : '-'} />
          </div>
          {planRows.length ? (
            <div className="mt-3 overflow-hidden rounded-lg border border-border/60">
              <div className="grid grid-cols-[70px_70px_70px_minmax(120px,1fr)] bg-surface px-3 py-2 text-xs font-medium text-secondary-text">
                <span>股票</span>
                <span>动作</span>
                <span>首仓</span>
                <span>触发</span>
              </div>
              {planRows.map((row, index) => (
                <div key={`${String(row.code || '-')}-${index}`} className="grid grid-cols-[70px_70px_70px_minmax(120px,1fr)] border-t border-border/40 px-3 py-2 text-xs">
                  <span className="font-mono text-foreground">{String(row.code || '-')}</span>
                  <span className="text-secondary-text">{String(row.action || '-')}</span>
                  <span className="text-secondary-text">{formatPercent(row.initial_position_pct)}</span>
                  <span className="truncate text-secondary-text">{String(row.entry_condition || '-')}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-3 text-sm text-secondary-text">暂无配置行，通常表示本轮不建仓或证据不足。</p>
          )}
        </div>

        <div className="rounded-lg border border-border/60 bg-base/70 p-3">
          <TracePanelTitle icon={ClipboardList} title="裁决摘要" />
          <p className="mt-3 text-sm leading-6 text-foreground">
            {String(judgeSummary.decision_summary || allocationSummary.core_reason || '-')}
          </p>
          <KeyValueList label="反方重点" items={toStringList(adversarialSummary.top_risk_points)} />
          <KeyValueList label="证据缺口" items={toStringList(adversarialSummary.top_evidence_gaps)} />
          <KeyValueList label="风控" items={riskControls} />
        </div>
      </div>
    </Card>
  );
};

const ChipList: React.FC<{ label: string; items: string[] }> = ({ label, items }) => {
  if (!items.length) return null;
  return (
    <div className="mt-3">
      <p className="text-xs font-medium uppercase tracking-wide text-secondary-text">{label}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {items.map((item) => (
          <span key={`${label}-${item}`} className="rounded-md border border-border/70 bg-surface px-2 py-1 font-mono text-xs text-foreground">
            {item}
          </span>
        ))}
      </div>
    </div>
  );
};

const DebateThesisCard: React.FC<{ title: string; thesis: Record<string, unknown> }> = ({ title, thesis }) => (
  <div className="rounded-lg border border-border/60 bg-base/70 p-3">
    <TracePanelTitle icon={Braces} title={title} />
    <div className="mt-3 grid gap-2">
      <TraceMeta label="立场" value={String(thesis.direction || '-')} />
      <TraceMeta label="动作" value={String(thesis.action || '-')} />
    </div>
    <p className="mt-3 text-sm text-foreground">{String(thesis.summary || '-')}</p>
    <KeyValueList label="证据" items={toStringList(thesis.evidence)} />
    <DimensionEvidenceList value={thesis.evidence_by_dimension} />
    <KeyValueList label="失效条件" items={toStringList(thesis.failure_conditions)} />
  </div>
);

const KeyValueList: React.FC<{ label: string; items: string[] }> = ({ label, items }) => {
  if (!items.length) return null;
  return (
    <div className="mt-3">
      <p className="text-xs font-medium uppercase tracking-wide text-secondary-text">{label}</p>
      <ul className="mt-1 space-y-1 text-sm text-secondary-text">
        {items.map((item, index) => <li key={`${label}-${index}`}>{item}</li>)}
      </ul>
    </div>
  );
};

const DIMENSION_LABELS: Record<string, string> = {
  account_risk: '账户风险',
  technical: '技术面',
  capital_flow: '资金面',
  news_event: '消息面',
  fundamental: '基本面',
  data_quality: '数据质量',
  chip_distribution: '筹码面',
  market_state: '市场状态',
};

const VERDICT_TONES: Record<string, string> = {
  supports_primary: 'border-success/30 bg-success/10 text-success',
  supports_opposing: 'border-danger/30 bg-danger/10 text-danger',
  mixed: 'border-warning/30 bg-warning/10 text-warning',
  insufficient_data: 'border-border bg-surface text-secondary-text',
};

const DimensionAssessmentGrid: React.FC<{ items: Record<string, unknown>[] }> = ({ items }) => (
  <div className="mt-3 grid gap-2">
    {items.map((item, index) => {
      const dimension = String(item.dimension || '-');
      const verdict = String(item.verdict || '-');
      const tone = VERDICT_TONES[verdict] || 'border-border bg-surface text-secondary-text';
      const evidence = toStringList(item.evidence);
      const missing = toStringList(item.missing);
      return (
        <details key={`${dimension}-${index}`} className="rounded-lg border border-border/60 bg-surface p-3">
          <summary className="cursor-pointer list-none">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-sm font-medium text-foreground">{DIMENSION_LABELS[dimension] || dimension}</span>
              <span className={`rounded-md border px-2 py-1 text-xs font-medium ${tone}`}>
                {verdict} · {String(item.weight || '-')}
              </span>
            </div>
            <p className="mt-2 text-sm leading-6 text-secondary-text">{String(item.summary || '-')}</p>
          </summary>
          <KeyValueList label="证据" items={evidence} />
          <KeyValueList label="缺口" items={missing} />
        </details>
      );
    })}
  </div>
);

const DimensionEvidenceList: React.FC<{ value: unknown }> = ({ value }) => {
  const record = asRecord(value);
  if (!record) return null;
  const entries = Object.entries(record)
    .map(([key, val]) => [key, toStringList(val)] as const)
    .filter(([, items]) => items.length);
  if (!entries.length) return null;
  return (
    <div className="mt-3">
      <p className="text-xs font-medium uppercase tracking-wide text-secondary-text">分维度证据</p>
      <div className="mt-2 grid gap-2">
        {entries.map(([key, items]) => (
          <div key={key} className="rounded-md border border-border/60 bg-surface p-2">
            <p className="text-xs font-medium text-foreground">{DIMENSION_LABELS[key] || key}</p>
            <ul className="mt-1 space-y-1 text-xs leading-5 text-secondary-text">
              {items.map((item, index) => <li key={`${key}-${index}`}>{item}</li>)}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
};

const SessionOutputsPanel: React.FC<{ debate: Record<string, unknown>; finalContent: string }> = ({ debate, finalContent }) => {
  const debugOutputs = asRecord(debate.debug_outputs) || {};
  const items = [
    { label: '原始主报告输出', value: debugOutputs.primary_report_raw },
    { label: 'Primary Thesis 原始输出', value: debugOutputs.primary_thesis_raw },
    { label: 'Opposing Thesis 原始输出', value: debugOutputs.opposing_thesis_raw },
    { label: 'Judge 原始输出', value: debugOutputs.judge_raw },
    { label: '最终合并输出', value: debugOutputs.final_report_with_debate || finalContent },
  ].filter((item) => typeof item.value === 'string' && item.value.trim());

  if (!items.length) return null;
  return (
    <div className="mt-4">
      <TracePanelTitle icon={ClipboardList} title="Session Outputs" />
      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        {items.map((item) => (
          <details key={item.label} className="rounded-lg border border-border/60 bg-base/70 p-3" open={item.label === 'Judge 原始输出'}>
            <summary className="cursor-pointer text-sm font-medium text-foreground">{item.label}</summary>
            <pre className="mt-3 max-h-[420px] overflow-auto whitespace-pre-wrap break-words rounded-lg bg-surface p-3 text-xs text-secondary-text">
              {String(item.value)}
            </pre>
          </details>
        ))}
      </div>
    </div>
  );
};

const formatNumber = (value: unknown): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
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

const formatDateTime = (value: string): string => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

const buildPlannerSummary = (planner?: Record<string, unknown> | null): Array<{ label: string; value: string }> => {
  if (!planner) return [];
  const requiredTools = Array.isArray(planner.required_tools) ? planner.required_tools : [];
  const riskChecks = Array.isArray(planner.risk_checks) ? planner.risk_checks : [];
  return [
    { label: 'Intent', value: String(planner.intent || '-') },
    { label: 'Symbol', value: String(planner.primary_symbol || '-') },
    { label: 'Position', value: planner.has_position ? '持仓命中' : '未命中持仓' },
    { label: 'Tools', value: requiredTools.length ? requiredTools.join(', ') : '-' },
    { label: 'Risk Checks', value: riskChecks.length ? riskChecks.join(', ') : '-' },
    { label: 'Expected Output', value: String(planner.expected_output || '-') },
  ];
};

const createEmptyTraceResult = (): AgentTraceRunResponse => ({
  success: false,
  session_id: '',
  content: '',
  error: null,
  total_steps: 0,
  total_tokens: 0,
  provider: '',
  model: '',
  mode: 'planning_execute',
  events: [],
  tool_calls: [],
  planner: null,
  agent_user_context: null,
  context_summary: null,
  debate: null,
  stock_selection: null,
  artifact_dir: null,
});

const asRecord = (value: unknown): Record<string, unknown> | null => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
);

const eventToToolCall = (event: TraceStreamEvent): AgentTraceToolCall => ({
  step: typeof event.step === 'number' ? event.step : 0,
  tool: String(event.tool || ''),
  arguments: event.arguments || {},
  success: event.success === true,
  duration: typeof event.duration === 'number' ? event.duration : undefined,
  result_length: typeof event.result_length === 'number' ? event.result_length : undefined,
  result_preview: typeof event.result_preview === 'string' ? event.result_preview : undefined,
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
  } catch {
    return [];
  }
};

const saveTraceHistory = (items: TraceHistoryItem[]) => {
  window.localStorage.setItem(TRACE_HISTORY_KEY, JSON.stringify(items.slice(0, TRACE_HISTORY_LIMIT)));
};

const persistTraceHistory = (items: TraceHistoryItem[], next: TraceHistoryItem): TraceHistoryItem[] => {
  const merged = [
    next,
    ...items.filter((item) => item.id !== next.id),
  ].slice(0, TRACE_HISTORY_LIMIT);
  saveTraceHistory(merged);
  return merged;
};

export default AgentTracePage;
