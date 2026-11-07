import { expect, test, type Page } from '@playwright/test';

const dates = Array.from({ length: 220 }, (_, index) => {
  const value = new Date(Date.UTC(2025, 0, 2 + index));
  return value.toISOString().slice(0, 10);
});

const bars = dates.map((date, index) => {
  const close = 100 + index * 0.12 + Math.sin(index / 7) * 3;
  const open = close + Math.sin(index / 3) * 0.8;
  const dif = Math.sin(index / 9) * 0.8;
  const dea = Math.sin((index - 2) / 9) * 0.65;
  return {
    date,
    open,
    high: Math.max(open, close) + 1.4,
    low: Math.min(open, close) - 1.4,
    close,
    volume: 800000 + index * 2500,
    amount: close * (800000 + index * 2500),
    pct_change: index ? 0.2 : 0,
    source: 'local_db',
    guide_line: index >= 199 ? close - 0.8 : null,
    ma57: index >= 56 ? close - 1.7 : null,
    volume_ma5: index >= 4 ? 800000 + (index - 2) * 2500 : null,
    dif: index >= 199 ? dif : null,
    dea: index >= 199 ? dea : null,
    macd_histogram: index >= 199 ? (dif - dea) * 2 : null,
  };
});

const listPayload = {
  entry_signals: [{
    signal_id: 'signal-600519',
    stock_code: '600519',
    stock_name: '贵州茅台',
    market: 'cn',
    signal_type: 'entry',
    status: 'proposed',
    effective_status: 'proposed',
    revision: 2,
    entry_low: 120,
    entry_high: 123,
    trigger_rule: {
      type: 'price_entered_zone',
      zones: [
        { label: 'ideal', low: 120, high: 123, rationale: '回踩 MA20 与成本区' },
        { label: 'secondary', low: 126, high: 128, rationale: '回踩 MA5 且缩量企稳' },
      ],
    },
    stop_price: 114,
    created_as_of: '2025-07-20T15:00:00',
    last_checked_as_of: '2025-07-20T15:00:00',
    valid_until: '2025-07-27',
    metadata: { current_action: 'reject', trigger_action: 'open', plan_mode: 'observation_only', execution_eligible: false },
  }],
  position_watches: [{
    watch_id: 'watch-000001',
    account_id: 1,
    stock_code: '000001',
    stock_name: '平安银行',
    market: 'cn',
    position_cycle_id: 1,
    status: 'active',
    quantity: 1000,
    average_cost: 118,
    last_checked_as_of: '2025-08-09T18:00:00',
    risk_level: 'medium',
    current_action: 'hold',
    latest_score: 14,
    latest_exit_warning_level: 1,
  }],
  scheduler: [
    { market: 'cn', status: 'success', online: true, last_success_at: '2025-08-09T10:10:00' },
    { market: 'hk', status: 'idle', online: true, last_success_at: null },
    { market: 'us', status: 'idle', online: true, last_success_at: null },
  ],
  counts: { entry: 1, active_entry: 1, positions: 1, high_risk: 0 },
};

const detailPayload = {
  kind: 'entry_signal',
  subject: listPayload.entry_signals[0],
  events: [
    { event_id: 'event-1', event_type: 'signal_created', occurred_at: '2025-07-20T15:00:00', as_of: '2025-07-20T15:00:00', source: 'single_stock_query', payload: { entry_low: 119, entry_high: 122, revision: 1 } },
    { event_id: 'event-revised', event_type: 'signal_revised', occurred_at: '2025-07-22T15:00:00', as_of: '2025-07-22T15:00:00', source: 'single_stock_query', payload: { entry_low: 120, entry_high: 123, entry_zones: [{ label: 'ideal', low: 120, high: 123 }, { label: 'secondary', low: 126, high: 128 }], revision: 2 } },
    { event_id: 'event-2', event_type: 'entry_price_triggered', occurred_at: '2025-07-23T15:00:00', as_of: '2025-07-23T15:00:00', source: 'lazy_reconcile', payload: { bar: { close: 121 } } },
  ],
  chart: {
    stock_code: '600519',
    market: 'cn',
    status: 'ok',
    bars,
    structure: {
      status: 'ok',
      data_quality: 'sufficient',
      pens: [
        { id: 1, direction: 'up', start: { time: dates[198], price: 117 }, end: { time: dates[208], price: 129 } },
        { id: 2, direction: 'down', start: { time: dates[208], price: 129 }, end: { time: dates[216], price: 123 } },
      ],
      centers: [{ id: 1, ZD: 121, ZG: 126, start_date: dates[201], end_date: dates[216] }],
      signals: [{ name: 'first_buy', timeframe: '1d', triggered: true }],
    },
    composite: {
      snapshot: {
        as_of: dates.at(-1),
        data_quality: 'ok',
        s4: { status: 'ok', region_pass: true, resonance_count: 4, volume_ratio_5d: 1.42, volatility_pct: 4.8, gain_10d_pct: 6.2, golden_cross_today: true, all_ok: true },
      },
      composite_verdict: {
        score: 18,
        scale_max: 20,
        signal_level: 'strong_confirmed',
        verdict_status: 'evaluable',
        entry_permission: 'allowed',
        components: { s4: { score: 10, status: 'ok' }, chan: { score: 8, status: 'ok' } },
        exit_warning: { level: 0, items: [] },
        missing_features: [],
      },
    },
    corporate_actions: [{ date: dates[205], action_type: 'cash_dividend', note: '现金分红' }],
    data_window: { start: dates[0], end: dates.at(-1), bar_count: bars.length, sources: ['local_db'], price_basis: 'local_cash_price_unadjusted' },
  },
};

