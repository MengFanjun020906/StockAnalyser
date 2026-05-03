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
  const [stockCode, setStockCode] = useState('600519');
  const [accounts, setAccounts] = useState<PortfolioAccountItem[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>('');
  const [riskPreference, setRiskPreference] = useState('balanced');
  const [tradingHorizon, setTradingHorizon] = useState('long_term');
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
    });
    try {
      const response = await agentApi.traceStream({
        message,
        account_id: selectedAccountId ? Number(selectedAccountId) : undefined,
        stock_code: stockCode.trim() || undefined,
        inject_portfolio_context: injectPortfolioContext,
        analysis_mode: 'planning_execute',
        risk_preference: riskPreference,
        trading_horizon: tradingHorizon,
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
        };
        setHistoryItems((items) => persistTraceHistory(items, {
          id: nextResult.session_id || `${Date.now()}`,
          createdAt: new Date().toISOString(),
          message,
          stockCode,
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
          stockCode,
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
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_180px_220px_180px_180px]">
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

          <Card title="AgentUserContext" subtitle="Context" padding="md" className="rounded-lg">
            <JsonViewer data={result.agent_user_context || null} maxHeight="520px" />
          </Card>
        </>
      ) : (
        <Card padding="lg" className="rounded-lg">
          <div className="grid gap-4 md:grid-cols-3">
            <TraceEmpty icon={ClipboardList} title="Plan" text="展示 capability 到 tools 的展开结果、缺失工具和风险检查。" />
            <TraceEmpty icon={Route} title="Execute" text="展示每一步工具调用、参数、耗时、成功状态和结果预览。" />
            <TraceEmpty icon={Braces} title="Raw" text="保留 planner、AgentUserContext 和工具日志 JSON，方便定位链路问题。" />
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

const formatNumber = (value: unknown): string => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-';
  return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
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
  success: event.success !== false,
  duration: typeof event.duration === 'number' ? event.duration : undefined,
  result_length: typeof event.result_length === 'number' ? event.result_length : undefined,
  result_preview: typeof event.result_preview === 'string' ? event.result_preview : undefined,
  cached: typeof event.cached === 'boolean' ? event.cached : undefined,
  timeout: typeof event.timeout === 'boolean' ? event.timeout : undefined,
});

const eventToTraceEvent = (event: TraceStreamEvent): AgentTraceRunResponse['events'][number] => ({
  ...event,
  type: event.type || 'unknown',
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
