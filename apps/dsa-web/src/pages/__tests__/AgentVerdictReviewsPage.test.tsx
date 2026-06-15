import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';
import AgentVerdictReviewsPage from '../AgentVerdictReviewsPage';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  rebuild: vi.fn(),
}));

vi.mock('../../api/agentVerdictReviews', () => ({
  agentVerdictReviewsApi: {
    list: mocks.list,
    rebuild: mocks.rebuild,
  },
}));

describe('AgentVerdictReviewsPage', () => {
  beforeEach(() => {
    mocks.list.mockReset();
    mocks.rebuild.mockReset();
    mocks.list.mockResolvedValue(makeResponse());
    mocks.rebuild.mockResolvedValue({
      traceCount: 2,
      reviewCount: 2,
      skipped: 0,
      outputPath: 'data/agent_reviews/verdict_review.jsonl',
      evalWindows: [7, 30],
    });
  });

  it('renders stock-selection and single-stock verdict review rows', async () => {
    render(<AgentVerdictReviewsPage />);

    expect(await screen.findByText('Agent 后验复盘')).toBeInTheDocument();
    expect(screen.getAllByText('选股链路').length).toBeGreaterThan(0);
    expect(screen.getAllByText('单股链路').length).toBeGreaterThan(0);
    expect(screen.getAllByText('错过上涨').length).toBeGreaterThan(0);
    expect(screen.getAllByText('命中').length).toBeGreaterThan(0);
    expect(screen.getByText('600001')).toBeInTheDocument();
    expect(screen.getByText('600519')).toBeInTheDocument();
    expect(screen.getByText('+6.00%')).toHaveClass('text-danger');
    expect(screen.getByText('+3.00%')).toHaveClass('text-danger');
  });

  it('submits filters to the API', async () => {
    render(<AgentVerdictReviewsPage />);

    await screen.findByText('Agent 后验复盘');
    fireEvent.change(screen.getByLabelText('链路'), { target: { value: 'single_stock_analysis' } });
    fireEvent.change(screen.getByLabelText('标签'), { target: { value: 'hit' } });
    fireEvent.change(screen.getByPlaceholderText('600519'), { target: { value: '600519' } });
    fireEvent.click(screen.getByRole('button', { name: '筛选' }));

    await waitFor(() => {
      expect(mocks.list).toHaveBeenLastCalledWith({
        chainType: 'single_stock_analysis',
        reviewLabel: 'hit',
        symbol: '600519',
        limit: 300,
      });
    });
  });

  it('shows empty state when no review rows exist', async () => {
    mocks.list.mockResolvedValue({ exists: false, total: 0, summary: {}, items: [] });

    render(<AgentVerdictReviewsPage />);

    expect(await screen.findByText('还没有可展示的复盘样本')).toBeInTheDocument();
    expect(screen.getByText('文件未生成')).toBeInTheDocument();
  });

  it('rebuilds review samples and reloads the list', async () => {
    render(<AgentVerdictReviewsPage />);

    await screen.findByText('Agent 后验复盘');
    fireEvent.click(screen.getByRole('button', { name: '重建样本' }));

    expect(await screen.findByText(/已重建 2 条复盘样本/)).toBeInTheDocument();
    expect(mocks.rebuild).toHaveBeenCalledWith({ windows: '7,30', limit: 300 });
    expect(mocks.list).toHaveBeenCalledTimes(2);
  });
});

function makeResponse() {
  return {
    exists: true,
    total: 2,
    summary: {
      total: 2,
      completedCount: 2,
      completionRatePct: 100,
      avgFutureReturnPct: 4.5,
      chainCounts: {
        stock_selection: 1,
        single_stock_analysis: 1,
      },
      labelCounts: {
        missed_up: 1,
        hit: 1,
      },
    },
    items: [
      {
        chainType: 'stock_selection',
        traceId: 'trace-selection',
        decisionDate: '2024-01-01',
        symbol: '600001',
        name: '测试一',
        symbolAction: 'wait',
        finalAction: 'wait',
        primaryPlanVerdict: 'accept_with_changes',
        regime: 'range_bound',
        reviewLabel: 'missed_up',
        windows: {
          7: { evalStatus: 'completed', futureReturnPct: 6 },
        },
      },
      {
        chainType: 'single_stock_analysis',
        traceId: 'trace-single',
        decisionDate: '2024-01-01',
        symbol: '600519',
        name: '贵州茅台',
        symbolAction: 'hold',
        finalAction: 'hold',
        operationAdvice: '持有',
        decisionType: 'hold',
        reviewLabel: 'hit',
        windows: {
          7: { evalStatus: 'completed', futureReturnPct: 3 },
        },
      },
    ],
  };
}
