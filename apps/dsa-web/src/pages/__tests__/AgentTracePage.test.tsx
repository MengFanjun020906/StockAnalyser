import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentTracePage from '../AgentTracePage';

const mocks = vi.hoisted(() => ({
  getAccounts: vi.fn(),
  runTrace: vi.fn(),
  traceStream: vi.fn(),
}));

vi.mock('../../api/agent', () => ({
  agentApi: {
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
  beforeEach(() => {
    window.localStorage.clear();
    mocks.getAccounts.mockReset();
    mocks.runTrace.mockReset();
    mocks.traceStream.mockReset();
  });

  it('runs a trace and renders planner, events, tools, and final output', async () => {
    mocks.getAccounts.mockResolvedValue({
      accounts: [{ id: 7, name: 'A股主账户', market: 'cn', baseCurrency: 'CNY', isActive: true }],
    });
    mocks.traceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'context_ready',
        session_id: 'trace-1',
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
      { type: 'planner_ready', session_id: 'trace-1', planner: { intent: 'position_review', required_tools: ['get_realtime_quote'] } },
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
        session_id: 'trace-1',
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

    render(<AgentTracePage />);

    expect(await screen.findByDisplayValue('A股主账户 · CN · CNY')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /运行 Trace/ }));

    await waitFor(() => {
      expect(mocks.traceStream).toHaveBeenCalledWith(expect.objectContaining({
        account_id: 7,
        analysis_mode: 'planning_execute',
        stock_code: '600519',
        report_intent: undefined,
        risk_preference: 'balanced',
        trading_horizon: 'long_term',
        max_single_position_pct: 20,
        max_total_equity_exposure_pct: 80,
        max_acceptable_drawdown_pct: 15,
        default_stop_loss_pct: 8,
      }));
    });

    expect(screen.getByText('A股主账户')).toBeInTheDocument();
    expect(screen.getByText('150,000')).toBeInTheDocument();
    expect(screen.getAllByText('20%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('80%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('15%').length).toBeGreaterThan(0);
    expect(screen.getAllByText('8%').length).toBeGreaterThan(0);
    await waitFor(() => {
      expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    });
    expect(screen.getByRole('heading', { name: '最终结论' })).toBeInTheDocument();
    expect(screen.getByText('持有，但不急着加仓')).toBeInTheDocument();
    expect(screen.getByText('Trace 已完成')).toBeInTheDocument();
    expect(screen.getByText('Debate Judge')).toBeInTheDocument();
    expect(screen.getAllByText('Risk Gate').length).toBeGreaterThan(0);
    expect(screen.getAllByText('风控通过，允许动作保持为 hold。').length).toBeGreaterThan(0);
    expect(screen.getByText('critical_data_quality')).toBeInTheDocument();
    expect(screen.getByText('关键数据质量未标记为失败或不足。')).toBeInTheDocument();
    expect(screen.getByText('主观点支持继续持有')).toBeInTheDocument();
    expect(screen.getByText('反方提醒仓位风险')).toBeInTheDocument();
    expect(screen.getAllByText('维持持有，但资金面和消息面证据不足，需要继续观察。').length).toBeGreaterThan(0);
    expect(screen.getAllByText('资金面').length).toBeGreaterThan(0);
    expect(screen.getAllByText('消息面').length).toBeGreaterThan(0);
    expect(screen.getByText('资金面证据不足')).toBeInTheDocument();
    expect(screen.getByText('新闻搜索结果不足')).toBeInTheDocument();
    expect(screen.getByText('Session Outputs')).toBeInTheDocument();
    expect(screen.getByText('原始主报告输出')).toBeInTheDocument();
    expect(screen.getByText('Primary Thesis 原始输出')).toBeInTheDocument();
    expect(screen.getByText('Opposing Thesis 原始输出')).toBeInTheDocument();
    expect(screen.getByText('Judge 原始输出')).toBeInTheDocument();
    expect(screen.getAllByText(/final_action/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('position_review').length).toBeGreaterThan(0);
    expect(screen.getAllByText('获取实时行情').length).toBeGreaterThan(0);
    expect(screen.getByText('最近 1 次运行')).toBeInTheDocument();
  });

  it('loads a completed trace from local history', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
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

    render(<AgentTracePage />);

    expect(await screen.findByText('最近 1 次运行')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /601399/ }));

    expect(screen.getByText('历史结论')).toBeInTheDocument();
    expect(screen.getByText('已加载历史 Trace')).toBeInTheDocument();
    expect(screen.getByDisplayValue('601399')).toBeInTheDocument();
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

    render(<AgentTracePage />);

    fireEvent.click(await screen.findByRole('button', { name: /运行 Trace/ }));

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

    render(<AgentTracePage />);

    fireEvent.click(await screen.findByRole('button', { name: /运行 Trace/ }));

    expect((await screen.findAllByText('Risk Gate')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('风控阻断：原动作 sell 被改为 manual_review，失败规则 1 条。').length).toBeGreaterThan(0);
    expect(screen.getByText('a_share_t_plus_one')).toBeInTheDocument();
    expect(screen.getAllByText('A 股 T+1 约束：当日买入持仓不能在当日卖出或减仓。').length).toBeGreaterThan(0);
    expect(screen.getByText('建议动作：manual_review')).toBeInTheDocument();
  });

  it('does not send the default stock code for stock-selection prompts', async () => {
    mocks.getAccounts.mockResolvedValue({ accounts: [] });
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
              candidate_source: 'multi_recall',
              candidate_count: 2,
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
                },
                {
                  code: '600002',
                  name: '测试二',
                  source: 'akshare:industry:半导体',
                  recall_sources: ['akshare:industry:半导体'],
                  strategy_tags: ['hot_sector'],
                  reason: '强势板块成分股进入候选池。',
                  signal_score: 71,
                  latest_date: '2026-05-08',
                  metrics: { sector_rank: 3 },
                },
              ],
              discovery_steps: [
                { source: 'sequoia', status: 'ok', count: 1, db_path: 'Sequoia-X/data/sequoia_v2.db', strategy_names: ['turtle_trade', 'rps_breakout'] },
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
            next_step: 'render_final_report',
            stages: {
              candidate_discovery: { status: 'ok', summary: { candidate_codes: ['600001', '600002'] }, full_ref: 'candidate_discovery.json' },
              candidate_screening: { status: 'ok', summary: { deep_dive_targets: ['600001'] }, full_ref: 'candidate_screening.json' },
              single_stock_deep_dive: { status: 'ok', summary: { wait_targets: ['600001'] }, full_ref: 'deep_dive_results.json' },
              portfolio_allocation: { status: 'ok', summary: { portfolio_action: 'wait' }, full_ref: 'portfolio_allocation.json' },
              adversarial_review: { status: 'ok', summary: { opposing_summary: '资金面缺失' }, full_ref: 'adversarial_review.json' },
              judge_decision: { status: 'ok', summary: { final_action: 'wait', primary_plan_verdict: 'accept_with_changes', decision_summary: '等待确认', next_step: 'render_final_report' }, full_ref: 'judge_decision.json' },
            },
          },
          final_report_json: {
            candidate_discovery: {
              summary: {
                candidate_codes: ['600001', '600002'],
                candidate_sources: ['sequoia:multi_strategy', 'akshare:industry:半导体'],
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

    render(<AgentTracePage />);

    const promptInput = await screen.findByDisplayValue(/我持有 600519/);
    fireEvent.change(promptInput, { target: { value: '我现在有5w，我希望你帮我选股，并告诉我怎么分配仓位' } });
    expect(screen.getByText('当前问题像选股/组合配置，将不会发送该股票代码。')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /运行 Trace/ }));

    await waitFor(() => {
      expect(mocks.traceStream).toHaveBeenCalledWith(expect.objectContaining({
        stock_code: undefined,
      }));
    });
    expect(screen.getByText('选股结论')).toBeInTheDocument();
    expect(screen.getByText('Stock Selection Pipeline')).toBeInTheDocument();
    expect(screen.getByText('L1 Data & Candidate Layer / 数据与候选池层')).toBeInTheDocument();
    expect(screen.queryByText('L2 Candidate Layer / 候选池层')).not.toBeInTheDocument();
    expect(screen.getByText('候选来源审计')).toBeInTheDocument();
    expect(screen.getByText('召回路径')).toBeInTheDocument();
    expect(screen.queryByText('为什么后续工具查这些股票')).not.toBeInTheDocument();
    expect(screen.queryByText('入池候选与理由')).not.toBeInTheDocument();
    expect(screen.getByText('Sequoia-X/data/sequoia_v2.db')).toBeInTheDocument();
    expect(screen.getAllByText('多路召回').length).toBeGreaterThan(0);
    expect(screen.getByText('强势板块成分股')).toBeInTheDocument();
    expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    expect(screen.getByText('候选池列表')).toBeInTheDocument();
    expect(screen.getAllByText('强势板块').length).toBeGreaterThan(0);
    expect(screen.getByText('candidate_discovery.json')).toBeInTheDocument();
    expect(screen.getAllByText('600001').length).toBeGreaterThan(0);
    expect(screen.getAllByText('测试一').length).toBeGreaterThan(0);
    expect(screen.getAllByText('92.5').length).toBeGreaterThan(0);
    expect(screen.getAllByText('海龟突破').length).toBeGreaterThan(0);
    expect(screen.getAllByText('RPS 强势突破').length).toBeGreaterThan(0);
    expect(screen.getAllByText('多策略共振：均线放量突破、海龟突破、RPS 强势突破。').length).toBeGreaterThan(0);
    expect(screen.queryByText('ma_volume')).not.toBeInTheDocument();
    expect(screen.queryByText('turtle_trade')).not.toBeInTheDocument();
    expect(screen.queryByText('rps_breakout')).not.toBeInTheDocument();
    expect(screen.getAllByText(/turnover/).length).toBeGreaterThan(0);
    expect(screen.getByText('回踩确认')).toBeInTheDocument();
    expect(screen.getByText('capital_flow')).toBeInTheDocument();
    expect(screen.getByText('不追高')).toBeInTheDocument();
  });
});

function makeStreamResponse(events: Array<Record<string, unknown>>): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}
