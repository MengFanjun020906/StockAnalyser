import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentTracePage from '../AgentTracePage';

const mockRunTrace = vi.fn();
const mockTraceStream = vi.fn();
const mockGetAccounts = vi.fn();

vi.mock('../../api/agent', () => ({
  agentApi: {
    runTrace: mockRunTrace,
    traceStream: mockTraceStream,
  },
}));

vi.mock('../../api/portfolio', () => ({
  portfolioApi: {
    getAccounts: mockGetAccounts,
  },
}));

describe('AgentTracePage', () => {
  beforeEach(() => {
    window.localStorage.clear();
    mockGetAccounts.mockReset();
    mockRunTrace.mockReset();
    mockTraceStream.mockReset();
  });

  it('runs a trace and renders planner, events, tools, and final output', async () => {
    mockGetAccounts.mockResolvedValue({
      accounts: [{ id: 7, name: 'A股主账户', market: 'cn', baseCurrency: 'CNY', isActive: true }],
    });
    mockTraceStream.mockResolvedValue(makeStreamResponse([
      {
        type: 'context_ready',
        session_id: 'trace-1',
        agent_user_context: { report: { analysis_mode: 'planning_execute' } },
        context_summary: {
          account_count: 1,
          position_count: 2,
          investor: { risk_preference: 'balanced', trading_horizon: 'long_term', notes: '偏长期持有' },
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
        context_summary: {
          account_count: 1,
          position_count: 2,
          investor: { risk_preference: 'balanced', trading_horizon: 'long_term', notes: '偏长期持有' },
          accounts: [{ account_id: 7, account_name: 'A股主账户', market: 'cn', available_cash: 16982.65, total_equity: 736532.65 }],
          target_position: { symbol: '600519', quantity: 150000, avg_cost: 4.797, last_price: 5, unrealized_pnl: 30450, position_pct: 50.1 },
        },
      },
    ]));

    render(<AgentTracePage />);

    expect(await screen.findByDisplayValue('7')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /运行 Trace/ }));

    await waitFor(() => {
      expect(mockTraceStream).toHaveBeenCalledWith(expect.objectContaining({
        account_id: 7,
        analysis_mode: 'planning_execute',
        stock_code: '600519',
        risk_preference: 'balanced',
        trading_horizon: 'long_term',
      }));
    });

    expect(screen.getByText('A股主账户')).toBeInTheDocument();
    expect(screen.getByText('150,000')).toBeInTheDocument();
    expect(await screen.findByText('get_realtime_quote')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '最终结论' })).toBeInTheDocument();
    expect(screen.getByText('持有，但不急着加仓')).toBeInTheDocument();
    expect(screen.getByText('Trace 已完成')).toBeInTheDocument();
    expect(screen.getAllByText('position_review').length).toBeGreaterThan(0);
    expect(screen.getAllByText('获取实时行情').length).toBeGreaterThan(0);
    expect(screen.getByText('最近 1 次运行')).toBeInTheDocument();
  });

  it('loads a completed trace from local history', async () => {
    mockGetAccounts.mockResolvedValue({ accounts: [] });
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
});

function makeStreamResponse(events: Array<Record<string, unknown>>): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('');
  return new Response(body, {
    status: 200,
    headers: { 'Content-Type': 'text/event-stream' },
  });
}
