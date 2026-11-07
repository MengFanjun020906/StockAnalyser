import { describe, expect, it, vi } from 'vitest';
import { KRONOS_ANALYZE_TIMEOUT_MS, kronosApi } from '../kronos';

const mocks = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}));

vi.mock('../index', () => ({
  default: {
    get: mocks.get,
    post: mocks.post,
  },
}));

describe('kronosApi', () => {
  it('uses an extended timeout for model-backed analysis', async () => {
    mocks.post.mockResolvedValue({
      data: {
        status: 'ok',
        bars: [],
        forecast: [],
        signal: {},
        forecast_summary: {},
        pattern_instances: [],
      },
    });

    await kronosApi.analyze({ stockCode: '600519', lookback: 60, predLen: 3 });

    expect(mocks.post).toHaveBeenCalledWith(
      '/api/v1/kronos/signals/analyze',
      expect.objectContaining({
        stock_code: '600519',
        frequency: '1d',
        lookback: 60,
        pred_len: 3,
      }),
      expect.objectContaining({ timeout: KRONOS_ANALYZE_TIMEOUT_MS }),
    );
    expect(KRONOS_ANALYZE_TIMEOUT_MS).toBeGreaterThan(30000);
  });
});
