import type { EntrySignal } from '../api/signalPreview';

function metadataValue(signal: EntrySignal, key: 'currentAction' | 'triggerAction' | 'planMode' | 'executionStatus'): string {
  const metadata = signal.metadata || {};
  const direct = metadata[key];
  if (typeof direct === 'string' && direct) return direct;
  const tradePlan = metadata.tradePlan;
  if (tradePlan && typeof tradePlan === 'object') {
    const nested = (tradePlan as Record<string, unknown>)[key];
    if (typeof nested === 'string' && nested) return nested;
  }
  return '';
}

export function entryCurrentAction(signal: EntrySignal): string {
  return metadataValue(signal, 'currentAction');
}

export function entryTriggerAction(signal: EntrySignal): string {
  return metadataValue(signal, 'triggerAction');
}

export function entryPlanMode(signal: EntrySignal): string {
  return metadataValue(signal, 'planMode');
}

export function entryDisplayState(signal: EntrySignal): string {
  const lifecycleState = signal.effectiveStatus || signal.status;
  if (!['active', 'proposed', 'needs_review'].includes(lifecycleState)) return lifecycleState;
  const planMode = entryPlanMode(signal);
  const executionStatus = metadataValue(signal, 'executionStatus');
  if (executionStatus === 'waiting_confirmation' || planMode === 'waiting_confirmation') return 'waiting_confirmation';
  if (planMode === 'risk_blocked') return 'risk_blocked';
  if (planMode === 'observation_only' || entryCurrentAction(signal) === 'reject') return 'observation_only';
  if (entryCurrentAction(signal) === 'wait') {
    return 'waiting_trigger';
  }
  return lifecycleState;
}
