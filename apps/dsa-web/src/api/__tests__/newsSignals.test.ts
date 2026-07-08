import { describe, expect, it, vi } from 'vitest';
import { newsSignalsApi } from '../newsSignals';

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

describe('newsSignalsApi', () => {
  it('uses extended timeouts for slow rebuild and Graphiti sync operations', async () => {
    mocks.post.mockResolvedValue({ data: { status: 'ok' } });

    await newsSignalsApi.rebuild();
    await newsSignalsApi.graphSync({ signalDate: '2026-07-04' });

    expect(mocks.post).toHaveBeenNthCalledWith(
      1,
      '/api/v1/news-signals/rebuild',
      undefined,
      expect.objectContaining({
        timeout: 120000,
        params: expect.objectContaining({ sync_graphiti: false, include_semantic_edges: false }),
      }),
    );
    expect(mocks.post).toHaveBeenNthCalledWith(
      2,
      '/api/v1/news-signals/graph-sync',
      undefined,
      expect.objectContaining({
        timeout: 300000,
        params: expect.objectContaining({ include_semantic_edges: false }),
      }),
    );
  });

  it('loads a card graph from the graph endpoint', async () => {
    mocks.get.mockResolvedValue({ data: { center_card_id: 'card:test', nodes: [], edges: [] } });

    const result = await newsSignalsApi.graph('card:test', 50);

    expect(mocks.get).toHaveBeenCalledWith('/api/v1/news-signals/card%3Atest/graph', {
      params: { limit: 50 },
    });
    expect(result.centerCardId).toBe('card:test');
    expect(result.nodes).toEqual([]);
    expect(result.edges).toEqual([]);
  });
});
