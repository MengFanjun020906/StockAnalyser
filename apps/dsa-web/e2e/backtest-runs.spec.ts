import { expect, test } from '@playwright/test';

const runSummary = {
  run_id: 'bt-e2e-20260105',
  mode: 'historical_agent_research',
  source_pipeline: 'research',
  risk_profile: 'balanced',
  status: 'succeeded',
  attempt: 1,
  request_fingerprint: 'sha256:req',
  experiment_fingerprint: 'sha256:exp',
  data_quality: 'partial',
  quality_flags: [],
  progress: 1,
  phase: 'completed',
  metrics: {
    total_return_pct: 2.8,
    max_drawdown_pct: -0.5,
    sharpe: null,
    annualized_return_pct: null,
    benchmark_return_pct: null,
    excess_return_pct: null,
    cash_ratio_pct: 80,
    turnover_pct: 40,
    fee_drag_pct: 0.02,
    slippage_drag_pct: 0.01,
    trading_days: 5,
    daily_return_count: 5,
    not_applicable: { sharpe: 'short_sample' },
    single_trade: { win_rate_pct: 100, closed_trades: 1, unclosed_trades: 0 },
  },
  counts: { orders: 2, fills: 2, closed_trades: 1, equity_points: 5 },
  created_at: '2026-01-05T10:00:00',
  started_at: '2026-01-05T10:00:01',
  finished_at: '2026-01-05T10:00:02',
  result_available: true,
};

const detail = {
  ...runSummary,
  manifest: {
    sessions: ['2026-01-05'],
    symbols: ['600519'],
    coverage: [
      {
        symbol: '600519',
        sessions_expected: 5,
        sessions_full: 5,
        sessions_partial: 0,
        sessions_missing: 0,
        sessions_suspended: 0,
        sessions_pre_listing: 0,
        missing_bars: 0,
        expected_bars_per_session: 48,
        quality: 'complete',
        flags: [],
      },
    ],
  },
  coverage: [
    {
      symbol: '600519',
      sessions_expected: 5,
      sessions_full: 5,
      sessions_partial: 0,
      sessions_missing: 0,
      sessions_suspended: 0,
      sessions_pre_listing: 0,
      missing_bars: 0,
      expected_bars_per_session: 48,
      quality: 'complete',
      flags: [],
    },
  ],
  inputs: [
    {
      input_id: 'input-1',
      source_id: 'plan-1',
      plan_id: 'plan-1',
      scenario_id: 's1',
      symbol: '600519',
      included: true,
      source_pipeline: 'single_stock',
      link_quality: 'single_stock',
    },
  ],
  diagnostics: { ambiguous_fills: 0, stale_sessions: 0 },
  research: {
    modelRef: 'fake/research-model',
    llmCalls: 4,
    cacheHits: 0,
    dataSource: 'sequoia:vendor/Sequoia-X/data/sequoia_v2.db',
    labels: ['survivor_bias', 'local_coverage_universe'],
    decisions: [],
  },
};

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/auth/status', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ authEnabled: false, loggedIn: true, setupState: 'no_password' }),
    }),
  );
  await page.route('**/api/v1/backtest/risk-policies', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([
        {
          profile: 'balanced',
          policy_version: 'risk-policy.v1',
          max_single_position_pct: 20,
          max_total_exposure_pct: 80,
          max_concurrent_positions: 5,
          min_cash_pct: 10,
          risk_per_trade_pct: 1,
          require_stop_loss: true,
          selection_top_n: 10,
        },
      ]),
    }),
  );
  await page.route('**/api/v1/backtest/fee-policies', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([{ version: 'cn-a-share-2023.v1', description: 'test', rules: [] }]),
    }),
  );
  await page.route('**/api/v1/backtest/candidates**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ total: 1, items: [{ plan_id: 'plan-1', symbol: '600519' }] }) }),
  );
  await page.route('**/api/v1/backtest/runs', async (route) => {
    if (route.request().method() === 'POST') {
      await route.fulfill({ status: 202, contentType: 'application/json', body: JSON.stringify(runSummary) });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ total: 1, page: 1, page_size: 20, items: [runSummary] }),
    });
  });
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(detail) }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/benchmarks', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        run_id: 'bt-e2e-20260105',
        initial_equity: 100000,
        series: [
          {
            symbol: '000001.SH',
            name: '上证指数',
            status: 'ok',
            points: [{ session: '2026-01-05', value: 100000 }],
          },
        ],
        warnings: [],
      }),
    }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/equity**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total: 1,
        page: 1,
        page_size: 500,
        items: [
          {
            session: '2026-01-05',
            cash: 100000,
            reserved_cash: 0,
            available_cash: 100000,
            market_value: 0,
            equity: 102800,
            peak_equity: 102800,
            drawdown_pct: 0,
            stale: false,
          },
        ],
      }),
    }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/orders**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ total: 0, page: 1, page_size: 100, items: [] }) }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/fills**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        total: 1,
        page: 1,
        page_size: 100,
        items: [
          {
            fill_id: 'fill-1',
            order_id: 'order-1',
            symbol: '600519',
            side: 'buy',
            quantity: 100,
            price: 10,
            gross_amount: 1000,
            commission: 5,
            transfer_fee: 0,
            stamp_duty: 0,
            slippage_cost: 0,
            session: '2026-01-05',
            quality: 'full',
            reason: 'buy_limit',
          },
        ],
      }),
    }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/trades**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ total: 0, page: 1, page_size: 100, items: [] }) }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/positions', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: [] }) }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/inputs**', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: detail.inputs }) }),
  );
  await page.route('**/api/v1/backtest/runs/bt-e2e-20260105/diagnostics', (route) =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: detail.diagnostics }) }),
  );
});

test('create P3 research, inspect fills, and expose export actions', async ({ page }) => {
  await page.goto('/backtest');
  await expect(page.getByText('新建历史 Agent 研究')).toBeVisible();
  await expect(page.getByText('实验列表')).toBeVisible();

  await page.getByRole('button', { name: /创建实验/ }).click();
  await expect(page.getByText(/bt-e2e-20260105/)).toBeVisible();
  await expect(page.getByText('总收益')).toBeVisible();

  await page.getByRole('button', { name: '成交' }).click();
  await expect(page.getByText('600519')).toBeVisible();
  await expect(page.getByRole('button', { name: /^fills$/ })).toBeVisible();
});
