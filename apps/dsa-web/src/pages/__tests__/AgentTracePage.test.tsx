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
    expect(screen.getByText('主观点支持继续持有')).toBeInTheDocument();
    expect(screen.getByText('反方提醒仓位风险')).toBeInTheDocument();
    expect(screen.getByText('维持持有，但资金面和消息面证据不足，需要继续观察。')).toBeInTheDocument();
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
        tool_calls: [],
        planner: { intent: 'watchlist_scan' },
        agent_user_context: { report: { intent: 'watchlist_scan' } },
        context_summary: { account_count: 0, position_count: 0, accounts: [], investor: null },
        debate: null,
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
  });
});

function makeStreamResponse(events: Array<Record<string, unknown>>): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}
