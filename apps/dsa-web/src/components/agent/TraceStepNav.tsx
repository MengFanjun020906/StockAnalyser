import React from 'react';
import { AlertTriangle, Check, Circle, Loader2 } from 'lucide-react';
import { cn } from '../../utils/cn';

export type TraceStepStatus = 'pending' | 'active' | 'done' | 'error';

export type TraceNavStep = {
  key: string;
  label: string;
  title: string;
  status: TraceStepStatus;
  meta?: string;
};

type TraceStepNavProps = {
  steps: TraceNavStep[];
  activeKey: string;
  onSelect: (key: string) => void;
};

const StatusIcon: React.FC<{ status: TraceStepStatus }> = ({ status }) => {
  if (status === 'done') return <Check className="h-3 w-3" />;
  if (status === 'active') return <Loader2 className="h-3 w-3 animate-spin" />;
  if (status === 'error') return <AlertTriangle className="h-3 w-3" />;
  return <Circle className="h-2 w-2" />;
};

const statusClass = (status: TraceStepStatus): string => {
  if (status === 'done') return 'border-success/25 bg-success/10 text-success';
  if (status === 'active') return 'border-cyan/25 bg-cyan/10 text-cyan';
  if (status === 'error') return 'border-danger/25 bg-danger/10 text-danger';
  return 'border-border/60 bg-surface-2 text-muted-text';
};

export const TraceStepNav: React.FC<TraceStepNavProps> = ({ steps, activeKey, onSelect }) => (
  <nav className="overflow-y-auto rounded-xl border border-border/60 bg-card/80 p-2 shadow-sm shadow-black/10 xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)]" aria-label="Trace 步骤导航">
    <div className="flex items-center justify-between px-2 py-1.5">
      <span className="text-[10px] uppercase tracking-[0.14em] text-muted-text">运行步骤</span>
      <span className="font-mono text-[10px] text-muted-text">{steps.length}</span>
    </div>
    <div className="space-y-0.5">
      {steps.map((step) => {
        const isActive = activeKey === step.key;
        return (
          <button
            key={step.key}
            type="button"
            onClick={() => onSelect(step.key)}
            className={cn(
              'flex w-full items-start gap-2 rounded-lg border px-2 py-2 text-left transition',
              isActive
                ? 'border-cyan/25 bg-cyan/8'
                : 'border-transparent hover:border-border/50 hover:bg-surface-2/50',
            )}
            aria-current={isActive ? 'step' : undefined}
          >
            <span className={cn('mt-0.5 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border', statusClass(step.status))}>
              <StatusIcon status={step.status} />
            </span>
            <span className="min-w-0 flex-1">
              <span className="flex items-center gap-1.5">
                <span className={cn('font-mono text-[9px] uppercase tracking-[0.08em]', isActive ? 'text-cyan' : 'text-muted-text')}>{step.label}</span>
                {step.meta ? <span className="truncate text-[9px] text-muted-text">{step.meta}</span> : null}
              </span>
              <span className={cn('mt-0.5 block truncate text-[11px]', isActive ? 'text-foreground' : 'text-secondary-text')}>{step.title}</span>
            </span>
          </button>
        );
      })}
    </div>
  </nav>
);
