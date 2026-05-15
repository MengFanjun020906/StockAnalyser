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

    fireEvent.click(screen.getByRole('button', { name: /展开配置/ }));
    await waitFor(() => {
      expect(screen.getByDisplayValue('A股主账户')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole('button', { name: /^运行$/ }));

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
    await waitFor(() => {
      expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    });
    expect(screen.getByRole('heading', { name: '最终结论' })).toBeInTheDocument();
    expect(screen.getByText('持有，但不急着加仓')).toBeInTheDocument();
    expect(screen.getByText('分析完成')).toBeInTheDocument();
    expect(screen.getAllByText('风控闸门').length).toBeGreaterThan(0);
    expect(screen.getAllByText('风控通过，允许执行「hold」。').length).toBeGreaterThan(0);
    expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    expect(screen.getByText('历史')).toBeInTheDocument();
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

    expect(await screen.findByText('历史')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /601399/ }));

    expect(screen.getByText('历史结论')).toBeInTheDocument();
    expect(screen.getByText('已加载历史')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /展开配置/ }));
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

    render(<AgentTracePage />);

    fireEvent.click(await screen.findByRole('button', { name: /^运行$/ }));

    expect((await screen.findAllByText('风控闸门')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('风控阻断：原动作「sell」被改为「manual_review」。').length).toBeGreaterThan(0);
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
              discovery_steps: [
                { source: 'alphasift', status: 'ok', count: 1, db_path: 'Sequoia-X/data/sequoia_v2.db', strategy_names: ['volume_breakout'] },
                { source: 'sequoia', status: 'ok', count: 1, db_path: 'Sequoia-X/data/sequoia_v2.db', strategy_names: ['turtle_trade', 'rps_breakout'] },
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

    render(<AgentTracePage />);

    const promptInput = await screen.findByDisplayValue(/我持有 600519/);
    fireEvent.change(promptInput, { target: { value: '我现在有5w，我希望你帮我选股，并告诉我怎么分配仓位' } });
    expect(screen.getByText('当前问题像选股/组合配置，将不会发送该股票代码。')).toBeInTheDocument();
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
    expect(screen.getByText('等待验证')).toBeInTheDocument();
    expect(screen.getAllByText('石油石化').length).toBeGreaterThan(0);
    expect(screen.getAllByText('航运港口').length).toBeGreaterThan(0);
    expect(screen.getAllByText('观察中，未形成个股候选').length).toBeGreaterThan(0);
    expect(screen.getByText('多专家选股状态')).toBeInTheDocument();
    expect(screen.getByText('expert_graph')).toBeInTheDocument();
    expect(screen.getByText('市场环境专家')).toBeInTheDocument();
    expect(screen.getByText('候选发现专家')).toBeInTheDocument();
    expect(screen.getByText('技术结构专家')).toBeInTheDocument();
    expect(screen.getByText('资金筹码专家')).toBeInTheDocument();
    expect(screen.getByText('消息情绪专家')).toBeInTheDocument();
    expect(screen.getByText('按专家维度分组的候选')).toBeInTheDocument();
    expect(screen.getByText('策略候选')).toBeInTheDocument();
    expect(screen.getByText('技术面候选')).toBeInTheDocument();
    expect(screen.getByText('资金面候选')).toBeInTheDocument();
    expect(screen.getByText('情绪/热点候选')).toBeInTheDocument();
    expect(screen.queryByText('为什么后续工具查这些股票')).not.toBeInTheDocument();
    expect(screen.queryByText('入池候选与理由')).not.toBeInTheDocument();
    expect(screen.getAllByText('get_realtime_quote').length).toBeGreaterThan(0);
    expect(screen.getByText('候选池列表 (3)')).toBeInTheDocument();
    expect(screen.getAllByText('强势板块').length).toBeGreaterThan(0);
    expect(screen.getAllByText('600001').length).toBeGreaterThan(0);
    expect(screen.getAllByText('测试一').length).toBeGreaterThan(0);
    expect(screen.getAllByText('301183').length).toBeGreaterThan(0);
    expect(screen.getAllByText('东田微').length).toBeGreaterThan(0);
    expect(screen.getAllByText('评分 92.5').length).toBeGreaterThan(0);
    expect(screen.getAllByText('AlphaSift 多因子：volume_breakout').length).toBeGreaterThan(0);
    expect(screen.getAllByText(/AlphaSift YAML 多因子策略入池/).length).toBeGreaterThan(0);
    expect(screen.getAllByText('海龟突破').length).toBeGreaterThan(0);
    expect(screen.getAllByText('RPS 强势突破').length).toBeGreaterThan(0);
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
});

function makeStreamResponse(events: Array<Record<string, unknown>>): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}
