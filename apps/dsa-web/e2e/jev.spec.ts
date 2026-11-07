import { expect, test } from '@playwright/test';

const answer = { type: 'choice', choice: 'neutral', confidence: 0.61,
  probabilities: { bullish: 0.2, neutral: 0.6, bearish: 0.15, insufficient: 0.05 } };
const observation = {
  evaluation_id: 'jev-browser-fixture', stock_code: 'AAPL', market: 'us', as_of: '2026-09-18',
  status: 'ok', shadow_mode: true, execution_eligible: false, purpose: 'entry',
  model: 'browser-fixture', latency_ms: 800, usage: { input_tokens: 420, output_tokens: 80 },
  evaluation_mode: 'forward_shadow', source: 'manual',
  answers: Object.fromEntries([1, 3, 5].flatMap((n) => [
    [`direction_${n}`, answer],
    [`risk_${n}`, { ...answer, choice: 'elevated', probabilities: { normal: 0.2, elevated: 0.6, high: 0.15, insufficient: 0.05 } }],
  ])),
  state: { bar_count: 60, history_source: 'browser-fixture', missing_evidence: ['news', 'fundamentals'],
    price_plan: { status: 'unavailable' }, position: { status: 'not_supplied' } },
  outcomes: [1, 3, 5].map((horizon) => ({ horizon, status: 'pending', price_return_pct: null })),
};

for (const viewport of [{ width: 1440, height: 1000 }, { width: 390, height: 844 }]) {
  test(`Jev evaluation and history at ${viewport.width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/v1/auth/status', (route) => route.fulfill({ json: {
      authEnabled: false, loggedIn: false, setupState: 'no_password',
    } }));
    await page.route('**/api/v1/portfolio/accounts*', (route) => route.fulfill({ json: { accounts: [] } }));
    await page.route('**/api/v1/jev/status', (route) => route.fulfill({ json: {
      enabled: true, configured: true, shadow_mode: true, model: 'browser-fixture',
      markets: ['cn', 'hk', 'us'], horizons: [1, 3, 5],
    } }));
    await page.route('**/api/v1/jev/evaluations', (route) => route.fulfill({ json: { items: [observation] } }));
    await page.route('**/api/v1/jev/evaluations/jev-browser-fixture', (route) => route.fulfill({ json: observation }));
    await page.route('**/api/v1/jev/evaluate', async (route) => {
      expect(route.request().postDataJSON()).toEqual({ stock_code: 'AAPL', fallback_to_network: false });
      await route.fulfill({ json: observation });
    });
    await page.goto('/jev');
    await expect(page.getByRole('heading', { name: 'Jev 日线评估' })).toBeVisible();
    await page.getByRole('textbox', { name: '股票代码' }).fill('AAPL');
    await page.getByRole('button', { name: '运行评估' }).click();
    await expect(page.getByRole('region', { name: '评估详情' })).toBeVisible();
    await expect(page.getByText('无可复核方案')).toBeVisible();
    await expect(page.getByText('不可评估', { exact: true })).toHaveCount(3);
    await page.getByRole('region', { name: '评估记录' }).getByRole('button', { name: 'AAPL' }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    await page.evaluate(() => scrollTo(0, 0));
    await page.screenshot({ path: testInfo.outputPath(`jev-${viewport.width}.png`), fullPage: true, animations: 'disabled' });
  });
}
