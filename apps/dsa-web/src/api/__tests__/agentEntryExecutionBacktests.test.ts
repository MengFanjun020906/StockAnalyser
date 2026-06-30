import { describe, expect, it, vi } from 'vitest';
import { agentEntryExecutionBacktestsApi } from '../agentEntryExecutionBacktests';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../index', () => ({
  default: {
    get: mocks.get,
    post: mocks.post,
  },
}));

describe('agentEntryExecutionBacktestsApi', () => {
  it('preserves strategy and status metric keys after camelCase conversion', async () => {
    mocks.get.mockResolvedValueOnce({
      data: {
        source_path: 'data/agent_reviews/entry_execution_backtest.jsonl',
        exists: true,
        total: 1,
        page: 1,
        page_size: 20,
        total_pages: 1,
        available_dates: ['2026-06-13'],
        summary: {
          total: 1,
          fill_rate_pct: 100,
          strategy_counts: {
            strict_ai_entry: 1,
            next_open_baseline: 1,
          },
          status_counts: {
            not_filled: 1,
            strategy_skipped: 1,
          },
          avg_pnl_pct: {
            strict_ai_entry: 3.2,
            next_open_baseline: -1.1,
          },
          median_pnl_pct: {
            strict_ai_entry: 3.2,
            next_open_baseline: -1.1,
          },
          strategy_metrics: {
            strict_ai_entry: {
              total: 1,
              filled: 1,
              win_rate_pct: 100,
              compounded_pnl_pct: 3.2,
            },
            next_open_baseline: {
              total: 1,
              filled: 0,
              win_rate_pct: 0,
              compounded_pnl_pct: null,
            },
          },
          headline_metrics: {
            best_strategy: 'strict_ai_entry',
            best_compounded_pnl_pct: 3.2,
            best_win_rate_pct: 100,
          },
        },
        history_summary: {
          total: 2,
          strategy_counts: {
            strict_ai_entry: 2,
          },
          status_counts: {
            filled: 2,
          },
          avg_pnl_pct: {
            strict_ai_entry: 2.1,
          },
          median_pnl_pct: {
            strict_ai_entry: 2.1,
          },
          strategy_metrics: {
            strict_ai_entry: {
              total: 2,
              filled: 2,
              win_rate_pct: 50,
              compounded_pnl_pct: 4.2,
            },
          },
          headline_metrics: {
            best_strategy: 'strict_ai_entry',
            best_compounded_pnl_pct: 4.2,
          },
        },
        items: [
          {
            trace_id: 'trace-a',
            decision_date: '2026-06-13',
            ts_code: '600001',
            strategies: {
              strict_ai_entry: {
                status: 'filled',
                entry_price: 10,
                pnl_pct: 3.2,
              },
              next_open_baseline: {
                status: 'not_filled',
              },
            },
          },
        ],
      },
    });

    const result = await agentEntryExecutionBacktestsApi.list();

    expect(result.summary.avgPnlPct?.strict_ai_entry).toBe(3.2);
    expect(result.summary.avgPnlPct?.next_open_baseline).toBe(-1.1);
    expect(result.summary.strategyCounts?.strict_ai_entry).toBe(1);
    expect(result.summary.statusCounts?.not_filled).toBe(1);
    expect(result.summary.statusCounts?.strategy_skipped).toBe(1);
    expect(result.summary.strategyMetrics?.strict_ai_entry?.compoundedPnlPct).toBe(3.2);
    expect(result.summary.strategyMetrics?.next_open_baseline?.winRatePct).toBe(0);
    expect(result.summary.headlineMetrics?.bestStrategy).toBe('strict_ai_entry');
    expect(result.summary.headlineMetrics?.bestCompoundedPnlPct).toBe(3.2);
    expect(result.historySummary?.strategyMetrics?.strict_ai_entry?.compoundedPnlPct).toBe(4.2);
    expect(result.historySummary?.headlineMetrics?.bestStrategy).toBe('strict_ai_entry');
    expect(result.items[0].strategies?.strict_ai_entry?.entryPrice).toBe(10);
    expect(result.items[0].strategies?.next_open_baseline?.status).toBe('not_filled');
  });
});
