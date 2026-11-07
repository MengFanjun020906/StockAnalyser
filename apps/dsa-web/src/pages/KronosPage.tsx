import type React from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as echarts from 'echarts';
import { Activity, AlertTriangle, Boxes, CandlestickChart, Database, RefreshCw, ScanLine, Search, TrendingUp } from 'lucide-react';
import { kronosApi, type KronosAnalyzeResponse, type KronosPatternInstance, type KronosStatusResponse } from '../api/kronos';
import type { ParsedApiError } from '../api/error';
import { getParsedApiError } from '../api/error';
import { ApiErrorAlert, AppPage, Badge, Button, EmptyState, Input, StatCard } from '../components/common';
import { buildKronosChartOption } from './kronosChartOption';

function fmtPct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  const sign = Number(value) > 0 ? '+' : '';
  return `${sign}${Number(value).toFixed(2)}%`;
}

function fmtNum(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return Number(value).toFixed(2);
}

function confidencePct(value?: number | null): string {
  if (value == null || !Number.isFinite(Number(value))) return '--';
  return `${Math.round(Number(value) * 100)}%`;
}

function biasLabel(value?: string): string {
  if (value === 'bullish') return '偏多';
  if (value === 'bearish') return '偏空';
  if (value === 'neutral') return '中性';
  return value || '--';
}

function biasTone(value?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (value === 'bullish') return 'success';
  if (value === 'bearish') return 'danger';
  if (value === 'neutral') return 'default';
  return 'warning';
}

function patternTone(value?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (value === 'bullish') return 'success';
  if (value === 'bearish') return 'danger';
  return 'info';
}

function statusLabel(value?: string): string {
  const labels: Record<string, string> = {
    ok: '已完成',
    unavailable: '未启用',
    insufficient_data: '样本不足',
    unsupported_market: '市场不支持',
    unsupported_frequency: '周期不支持',
    error: '失败',
  };
  return labels[value || ''] || value || '--';
}

function statusTone(value?: string): React.ComponentProps<typeof Badge>['variant'] {
  if (value === 'ok') return 'success';
  if (value === 'unavailable' || value === 'insufficient_data') return 'warning';
  if (value === 'error' || value === 'unsupported_market' || value === 'unsupported_frequency') return 'danger';
  return 'default';
}

const KronosKlineChart: React.FC<{ data: KronosAnalyzeResponse | null }> = ({ data }) => {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!ref.current || !data || !data.bars.length) return undefined;
    const chart = echarts.init(ref.current);
    try {
      chart.setOption(buildKronosChartOption(data));
    } catch (error) {
      console.error('Kronos chart render failed:', error);
      chart.dispose();
      return undefined;
    }
    const resize = () => chart.resize();
    window.addEventListener('resize', resize);
    return () => {
      window.removeEventListener('resize', resize);
      chart.dispose();
    };
  }, [data]);

  if (!data || !data.bars.length) {
    return (
      <EmptyState
        title="暂无 K 线结果"
        description="运行一次 Kronos 分析后展示日线、预测线和形态红框。"
        icon={<CandlestickChart className="h-7 w-7" />}
        className="min-h-[420px]"
      />
    );
  }

  return <div ref={ref} className="h-[460px] w-full border border-border/70 bg-card/60" data-testid="kronos-kline-chart" />;
};

