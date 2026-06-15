import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import SeedPoolQualityPage from '../SeedPoolQualityPage';

const mocks = vi.hoisted(() => ({
  getDates: vi.fn(),
  getByDate: vi.fn(),
  getChartData: vi.fn(),
  evaluate: vi.fn(),
}));

vi.mock('echarts', () => ({
  init: () => ({
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn(),
  }),
}));

vi.mock('../../api/seedPoolQuality', () => ({
  seedPoolQualityApi: {
    getDates: mocks.getDates,
    getByDate: mocks.getByDate,
    getChartData: mocks.getChartData,
    evaluate: mocks.evaluate,
  },
}));

describe('SeedPoolQualityPage', () => {
  beforeEach(() => {
    mocks.getDates.mockReset();
    mocks.getByDate.mockReset();
    mocks.getChartData.mockReset();
    mocks.evaluate.mockReset();
  });

  it('uses A-share red-up green-down colors and latest-pool date label', async () => {
    mocks.getDates.mockResolvedValue([{ seedDate: '2026-06-05', snapshotCount: 1 }]);
    mocks.getByDate.mockResolvedValue(makeQualityResponse());
    mocks.getChartData.mockResolvedValue(null);

    render(
      <MemoryRouter>
        <SeedPoolQualityPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText('种子池质量监控')).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '2026-06-05 · 最新池' })).toBeInTheDocument();
    expect(await screen.findByText('上涨票')).toBeInTheDocument();

    const upRow = screen.getByText('上涨票').closest('tr');
    const downRow = screen.getByText('下跌票').closest('tr');
    expect(upRow).not.toBeNull();
    expect(downRow).not.toBeNull();

    expect(within(upRow as HTMLElement).getByText('+3.50%')).toHaveClass('text-danger');
    expect(within(upRow as HTMLElement).getByText('+4.00%')).toHaveClass('text-danger');
    expect(within(downRow as HTMLElement).getByText('-2.50%')).toHaveClass('text-success');
    expect(within(downRow as HTMLElement).getByText('-1.00%')).toHaveClass('text-success');
    expect(await screen.findAllByText('未入席')).toHaveLength(4);
    expect(screen.getAllByText('未进入该席位评估范围。')).toHaveLength(4);
    expect(screen.queryByText('未落盘该席位理由')).not.toBeInTheDocument();
  });
});

function makeQualityResponse() {
  return {
    snapshot: {
      id: 1,
      runId: 'run-new',
      traceId: 'trace-new',
      seedDate: '2026-06-05',
      generatedAt: '2026-06-05T18:00:00+08:00',
      market: 'cn',
      seedCount: 2,
      status: 'ok',
    },
    summary: {
      seedCount: 2,
      evaluatedCount: 2,
      tradableCount: 2,
      missingPriceCount: 0,
      upCount: 1,
      downCount: 1,
      winRatePct: 50,
      avgReturnPct: 1.5,
      avgAlphaReturnPct: 0.5,
    },
    sourceStats: [],
    deskStats: [],
    catalystTierStats: [],
    items: [
      {
        id: 1,
        snapshotId: 1,
        code: '600001',
        name: '上涨票',
        source: 'daily_screener',
        catalystTier: 1,
        catalystTags: [],
        evaluation: {
          nextCloseReturnPct: 4,
          alphaReturnPct: 3.5,
          liquidityStatus: 'NORMAL',
          dataStatus: 'ok',
        },
        deskOutcomes: [],
      },
      {
        id: 2,
        snapshotId: 1,
        code: '600002',
        name: '下跌票',
        source: 'daily_screener',
        catalystTier: 3,
        catalystTags: [],
        evaluation: {
          nextCloseReturnPct: -1,
          alphaReturnPct: -2.5,
          liquidityStatus: 'NORMAL',
          dataStatus: 'ok',
        },
        deskOutcomes: [],
      },
    ],
  };
}