const evaluationPayload = {
  sample_count: 42,
  eligible_count: 36,
  triggered_count: 18,
  untriggered_count: 18,
  ambiguous_count: 2,
  hit_rate_pct: 50,
  average_wait_trading_days: 2.8,
  execution_adjusted_return_pct: 1.6,
  opportunity_cost_pct: 2.1,
  avoided_loss_pct: 1.2,
  horizons: {
    5: { signal_return_pct: 2.4, benchmark_return_pct: 1.1, excess_return_pct: 1.29, sample_count: 18 },
    10: { signal_return_pct: 4.2, benchmark_return_pct: 1.8, excess_return_pct: 2.36, sample_count: 17 },
    20: { signal_return_pct: 6.8, benchmark_return_pct: 3.1, excess_return_pct: 3.59, sample_count: 15 },
  },
  by_setup_type: [{
    setup_type: 'chan_first_buy', sample_count: 32, eligible_count: 30, triggered_count: 17,
    hit_rate_pct: 56.67, coverage_days: 90, review_status: 'reviewable', calibration_applied: false,
  }],
  review_policy: { min_samples: 30, min_coverage_days: 60, mode: 'shadow_only' },
  rows: [],
};

async function mockSignalApi(page: Page) {
  await page.route('**/api/v1/signal-preview/evaluation*', (route) => route.fulfill({ json: evaluationPayload }));
  await page.route('**/api/v1/signal-preview', (route) => route.fulfill({ json: listPayload }));
  await page.route('**/api/v1/signal-preview/entries/**', (route) => route.fulfill({ json: detailPayload }));
}

async function expectNonBlankCanvas(page: Page) {
  const result = await page.locator('[data-testid="signal-preview-chart"] canvas').first().evaluate((canvas) => {
    const element = canvas as HTMLCanvasElement;
    const context = element.getContext('2d');
    if (!context) return { colored: 0, width: element.width, height: element.height };
    const pixels = context.getImageData(0, 0, element.width, element.height).data;
    let colored = 0;
    for (let index = 0; index < pixels.length; index += 64) {
      if (pixels[index + 3] > 0 && (pixels[index] + pixels[index + 1] + pixels[index + 2]) > 20) colored += 1;
    }
    return { colored, width: element.width, height: element.height };
  });
  expect(result.width).toBeGreaterThan(800);
  expect(result.height).toBeGreaterThan(500);
  expect(result.colored).toBeGreaterThan(100);
}

async function expectNoHorizontalOverflow(page: Page) {
  const result = await page.evaluate(() => ({
    viewport: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    offenders: Array.from(document.querySelectorAll<HTMLElement>('body *'))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        return { tag: element.tagName, className: element.className, left: rect.left, right: rect.right, width: rect.width };
      })
      .filter((item) => item.left < -1 || item.right > window.innerWidth + 1)
      .slice(0, 10),
  }));
  expect(result, JSON.stringify(result, null, 2)).toMatchObject({ scrollWidth: result.viewport, offenders: [] });
}

test('signal preview renders chart evidence on desktop and mobile', async ({ page }, testInfo) => {
  await mockSignalApi(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('/signal-preview');

  await expect(page.getByRole('heading', { name: '信号预览' })).toBeVisible();
  await expect(page.getByText('贵州茅台').first()).toBeVisible();
  await expect(page.getByText('仅观察').first()).toBeVisible();
  await expect(page.getByText('18 / 20')).toBeVisible();
  await expect(page.getByText('1.42')).toBeVisible();
  await expect(page.getByRole('heading', { name: '信号影子评估' })).toBeVisible();
  await expect(page.getByText('缠论一买')).toBeVisible();
  await expect(page.locator('[data-testid="signal-preview-chart"] canvas').first()).toBeVisible();
  await expectNonBlankCanvas(page);
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: testInfo.outputPath('signal-preview-desktop.png'), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByText('个股计划').first()).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await expect(page.getByText('贵州茅台').first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('signal-preview-mobile.png'), fullPage: false });
});

test('legacy verdict path redirects to signal preview', async ({ page }) => {
  await mockSignalApi(page);
  await page.goto('/agent-verdict-reviews');
  await expect(page).toHaveURL(/\/signal-preview$/);
});
