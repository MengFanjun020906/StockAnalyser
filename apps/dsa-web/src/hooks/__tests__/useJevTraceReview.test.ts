import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useJevTraceReview } from '../useJevTraceReview';
import type { JevTraceReview } from '../../api/jev';

const read = vi.hoisted(() => vi.fn());
vi.mock('../../api/jev', () => ({ jevApi: { traceReview: read } }));
const pending: JevTraceReview = { status: 'pending', shadow_mode: true, capture_id: 'capture' };

describe('Trace Jev polling', () => {
  beforeEach(() => { read.mockReset(); });

  it('does not request a review before the original run finishes', () => {
    renderHook(() => useJevTraceReview('trace-a', pending, false));
    expect(read).not.toHaveBeenCalled();
  });

  it('ignores an old response after switching Trace sessions', async () => {
    let resolveOld!: (value: JevTraceReview) => void;
    read.mockImplementation((id: string) => id === 'trace-a' ? new Promise((resolve) => { resolveOld = resolve; })
      : Promise.resolve({ status: 'ok', session_id: 'trace-b', capture_id: 'capture', shadow_mode: true }));
    const { result, rerender } = renderHook(({ id }) => useJevTraceReview(id, pending, true), { initialProps: { id: 'trace-a' } });
    rerender({ id: 'trace-b' });
    await waitFor(() => expect(result.current.review?.session_id).toBe('trace-b'));
    await act(async () => resolveOld({ status: 'error', session_id: 'trace-a', shadow_mode: true }));
    expect(result.current.review?.session_id).toBe('trace-b');
  });

  it('keeps the initial review and exposes read errors', async () => {
    read.mockRejectedValue(new Error('offline'));
    const { result, unmount } = renderHook(() => useJevTraceReview('trace-a', pending, true));
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.review).toBe(pending);
    unmount();
  });
});
