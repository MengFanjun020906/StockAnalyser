import { describe, expect, it } from 'vitest';
import type { EntrySignal } from '../../api/signalPreview';
import { entryDisplayState } from '../signalPreviewPresentation';

function signal(status: string): EntrySignal {
  return {
    signalId: 'signal-1',
    stockCode: '600519',
    market: 'cn',
    signalType: 'entry',
    status,
    revision: 1,
    createdAsOf: '2026-08-01T15:00:00',
    lastCheckedAsOf: '2026-08-01T15:00:00',
    validUntil: '2026-08-08',
    metadata: { currentAction: 'wait', triggerAction: 'open' },
  };
}

describe('signalPreviewPresentation', () => {
  it('shows an active conditional plan as waiting for trigger', () => {
    expect(entryDisplayState(signal('active'))).toBe('waiting_trigger');
  });

  it('does not hide a terminal lifecycle status', () => {
    expect(entryDisplayState(signal('invalidated'))).toBe('invalidated');
  });

  it('distinguishes observation-only and risk-blocked plans', () => {
    const observation = signal('needs_review');
    observation.metadata = { currentAction: 'reject', triggerAction: 'open', planMode: 'observation_only' };
    expect(entryDisplayState(observation)).toBe('observation_only');

    const blocked = signal('proposed');
    blocked.metadata = { currentAction: 'wait', triggerAction: 'open', planMode: 'risk_blocked' };
    expect(entryDisplayState(blocked)).toBe('risk_blocked');
  });

  it('does not label normal technical waiting as a risk gate block', () => {
    const waiting = signal('proposed');
    waiting.metadata = {
      currentAction: 'wait',
      triggerAction: 'open',
      planMode: 'waiting_confirmation',
      executionStatus: 'waiting_confirmation',
    };
    expect(entryDisplayState(waiting)).toBe('waiting_confirmation');
  });
});
