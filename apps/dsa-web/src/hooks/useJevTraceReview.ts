import { useEffect, useState } from 'react';
import { jevApi, type JevTraceReview } from '../api/jev';
import { getParsedApiError, type ParsedApiError } from '../api/error';

export function useJevTraceReview(sessionId: string, initial: JevTraceReview | null | undefined, enabled: boolean) {
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [remote, setRemote] = useState<{ sessionId: string; captureId?: string; review?: JevTraceReview;
    error?: ParsedApiError } | null>(null);
  const captureId = initial?.capture_id;
  const hasReview = Boolean(initial);
  useEffect(() => {
    if (!sessionId || !hasReview || !enabled) return;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    const read = async () => {
      try {
        const review = await jevApi.traceReview(sessionId);
        if (!active) return;
        if (review.session_id && review.session_id !== sessionId) return;
        if (captureId && review.capture_id && review.capture_id !== captureId) return;
        failures = 0;
        setRemote({ sessionId, captureId, review });
        if (review.status === 'pending') timer = setTimeout(() => void read(), 2000);
      } catch (cause) {
        if (!active) return;
        setRemote({ sessionId, captureId, error: getParsedApiError(cause) });
        if (++failures < 3) timer = setTimeout(() => void read(), 3000);
      }
    };
    void read();
    return () => { active = false; clearTimeout(timer); };
  }, [sessionId, captureId, hasReview, enabled, refreshVersion]);
  const current = remote?.sessionId === sessionId && remote.captureId === captureId ? remote : null;
  return { review: current?.review || initial, error: current?.error,
    refresh: () => setRefreshVersion((version) => version + 1) };
}
