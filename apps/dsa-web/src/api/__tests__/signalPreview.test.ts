import { describe, expect, it, vi } from 'vitest';
import { dedupeLatestByStock, signalPreviewApi } from '../signalPreview';

const mocks = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock('../index', () => ({
  default: { get: mocks.get },
}));

describe('signalPreviewApi', () => {
  it('keeps the first newest row for each market and stock code', () => {
    const rows = [
      { planId: 'new', stockCode: '600330', market: 'cn' },
      { planId: 'old', stockCode: '600330', market: 'CN' },
      { planId: 'other', stockCode: '600519', market: 'cn' },
    ];

    expect(dedupeLatestByStock(rows).map((item) => item.planId)).toEqual(['new', 'other']);
  });

  it('does not show a legacy entry beside the latest deterministic plan for the same stock', async () => {
    mocks.get.mockResolvedValueOnce({
      data: {
        entry_signals: [
          { signal_id: 'legacy', stock_code: '600330', market: 'cn' },
          { signal_id: 'other', stock_code: '600519', market: 'cn' },
        ],
        price_plans: [
          { plan_id: 'new-plan', stock_code: '600330', market: 'cn' },
          { plan_id: 'old-plan', stock_code: '600330', market: 'cn' },
        ],
        position_watches: [],
        scheduler: [],
        counts: { entry: 2, plans: 2, active_entry: 0, positions: 0, high_risk: 0 },
      },
    });

    const payload = await signalPreviewApi.list();

    expect(payload.pricePlans.map((item) => item.planId)).toEqual(['new-plan']);
    expect(payload.entrySignals.map((item) => item.signalId)).toEqual(['other']);
  });
});
