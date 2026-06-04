import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import CandidatePoolPage from '../CandidatePoolPage';

const mocks = vi.hoisted(() => ({
  getLatest: vi.fn(),
  getRuns: vi.fn(),
  getRun: vi.fn(),
}));

vi.mock('../../api/candidatePool', () => ({
  candidatePoolApi: {
    getLatest: mocks.getLatest,
    getRuns: mocks.getRuns,
    getRun: mocks.getRun,
  },
}));

describe('CandidatePoolPage', () => {
  beforeEach(() => {
    mocks.getLatest.mockReset();
    mocks.getRuns.mockReset();
    mocks.getRun.mockReset();
  });

  it('renders latest candidate pool summary and items', async () => {
    mocks.getLatest.mockResolvedValue(makeDetail('run-1'));
    mocks.getRuns.mockResolvedValue({ runs: [makeDetail('run-1').run] });

    render(<CandidatePoolPage />);

    expect(await screen.findByText('候选池')).toBeInTheDocument();
    expect(screen.getByText('贵州茅台')).toBeInTheDocument();
    expect(screen.getByText('600519')).toBeInTheDocument();
    expect(screen.getByText('AlphaSift YAML 多因子策略入池')).toBeInTheDocument();
    expect(screen.getByText('候选入池榜')).toBeInTheDocument();
    expect(screen.getByText('重点观察')).toBeInTheDocument();
    expect(screen.getByText('硬策略主干')).toBeInTheDocument();
    expect(screen.getByText('可用')).toBeInTheDocument();
    expect(screen.getAllByText('新进入').length).toBeGreaterThanOrEqual(1);
  });

  it('can switch to a historical run', async () => {
    mocks.getLatest.mockResolvedValue(makeDetail('run-1'));
    mocks.getRuns.mockResolvedValue({
      runs: [makeDetail('run-1').run, makeDetail('run-2', '300750', '宁德时代').run],
    });
    mocks.getRun.mockResolvedValue(makeDetail('run-2', '300750', '宁德时代'));

    render(<CandidatePoolPage />);

    await screen.findByText('贵州茅台');
    fireEvent.change(screen.getByLabelText('选择候选池运行'), { target: { value: 'run-2' } });

    await waitFor(() => expect(mocks.getRun).toHaveBeenCalledWith('run-2'));
    expect(await screen.findByText('宁德时代')).toBeInTheDocument();
  });
});

function makeDetail(runId: string, code = '600519', name = '贵州茅台') {
  return {
    run: {
      runId,
      createdAt: '2026-05-16T10:00:00+08:00',
      market: 'cn',
      candidateSource: 'expert_graph_discovery',
      candidateCount: 1,
      fallbackUsed: false,
      status: 'ok',
      quality: { hardStrategyTrunkMissing: false },
      hardExclusion: { excludedCount: 1 },
      note: '多专家候选召回结果',
    },
    items: [
      {
        id: 1,
        runId,
        code,
        name,
        source: 'alphasift:quality_value',
        signalScore: 88,
        candidateDimensions: ['strategy'],
        recallSources: ['alphasift:quality_value'],
        reason: 'AlphaSift YAML 多因子策略入池',
        reasonDimensions: [{ dimension: 'strategy', label: '策略', detail: 'AlphaSift YAML 多因子策略入池' }],
        lifecycleStatus: 'new',
        recurrenceCount: 1,
      },
    ],
    quality: { hardStrategyTrunkMissing: false },
    hardExclusion: { excludedCount: 1 },
    summary: {
      candidateCount: 1,
      dimensionCounts: { strategy: 1 },
      sourceCounts: { alphasift: 1 },
      lifecycleCounts: { new: 1 },
      recurringCount: 0,
      multiSourceCount: 0,
      fallbackCount: 0,
      hardExclusionCount: 1,
      hardStrategyTrunkMissing: false,
    },
  };
}
