import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import BacktestRunsPage from '../BacktestRunsPage';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  create: vi.fn(),
  precheck: vi.fn(),
  getJob: vi.fn(),
  syncMinuteBars: vi.fn(),
  equity: vi.fn(),
  benchmarks: vi.fn(),
  orders: vi.fn(),
  fills: vi.fn(),
  trades: vi.fn(),
  positions: vi.fn(),
  inputs: vi.fn(),
  diagnostics: vi.fn(),
  cancel: vi.fn(),
  rerun: vi.fn(),
  riskPolicies: vi.fn(),
  feePolicies: vi.fn(),
  candidates: vi.fn(),
  downloadExport: vi.fn(),
}));

vi.mock('../../api/backtestRuns', () => ({ backtestRunsApi: mocks }));

const runSummary = {
  runId: 'bt-test-20260105',
  mode: 'recorded_plan_replay',
  sourcePipeline: 'single_stock',
  riskProfile: 'balanced',
  status: 'queued',
  attempt: 1,
  progress: 0.1,
  phase: 'queued',
  metrics: null,
  counts: {},
  qualityFlags: [],
  createdAt: '2026-01-05T10:00:00',
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.list.mockResolvedValue({ total: 1, page: 1, pageSize: 20, items: [runSummary] });
  mocks.riskPolicies.mockResolvedValue([
    {
      profile: 'balanced',
      policyVersion: 'risk-policy.v1',
      maxSinglePositionPct: 20,
      maxTotalExposurePct: 80,
      maxConcurrentPositions: 5,
      minCashPct: 10,
      riskPerTradePct: 1,
      requireStopLoss: true,
      selectionTopN: 10,
    },
  ]);
  mocks.feePolicies.mockResolvedValue([
    { version: 'cn-a-share-2023.v1', description: 'test', rules: [] },
  ]);
  mocks.candidates.mockResolvedValue({ total: 0, items: [] });
  mocks.benchmarks.mockResolvedValue({ runId: 'bt-test-20260105', initialEquity: 100000, series: [], warnings: [] });
  mocks.precheck.mockResolvedValue({
    jobId: 'dj-1',
    jobType: 'precheck',
    status: 'succeeded',
    result: {
      status: 'ok',
      includedPlans: 1,
      excludedPlans: 0,
      warnings: [],
      blockingReasons: [],
      coverage: [],
    },
  });
  mocks.getJob.mockResolvedValue({
    jobId: 'dj-1',
    jobType: 'precheck',
    status: 'succeeded',
    result: {
      status: 'ok',
      includedPlans: 1,
      excludedPlans: 0,
      warnings: [],
      blockingReasons: [],
      coverage: [],
    },
  });
  mocks.create.mockResolvedValue(runSummary);
  mocks.get.mockResolvedValue(runSummary);
});

describe('BacktestRunsPage', () => {
  it('renders the experiment form and run list', async () => {
    render(<BacktestRunsPage />);
    expect(screen.getByText('新建历史 Agent 研究')).toBeInTheDocument();
    expect(screen.getByLabelText('开始日期')).toBeInTheDocument();
    expect(screen.getByLabelText('初始本金 (CNY)')).toBeInTheDocument();
    expect(screen.getByLabelText('候选范围')).toBeInTheDocument();
    expect(screen.getByText('实验列表')).toBeInTheDocument();
    expect(await screen.findByText(/bt-test-20260105/)).toBeInTheDocument();
    expect(screen.getAllByText('平衡').length).toBeGreaterThan(0);
  });

  it('runs precheck and creates an experiment', async () => {
    render(<BacktestRunsPage />);
    fireEvent.click(screen.getByRole('button', { name: /数据预检/ }));
    await waitFor(() => expect(mocks.precheck).toHaveBeenCalledTimes(1));
    expect(await screen.findByText('预检通过')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /创建实验/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    const [config, allowIncomplete] = mocks.create.mock.calls[0];
    expect(config).toMatchObject({
      mode: 'historical_agent_research',
      source_pipeline: 'research',
      granularity: '1d',
      risk_profile: 'balanced',
      research: {
        universe: [],
        universe_mode: 'auto_local_coverage',
        confirmation_mode: 'auto_accept',
        decision_frequency: 'monthly',
      },
    });
    expect(allowIncomplete).toBe(false);
  });

  it('builds a P3 research config with universe and budget', async () => {
    const researchResult = {
      status: 'warning',
      includedPlans: 0,
      excludedPlans: 0,
      warnings: ['research_not_a_production_strategy'],
      blockingReasons: [],
      coverage: [],
      diagnostics: {
        research: {
          universe: ['600519', '600330'],
          universeSize: 2,
          decisionCount: 3,
          estimatedLlmCalls: 12,
          maxLlmCalls: 500,
          modelRef: 'fake/model',
          labels: ['survivor_bias', 'model_knowledge_leakage_risk'],
          toolGaps: { fundamental: 'gap', news: 'gap' },
        },
      },
    };
    mocks.precheck.mockResolvedValue({
      jobId: 'dj-p3',
      jobType: 'precheck',
      status: 'succeeded',
      result: researchResult,
    });
    mocks.getJob.mockResolvedValue({
      jobId: 'dj-p3',
      jobType: 'precheck',
      status: 'succeeded',
      result: researchResult,
    });
    render(<BacktestRunsPage />);
    expect((await screen.findAllByText(/历史 Agent 研究/)).length).toBeGreaterThan(0);
    fireEvent.change(screen.getByLabelText('候选范围'), { target: { value: 'explicit' } });
    fireEvent.change(screen.getByPlaceholderText('600519, 600330, 002475'), {
      target: { value: '600519, 600330' },
    });
    fireEvent.change(screen.getByLabelText('LLM 调用上限'), { target: { value: '500' } });
    fireEvent.click(screen.getByRole('button', { name: /数据预检/ }));
    expect(await screen.findByText('P3 研究预检')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /创建实验/ }));
    await waitFor(() => expect(mocks.create).toHaveBeenCalledTimes(1));
    const [config] = mocks.create.mock.calls[0];
    expect(config).toMatchObject({
      mode: 'historical_agent_research',
      source_pipeline: 'research',
      research: {
        universe: ['600519', '600330'],
        decision_frequency: 'monthly',
        max_llm_calls: 500,
        selection_top_n: 5,
      },
    });
  });
});
