import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentTracePage from '../AgentTracePage';

const mocks = vi.hoisted(() => ({
  getAccounts: vi.fn(),
  getRuntimeConfig: vi.fn(),
  getTraceSession: vi.fn(),
  runTrace: vi.fn(),
  traceStream: vi.fn(),
}));

vi.mock('../../api/agent', () => ({
  agentApi: {
    getRuntimeConfig: mocks.getRuntimeConfig,
    getTraceSession: mocks.getTraceSession,
    runTrace: mocks.runTrace,
    traceStream: mocks.traceStream,
  },
}));

vi.mock('../../api/portfolio', () => ({
  portfolioApi: {
    getAccounts: mocks.getAccounts,
  },
}));

describe('AgentTracePage', () => {
  const renderPage = (initialEntry = '/agent-trace') => render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/agent-trace" element={<AgentTracePage />} />
        <Route path="/agent-trace/:sessionId" element={<AgentTracePage />} />
      </Routes>
    </MemoryRouter>,
  );

  beforeEach(() => {
    window.localStorage.clear();
    mocks.getAccounts.mockReset();
    mocks.getRuntimeConfig.mockReset();
    mocks.getTraceSession.mockReset();
    mocks.runTrace.mockReset();
    mocks.traceStream.mockReset();
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'legacy' } });
    mocks.getTraceSession.mockRejectedValue(new Error('not found'));
  });

  it('runs a trace and renders planner, events, tools, and final output', async () => {
    vi.spyOn(crypto, 'randomUUID').mockReturnValue('12345678-1234-1234-1234-123456789abc');
    mocks.getAccounts.mockResolvedValue({
      accounts: [{ id: 7, name: 'A股主账户', market: 'cn', baseCurrency: 'CNY', isActive: true }],
    });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'context_ready',
        session_id: 'trace-12345678123412341234123456789abc',
        agent_user_context: { report: { analysis_mode: 'planning_execute' } },
        context_summary: {
          account_count: 1,
          position_count: 2,
          investor: {
            risk_preference: 'balanced',
            trading_horizon: 'long_term',
            max_single_position_pct: 20,
            max_total_equity_exposure_pct: 80,
            max_acceptable_drawdown_pct: 15,
            default_stop_loss_pct: 8,
            notes: '偏长期持有',
          },
          accounts: [
            {
              account_id: 7,
              account_name: 'A股主账户',
              market: 'cn',
              base_currency: 'CNY',
              available_cash: 16982.65,
              total_equity: 736532.65,
            },
          ],
          target_position: {
            symbol: '600519',
            account_id: 7,
            quantity: 150000,
            avg_cost: 4.797,
            last_price: 5,
            unrealized_pnl: 30450,
            position_pct: 50.1,
          },
        },
      },
      { type: 'planner_ready', session_id: 'trace-12345678123412341234123456789abc', planner: { intent: 'position_review', required_tools: ['get_realtime_quote'] } },
      { type: 'tool_start', step: 1, tool: 'get_realtime_quote', display_name: '获取实时行情', arguments: { stock_code: '600519' } },
      {
        type: 'tool_done',
        step: 1,
        tool: 'get_realtime_quote',
        display_name: '获取实时行情',
        arguments: { stock_code: '600519' },
        success: true,
        duration: 0.2,
        result_length: 18,
        result_preview: '{"price": 100}',
      },
      {
        type: 'done',
        success: true,
        session_id: 'trace-12345678123412341234123456789abc',
        content: '## 最终结论\n\n持仓策略：**持有，但不急着加仓**',
        error: null,
        total_steps: 2,
        total_tokens: 321,
        provider: 'deepseek',
        model: 'deepseek/deepseek-v4-pro',
        mode: 'planning_execute',
        tool_calls: [
          {
            step: 1,
            tool: 'get_realtime_quote',
            arguments: { stock_code: '600519' },
            success: true,
            duration: 0.2,
            result_length: 18,
            result_preview: '{"price": 100}',
          },
        ],
        planner: { intent: 'position_review', required_tools: ['get_realtime_quote'] },
        agent_user_context: { report: { analysis_mode: 'planning_execute' } },
        debate: {
          enabled: true,
          success: true,
          mode: 'forced_opposition_judge',
          intent: 'position_review',
          primary_thesis: {
            direction: 'bullish',
            action: 'hold',
            summary: '主观点支持继续持有',
            evidence: ['price=100'],
            evidence_by_dimension: {
              technical: ['价格仍在成本上方'],
              capital_flow: ['资金面数据缺失'],
              news_event: ['消息面数据缺失'],
            },
            failure_conditions: ['跌破成本区'],
          },
          opposing_thesis: {
            direction: 'neutral_bearish',
            action: 'reduce',
            summary: '反方提醒仓位风险',
            evidence: ['仓位偏高'],
            evidence_by_dimension: {
              account_risk: ['单票仓位偏高'],
              news_event: ['没有可确认的消息催化'],
            },
            failure_conditions: ['放量突破'],
          },
          judge_decision: {
            winner: 'primary',
            final_action: 'hold',
            decision_summary: '维持持有，但资金面和消息面证据不足，需要继续观察。',
            reason: '持有证据更强，但需要风控。',
            reason_points: ['账户风险可控', '资金面证据不足', '消息面证据不足'],
            dimension_assessments: [
              { dimension: 'account_risk', verdict: 'supports_primary', weight: 'high', summary: '账户仍可承受持有。', evidence: ['成本上方'], missing: [] },
              { dimension: 'capital_flow', verdict: 'insufficient_data', weight: 'medium', summary: '资金面未形成有效证据。', evidence: [], missing: ['未获取主力资金流'] },
              { dimension: 'news_event', verdict: 'insufficient_data', weight: 'medium', summary: '消息面未确认催化。', evidence: [], missing: ['新闻搜索结果不足'] },
            ],
            accepted_arguments: ['成本上方'],
            rejected_arguments: ['立即减仓'],
            risk_controls: ['跌破成本区复查'],
          },
          debug_outputs: {
            primary_report_raw: '## 原始主报告\n\n持仓策略：持有。',
            primary_thesis_raw: '{"action":"hold","summary":"主观点支持继续持有"}',
            opposing_thesis_raw: '{"action":"reduce","summary":"反方提醒仓位风险"}',
            judge_raw: '{"winner":"primary","final_action":"hold"}',
            final_report_with_debate: '## 最终结论\n\n持仓策略：持有。\n\n## 对抗式辩论裁决',
          },
        },
        context_summary: {
          account_count: 1,
          position_count: 2,
          investor: {
            risk_preference: 'balanced',
            trading_horizon: 'long_term',
            max_single_position_pct: 20,
            max_total_equity_exposure_pct: 80,
            max_acceptable_drawdown_pct: 15,
            default_stop_loss_pct: 8,
            notes: '偏长期持有',
          },
          accounts: [{ account_id: 7, account_name: 'A股主账户', market: 'cn', available_cash: 16982.65, total_equity: 736532.65 }],
          target_position: { symbol: '600519', quantity: 150000, avg_cost: 4.797, last_price: 5, unrealized_pnl: 30450, position_pct: 50.1 },
        },
        risk_gate: {
          schema_version: 1,
          source: 'debate_judge',
          trade_plan: {
            symbol: '600519',
            action: 'hold',
            order_type: 'manual',
            target_position_pct: 50.1,
            invalidation_conditions: [],
            review_triggers: ['跌破成本区复查'],
          },
          risk_gate: {
            status: 'passed',
            original_action: 'hold',
            allowed_action: 'hold',
            required_manual_review: false,
            blocked_reasons: [],
            warnings: [],
            checks: [
              {
                rule_id: 'critical_data_quality',
                passed: true,
                severity: 'info',
                message: '关键数据质量未标记为失败或不足。',
              },
            ],
          },
          quote: {
            symbol: '600519',
            last_price: 100,
            pct_change: 1.2,
            is_limit_up: false,
            is_limit_down: false,
          },
        },
      },
    ]));

    renderPage();

    fireEvent.click(screen.getByRole('button', { name: /展开配置/ }));
    await waitFor(() => {
      expect(screen.getByDisplayValue('A股主账户')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    await waitFor(() => {
      expect(mocks.traceStream).toHaveBeenCalledWith(expect.objectContaining({
        session_id: 'trace-12345678123412341234123456789abc',
        account_id: 7,
        analysis_mode: 'planning_execute',
        stock_code: undefined,
        report_intent: undefined,
        risk_preference: 'balanced',
        trading_horizon: 'long_term',
        max_single_position_pct: 20,
        max_total_equity_exposure_pct: 80,
        max_acceptable_drawdown_pct: 15,
        default_stop_loss_pct: 8,
        candidate_discovery_mode: 'thesis_desk_committee',
      }));
    });
    expect(screen.getByText('trace-12345678123412341234123456789abc')).toBeInTheDocument();
    vi.spyOn(crypto, 'randomUUID').mockRestore();

    expect(screen.getByText('A股主账户')).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    });
    expect(screen.getByRole('heading', { name: '最终结论' })).toBeInTheDocument();
    expect(screen.getByText('持有，但不急着加仓')).toBeInTheDocument();
    expect(screen.getByText('分析完成')).toBeInTheDocument();
    const createObjectURL = vi.fn((blob: Blob | MediaSource) => {
      expect(blob).toBeInstanceOf(Blob);
      return 'blob:agent-report';
    });
    const revokeObjectURL = vi.fn();
    Object.defineProperty(window.URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    fireEvent.click(screen.getByRole('button', { name: '导出 MD' }));
    expect(createObjectURL).toHaveBeenCalledTimes(1);
    const exportedBlob = createObjectURL.mock.calls[0]?.[0];
    expect(exportedBlob).toBeInstanceOf(Blob);
    await expect((exportedBlob as Blob).text()).resolves.toContain('## 最终结论');
    expect(anchorClick).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:agent-report');
    expect(screen.getAllByText('风控闸门').length).toBeGreaterThan(0);
    expect(screen.getAllByText('风控通过，允许执行「hold」。').length).toBeGreaterThan(0);
    expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    expect(screen.getByText('历史')).toBeInTheDocument();
  });

  it('loads a completed trace from local history', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    window.localStorage.setItem('dsa.agentTrace.history.v1', JSON.stringify([
      {
        id: 'trace-old',
        createdAt: '2026-05-03T10:00:00.000Z',
        message: '历史问题',
        stockCode: '601399',
        status: 'success',
        result: {
          success: true,
          session_id: 'trace-old',
          content: '历史结论',
          error: null,
          total_steps: 3,
          total_tokens: 88,
          provider: 'deepseek',
          model: 'deepseek/deepseek-v4-pro',
          mode: 'planning_execute',
          events: [],
          tool_calls: [],
          planner: { intent: 'position_review' },
          agent_user_context: { report: { primary_symbol: '601399' } },
          debate: null,
          stock_selection: {
            enabled: true,
            success: true,
            final_report_json: { orchestration_mode: 'legacy' },
            selection_context: { orchestration_mode: 'legacy' },
          },
          context_summary: {
            account_count: 1,
            position_count: 1,
            investor: { risk_preference: 'balanced', trading_horizon: 'long_term' },
            accounts: [{ account_id: 1, account_name: 'A股主账户', market: 'cn', available_cash: 100, total_equity: 200 }],
            target_position: { symbol: '601399', quantity: 150000 },
          },
        },
      },
    ]));

    renderPage();

    expect(await screen.findByText('历史')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /601399/ }));

    expect(screen.getByText('历史结论')).toBeInTheDocument();
    expect(screen.getByText('已加载历史')).toBeInTheDocument();
    expect(await screen.findByText('expert_graph')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /展开配置/ }));
    expect(screen.getByDisplayValue('601399')).toBeInTheDocument();
  });

  it('loads a completed trace from backend artifact when URL session is not in local history', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.getTraceSession.mockResolvedValue({
      id: 'trace-remote',
      createdAt: '2026-05-17T12:57:45',
      message: '帮我选一下下周可以入手的股票',
      stockCode: '',
      accountId: 3,
      status: 'success',
      result: {
        success: true,
        session_id: 'trace-remote',
        content: '远端落盘结论',
        error: null,
        total_steps: 33,
        total_tokens: 53301,
        provider: 'agent',
        model: 'xiaomi-mimo',
        mode: 'planning_execute',
        events: [],
        tool_calls: [],
        planner: { intent: 'watchlist_scan' },
        context_summary: {
          account_count: 1,
          position_count: 1,
          investor: { risk_preference: 'balanced', trading_horizon: 'long_term' },
        },
        stock_selection: { enabled: true, success: true, final_report_json: { orchestration_mode: 'expert_graph' } },
        risk_gate: null,
        artifact_dir: '/tmp/trace-remote',
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    });

    renderPage('/agent-trace/trace-remote');

    expect(await screen.findByText('远端落盘结论')).toBeInTheDocument();
    expect(screen.getByText('已从后端加载 Trace')).toBeInTheDocument();
    expect(mocks.getTraceSession).toHaveBeenCalledWith('trace-remote');
    expect(JSON.parse(window.localStorage.getItem('dsa.agentTrace.history.v1') || '[]')[0].id).toBe('trace-remote');
  });

  it('does not show OK for get_capital_flow events without explicit success', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'tool_done',
        step: 1,
        tool: 'get_capital_flow',
        display_name: '获取资金流向',
        arguments: { stock_code: '600519' },
        duration: 30.1,
        result_length: 120,
        result_preview: '{"status":"partial","main_net_inflow":123.4,"errors":["capital flow fetch failed"]}',
      },
      {
        type: 'done',
        success: true,
        session_id: 'trace-flow-failed',
        content: '资金流工具失败，资金面证据缺失。',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'deepseek',
        model: 'deepseek/deepseek-v4-pro',
        mode: 'planning_execute',
        planner: { intent: 'entry_analysis', required_tools: ['get_capital_flow'] },
        agent_user_context: { report: { analysis_mode: 'planning_execute' } },
        context_summary: { account_count: 0, position_count: 0, accounts: [], investor: null },
        debate: null,
      },
    ]));

    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /^运行$/ }));

    expect((await screen.findAllByText('get_capital_flow')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('FAIL').length).toBeGreaterThan(0);
    expect(screen.getByText('资金流工具失败，资金面证据缺失。')).toBeInTheDocument();
    expect(screen.getAllByText(/capital flow fetch failed/).length).toBeGreaterThan(0);
  });

  it('renders risk gate blocking reasons for T+1 sell attempts', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-risk-blocked',
        content: 'A 股 T+1 不允许当日卖出。',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'deepseek',
        model: 'deepseek/deepseek-v4-pro',
        mode: 'planning_execute',
        tool_calls: [],
        planner: { intent: 'position_review' },
        agent_user_context: { report: { analysis_mode: 'planning_execute' } },
        context_summary: { account_count: 0, position_count: 1, accounts: [], investor: null },
        debate: null,
        risk_gate: {
          schema_version: 1,
          source: 'debate_judge',
          trade_plan: {
            symbol: '600519',
            action: 'sell',
            order_type: 'manual',
            invalidation_conditions: ['卖出/减仓计划需结合可执行交易状态复核'],
          },
          risk_gate: {
            status: 'blocked',
            original_action: 'sell',
            allowed_action: 'manual_review',
            required_manual_review: true,
            blocked_reasons: ['A 股 T+1 约束：当日买入持仓不能在当日卖出或减仓。'],
            warnings: [],
            checks: [
              {
                rule_id: 'a_share_t_plus_one',
                passed: false,
                severity: 'blocking',
                message: 'A 股 T+1 约束：当日买入持仓不能在当日卖出或减仓。',
                suggested_action: 'manual_review',
              },
            ],
          },
          quote: {
            symbol: '600519',
            last_price: 100,
            is_limit_up: false,
            is_limit_down: false,
          },
        },
      },
    ]));

    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /^运行$/ }));

    expect((await screen.findAllByText('风控闸门')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('风控阻断：原动作「sell」被改为「manual_review」。').length).toBeGreaterThan(0);
  });

  it('does not send any stock code from the default stock-selection prompt', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-selection',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'deepseek',
        model: 'deepseek/deepseek-v4-pro',
        mode: 'planning_execute',
        tool_calls: [
          {
            step: 1,
            tool: 'discover_watchlist_candidates',
            arguments: { market: 'cn', seed_symbols: [], limit: 8 },
            success: true,
            duration: 0.3,
            result_length: 1000,
            result_preview: '{"status":"ok","candidate_source":"multi_recall","candidates":[{"code":"301183"...[truncated 4980 chars]',
            result_json: {
              status: 'ok',
              market: 'cn',
              candidate_source: 'expert_graph_discovery',
              candidate_count: 2,
              candidates: [
                {
                  code: '600001',
                  name: '测试一',
                  source: 'multi_expert_recall',
                  recall_sources: ['sequoia:turtle_trade', 'akshare:industry:半导体'],
                  candidate_experts: ['technical_candidate_expert', 'sector_theme_expert'],
                  candidate_dimensions: ['technical', 'sentiment'],
                  expert_confidences: { technical_candidate_expert: 0.78, sector_theme_expert: 0.63 },
                  matched_strategies: ['turtle_trade', 'rps_breakout'],
                  strategy_tags: ['breakout', 'rps'],
                  reason: '多专家候选共振：technical、sentiment。',
                  signal_score: 92.5,
                  consensus_bonus: 7.05,
                  lifecycle_status: 'new',
                  mixed_evidence: true,
                  latest_date: '2026-05-08',
                  metrics: { turnover: 150000000, rps: 94.2 },
                  reason_dimensions: [
                    { dimension: 'strategy', label: '策略', detail: 'Sequoia 形态/动量策略入池：海龟突破、RPS 强势突破' },
                    { dimension: 'technical', label: '技术面', detail: '形态/趋势信号满足候选条件；RPS=94.2' },
                    { dimension: 'capital', label: '资金面', detail: '成交额=1.50亿' },
                    { dimension: 'sentiment', label: '情绪/热点', detail: '来自强势板块「半导体」成分股' },
                  ],
                },
                {
                  code: '301183',
                  name: '东田微',
                  source: 'alphasift:volume_breakout',
                  recall_sources: ['alphasift:volume_breakout'],
                  candidate_experts: ['strategy_factor_expert'],
                  candidate_dimensions: ['strategy'],
                  expert_confidences: { strategy_factor_expert: 0.74 },
                  matched_strategies: ['volume_breakout'],
                  strategy_tags: ['breakout', 'liquidity'],
                  reason: 'AlphaSift 放量突破策略入池。',
                  signal_score: 88,
                  latest_date: '2026-05-08',
                  metrics: { amount: 230000000, breakout_20d_pct: 4.2 },
                  reason_dimensions: [
                    { dimension: 'strategy', label: '策略', detail: 'AlphaSift YAML 多因子策略入池：放量突破' },
                    { dimension: 'technical', label: '技术面', detail: '20 日突破幅度=4.2' },
                    { dimension: 'capital', label: '资金面', detail: '成交额=2.30亿' },
                  ],
                },
                {
                  code: '600002',
                  name: '测试二',
                  source: 'akshare:industry:半导体',
                  recall_sources: ['akshare:industry:半导体'],
                  candidate_experts: ['sector_theme_expert'],
                  candidate_dimensions: ['sentiment'],
                  expert_confidences: { sector_theme_expert: 0.61 },
                  strategy_tags: ['hot_sector'],
                  reason: '强势板块成分股进入候选池。',
                  signal_score: 71,
                  latest_date: '2026-05-08',
                  metrics: { sector_rank: 3 },
                  reason_dimensions: [
                    { dimension: 'sentiment', label: '情绪/热点', detail: '来自强势板块「半导体」成分股' },
                  ],
                },
              ],
              expert_packets: [
                {
                  expert: 'strategy_factor_expert',
                  dimension: 'strategy',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [{ code: '301183', name: '东田微' }],
                  themes: [],
                },
                {
                  expert: 'technical_candidate_expert',
                  dimension: 'technical',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [{ code: '600001', name: '测试一' }],
                  themes: [],
                },
                {
                  expert: 'sector_theme_expert',
                  dimension: 'sentiment',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [{ code: '600001', name: '测试一' }, { code: '600002', name: '测试二' }],
                  themes: [],
                },
                {
                  expert: 'news_event_expert',
                  dimension: 'message',
                  status: 'empty',
                  data_quality: { freshness: 'intraday', warnings: ['近期新闻未形成可验证个股候选'] },
                  candidates: [],
                  themes: [],
                },
                {
                  expert: 'sentiment_theme_expert',
                  dimension: 'sentiment',
                  status: 'partial',
                  data_quality: { freshness: 'intraday', warnings: ['事件仍在观察期'] },
                  candidates: [],
                  themes: [
                    {
                      theme: '人工智能',
                      event_title: 'AI 应用订单与算力投入持续升温',
                      status: 'developing',
                      reason: '热点仍处主题观察阶段，等待后续订单、业绩或资金流验证。',
                      confidence: 0.58,
                    },
                  ],
                },
                {
                  expert: 'capital_flow_expert',
                  dimension: 'capital',
                  status: 'empty',
                  data_quality: { freshness: 'unknown', warnings: ['资金面全市场发现尚未接线'] },
                  candidates: [],
                  themes: [],
                },
                {
                  expert: 'fundamental_expert',
                  dimension: 'fundamental',
                  status: 'empty',
                  data_quality: { freshness: 'unknown', warnings: ['基本面全市场发现尚未接线'] },
                  candidates: [],
                  themes: [],
                },
              ],
              themes: [
                {
                  theme: '人工智能',
                  event_title: 'AI 应用订单与算力投入持续升温',
                  status: 'developing',
                  reason: '热点仍处主题观察阶段，等待后续订单、业绩或资金流验证。',
                  confidence: 0.58,
                },
              ],
              capacity: {
                max_candidates_to_deep_dive: 8,
                min_per_expert: 1,
                max_per_expert: 4,
                max_theme_watch_items: 8,
              },
              quality: {
                candidate_count: 3,
                hard_strategy_trunk_missing: false,
                hard_exclusion_count: 1,
                fallback_count: 0,
                multi_source_count: 1,
                dimension_counts: { strategy: 1, technical: 1, sentiment: 2 },
                source_counts: { strategy_factor_expert: 1, technical_candidate_expert: 1, sector_theme_expert: 2 },
                lifecycle_counts: { new: 3 },
              },
              hard_exclusion: {
                excluded_count: 1,
                reason_counts: { st_or_special_treatment: 1 },
                examples: [{ code: '600999', name: 'ST测试', reason: 'st_or_special_treatment', source: 'sequoia:turtle_trade' }],
                policy: { min_avg_amount: 0, min_listing_days: 0, blacklist_count: 0, enforce_name_code_match: true },
              },
              discovery_steps: [
                { source: 'candidate_expert:strategy_factor_expert', status: 'ok', count: 1, theme_count: 0 },
                { source: 'candidate_expert:technical_candidate_expert', status: 'ok', count: 1, theme_count: 0 },
                { source: 'candidate_expert:sector_theme_expert', status: 'ok', count: 2, theme_count: 0 },
                { source: 'candidate_expert:capital_flow_expert', status: 'empty', count: 0, theme_count: 0 },
                { source: 'candidate_expert:news_event_expert', status: 'empty', count: 0, theme_count: 0 },
                { source: 'candidate_expert:sentiment_theme_expert', status: 'partial', count: 0, theme_count: 1 },
                {
                  source: 'event_impact',
                  status: 'watch_only',
                  count: 0,
                  events: [
                    {
                      event_id: 'hormuz-watch',
                      title: '霍尔木兹海峡允许通行，原油风险溢价回落',
                      snippet: '事件仍处突发阶段，后续油价、运价和保险费变化仍待验证。',
                      event_type: 'geopolitical_energy',
                      maturity: 'developing',
                      impact_variables: ['oil_risk_premium', 'shipping_cost', 'risk_appetite'],
                      watch_themes: ['石油石化', '航运港口', '化工', '航空机场'],
                      validation_window_days: 7,
                      source: 'example.com',
                      published_date: '2026-05-13',
                      validation_matches: [
                        { theme: '石油石化', status: 'watch_only', results: [] },
                        { theme: '航运港口', status: 'watch_only', results: [] },
                      ],
                    },
                  ],
                  queries: [],
                  diagnostics: [],
                },
                { source: 'get_sector_rankings', status: 'ok', sectors: ['半导体'] },
                { source: 'sector_constituents', sector: '半导体', status: 'ok', count: 1 },
              ],
              next_required_tools: ['get_realtime_quote', 'analyze_trend', 'get_capital_flow'],
            },
          },
          {
            step: 2,
            tool: 'get_realtime_quote',
            arguments: { stock_code: '600001' },
            success: true,
            duration: 0.2,
            result_length: 20,
            result_preview: '{"price":10}',
          },
        ],
        planner: { intent: 'watchlist_scan' },
        agent_user_context: { report: { intent: 'watchlist_scan' } },
        context_summary: { account_count: 0, position_count: 0, accounts: [], investor: null },
        debate: null,
        stock_selection: {
          enabled: true,
          success: true,
          selection_context: {
            candidate_strategy: 'hot_sector',
            orchestration_mode: 'expert_graph',
            next_step: 'render_final_report',
            expert_state: {
              status: 'ok',
              orchestration_mode: 'expert_graph',
              expert_opinions: {
                market_regime_expert: {
                  expert_name: 'market_regime_expert',
                  dimension: 'market_regime',
                  verdict: 'caution',
                  confidence: 0.8,
                  summary: '市场波动正常但仍需控制追高。',
                  supporting_evidence: ['趋势向上时可接受回踩确认后的顺势策略。'],
                  missing_evidence: [],
                  risk_flags: ['市场状态数据质量有限'],
                },
                candidate_discovery_expert: {
                  expert_name: 'candidate_discovery_expert',
                  dimension: 'candidate_discovery',
                  verdict: 'support',
                  confidence: 0.7,
                  summary: '候选池共 3 只，来源维度包含策略、技术面、资金面、情绪/热点。',
                  supporting_evidence: ['600001 测试一: Sequoia 形态/动量策略入池'],
                  missing_evidence: [],
                  risk_flags: [],
                },
                technical_expert: {
                  expert_name: 'technical_expert',
                  dimension: 'technical',
                  verdict: 'support',
                  confidence: 0.72,
                  summary: '技术结构证据已覆盖趋势/结构工具。',
                  supporting_evidence: ['600001 测试一: technical=support'],
                  missing_evidence: [],
                  risk_flags: [],
                },
                capital_chip_expert: {
                  expert_name: 'capital_chip_expert',
                  dimension: 'capital_chip',
                  verdict: 'caution',
                  confidence: 0.55,
                  summary: '资金/筹码证据仍有缺口。',
                  supporting_evidence: ['600001 测试一: capital_flow=tool_failed'],
                  missing_evidence: ['get_capital_flow'],
                  risk_flags: ['资金流失败时不追高'],
                },
                news_sentiment_expert: {
                  expert_name: 'news_sentiment_expert',
                  dimension: 'news_sentiment',
                  verdict: 'neutral',
                  confidence: 0.45,
                  summary: '候选池存在消息/热点/情绪来源，但深度情绪工具仍未闭环。',
                  supporting_evidence: ['600002 测试二: 情绪/热点 - 来自强势板块「半导体」成分股'],
                  missing_evidence: ['sentiment_tools'],
                  risk_flags: [],
                },
              },
            },
            stages: {
              candidate_discovery: { status: 'ok', summary: { candidate_codes: ['600001', '301183', '600002'] }, full_ref: 'candidate_discovery.json' },
              candidate_screening: { status: 'ok', summary: { deep_dive_targets: ['600001'] }, full_ref: 'candidate_screening.json' },
              single_stock_deep_dive: { status: 'ok', summary: { wait_targets: ['600001'] }, full_ref: 'deep_dive_results.json' },
              portfolio_allocation: { status: 'ok', summary: { portfolio_action: 'wait' }, full_ref: 'portfolio_allocation.json' },
              adversarial_review: { status: 'ok', summary: { opposing_summary: '资金面缺失' }, full_ref: 'adversarial_review.json' },
              judge_decision: { status: 'ok', summary: { final_action: 'wait', primary_plan_verdict: 'accept_with_changes', decision_summary: '等待确认', next_step: 'render_final_report' }, full_ref: 'judge_decision.json' },
            },
          },
          final_report_json: {
            orchestration_mode: 'expert_graph',
            expert_state: {
              status: 'ok',
              orchestration_mode: 'expert_graph',
              expert_opinions: {
                market_regime_expert: {
                  expert_name: 'market_regime_expert',
                  dimension: 'market_regime',
                  verdict: 'caution',
                  confidence: 0.8,
                  summary: '市场波动正常但仍需控制追高。',
                  supporting_evidence: ['趋势向上时可接受回踩确认后的顺势策略。'],
                  missing_evidence: [],
                  risk_flags: ['市场状态数据质量有限'],
                },
                candidate_discovery_expert: {
                  expert_name: 'candidate_discovery_expert',
                  dimension: 'candidate_discovery',
                  verdict: 'support',
                  confidence: 0.7,
                  summary: '候选池共 3 只，来源维度包含策略、技术面、资金面、情绪/热点。',
                  supporting_evidence: ['600001 测试一: Sequoia 形态/动量策略入池'],
                  missing_evidence: [],
                  risk_flags: [],
                },
                technical_expert: {
                  expert_name: 'technical_expert',
                  dimension: 'technical',
                  verdict: 'support',
                  confidence: 0.72,
                  summary: '技术结构证据已覆盖趋势/结构工具。',
                  supporting_evidence: ['600001 测试一: technical=support'],
                  missing_evidence: [],
                  risk_flags: [],
                },
                capital_chip_expert: {
                  expert_name: 'capital_chip_expert',
                  dimension: 'capital_chip',
                  verdict: 'caution',
                  confidence: 0.55,
                  summary: '资金/筹码证据仍有缺口。',
                  supporting_evidence: ['600001 测试一: capital_flow=tool_failed'],
                  missing_evidence: ['get_capital_flow'],
                  risk_flags: ['资金流失败时不追高'],
                },
                news_sentiment_expert: {
                  expert_name: 'news_sentiment_expert',
                  dimension: 'news_sentiment',
                  verdict: 'neutral',
                  confidence: 0.45,
                  summary: '候选池存在消息/热点/情绪来源，但深度情绪工具仍未闭环。',
                  supporting_evidence: ['600002 测试二: 情绪/热点 - 来自强势板块「半导体」成分股'],
                  missing_evidence: ['sentiment_tools'],
                  risk_flags: [],
                },
              },
            },
            candidate_discovery: {
              summary: {
                candidate_codes: ['600001', '301183', '600002'],
                candidate_sources: ['sequoia:multi_strategy', 'alphasift:volume_breakout', 'akshare:industry:半导体'],
                main_limitations: ['候选需要深度取证'],
              },
              full: {
                candidates: [
                  {
                    code: '600001',
                    name: '测试一',
                    source: 'sequoia:multi_strategy',
                    recall_sources: ['sequoia:turtle_trade', 'akshare:industry:半导体'],
                    matched_strategies: ['turtle_trade', 'rps_breakout'],
                    strategy_tags: ['breakout', 'rps'],
                    reason: '多策略共振：ma_volume, turtle_trade, rps_breakout。',
                    signal_score: 92.5,
                    latest_date: '2026-05-08',
                    metrics: { turnover: 150000000, rps: 94.2 },
                    reason_dimensions: [
                      { dimension: 'strategy', label: '策略', detail: 'Sequoia 形态/动量策略入池：海龟突破、RPS 强势突破' },
                      { dimension: 'technical', label: '技术面', detail: '形态/趋势信号满足候选条件；RPS=94.2' },
                      { dimension: 'capital', label: '资金面', detail: '成交额=1.50亿' },
                      { dimension: 'sentiment', label: '情绪/热点', detail: '来自强势板块「半导体」成分股' },
                    ],
                  },
                  {
                    code: '301183',
                    name: '东田微',
                    source: 'alphasift:volume_breakout',
                    recall_sources: ['alphasift:volume_breakout'],
                    matched_strategies: ['volume_breakout'],
                    strategy_tags: ['breakout', 'liquidity'],
                    reason: 'AlphaSift 放量突破策略入池。',
                    signal_score: 88,
                    latest_date: '2026-05-08',
                    metrics: { amount: 230000000, breakout_20d_pct: 4.2 },
                    reason_dimensions: [
                      { dimension: 'strategy', label: '策略', detail: 'AlphaSift YAML 多因子策略入池：放量突破' },
                      { dimension: 'technical', label: '技术面', detail: '20 日突破幅度=4.2' },
                      { dimension: 'capital', label: '资金面', detail: '成交额=2.30亿' },
                    ],
                  },
                  {
                    code: '600002',
                    name: '测试二',
                    source: 'akshare:industry:半导体',
                    recall_sources: ['akshare:industry:半导体'],
                    matched_strategies: [],
                    strategy_tags: ['hot_sector'],
                    reason: '强势板块成分股进入候选池。',
                    signal_score: 71,
                    latest_date: '2026-05-08',
                    metrics: { sector_rank: 3 },
                    reason_dimensions: [
                      { dimension: 'sentiment', label: '情绪/热点', detail: '来自强势板块「半导体」成分股' },
                    ],
                  },
                ],
              },
            },
            candidate_screening: { summary: { deep_dive_targets: ['600001'], main_limitations: ['资金面待确认'] } },
            single_stock_deep_dive: { summary: { wait_targets: ['600001'], open_targets: [], reject_targets: [] } },
            portfolio_allocation: {
              summary: { portfolio_action: 'wait', core_reason: '等待确认' },
              full: {
                positions_plan: [
                  { rank: 1, code: '600001', name: '测试一', action: 'wait', initial_position_pct: 0, entry_condition: '回踩确认' },
                ],
              },
            },
            adversarial_review: { summary: { top_risk_points: ['资金面缺失'], top_evidence_gaps: ['capital_flow'] } },
            judge_decision: { summary: { final_action: 'wait', primary_plan_verdict: 'accept_with_changes', decision_summary: '等待确认', next_step: 'render_final_report' }, full: { risk_controls: ['不追高'] } },
          },
        },
        artifact_dir: '/tmp/trace-selection',
      },
    ]));

    renderPage();

    expect(await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/)).toBeInTheDocument();
    expect(screen.queryByText('默认股票代码未在问题中出现，本次不发送该代码；意图由后端模型识别。')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    await waitFor(() => {
      expect(mocks.traceStream).toHaveBeenCalledWith(expect.objectContaining({
        stock_code: undefined,
      }));
    });
    expect(screen.getByText('选股结论')).toBeInTheDocument();
    expect(screen.getByText('数据与候选池')).toBeInTheDocument();
    expect(screen.queryByText('候选池层')).not.toBeInTheDocument();
    expect(await screen.findByText('候选来源审计')).toBeInTheDocument();
    expect(screen.getByText('消息/事件观察 (1)')).toBeInTheDocument();
    expect(screen.getByText('霍尔木兹海峡允许通行，原油风险溢价回落')).toBeInTheDocument();
    expect(screen.getAllByText('等待验证').length).toBeGreaterThan(0);
    expect(screen.getAllByText('石油石化').length).toBeGreaterThan(0);
    expect(screen.getAllByText('航运港口').length).toBeGreaterThan(0);
    expect(screen.getAllByText('观察中，未形成个股候选').length).toBeGreaterThan(0);
    expect(screen.getByText('多专家选股状态')).toBeInTheDocument();
    expect(screen.getByText('expert_graph')).toBeInTheDocument();
    expect(screen.getByText('1. L1 候选发现专家')).toBeInTheDocument();
    expect(screen.getByText(/这些专家只负责 discover 候选池/)).toBeInTheDocument();
    expect(screen.queryByText('2. 维度验证')).not.toBeInTheDocument();
    expect(screen.queryByText('市场环境专家')).not.toBeInTheDocument();
    expect(screen.queryByText('候选发现专家')).not.toBeInTheDocument();
    expect(screen.queryByText('技术结构专家')).not.toBeInTheDocument();
    expect(screen.queryByText('资金筹码专家')).not.toBeInTheDocument();
    expect(screen.queryByText('消息情绪专家')).not.toBeInTheDocument();
    expect(screen.getByText('2. 合并后的候选池')).toBeInTheDocument();
    expect(screen.getAllByText('AlphaSift 策略多因子专家').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Sequoia 技术形态专家').length).toBeGreaterThan(0);
    expect(screen.getAllByText('板块主题专家').length).toBeGreaterThan(0);
    expect(screen.getAllByText('资金发现专家').length).toBeGreaterThan(0);
    expect(screen.getAllByText('消息事件专家').length).toBeGreaterThan(0);
    expect(screen.getAllByText('必须出候选').length).toBeGreaterThan(0);
    expect(screen.getAllByText('默认只观察主题').length).toBeGreaterThan(0);
    expect(screen.getAllByText('情绪/宏观专家').length).toBeGreaterThan(0);
    expect(screen.getAllByText('基本面发现专家').length).toBeGreaterThan(0);
    expect(screen.getByText('深挖上限 8')).toBeInTheDocument();
    expect(screen.getByText('专家保底 1')).toBeInTheDocument();
    expect(screen.getByText('单专家最多 4')).toBeInTheDocument();
    expect(screen.getByText('候选池质量与门禁')).toBeInTheDocument();
    expect(screen.getByText('硬策略主干可用')).toBeInTheDocument();
    expect(screen.getByText('硬排除 1')).toBeInTheDocument();
    expect(screen.getByText('硬排除明细')).toBeInTheDocument();
    expect(screen.getAllByText(/ST\/特殊处理 1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/600999 ST测试 · ST\/特殊处理/)).toBeInTheDocument();
    expect(screen.getByText('多源共振')).toBeInTheDocument();
    expect(screen.getByText('新进入 3')).toBeInTheDocument();
    expect(screen.getByText('主题观察 (1)')).toBeInTheDocument();
    expect(screen.getByText('人工智能')).toBeInTheDocument();
    expect(screen.getByText('AI 应用订单与算力投入持续升温')).toBeInTheDocument();
    expect(screen.getByText('候选入池榜')).toBeInTheDocument();
    expect(screen.getByText('专家维度与原始候选分组')).toBeInTheDocument();
    expect(screen.getAllByText('策略候选').length).toBeGreaterThan(0);
    expect(screen.getAllByText('技术面候选').length).toBeGreaterThan(0);
    expect(screen.queryByText('资金面候选')).not.toBeInTheDocument();
    expect(screen.getAllByText('情绪/热点候选').length).toBeGreaterThan(0);
    expect(screen.queryByText('为什么后续工具查这些股票')).not.toBeInTheDocument();
    expect(screen.queryByText('入池候选与理由')).not.toBeInTheDocument();
    expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    expect(screen.queryByText('候选池列表 (3)')).not.toBeInTheDocument();
    expect(screen.getAllByText(/强势板块/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('600001').length).toBeGreaterThan(0);
    expect(screen.getAllByText('测试一').length).toBeGreaterThan(0);
    expect(screen.getAllByText('301183').length).toBeGreaterThan(0);
    expect(screen.getAllByText('东田微').length).toBeGreaterThan(0);
    expect(screen.getAllByText('共振 +7.05').length).toBeGreaterThan(0);
    expect(screen.getAllByText('新进入').length).toBeGreaterThan(0);
    expect(screen.getAllByText('存在反证').length).toBeGreaterThan(0);
    expect(screen.getAllByText('多专家候选共振').length).toBeGreaterThan(0);
    expect(screen.getAllByText('AlphaSift 多因子：volume_breakout').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/AlphaSift YAML 多因子策略入池/).length).toBeGreaterThan(0);
    fireEvent.click(screen.getAllByRole('button', { name: '证据' })[0]);
    expect(screen.getAllByText(/证据拆解/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/海龟突破/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/RPS 强势突破/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Sequoia 形态\/动量策略入池/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('策略').length).toBeGreaterThan(0);
    expect(screen.getAllByText('技术面').length).toBeGreaterThan(0);
    expect(screen.getAllByText('资金面').length).toBeGreaterThan(0);
    expect(screen.getAllByText('情绪/热点').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/形态\/趋势信号满足候选条件/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/流动性代理：成交额=1.50亿/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/多策略共振：均线放量突破, 海龟突破, RPS 强势突破。；形态\/趋势信号满足候选条件/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/来自强势板块「半导体」成分股/).length).toBeGreaterThan(0);
    expect(screen.queryByText('ma_volume')).not.toBeInTheDocument();
    expect(screen.queryByText('turtle_trade')).not.toBeInTheDocument();
    expect(screen.queryByText('rps_breakout')).not.toBeInTheDocument();
  });

  it('forces portfolio context injection when a specific account is selected', async () => {
    mocks.getAccounts.mockResolvedValue({
      accounts: [{ id: 3, name: '5w账户', market: 'cn', baseCurrency: 'CNY', isActive: true }],
    });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-account-context',
        content: '持仓建议',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'mimo',
        mode: 'planning_execute',
        tool_calls: [],
        planner: { intent: 'position_review' },
        context_summary: { account_count: 1, position_count: 1 },
        debate: null,
      },
    ]));

    renderPage();

    fireEvent.click(screen.getByRole('button', { name: /展开配置/ }));
    await waitFor(() => {
      expect(screen.getByDisplayValue('5w账户')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /注入持仓/ }));
    expect(screen.getByRole('checkbox', { name: /注入持仓/ })).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    await waitFor(() => {
      expect(mocks.traceStream).toHaveBeenCalledWith(expect.objectContaining({
        account_id: 3,
        inject_portfolio_context: true,
      }));
    });
  });

  it('keeps expert_graph status without treating validation expert_state as L1 discovery', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'selection_expert_graph_done',
        payload: {
          orchestration_mode: 'expert_graph',
          expert_count: 7,
          experts: ['market_regime_expert', 'candidate_discovery_expert'],
          expert_state: {
            status: 'completed',
            orchestration_mode: 'expert_graph',
            expert_opinions: {
              market_regime_expert: {
                expert_name: 'market_regime_expert',
                dimension: 'market_regime',
                verdict: 'neutral',
                confidence: 0.66,
                summary: '市场环境中性。',
                supporting_evidence: ['波动正常'],
                missing_evidence: [],
                risk_flags: [],
              },
            },
          },
        },
      },
      {
        type: 'done',
        success: true,
        session_id: 'trace-selection-partial',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'deepseek/deepseek-chat',
        mode: 'planning_execute',
        stock_selection: null,
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '我现在有5w，我希望你帮我选股' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('选股结论')).toBeInTheDocument();
    expect(await screen.findByText('expert_graph')).toBeInTheDocument();
    expect(await screen.findByText(/本轮没有返回 L1 候选发现专家包/)).toBeInTheDocument();
    expect(screen.queryByText('1. L1 候选发现专家')).not.toBeInTheDocument();
    expect(screen.queryByText('市场环境专家')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/本次选股结果仍为 legacy/)).not.toBeInTheDocument();
    });
  });

  it('keeps expert_graph status when final payload is stale legacy but hides validation experts from L1', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'selection_expert_graph_done',
        payload: {
          orchestration_mode: 'expert_graph',
          expert_count: 7,
          experts: ['news_sentiment_expert'],
          expert_state: {
            status: 'completed',
            orchestration_mode: 'expert_graph',
            expert_opinions: {
              news_sentiment_expert: {
                expert_name: 'news_sentiment_expert',
                dimension: 'news_sentiment',
                verdict: 'insufficient_data',
                confidence: 0.5,
                summary: '消息面证据不足。',
                supporting_evidence: [],
                missing_evidence: ['缺少个股新闻评分'],
                risk_flags: [],
              },
            },
          },
        },
      },
      {
        type: 'done',
        success: true,
        session_id: 'trace-selection-stale',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'deepseek/deepseek-chat',
        mode: 'planning_execute',
        stock_selection: {
          enabled: true,
          success: true,
          final_report_json: { orchestration_mode: 'legacy' },
          selection_context: { orchestration_mode: 'legacy' },
        },
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '我现在有5w，我希望你帮我选股' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('选股结论')).toBeInTheDocument();
    expect(await screen.findByText('expert_graph')).toBeInTheDocument();
    expect(await screen.findByText(/本轮没有返回 L1 候选发现专家包/)).toBeInTheDocument();
    expect(screen.queryByText('消息情绪专家')).not.toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText(/本次选股结果仍为 legacy/)).not.toBeInTheDocument();
    });
  });

  it('does not show legacy expert warning when MiMo fails but request falls back to watchlist scan', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({
      runtime_config: {
        agent_orchestration_mode: 'expert_graph',
        mimo_intent_classifier_configured: true,
        mimo_intent_classifier_model: 'mimo-v2.5',
      },
    });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'context_ready',
        session_id: 'trace-intent-failed',
        agent_user_context: { report: { intent: 'watchlist_scan', analysis_mode: 'planning_execute' } },
        context_summary: {
          intent_resolution: {
            source: 'default',
            intent: 'watchlist_scan',
            classifier_configured: true,
            classifier_model: 'mimo-v2.5',
            classifier_success: false,
            classifier_error: 'Not supported model MiMo-V2.5',
          },
        },
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
      { type: 'planner_ready', session_id: 'trace-intent-failed', planner: { intent: 'watchlist_scan', required_tools: [] } },
      {
        type: 'done',
        success: true,
        session_id: 'trace-intent-failed',
        content: '选股结论',
        error: null,
        total_steps: 0,
        total_tokens: 0,
        provider: 'agent',
        model: 'mimo-v2.5',
        mode: 'planning_execute',
        tool_calls: [],
        agent_user_context: { report: { intent: 'watchlist_scan', analysis_mode: 'planning_execute' } },
        context_summary: {
          intent_resolution: {
            source: 'default',
            intent: 'watchlist_scan',
            classifier_configured: true,
            classifier_model: 'mimo-v2.5',
            classifier_success: false,
            classifier_error: 'Not supported model MiMo-V2.5',
          },
        },
        stock_selection: null,
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '帮我选一下下周可以入手的股票' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText(/本轮没有返回 L1 候选发现专家包/)).toBeInTheDocument();
    expect(screen.queryByText(/本次选股结果仍为 legacy/)).not.toBeInTheDocument();
    expect(screen.queryByText(/本次请求未进入选股链路/)).not.toBeInTheDocument();
  });

  it('shows the running selection stage while expert_state is still pending', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeDelayedStreamResponse(
      [
        {
          type: 'context_ready',
          session_id: 'trace-selection-running',
          agent_user_context: { report: { intent: 'watchlist_scan', analysis_mode: 'planning_execute' } },
          context_summary: { intent_resolution: { source: 'mimo', intent: 'watchlist_scan', classifier_success: true } },
          runtime_config: { agent_orchestration_mode: 'expert_graph' },
        },
        { type: 'planner_ready', session_id: 'trace-selection-running', planner: { intent: 'watchlist_scan', required_tools: [] } },
        { type: 'selection_start', message: '开始五阶段选股流水线。' },
        {
          type: 'selection_candidate_discovery_done',
          payload: { status: 'partial', summary: { candidate_codes: ['601518', '002090'] } },
        },
      ],
      [
        {
          type: 'done',
          success: true,
          session_id: 'trace-selection-running',
          content: '选股结论',
          error: null,
          total_steps: 0,
          total_tokens: 0,
          provider: 'agent',
          model: 'mimo-v2.5',
          mode: 'planning_execute',
          tool_calls: [],
          agent_user_context: { report: { intent: 'watchlist_scan', analysis_mode: 'planning_execute' } },
          stock_selection: null,
          runtime_config: { agent_orchestration_mode: 'expert_graph' },
        },
      ],
      120,
    ));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '帮我选一下下周可以入手的股票' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText(/多专家候选发现正在运行/)).toBeInTheDocument();
    expect(screen.getByText(/最新阶段：候选发现完成/)).toBeInTheDocument();
    expect(screen.queryByText(/请重新运行选股链路/)).not.toBeInTheDocument();
    expect(await screen.findByText(/本轮没有返回 L1 候选发现专家包/)).toBeInTheDocument();
  });

  it('renders fallback seed candidates as observation pool instead of strategy candidates', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-fallback-candidates',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'deepseek/deepseek-chat',
        mode: 'planning_execute',
        tool_calls: [
          {
            step: 1,
            tool: 'discover_watchlist_candidates',
            arguments: { market: 'cn', candidate_source: 'fallback', limit: 8 },
            success: true,
            duration: 0,
            result_length: 100,
            result_json: {
              status: 'partial',
              candidate_source: 'fallback',
              fallback_used: true,
              candidates: [
                {
                  code: '688981',
                  name: '中芯国际',
                  source: 'fallback_seed_pool',
                  recall_sources: ['fallback_seed_pool'],
                  reason: '半导体制造核心标的，适合承接科技板块强弱判断。',
                  signal_score: 50,
                  reason_dimensions: [
                    { dimension: 'strategy', label: '策略', detail: '半导体制造核心标的，适合承接科技板块强弱判断。' },
                    { dimension: 'strategy', label: '策略', detail: '固定种子池兜底，仅用于保证后续取证链路可运行' },
                  ],
                },
              ],
              discovery_steps: [{ source: 'fallback_seed_pool', status: 'ok', count: 1 }],
            },
          },
        ],
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '我现在有5w，我希望你帮我选股' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('选股结论')).toBeInTheDocument();
    expect(await screen.findByText('兜底观察池 (1)')).toBeInTheDocument();
    expect(screen.getByText(/不是策略、资金或消息面筛选结果/)).toBeInTheDocument();
    expect(screen.getAllByText('688981').length).toBeGreaterThan(0);
    expect(screen.getAllByText('中芯国际').length).toBeGreaterThan(0);
    expect(screen.queryByText('策略候选')).not.toBeInTheDocument();
    expect(screen.queryByText(/固定种子池兜底，仅用于保证后续取证链路可运行$/)).not.toBeInTheDocument();
  });

  it('groups candidates by raw expert packets instead of merged candidate dimensions', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-expert-groups',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'deepseek/deepseek-chat',
        mode: 'planning_execute',
        tool_calls: [
          {
            step: 1,
            tool: 'discover_watchlist_candidates',
            arguments: { market: 'cn', seed_symbols: [], limit: 8 },
            success: true,
            duration: 0.2,
            result_length: 1000,
            result_json: {
              status: 'ok',
              candidate_source: 'expert_graph_discovery',
              candidate_count: 1,
              candidates: [
                {
                  code: '600001',
                  name: '测试一',
                  source: 'multi_expert_recall',
                  candidate_experts: ['technical_candidate_expert', 'sector_theme_expert'],
                  candidate_dimensions: ['technical', 'sentiment'],
                  reason_dimensions: [
                    { dimension: 'strategy', label: '策略', detail: 'Sequoia 形态/动量策略入池：海龟突破、RPS 强势突破' },
                    { dimension: 'technical', label: '技术面', detail: '形态/趋势信号满足候选条件；RPS=94.2' },
                    { dimension: 'capital', label: '资金面', detail: '流动性代理：成交额=1.50亿' },
                  ],
                  signal_score: 92.5,
                },
              ],
              expert_packets: [
                {
                  expert: 'strategy_factor_expert',
                  dimension: 'strategy',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [{ code: '301183', name: '东田微', source: 'alphasift:volume_breakout', reason: 'AlphaSift 放量突破策略入池。' }],
                  themes: [],
                },
                {
                  expert: 'technical_candidate_expert',
                  dimension: 'technical',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [{ code: '600001', name: '测试一', source: 'sequoia:turtle_trade', reason: 'Sequoia 技术形态候选。' }],
                  themes: [],
                },
                {
                  expert: 'capital_flow_expert',
                  dimension: 'capital',
                  status: 'empty',
                  data_quality: { freshness: 'daily', warnings: ['Capital-flow full-market discovery is not wired yet'] },
                  candidates: [],
                  themes: [],
                },
              ],
              quality: { candidate_count: 1, hard_strategy_trunk_missing: false, hard_exclusion_count: 0, fallback_count: 0, multi_source_count: 1, dimension_counts: { technical: 1 }, expert_counts: { technical_candidate_expert: 1 }, lifecycle_counts: { new: 1 } },
            },
          },
        ],
        stock_selection: null,
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '帮我选一下下周可以入手的股票' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('专家维度与原始候选分组')).toBeInTheDocument();
    expect(screen.getByText('候选入池榜')).toBeInTheDocument();
    expect(screen.getByText('策略候选')).toBeInTheDocument();
    expect(screen.getByText('技术面候选')).toBeInTheDocument();
    expect(screen.queryByText('资金面候选')).not.toBeInTheDocument();
    expect(screen.getByText('301183')).toBeInTheDocument();
    expect(screen.getByText('东田微')).toBeInTheDocument();
    expect(screen.getAllByText('600001').length).toBeGreaterThan(0);
  });

  it('dedupes expert grouped candidates across dimensions so resonance names do not repeat in every bucket', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-expert-groups-dedupe',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'deepseek/deepseek-chat',
        mode: 'planning_execute',
        tool_calls: [
          {
            step: 1,
            tool: 'discover_watchlist_candidates',
            arguments: { market: 'cn', seed_symbols: [], limit: 8 },
            success: true,
            duration: 0.2,
            result_length: 1000,
            result_json: {
              status: 'ok',
              candidate_source: 'expert_graph_discovery',
              candidate_count: 3,
              candidates: [
                {
                  code: '600001',
                  name: '测试一',
                  source: 'multi_expert_recall',
                  candidate_experts: ['strategy_factor_expert', 'technical_candidate_expert', 'fundamental_expert'],
                  candidate_dimensions: ['strategy', 'technical', 'fundamental'],
                  reason_dimensions: [
                    { dimension: 'strategy', label: '策略', detail: 'AlphaSift 策略共振' },
                    { dimension: 'technical', label: '技术面', detail: '形态突破' },
                    { dimension: 'fundamental', label: '基本面', detail: '质量因子较高' },
                  ],
                  signal_score: 100,
                },
                {
                  code: '600002',
                  name: '测试二',
                  source: 'sequoia:turtle_trade',
                  candidate_experts: ['technical_candidate_expert'],
                  candidate_dimensions: ['technical'],
                  signal_score: 90,
                },
                {
                  code: '600003',
                  name: '测试三',
                  source: 'fundamental:quality_snapshot',
                  candidate_experts: ['fundamental_expert'],
                  candidate_dimensions: ['fundamental'],
                  signal_score: 88,
                },
              ],
              expert_packets: [
                {
                  expert: 'strategy_factor_expert',
                  dimension: 'strategy',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [{ code: '600001', name: '测试一', source: 'alphasift:balanced_alpha', reason: '策略候选' }],
                  themes: [],
                },
                {
                  expert: 'technical_candidate_expert',
                  dimension: 'technical',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [
                    { code: '600001', name: '测试一', source: 'sequoia:turtle_trade', reason: '技术候选' },
                    { code: '600002', name: '测试二', source: 'sequoia:rps_breakout', reason: '技术候选 2' },
                  ],
                  themes: [],
                },
                {
                  expert: 'fundamental_expert',
                  dimension: 'fundamental',
                  status: 'ok',
                  data_quality: { freshness: 'daily', warnings: [] },
                  candidates: [
                    { code: '600001', name: '测试一', source: 'fundamental:quality_snapshot', reason: '基本面候选' },
                    { code: '600003', name: '测试三', source: 'fundamental:quality_snapshot', reason: '基本面候选 2' },
                  ],
                  themes: [],
                },
              ],
            },
          },
        ],
        stock_selection: null,
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '帮我选一下下周可以入手的股票' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('专家维度与原始候选分组')).toBeInTheDocument();
    expect(screen.getByText('候选入池榜')).toBeInTheDocument();
    expect(screen.getByText('策略候选')).toBeInTheDocument();
    expect(screen.getByText('技术面候选')).toBeInTheDocument();
    expect(screen.getByText('基本面候选')).toBeInTheDocument();
    expect(screen.getAllByText('测试一').length).toBeGreaterThan(0);
    expect(screen.getAllByText('测试二').length).toBeGreaterThan(0);
    expect(screen.getAllByText('测试三').length).toBeGreaterThan(0);
  });

  it('renders thesis desk seed pool and per-desk packets from stock selection artifact', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'legacy' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-thesis-desks',
        content: '选股结论',
        error: null,
        total_steps: 1,
        total_tokens: 10,
        provider: 'agent',
        model: 'deepseek/deepseek-chat',
        mode: 'planning_execute',
        tool_calls: [],
        stock_selection: {
          enabled: true,
          success: true,
          final_report_json: {
            orchestration_mode: 'legacy',
            candidate_discovery: {
              status: 'ok',
              full: {
                candidates: [
                  { code: '600001', name: '测试一', candidate_source: 'thesis_desk_committee', reason: '聚合后进入候选' },
                ],
                seed_pool_summary: {
                  seed_count: 20,
                  total_limit: 20,
                  seed_sources: { daily_screener: 12, alphasift: 8 },
                  signal_dimensions: { technical: 11, fundamental: 4 },
                  preview: [
                    { code: '600001', name: '测试一', source: 'daily_screener', priority_score: 98, trigger_signals: [{ summary: '放量突破' }] },
                    { code: '600002', name: '测试二', source: 'alphasift', priority_score: 91, trigger_signals: [{ summary: '质量修复' }] },
                  ],
                },
                thesis_desk_packets: [
                  {
                    expert: 'early_turn_desk',
                    status: 'ok',
                    seed_summary: { seed_count: 16, accepted_count: 1, rejected_count: 0 },
                    elapsed_ms: 120,
                    candidates: [{ code: '600001', name: '测试一', stance: 'support', setup_type: 'early_turn', reason: '低位启动确认' }],
                    rejected: [],
                    tool_calls: [{ tool: 'analyze_trend' }],
                    per_seed_packets: [
                      {
                        status: 'ok',
                        elapsed_ms: 80,
                        candidates: [{ code: '600001', name: '测试一' }],
                        rejected: [],
                        tool_calls: [{ tool: 'analyze_trend' }],
                        diagnostics: [{ source: 'desk_single_seed_checkpoint', status: 'saved', code: '600001' }],
                        errors: [],
                      },
                    ],
                    diagnostics: [],
                    errors: [],
                  },
                  {
                    expert: 'momentum_desk',
                    status: 'ok',
                    seed_summary: { seed_count: 16, accepted_count: 1, rejected_count: 0 },
                    candidates: [{ code: '600002', name: '测试二', stance: 'watch', setup_type: 'trend_continuation', reason: '动量仍在' }],
                    rejected: [],
                    tool_calls: [],
                    per_seed_packets: [
                      {
                        status: 'timeout',
                        elapsed_ms: 1000,
                        candidates: [],
                        rejected: [],
                        tool_calls: [{ tool: 'analyze_trend', status: 'requested_before_timeout', stock_code: '600003' }],
                        diagnostics: [
                          {
                            source: 'desk_single_seed_timeout',
                            status: 'timeout',
                            code: '600003',
                            reason: 'LLM 已返回工具调用，但工具执行未在行级超时前完成；pending_tools=["analyze_trend"]',
                          },
                        ],
                        errors: [
                          'momentum_desk seed 600003 timeout after 1.0s: LLM 已返回工具调用，但工具执行未在行级超时前完成；pending_tools=["analyze_trend"]',
                        ],
                      },
                    ],
                    diagnostics: [],
                    errors: [],
                  },
                  {
                    expert: 'quality_repair_desk',
                    status: 'empty',
                    seed_summary: { seed_count: 10, accepted_count: 0, rejected_count: 0 },
                    candidates: [],
                    rejected: [],
                    tool_calls: [],
                    diagnostics: [{ source: 'desk_filter', status: 'no_eligible_rows' }],
                    errors: [],
                  },
                ],
                thesis_desk_committee: { status: 'ok', degraded: false, diagnostics: [] },
              },
            },
          },
          selection_context: {},
        },
        runtime_config: { agent_orchestration_mode: 'legacy' },
      },
    ]));

    renderPage();

    const promptInput = await screen.findByDisplayValue(/帮我选一下下周可以入手的股票/);
    fireEvent.change(promptInput, { target: { value: '帮我选一下下周可以入手的股票' } });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('P4 四席位可观察性')).toBeInTheDocument();
    expect(screen.getByText('Seed 20 / 20')).toBeInTheDocument();
    expect(screen.getByText('Seed Preview (2)')).toBeInTheDocument();
    expect(screen.getByText('低位启动席')).toBeInTheDocument();
    expect(screen.getByText('动量席')).toBeInTheDocument();
    expect(screen.getByText('质量修复席')).toBeInTheDocument();
    expect(screen.getByText('低位启动确认')).toBeInTheDocument();
    expect(screen.getByText('动量仍在')).toBeInTheDocument();
    expect(screen.getByText('600003')).toBeInTheDocument();
    expect(document.body.textContent).toContain('seed 600003 timeout after 1.0s');
    expect(document.body.textContent).toContain('LLM 已返回工具调用');
    expect(screen.getByText('本席位未输出候选。')).toBeInTheDocument();
  });

  it('sends thesis desk candidate discovery mode in payload', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.getRuntimeConfig.mockResolvedValue({ runtime_config: { agent_orchestration_mode: 'expert_graph' } });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-candidate-mode',
        content: '选股结论',
        error: null,
        total_steps: 0,
        total_tokens: 0,
        provider: 'agent',
        model: 'mimo-v2.5',
        mode: 'planning_execute',
        tool_calls: [],
        agent_user_context: { report: { intent: 'watchlist_scan', analysis_mode: 'planning_execute' } },
        context_summary: { account_count: 0, position_count: 0, accounts: [], investor: null },
        stock_selection: null,
        runtime_config: { agent_orchestration_mode: 'expert_graph' },
      },
    ]));

    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: /展开配置/ }));
    const select = await screen.findByLabelText('候选发现模式');
    expect((select as HTMLSelectElement).value).toBe('thesis_desk_committee');

    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

    await waitFor(() => {
      expect(mocks.traceStream).toHaveBeenCalledWith(expect.objectContaining({
        candidate_discovery_mode: 'thesis_desk_committee',
      }));
    });
  });

  it('renders LLM telemetry and judge sanity observability from trace result', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'done',
        success: true,
        session_id: 'trace-observability',
        content: '选股结论',
        error: null,
        total_steps: 0,
        total_tokens: 0,
        provider: 'agent',
        model: 'mimo-v2.5',
        mode: 'planning_execute',
        tool_calls: [],
        agent_user_context: { report: { intent: 'watchlist_scan', analysis_mode: 'planning_execute' } },
        context_summary: { account_count: 0, position_count: 0, accounts: [], investor: null },
        llm_telemetry: {
          total_calls: 7,
          total_tokens: 12345,
          failed_calls: 0,
          total_latency_ms: 2450,
          estimated_cost: 0.012345,
          by_stage: [
            { stage: 'candidate_screening', calls: 1, total_tokens: 1200, failed_calls: 0 },
            { stage: 'judge_decision', calls: 1, total_tokens: 1800, failed_calls: 0 },
          ],
        },
        judge_sanity: {
          final_action: 'watch',
          primary_plan_verdict: 'downgraded',
          decision_summary: '等待回踩确认。',
          check_count: 1,
          required_change_count: 1,
          sanity_checks: [
            {
              rule_id: 'open_without_position_plan',
              action: 'downgrade',
              from_action: 'open',
              to_action: 'watch',
              reason: '缺少明确入场条件。',
            },
          ],
        },
        artifact_dir: '/tmp/trace-observability',
      },
    ]));

    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: /^运行$/ }));

    expect(await screen.findByText('可观测性')).toBeInTheDocument();
    expect(screen.getByText('LLM 调用')).toBeInTheDocument();
    expect(screen.getByText('12,345')).toBeInTheDocument();
    expect(screen.getByText('candidate_screening')).toBeInTheDocument();
    expect(screen.getByText('judge_decision')).toBeInTheDocument();
    expect(screen.getByText('Judge Sanity')).toBeInTheDocument();
    expect(screen.getByText('open_without_position_plan')).toBeInTheDocument();
    expect(screen.getByText('等待回踩确认。')).toBeInTheDocument();
  });
});

function makeStreamResponse(events: Array<Record<string, unknown>>): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}

function makeDelayedStreamResponse(
  immediateEvents: Array<Record<string, unknown>>,
  delayedEvents: Array<Record<string, unknown>>,
  delayMs: number,
): Response {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      immediateEvents.forEach((event) => {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      });
      window.setTimeout(() => {
        delayedEvents.forEach((event) => {
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        });
        controller.close();
      }, delayMs);
    },
  }), {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}
