import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import JevPage from '../JevPage';

const mocks = vi.hoisted(() => ({ status: vi.fn(), list: vi.fn(), evaluate: vi.fn(), detail: vi.fn(), accounts: vi.fn() }));
vi.mock('../../api/jev', () => ({ jevApi: mocks }));
vi.mock('../../api/portfolio', () => ({ portfolioApi: { getAccounts: mocks.accounts } }));

describe('Jev daily evaluation', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.status.mockResolvedValue({ enabled: true, configured: true, model: 'jev-test', shadow_mode: true });
    mocks.list.mockResolvedValue({ items: [] });
    mocks.accounts.mockResolvedValue({ accounts: [] });
  });

  it('keeps evaluation disabled when provider configuration is absent', async () => {
    mocks.status.mockResolvedValue({ enabled: false, configured: false, model: 'jev-test' });
    render(<MemoryRouter><JevPage /></MemoryRouter>);
    await screen.findByText('Jev 配置未就绪。');
    fireEvent.change(screen.getByPlaceholderText('600519 / hk00700 / AAPL'), { target: { value: 'AAPL' } });
    expect(screen.getByRole('button', { name: '运行评估' })).toBeDisabled();
    expect(mocks.evaluate).not.toHaveBeenCalled();
  });

  it('retains missing plan state and does not expose an execution action', async () => {
    const result = {
      evaluation_id: 'jev-unit', status: 'ok', stock_code: 'AAPL', market: 'us', as_of: '2026-07-24',
      shadow_mode: true, answers: {
        direction_1: { type: 'choice', choice: 'bullish', confidence: 0.6, probabilities: { bullish: 0.8, neutral: 0.2 } },
      },
    };
    mocks.evaluate.mockResolvedValue(result);
    mocks.detail.mockResolvedValue({ ...result, outcomes: [{ horizon: 1, status: 'pending', target_date: '2026-07-27' }] });
    render(<MemoryRouter><JevPage /></MemoryRouter>);
    await screen.findByText('已配置');
    fireEvent.change(screen.getByPlaceholderText('600519 / hk00700 / AAPL'), { target: { value: 'AAPL' } });
    fireEvent.click(screen.getByRole('button', { name: '运行评估' }));
    await waitFor(() => expect(mocks.evaluate).toHaveBeenCalledWith({ stock_code: 'AAPL', account_id: undefined, fallback_to_network: false }));
    expect(await screen.findByText('无可复核方案')).toBeInTheDocument();
    expect(screen.getByText('选项概率 80% · 置信度 60%')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /下单|买入|卖出/ })).not.toBeInTheDocument();
  });
});
