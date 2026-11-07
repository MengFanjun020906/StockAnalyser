import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, RefreshCw, Search } from 'lucide-react';
import { Link } from 'react-router-dom';
import { jevApi, type JevAnswer, type JevEvaluation, type JevStatus } from '../api/jev';
import { portfolioApi } from '../api/portfolio';
import { getParsedApiError, type ParsedApiError } from '../api/error';
import type { PortfolioAccountItem } from '../types/portfolio';
import { ApiErrorAlert, AppPage, Badge, Button, Input } from '../components/common';

const LABELS: Record<string, string> = {
  bullish: '偏多', neutral: '震荡', bearish: '偏空', insufficient: '证据不足',
  normal: '常规', elevated: '风险升高', high: '高风险', support: '条件支持', wait: '等待', avoid: '回避',
  ok: '已完成', error: '失败', disabled: '未启用', unavailable: '不可用', insufficient_data: '日线不足或过期',
  pending: '待观察', observed: '已观察', missing_data: '缺少后续行情', price_basis_changed: '价格口径变化',
  daily_snapshot_unavailable: '日线快照不可用', not_configured: 'Jev 未启用或密钥未配置',
  insufficient_history: '日线少于 30 根', stale_daily_bars: '缺少最近完整交易日日线',
  duplicate_daily_bars: '日线日期重复', invalid_daily_bars: '日线价格或成交量无效',
  timeout: '模型请求超时', connection_error: '模型连接失败', invalid_response: '模型响应格式无效',
  http_401: 'API 密钥无效', http_429: 'API 调用限流', http_529: '模型服务繁忙',
  above_rising_averages: '均线上方', below_falling_averages: '均线下方', mixed: '趋势分歧',
  close_above_ma5_above_ma20: '收盘价 > MA5 > MA20', close_below_ma5_below_ma20: '收盘价 < MA5 < MA20',
  fundamentals: '基本面', news: '消息面', capital_flow: '资金面',
  fundamental: '基本面', intel: '消息面', capital: '资金面',
};
const label = (value?: string) => value ? LABELS[value] || value : '--';
const marketLabel = (value?: string) => ({ cn: 'A 股', hk: '港股', us: '美股' }[value || ''] || value || '--');
const PROBABILITY_COLORS: Record<string, string> = {
  bullish: '#168c76', support: '#168c76', normal: '#168c76',
  neutral: '#b58521', wait: '#b58521', elevated: '#b58521',
  bearish: '#cc5353', avoid: '#cc5353', high: '#cc5353', insufficient: '#87949a',
};

function Answer({ answer }: { answer?: JevAnswer }) {
  if (!answer) return <span className="text-muted-text">不可评估</span>;
  return (
    <div className="min-w-0 space-y-1">
      <span className={['bearish', 'high', 'avoid'].includes(answer.choice) ? 'text-danger' : 'text-foreground'}>
        {label(answer.choice)}
      </span>
      <div className="text-xs tabular-nums text-secondary-text">
        选项概率 {(answer.probabilities[answer.choice] * 100).toFixed(0)}% · 置信度 {(answer.confidence * 100).toFixed(0)}%
      </div>
      <div className="flex h-1.5 w-full max-w-48 overflow-hidden rounded" aria-label="选项概率分布">
        {Object.entries(answer.probabilities).map(([key, probability]) => (
          <span key={key} title={`${label(key)} ${(probability * 100).toFixed(0)}%`}
            style={{ width: `${probability * 100}%`, backgroundColor: PROBABILITY_COLORS[key] || '#87949a' }} />
        ))}
      </div>
    </div>
  );
}

