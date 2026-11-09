import { expect, test, type Page } from '@playwright/test';

const smokePassword = process.env.DSA_WEB_SMOKE_PASSWORD;

async function login(page: Page) {
  test.skip(!smokePassword, 'Set DSA_WEB_SMOKE_PASSWORD to run authenticated smoke tests.');

  await page.goto('/login');
  await page.waitForLoadState('domcontentloaded');

  const passwordInput = page.locator('#password');
  const submitButton = page.getByRole('button', { name: /授权进入工作台|完成设置并登录/ });
  const traceLink = page.getByRole('link', { name: '链路' });

  const isAlreadyAuthenticated =
    page.url().includes('/agent-trace') ||
    await traceLink.isVisible({ timeout: 2_000 }).catch(() => false);

  if (isAlreadyAuthenticated) {
    await page.waitForLoadState('domcontentloaded');
    return;
  }

  await expect(passwordInput).toBeVisible({ timeout: 10_000 });
  await passwordInput.fill(smokePassword!);
  await expect(submitButton).toBeVisible();

  await Promise.all([
    page.waitForResponse(
      (response) => response.url().includes('/api/v1/auth/login') && response.status() === 200,
      { timeout: 15_000 }
    ),
    submitButton.click(),
  ]);

  await page.waitForURL('**/agent-trace', { timeout: 15_000 });
  await page.waitForLoadState('domcontentloaded');
  await page.waitForTimeout(1000);
}

test.describe('web smoke', () => {
  test('login page renders password form', async ({ page }) => {
    await page.goto('/login');
    await page.waitForLoadState('domcontentloaded');

    // Check for branding
    await expect(page.getByText('DAILY STOCK').first()).toBeVisible();
    await expect(page.getByText('Analysis Engine')).toBeVisible();

    // Check for password input
    await expect(page.locator('#password')).toBeVisible();

    // Check for submit button
    await expect(page.getByRole('button', { name: /授权进入工作台|完成设置并登录/ })).toBeVisible();
  });

  test('root path redirects to agent trace and removed entries are gone', async ({ page }) => {
    await login(page);

    await expect(page).toHaveURL(/\/agent-trace$/);
    await expect(page.getByRole('link', { name: '链路' })).toBeVisible();
    await expect(page.getByRole('link', { name: '信号' })).toBeVisible();
    await expect(page.getByRole('link', { name: '首页' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: '问股' })).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'Jev' })).toHaveCount(0);
  });

  test('mobile shell opens navigation drawer after login', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await login(page);

    // Try to open navigation menu
    const menuButton = page.getByRole('button', { name: /打开导航|菜单/i });
    if (await menuButton.isVisible({ timeout: 2000 }).catch(() => false)) {
      await menuButton.click();
    }

    // Check if navigation is visible
    await expect(page.getByRole('link', { name: '回测' })).toBeVisible({ timeout: 5000 });
  });

  test('settings page renders title and save actions after login', async ({ page }) => {
    await login(page);

    // Navigate to settings page by clicking the link
    await page.getByRole('link', { name: '设置' }).click();
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);

    // Use heading role for more precise selection
    await expect(page.getByRole('heading', { name: '系统设置' })).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: '重置' })).toBeVisible();
    await expect(page.getByRole('button', { name: /保存配置/ })).toBeVisible();
  });

  test('backtest page renders filter controls after login', async ({ page }) => {
    await login(page);

    // Navigate to backtest page by clicking the link
    await page.getByRole('link', { name: '回测' }).click();
    await page.waitForLoadState('domcontentloaded');
    await page.waitForTimeout(1000);

    // Check for filter controls
    const filterInput = page.getByPlaceholder(/stock code/i);
    await expect(filterInput).toBeVisible({ timeout: 10_000 });
    await expect(page.getByRole('button', { name: /filter/i })).toBeVisible();
    await expect(page.getByRole('button', { name: /run backtest/i })).toBeVisible();
  });
});
