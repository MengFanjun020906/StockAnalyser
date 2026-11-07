import { expect, test } from '@playwright/test';

const answers = Object.fromEntries([1, 3, 5].flatMap((day) => [
  [`direction_${day}`, { type: 'choice', choice: 'bearish', confidence: 0.7, probabilities: { bearish: 0.7, bullish: 0.1, neutral: 0.15, insufficient: 0.05 } }],
  [`risk_${day}`, { type: 'choice', choice: 'high', confidence: 0.7, probabilities: { normal: 0.1, elevated: 0.2, high: 0.7, insufficient: 0 } }],
]));
const review = {
  status: 'ok', shadow_mode: true, shared_snapshot: true, session_id: 'trace-jev-browser', capture_id: 'browser-one',
  baseline: { action: 'open', source: 'risk_gate', trading_horizon: 'swing', summary: '原证据支持等待价格确认后入场。', reasons: [] },
  evaluation: { status: 'ok', shadow_mode: true, stock_code: '600519', as_of: '2026-09-18', model: 'browser-fixture',
    latency_ms: 1000, usage: { input_tokens: 3000, output_tokens: 300 }, answers,
    state: { history_source: 'trace-fixture', bar_count: 90, missing_evidence: ['fundamental'],
      price_plan: { status: 'unavailable' }, position: { status: 'not_supplied' },
      agent_evidence: { technical: [{ tool: 'analyze_trend', quality: 'available', summary: '短期价格处于均线下方。' }] } },
    outcomes: [1, 3, 5].map((horizon) => ({ horizon, status: 'pending', price_return_pct: null })),
  },
  comparison: { status: 'divergent', rows: [1, 3, 5].map((horizon) => ({ horizon, status: 'divergent', flags: ['downside_conflict'] })) },
};

for (const width of [1440, 390]) {
  test(`Trace shows original report and appends Jev at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    const errors: string[] = [];
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/api/v1/auth/status', (route) => route.fulfill({ json: { authEnabled: false, loggedIn: false, setupState: 'no_password' } }));
    await page.route('**/api/v1/portfolio/accounts*', (route) => route.fulfill({ json: { accounts: [] } }));
    await page.route('**/api/v1/agent/runtime-config', (route) => route.fulfill({ json: { runtime_config: {} } }));
    await page.route('**/api/v1/agent/trace/sessions/trace-jev-browser', (route) => route.fulfill({ json: {
      id: 'trace-jev-browser', createdAt: '2026-09-20', message: '分析 600519 日线入场条件', stockCode: '600519', status: 'success',
      result: { success: true, session_id: 'trace-jev-browser', content: '## 最终结论\n\n原判断：条件入场，继续等待价格确认。',
        total_steps: 1, total_tokens: 100, provider: 'fixture', model: 'fixture', mode: 'planning_execute', events: [],
        tool_calls: [{ tool: 'get_stock_business_context', arguments: { stock_code: '600519' }, success: true, step: 1, duration: 0.01 }],
        artifact_dir: 'data/agent_traces/20260920-190518-trace-jev-browser-long-session-for-mobile-layout',
        planner: { intent: 'entry_analysis' }, jev_review: { ...review, status: 'pending', comparison: null } },
    } }));
    let completed = false;
    await page.route('**/api/v1/agent/trace/sessions/trace-jev-browser/jev', (route) => route.fulfill({
      json: completed ? review : { ...review, status: 'pending', comparison: null },
    }));
    await page.goto('/agent-trace/trace-jev-browser');
    await expect(page.getByText('原判断：条件入场，继续等待价格确认。')).toBeVisible();
    await expect(page.getByText('原分析已完成，Jev 复核中。')).toBeVisible();
    completed = true;
    const comparison = page.getByRole('region', { name: 'Jev 与原结论对照' });
    await expect(comparison.getByText('入场动作与 Jev 下行判断有分歧')).toHaveCount(3);
    await expect(page.getByText('原判断：条件入场，继续等待价格确认。')).toBeVisible();
    await expect(page.getByText('Jev 独立复核', { exact: true })).toBeVisible();
    await comparison.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(errors).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`jev-trace-${width}.png`), fullPage: true, animations: 'disabled' });
  });
}
