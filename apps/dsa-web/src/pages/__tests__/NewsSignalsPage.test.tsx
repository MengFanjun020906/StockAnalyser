import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import NewsSignalsPage from '../NewsSignalsPage';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  metrics: vi.fn(),
  get: vi.fn(),
  graph: vi.fn(),
  rebuild: vi.fn(),
  graphSync: vi.fn(),
  feedback: vi.fn(),
}));

vi.mock('../../api/newsSignals', () => ({
  newsSignalsApi: {
    list: mocks.list,
    metrics: mocks.metrics,
    get: mocks.get,
    graph: mocks.graph,
    rebuild: mocks.rebuild,
    graphSync: mocks.graphSync,
    feedback: mocks.feedback,
  },
}));

describe('NewsSignalsPage', () => {
  beforeEach(() => {
    mocks.list.mockReset();
    mocks.metrics.mockReset();
    mocks.get.mockReset();
    mocks.graph.mockReset();
    mocks.rebuild.mockReset();
    mocks.graphSync.mockReset();
    mocks.feedback.mockReset();
    mocks.list.mockResolvedValue(makeListResponse());
    mocks.metrics.mockResolvedValue({
      totalCards: 1,
      activeCards: 1,
      suppressedCards: 0,
      graphSyncCounts: { pending: 1 },
      feedbackCounts: {},
    });
    mocks.get.mockResolvedValue(makeListResponse().items[0]);
    mocks.graph.mockResolvedValue({
      centerCardId: 'card:test',
      nodes: [{ id: 'card:test', type: 'card', label: '央行开展逆回购操作' }],
      edges: [
        {
          edgeId: 'edge:test',
          sourceCardId: 'card:test',
          targetType: 'macro_theme',
          targetId: 'macro_theme:国内流动性',
          edgeClass: 'typed_relation',
          edgeType: 'affects_macro_theme',
          weight: 0.82,
          edgeQuality: 91,
          qualityGrade: 'high',
          qualityFlags: [],
          method: 'rule',
          rationale: '新闻卡片主主题指向国内流动性。',
        },
      ],
      summary: { edgeCount: 1 },
    });
    mocks.rebuild.mockResolvedValue({
      status: 'ok',
      rawEpisodesUpserted: 2,
      cardsUpserted: 1,
    });
    mocks.graphSync.mockResolvedValue({
      status: 'ok',
      synced: 1,
      failed: 0,
    });
  });

  it('rebuilds cards without Graphiti sync and exposes a separate graph sync button', async () => {
    render(<NewsSignalsPage />);

    expect(await screen.findByText('消息面')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '重建卡片' }));
    await waitFor(() => {
      expect(mocks.rebuild).toHaveBeenCalledWith(
        expect.objectContaining({
          syncGraphiti: false,
        }),
      );
    });

    fireEvent.click(screen.getByRole('button', { name: '同步图谱' }));
    await waitFor(() => {
      expect(mocks.graphSync).toHaveBeenCalledWith({
        signalDate: expect.any(String),
        limit: 100,
        includeSemanticEdges: false,
      });
    });
    expect(await screen.findByText(/图谱同步 ok/)).toBeInTheDocument();
    expect(await screen.findByText('事件线索')).toBeInTheDocument();
    expect(await screen.findByText('强边')).toBeInTheDocument();
    expect(await screen.findByText(/质量 91/)).toBeInTheDocument();
    expect(await screen.findByText('事件事实')).toBeInTheDocument();
    expect(await screen.findByText('source_verified')).toBeInTheDocument();
    expect((await screen.findAllByText('央行开展逆回购操作，维护流动性合理充裕。')).length).toBeGreaterThan(0);
    expect(await screen.findByText('事件得分 72.5')).toBeInTheDocument();
    expect((await screen.findAllByText(/\[政策\/宏观\]/)).length).toBeGreaterThan(0);
    expect(await screen.findByText('入库质量')).toBeInTheDocument();
    expect(await screen.findByText('medium')).toBeInTheDocument();
  });
});

function makeListResponse() {
  return {
    total: 1,
    items: [
      {
        cardId: 'card:test',
        signalDate: '2026-07-04',
        signalLayer: 'macro',
        summaryShort: '央行开展逆回购操作',
        newsTone: 'neutral',
        marketImpact: 'positive',
        impactHorizon: 'short',
        signalScore: 72,
        adjustedSignalScore: 72,
        mappingStatus: 'industry_only',
        status: 'active',
        primaryIndustries: ['国内流动性'],
        companyImpacts: [],
        transmissionPaths: [
          {
            source: '国内流动性',
            target: '银行间资金面',
            eventCategory: '政策/宏观',
            eventScore: 72.5,
            chainSteps: [
              { label: '政策/宏观', text: '央行开展逆回购操作', score: 22.5 },
              { label: '传导机制', text: '流动性投放影响风险偏好', score: 18 },
              { label: '映射落点', text: '产业级线索', score: 14 },
            ],
            conclusion: '政策/宏观对国内流动性形成中强催化。',
          },
        ],
        feedbackCounts: {},
        extractedEvents: [
          {
            eventId: 'event:test',
            rawEpisodeId: 'raw:test',
            cardId: 'card:test',
            eventType: '政策/宏观',
            trigger: '逆回购',
            subject: '国内流动性',
            object: '银行间资金面',
            direction: 'benefit',
            evidenceSentence: '央行开展逆回购操作，维护流动性合理充裕。',
            source: 'macro_finance',
            extractor: 'rule_fallback',
            confidence: 0.76,
            verificationStatus: 'source_verified',
            entityLinks: [{ entityType: 'industry', name: '国内流动性', confidence: 0.72 }],
          },
        ],
        rawEpisodes: [
          {
            episodeId: 'raw:test',
            title: '央行开展逆回购操作',
            source: 'macro_finance',
            publishedAt: '2026-07-04T09:05:00',
            normalizedContent: '央行开展逆回购操作，维护流动性合理充裕。',
            qualityScore: 68,
            qualityGrade: 'medium',
            qualityFlags: ['thin_content'],
          },
        ],
      },
    ],
    summary: {
      total: 1,
      active: 1,
      suppressed: 0,
      layerCounts: { macro: 1 },
    },
  };
}
