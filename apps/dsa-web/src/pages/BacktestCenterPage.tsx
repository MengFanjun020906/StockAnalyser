import { useState } from 'react';
import type { ComponentType } from 'react';
import { BarChart3, FlaskConical } from 'lucide-react';
import BacktestPage from './BacktestPage';
import BacktestRunsPage from './BacktestRunsPage';
import { AppPage } from '../components/common';
import { cn } from '../utils/cn';

type CenterTab = 'runs' | 'diagnostics';

const TABS: Array<{ key: CenterTab; label: string; description: string; icon: ComponentType<{ className?: string }> }> = [
  { key: 'runs', label: '组合实验', description: '资金账户、净值、订单与成交审计', icon: FlaskConical },
  { key: 'diagnostics', label: '历史分析诊断', description: '保留原有固定窗口后验评估', icon: BarChart3 },
];

export const BacktestCenterPage = () => {
  const [tab, setTab] = useState<CenterTab>('runs');
  const current = TABS.find((item) => item.key === tab) ?? TABS[0];
  return (
    <AppPage className="space-y-4">
      <div className="glass-panel-lg px-4 py-3">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div>
            <span className="label-uppercase">Backtest</span>
            <h1 className="mt-1 text-xl font-semibold text-foreground">回测中心</h1>
            <p className="mt-1 text-sm text-secondary-text">{current.description}</p>
          </div>
          <div className="flex rounded-xl border border-border/60 bg-elevated/60 p-1">
            {TABS.map(({ key, label, icon: Icon }) => (
              <button
                key={key}
                type="button"
                onClick={() => setTab(key)}
                className={cn(
                  'flex items-center gap-2 rounded-lg px-3 py-2 text-sm transition',
                  tab === key ? 'bg-cyan/10 font-medium text-cyan' : 'text-secondary-text hover:bg-hover',
                )}
              >
                <Icon className="h-4 w-4" />
                {label}
              </button>
            ))}
          </div>
        </div>
      </div>
      {tab === 'runs' ? <BacktestRunsPage /> : <BacktestPage />}
    </AppPage>
  );
};

export default BacktestCenterPage;
