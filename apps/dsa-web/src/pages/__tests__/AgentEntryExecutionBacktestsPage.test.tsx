import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import AgentEntryExecutionBacktestsPage from '../AgentEntryExecutionBacktestsPage';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  rebuild: vi.fn(),
  syncMinuteBars: vi.fn(),
}));

vi.mock('../../api/agentEntryExecutionBacktests', () => ({
  agentEntryExecutionBacktestsApi: {
    list: mocks.list,
    rebuild: mocks.rebuild,
    syncMinuteBars: mocks.syncMinuteBars,
  },
}));

vi.mock('echarts', () => ({
  init: () => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  }),
}));

describe('AgentEntryExecutionBacktestsPage', () => {
  beforeEach(() => {
    mocks.list.mockReset();
    mocks.rebuild.mockReset();
    mocks.syncMinuteBars.mockReset();
    mocks.list.mockResolvedValue(makeResponse());
    mocks.rebuild.mockResolvedValue({
      traceCount: 2,
      reviewCount: 2,
      skipped: 0,
      outputPath: 'data/agent_reviews/entry_execution_backtest.jsonl',
    });
    mocks.syncMinuteBars.mockResolvedValue({
      sync: {
        symbolCount: 1,
        fetchedSymbols: 1,
        failedSymbols: 0,
        fetchedRows: 2,
        writtenRows: 2,
      },
      rebuild: {
        traceCount: 2,
        reviewCount: 2,
        skipped: 0,
        outputPath: 'data/agent_reviews/entry_execution_backtest.jsonl',
      },
    });
  });

  it('renders entry execution rows and strategy metrics', async () => {
    render(<AgentEntryExecutionBacktestsPage />);

    expect(await screen.findByText('入场执行回测')).toBeInTheDocument();
    expect(screen.getByText('严格入场成交率')).toBeInTheDocument();
    expect(await screen.findByDisplayValue('2024-01-01')).toBeInTheDocument();
    expect(await screen.findByText('当日总览指标')).toBeInTheDocument();
    expect(screen.getByText('历史总览指标')).toBeInTheDocument();
    expect(screen.getAllByText('最佳策略累计 PnL').length).toBeGreaterThan(0);
    expect(screen.getByText('四套策略平均收益')).toBeInTheDocument();
    expect(screen.getAllByText(/当前日期/).length).toBeGreaterThan(0);
    expect(screen.getByText('600001')).toBeInTheDocument();
    expect(screen.getByText('测试一')).toBeInTheDocument();
    expect(screen.getByText('10.00 - 10.20')).toBeInTheDocument();
    expect(screen.getAllByText('+9.80%').some((item) => item.classList.contains('text-danger'))).toBe(true);
    expect(screen.getAllByText('已成交').length).toBeGreaterThan(0);
    expect(screen.getByTestId('entry-execution-kline')).toBeInTheDocument();
    expect(screen.getByText('当前策略入场')).toBeInTheDocument();
    expect(screen.getByText('当前策略出场')).toBeInTheDocument();
    expect(screen.getByText('AI 入场区')).toBeInTheDocument();
  });

  it('submits filters to the API', async () => {
    render(<AgentEntryExecutionBacktestsPage />);

    await screen.findByText('入场执行回测');
    fireEvent.change(screen.getByLabelText('策略视角'), { target: { value: 'atr_elastic_entry' } });
    fireEvent.change(screen.getByPlaceholderText('600519'), { target: { value: '600001' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));

    await waitFor(() => {
      expect(mocks.list).toHaveBeenLastCalledWith({
        strategy: 'atr_elastic_entry',
        symbol: '600001',
        decisionDate: '2024-01-01',
        page: 1,
        pageSize: 20,
        limit: 300,
      });
    });
  });

  it('rebuilds samples and reloads the list', async () => {
    render(<AgentEntryExecutionBacktestsPage />);

    await screen.findByText('入场执行回测');
    fireEvent.click(screen.getByRole('button', { name: '重建样本' }));

    expect(await screen.findByText(/已重建 2 条入场执行样本/)).toBeInTheDocument();
    expect(mocks.rebuild).toHaveBeenCalledWith({ limit: 300 });
    expect(mocks.list).toHaveBeenCalledWith({
      strategy: undefined,
      symbol: undefined,
      decisionDate: '2024-01-01',
      page: 1,
      pageSize: 20,
      limit: 300,
    });
  });

  it('syncs baostock minute bars and reloads rebuilt results', async () => {
    render(<AgentEntryExecutionBacktestsPage />);

    await screen.findByDisplayValue('2024-01-01');
    fireEvent.click(screen.getByRole('button', { name: '同步当前日期分钟线' }));

    expect(await screen.findByText(/已同步 1\/1 只最终报告标的分钟线/)).toBeInTheDocument();
    expect(mocks.syncMinuteBars).toHaveBeenCalledWith({
      limit: 300,
      decisionDate: '2024-01-01',
      symbol: undefined,
      frequency: '5',
      adjustflag: '3',
      rebuild: true,
    });
    expect(mocks.list).toHaveBeenCalledWith({
      strategy: undefined,
      symbol: undefined,
      decisionDate: '2024-01-01',
      page: 1,
      pageSize: 20,
      limit: 300,
    });
  });

  it('shows empty state when no samples exist', async () => {
    mocks.list.mockResolvedValue({ exists: false, total: 0, summary: {}, items: [] });

    render(<AgentEntryExecutionBacktestsPage />);

    expect(await screen.findByText('还没有可展示的入场执行样本')).toBeInTheDocument();
    expect(screen.getByText('文件未生成')).toBeInTheDocument();
  });
});

function makeResponse() {
  return {
    exists: true,
    total: 1,
    page: 1,
    pageSize: 20,
    totalPages: 1,
    availableDates: ['2024-01-01'],
    summary: {
      total: 1,
      fillRatePct: 100,
      avgPnlPct: {
        strict_ai_entry: 9.8,
        next_open_baseline: 6,
        atr_elastic_entry: 8,
        breakout_fallback_entry: null,
      },
      medianPnlPct: {
        strict_ai_entry: 9.8,
      },
      statusCounts: {
        filled: 3,
      },
      strategyMetrics: {
        strict_ai_entry: {
          total: 1,
          filled: 1,
          fillRatePct: 100,
          winCount: 1,
          winRatePct: 100,
          compoundedPnlPct: 9.8,
          avgPnlPct: 9.8,
          medianPnlPct: 9.8,
          bestPnlPct: 9.8,
          worstPnlPct: 9.8,
          payoffRatio: null,
        },
        next_open_baseline: {
          total: 1,
          filled: 1,
          fillRatePct: 100,
          winCount: 1,
          winRatePct: 100,
          compoundedPnlPct: 6,
          avgPnlPct: 6,
          medianPnlPct: 6,
          bestPnlPct: 6,
          worstPnlPct: 6,
          payoffRatio: null,
        },
        atr_elastic_entry: {
          total: 1,
          filled: 1,
          fillRatePct: 100,
          winCount: 1,
          winRatePct: 100,
          compoundedPnlPct: 8,
          avgPnlPct: 8,
          medianPnlPct: 8,
          bestPnlPct: 8,
          worstPnlPct: 8,
          payoffRatio: null,
        },
        breakout_fallback_entry: {
          total: 1,
          filled: 0,
          fillRatePct: 0,
          winCount: 0,
          winRatePct: 0,
          compoundedPnlPct: null,
          avgPnlPct: null,
          medianPnlPct: null,
          bestPnlPct: null,
          worstPnlPct: null,
          payoffRatio: null,
        },
      },
      headlineMetrics: {
        bestStrategy: 'strict_ai_entry',
        bestCompoundedPnlPct: 9.8,
        bestWinRatePct: 100,
        bestFillRatePct: 100,
        bestFilled: 1,
      },
    },
    historySummary: {
      total: 3,
      fillRatePct: 66.67,
      avgPnlPct: {
        strict_ai_entry: 4.2,
        next_open_baseline: 2,
        atr_elastic_entry: 3,
        breakout_fallback_entry: null,
      },
      medianPnlPct: {
        strict_ai_entry: 4.2,
      },
      strategyMetrics: {
        strict_ai_entry: {
          total: 3,
          filled: 2,
          fillRatePct: 66.67,
          winCount: 1,
          winRatePct: 50,
          compoundedPnlPct: 8.2,
          avgPnlPct: 4.2,
          medianPnlPct: 4.2,
          bestPnlPct: 9.8,
          worstPnlPct: -1.2,
          payoffRatio: 8.17,
        },
      },
      headlineMetrics: {
        bestStrategy: 'strict_ai_entry',
        bestCompoundedPnlPct: 8.2,
        bestWinRatePct: 50,
        bestFillRatePct: 66.67,
        bestFilled: 2,
      },
    },
    items: [
      {
        traceId: 'trace-entry',
        decisionDate: '2024-01-01',
        tsCode: '600001',
        name: '测试一',
        rank: 1,
        parseStatus: 'ok',
        priceData: {
          granularity: 'minute',
          barCount: 2,
          frequency: '5',
          dailyBars: [
            { date: '2024-01-02', open: 10.4, high: 10.5, low: 10.1, close: 10.2 },
            { date: '2024-01-03', open: 10.6, high: 11.3, low: 10.5, close: 11.2 },
          ],
        },
        tradePlan: {
          entryZoneLow: 10,
          entryZoneHigh: 10.2,
          stopLossPrice: 9.7,
          takeProfitPrice: 11.2,
        },
        strategies: {
          strict_ai_entry: {
            status: 'filled',
            entryPrice: 10.2,
            exitDate: '2024-01-03',
            exitPrice: 11.2,
            exitReason: 'take_profit',
            holdingDays: 2,
            pnlPct: 9.8,
          },
          atr_elastic_entry: {
            status: 'filled',
            pnlPct: 8,
          },
        },
      },
    ],
  };
}