export default function JevPage() {
  const [status, setStatus] = useState<JevStatus | null>(null);
  const [items, setItems] = useState<JevEvaluation[]>([]);
  const [selected, setSelected] = useState<JevEvaluation | null>(null);
  const [stockCode, setStockCode] = useState('');
  const [accountId, setAccountId] = useState('');
  const [accounts, setAccounts] = useState<PortfolioAccountItem[]>([]);
  const [network, setNetwork] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ParsedApiError | null>(null);
  const selectionVersion = useRef(0);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [nextStatus, history, accountList] = await Promise.all([
        jevApi.status(), jevApi.list(), portfolioApi.getAccounts(),
      ]);
      setStatus(nextStatus);
      setItems(history.items);
      setAccounts(accountList.accounts);
      setError(null);
    } catch (cause) { setError(getParsedApiError(cause)); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  async function select(item: JevEvaluation) {
    if (!item.evaluation_id) return;
    const version = ++selectionVersion.current;
    setSelected(item);
    try {
      const detail = await jevApi.detail(item.evaluation_id);
      if (selectionVersion.current === version) setSelected(detail);
    } catch (cause) { if (selectionVersion.current === version) setError(getParsedApiError(cause)); }
  }

  async function evaluate() {
    setBusy(true);
    setError(null);
    ++selectionVersion.current;
    try {
      const result = await jevApi.evaluate({
        stock_code: stockCode.trim(), account_id: accountId ? Number(accountId) : undefined,
        fallback_to_network: network,
      });
      setSelected(result);
      await refresh();
      if (result.evaluation_id) await select(result);
    } catch (cause) { setError(getParsedApiError(cause)); }
    finally { setBusy(false); }
  }

  return (
    <AppPage>
      <header className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-4">
        <div className="flex items-center gap-3">
          <Activity className="h-6 w-6 text-cyan" />
          <h1 className="text-xl font-semibold">Jev 日线评估</h1>
          <Badge variant="warning">影子模式</Badge>
        </div>
        <div className="flex items-center gap-3 text-sm text-secondary-text">
          <span>{status?.model || '--'}</span>
          <span>{status?.enabled && status.configured ? '已配置' : '未启用'}</span>
          <Button variant="ghost" size="sm" title="刷新记录" aria-label="刷新记录" disabled={loading} onClick={() => void refresh()}>
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          </Button>
        </div>
      </header>
      <form className="grid grid-cols-1 items-end gap-3 border-b border-border py-4 sm:grid-cols-2 lg:grid-cols-4"
        onSubmit={(event) => { event.preventDefault(); void evaluate(); }}>
        <label className="space-y-1 text-sm">股票代码
          <Input value={stockCode} onChange={(event) => setStockCode(event.target.value)} placeholder="600519 / hk00700 / AAPL" required />
        </label>
        <label className="space-y-1 text-sm">评估账户
          <select className="h-10 w-full rounded-lg border border-border bg-card px-3" value={accountId}
            onChange={(event) => setAccountId(event.target.value)}>
            <option value="">单股观察</option>
            {accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}
          </select>
        </label>
        <label className="flex h-10 items-center gap-2 text-sm">
          <input type="checkbox" checked={network} onChange={(event) => setNetwork(event.target.checked)} />补取缺失行情
        </label>
        <Button type="submit" size="sm" className="h-10" isLoading={busy}
          disabled={!stockCode.trim() || !status?.enabled || !status.configured}>
          <Search className="h-4 w-4" />运行评估
        </Button>
      </form>
      {error && <div className="py-3"><ApiErrorAlert error={error} /></div>}
      {status && (!status.enabled || !status.configured) && <p className="py-4 text-sm text-secondary-text">
        Jev 配置未就绪。<Link to="/settings" className="text-cyan underline">模型设置</Link>
      </p>}
      {selected && <section className="border-b border-border py-5" aria-label="评估详情">
        <div className="mb-4 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-lg font-medium">{selected.stock_code || stockCode} <span className="text-sm text-secondary-text">{marketLabel(selected.market)} · {selected.as_of || '--'}</span></h2>
          <span className="text-sm text-secondary-text">{label(selected.status)}{selected.evaluation_mode === 'historical_replay' ? ' · 历史重放' : ''}{selected.cached ? ' · 已有记录' : ''}</span>
        </div>
        {selected.reason && <p className="mb-3 text-sm text-danger">{label(selected.reason)}</p>}
        <div className="overflow-x-auto">
          <table className="w-full min-w-[680px] text-left text-sm">
            <thead className="border-b border-border text-secondary-text"><tr>
              <th className="py-2 pr-4">交易日</th><th className="px-3 py-2">方向</th><th className="px-3 py-2">买点复核</th><th className="px-3 py-2">下行风险</th><th className="py-2 pl-3">后续收盘价变化</th>
            </tr></thead>
            <tbody>{[1, 3, 5].map((horizon) => {
              const outcome = selected.outcomes?.find((item) => item.horizon === horizon);
              return <tr key={horizon} className="border-b border-border/50">
                <td className="py-4 pr-4 font-medium">{horizon} 日</td>
                <td className="px-3 py-4"><Answer answer={selected.answers?.[`direction_${horizon}`]} /></td>
                <td className="px-3 py-4"><Answer answer={selected.answers?.[`entry_${horizon}`]} /></td>
                <td className="px-3 py-4"><Answer answer={selected.answers?.[`risk_${horizon}`]} /></td>
                <td className="py-4 pl-3 tabular-nums">{outcome?.price_return_pct != null ? `${outcome.price_return_pct.toFixed(2)}%` : label(outcome?.status || 'pending')}
                  <div className="text-xs text-secondary-text">{outcome?.target_date}</div></td>
              </tr>;
            })}</tbody>
          </table>
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-4">
          <div><dt className="text-secondary-text">模型</dt><dd className="break-all">{selected.model || '--'}</dd></div>
          <div><dt className="text-secondary-text">调用耗时</dt><dd>{selected.latency_ms != null ? `${selected.latency_ms} ms` : '--'}</dd></div>
          <div><dt className="text-secondary-text">输入 / 输出 Token</dt><dd>{selected.usage?.input_tokens ?? '--'} / {selected.usage?.output_tokens ?? '--'}</dd></div>
          <div><dt className="text-secondary-text">行情来源</dt><dd className="break-all">{selected.state?.history_source || '--'}</dd></div>
          <div><dt className="text-secondary-text">价格方案</dt><dd>{selected.state?.price_plan.status === 'available' ? '已匹配' : '无可复核方案'}</dd></div>
          <div><dt className="text-secondary-text">日线样本</dt><dd>{selected.state?.bar_count ?? '--'}</dd></div>
          <div><dt className="text-secondary-text">缺失证据</dt><dd>{selected.state?.missing_evidence.map(label).join('、') || '--'}</dd></div>
          <div><dt className="text-secondary-text">原分析</dt><dd>{selected.source === 'agent' && selected.source_ref
            ? <Link className="text-cyan underline" to={`/agent-trace/${encodeURIComponent(selected.source_ref)}`}>查看链路</Link> : selected.source || '--'}</dd></div>
        </dl>
      </section>}
      <section className="py-5" aria-label="评估记录">
        <h2 className="mb-3 text-base font-medium">最近评估</h2>
        {!items.length ? <p className="py-8 text-sm text-secondary-text">{loading ? '正在加载' : '暂无评估记录'}</p> : (
          <div className="overflow-x-auto"><table className="w-full min-w-[620px] text-left text-sm">
            <thead className="border-b border-border text-secondary-text"><tr>
              <th className="py-2">标的</th><th>市场</th><th>行情日期</th><th>用途</th><th>状态</th><th>5 日方向</th><th>5 日风险</th>
            </tr></thead>
            <tbody>{items.map((item) => <tr key={item.evaluation_id} className="border-b border-border/50 hover:bg-hover">
              <td className="py-3"><button className="cursor-pointer text-cyan underline" disabled={busy} onClick={() => void select(item)}>{item.stock_code}</button></td>
              <td>{marketLabel(item.market)}</td><td className="tabular-nums">{item.as_of}</td><td>{item.purpose === 'position' ? '持仓' : '入场'}</td>
              <td>{label(item.status)}</td><td>{label(item.answers?.direction_5?.choice)}</td><td>{label(item.answers?.risk_5?.choice)}</td>
            </tr>)}</tbody>
          </table></div>
        )}
      </section>
    </AppPage>
  );
}