const PatternTable: React.FC<{ patterns: KronosPatternInstance[] }> = ({ patterns }) => {
  if (!patterns.length) {
    return (
      <EmptyState
        title="未识别到形态实例"
        description="当前样本没有形成可标注的 Kronos 形态区间。"
        icon={<ScanLine className="h-7 w-7" />}
        className="py-8"
      />
    );
  }

  return (
    <div className="overflow-x-auto border border-border/60">
      <table className="min-w-[880px] divide-y divide-border/50 text-sm">
        <thead className="bg-elevated/70 text-xs uppercase tracking-[0.08em] text-secondary-text">
          <tr>
            <th className="px-3 py-2 text-left">形态</th>
            <th className="px-3 py-2 text-left">方向</th>
            <th className="px-3 py-2 text-right">置信度</th>
            <th className="px-3 py-2 text-left">区间</th>
            <th className="px-3 py-2 text-right">价格框</th>
            <th className="px-3 py-2 text-left">摘要</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/50">
          {patterns.map((pattern) => (
            <tr key={pattern.patternId} className="hover:bg-hover/60">
              <td className="px-3 py-2 font-medium text-foreground">{pattern.label || pattern.patternType}</td>
              <td className="px-3 py-2"><Badge variant={patternTone(pattern.direction)}>{biasLabel(pattern.direction)}</Badge></td>
              <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{confidencePct(pattern.confidence)}</td>
              <td className="px-3 py-2 text-secondary-text">{pattern.startTime} 至 {pattern.endTime}</td>
              <td className="px-3 py-2 text-right tabular-nums text-secondary-text">{fmtNum(pattern.priceLow)} / {fmtNum(pattern.priceHigh)}</td>
              <td className="px-3 py-2 text-secondary-text">{pattern.summary || '--'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
};

const DataQualityPanel: React.FC<{ data: KronosAnalyzeResponse | null; status: KronosStatusResponse | null }> = ({ data, status }) => {
  const flags = data?.dataQuality?.flags || [];
  const warnings = data?.dataQuality?.warnings || [];
  return (
    <div className="border border-border/70 bg-card/60 p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold text-foreground">
        <Database className="h-4 w-4 text-cyan" />
        数据与模型状态
      </div>
      <div className="grid gap-3 text-sm md:grid-cols-2 xl:grid-cols-4">
        <div>
          <div className="text-xs text-secondary-text">模型</div>
          <div className="mt-1 text-foreground">{status?.modelName || '--'}</div>
        </div>
        <div>
          <div className="text-xs text-secondary-text">权重</div>
          <div className="mt-1 text-foreground">{status?.enabled ? '已启用' : '未启用'}</div>
        </div>
        <div>
          <div className="text-xs text-secondary-text">样本</div>
          <div className="mt-1 text-foreground">{data?.dataQuality?.barCount ?? '--'} / {data?.dataQuality?.requiredBars ?? '--'}</div>
        </div>
        <div>
          <div className="text-xs text-secondary-text">来源</div>
          <div className="mt-1 text-foreground">{data?.dataQuality?.source || '--'}</div>
        </div>
      </div>
      {flags.length || warnings.length ? (
        <div className="mt-4 flex flex-wrap gap-2">
          {flags.map((flag) => <Badge key={flag} variant="warning">{flag}</Badge>)}
          {warnings.slice(0, 2).map((warning) => <span key={warning} className="text-xs text-secondary-text">{warning}</span>)}
        </div>
      ) : null}
    </div>
  );
};

const KronosPage: React.FC = () => {
  const [stockCode, setStockCode] = useState('600519');
  const [lookback, setLookback] = useState(160);
  const [predLen, setPredLen] = useState(10);
  const [fallbackToNetwork, setFallbackToNetwork] = useState(true);
  const [status, setStatus] = useState<KronosStatusResponse | null>(null);
  const [result, setResult] = useState<KronosAnalyzeResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ParsedApiError | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await kronosApi.getStatus());
    } catch (err) {
      setError(getParsedApiError(err));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const runAnalysis = useCallback(async () => {
    const code = stockCode.trim();
    if (!code) return;
    setLoading(true);
    setError(null);
    try {
      const data = await kronosApi.analyze({
        stockCode: code,
        lookback,
        predLen,
        fallbackToNetwork,
      });
      setResult(data);
    } catch (err) {
      setError(getParsedApiError(err));
    } finally {
      setLoading(false);
    }
  }, [fallbackToNetwork, lookback, predLen, stockCode]);

  const metrics = useMemo(() => {
    const summary = result?.forecastSummary || {};
    const signal = result?.signal || {};
    return [
      { label: '预期收益', value: fmtPct(summary.expectedReturnPct), hint: `预测终值 ${fmtNum(summary.finalClose)}`, icon: <TrendingUp className="h-4 w-4" />, tone: Number(summary.expectedReturnPct) > 0 ? 'success' as const : 'danger' as const },
      { label: '方向', value: biasLabel(signal.bias), hint: `置信度 ${confidencePct(signal.confidence)}`, icon: <Activity className="h-4 w-4" />, tone: signal.bias === 'bullish' ? 'success' as const : signal.bias === 'bearish' ? 'danger' as const : 'default' as const },
      { label: '盈亏比', value: summary.riskReward != null ? Number(summary.riskReward).toFixed(2) : '--', hint: `最大上行 ${fmtPct(summary.maxUpsidePct)}`, icon: <Boxes className="h-4 w-4" />, tone: 'primary' as const },
      { label: '形态数', value: String(result?.patternInstances?.length ?? 0), hint: result?.asOf ? `截至 ${result.asOf}` : '等待分析', icon: <ScanLine className="h-4 w-4" />, tone: 'default' as const },
    ];
  }, [result]);

  return (
    <AppPage className="space-y-5">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2 text-sm text-secondary-text">
            <CandlestickChart className="h-4 w-4 text-cyan" />
            Kronos · A股日线
          </div>
          <h1 className="mt-2 text-2xl font-semibold tracking-normal text-foreground">K线形态识别</h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={status?.enabled ? 'success' : 'warning'}>{status?.enabled ? '模型已启用' : '模型未启用'}</Badge>
          <Badge variant={status?.repoAvailable ? 'success' : 'warning'}>{status?.repoAvailable ? 'Kronos仓库可用' : 'Kronos仓库未找到'}</Badge>
          <Button variant="secondary" size="sm" onClick={() => void loadStatus()}>
            <RefreshCw className="h-4 w-4" />
            刷新
          </Button>
        </div>
      </div>

      {error ? <ApiErrorAlert error={error} onDismiss={() => setError(null)} /> : null}

      <div className="border border-border/70 bg-card/60 p-4">
        <div className="grid gap-3 md:grid-cols-[minmax(160px,1fr)_120px_120px_auto_auto] md:items-end">
          <Input
            label="股票代码"
            value={stockCode}
            onChange={(event) => setStockCode(event.target.value)}
            placeholder="600519"
          />
          <Input
            label="Lookback"
            type="number"
            min={30}
            max={512}
            value={lookback}
            onChange={(event) => setLookback(Math.max(30, Number(event.target.value) || 30))}
          />
          <Input
            label="预测天数"
            type="number"
            min={1}
            max={60}
            value={predLen}
            onChange={(event) => setPredLen(Math.max(1, Number(event.target.value) || 1))}
          />
          <label className="flex h-11 items-center gap-2 border border-border/70 bg-elevated/50 px-3 text-sm text-secondary-text">
            <input
              type="checkbox"
              className="h-4 w-4 accent-primary"
              checked={fallbackToNetwork}
              onChange={(event) => setFallbackToNetwork(event.target.checked)}
            />
            网络补数
          </label>
          <Button onClick={() => void runAnalysis()} isLoading={loading} loadingText="分析中" className="h-11">
            <Search className="h-4 w-4" />
            分析
          </Button>
        </div>
      </div>

      {result ? (
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={statusTone(result.status)}>{statusLabel(result.status)}</Badge>
          {result.signal?.bias ? <Badge variant={biasTone(result.signal.bias)}>{biasLabel(result.signal.bias)}</Badge> : null}
          {result.reason ? <span className="text-sm text-secondary-text">{result.reason}</span> : null}
        </div>
      ) : null}

      {result?.status === 'unavailable' ? (
        <div className="border border-warning/30 bg-warning/10 px-4 py-3 text-sm text-warning">
          <div className="flex items-start gap-2">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div>KRONOS_ENABLED 当前为 false，页面会保留状态但不触发模型推理。</div>
          </div>
        </div>
      ) : null}

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        {metrics.map((metric) => (
          <StatCard key={metric.label} label={metric.label} value={<span className="tabular-nums">{metric.value}</span>} hint={metric.hint} icon={metric.icon} tone={metric.tone} />
        ))}
      </div>

      <KronosKlineChart data={result} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
        <div className="border border-border/70 bg-card/60 p-4">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div className="flex items-center gap-2 text-sm font-semibold text-foreground">
              <ScanLine className="h-4 w-4 text-cyan" />
              形态实例
            </div>
            <span className="text-xs text-secondary-text">Seed gate: backtest_pending</span>
          </div>
          <PatternTable patterns={result?.patternInstances ?? []} />
        </div>
        <div className="space-y-4">
          <DataQualityPanel data={result} status={status} />
          <div className="border border-border/70 bg-card/60 p-4">
            <div className="mb-3 text-sm font-semibold text-foreground">来源链</div>
            <div className="flex flex-wrap gap-2">
              {(result?.sourceChain || []).length ? (
                result?.sourceChain?.map((item) => <Badge key={item} variant="history">{item}</Badge>)
              ) : (
                <span className="text-sm text-secondary-text">--</span>
              )}
            </div>
          </div>
        </div>
      </div>
    </AppPage>
  );
};

export default KronosPage;
