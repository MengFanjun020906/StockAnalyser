import { AlertTriangle, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { JevAnswer, JevTraceReview as Review } from '../../api/jev';
import type { ParsedApiError } from '../../api/error';
import { ApiErrorAlert, Button, Collapsible, JsonViewer } from '../common';

const LABELS: Record<string, string> = {
  pending: '复核中', ok: '复核完成', error: '复核失败', disabled: '未启用', skipped: '不适用',
  not_recorded: '未记录 Jev 复核', unavailable: '复核不可用', insufficient_data: '复核证据不足', interrupted: '复核已中断',
  bullish: '偏多', neutral: '震荡', bearish: '偏空', insufficient: '证据不足',
  normal: '常规', elevated: '风险升高', high: '高风险', support: '条件支持', wait: '等待', avoid: '回避',
  open: '条件入场', add: '加仓', hold: '持有', reduce: '减仓', take_profit: '止盈', stop_loss: '止损',
  exit: '退出', reject: '不入场', monitor: '观察', manual_review: '人工复核',
  short_term: '短线', swing: '波段', medium_term: '中线', long_term: '长线',
  risk_gate: '风控最终动作', judge: 'Judge 动作',
  entry_caution: '原动作支持入场，Jev 建议等待或回避',
  downside_conflict: '入场动作与 Jev 下行判断有分歧',
  holding_risk_conflict: '持有动作与 Jev 高风险判断有分歧',
  direction_vs_reduction: 'Jev 偏多，原动作减仓或退出；需核对账户约束',
  no_direct_conflict: '未发现直接冲突', not_comparable: '仅并列展示', divergent: '存在分歧',
  different_or_unknown_horizon: '原分析周期不同或未记录，仅并列展示',
  missing_agent_action: '缺少原结构化动作，仅并列展示',
  legacy_snapshot_unverified: '历史记录未确认共享快照，仅并列展示',
  insufficient_evidence: '证据不足，无法对照', review_unavailable: '暂无可用复核结果',
  single_stock_only: '本次不属于成功的单股分析', not_configured: 'Jev 未启用或未配置',
  trace_history_unavailable: '本轮未留存可复用的日线快照', insufficient_history: '日线样本不足',
  stale_daily_bars: '日线未覆盖评估日期', review_expired: '复核超时或服务中断',
  timeout: '模型请求超时', connection_error: '模型连接失败', shadow_capture_error: '复核处理失败',
  shadow_prepare_error: '复核准备失败', review_read_error: '复核记录读取失败',
  target_symbol_mismatch: '原分析存在股票目标冲突，复核结果已隔离',
  missing_data: '缺少后续行情', price_basis_changed: '价格口径变化', observed: '已观察',
  technical: '技术面', fundamental: '基本面', intel: '消息面', capital: '资金面',
  fundamentals: '基本面', news: '消息面', capital_flow: '资金面',
};
const label = (value?: string | null) => value ? LABELS[value] || value : '--';
const jevTraceStatusLabel = (review?: Review | null) => label(review?.status || 'not_recorded');

function Answer({ value }: { value?: JevAnswer }) {
  return value ? <div>
    <span>{label(value.choice)}</span>
    <div className="mt-1 text-xs text-muted-text">选项概率 {Math.round(value.probabilities[value.choice] * 100)}%</div>
  </div> : <span className="text-muted-text">不可评估</span>;
}

export function JevTraceReview({ review, detail = false, error, onRefresh }: {
  review?: Review | null; detail?: boolean; error?: ParsedApiError; onRefresh?: () => void;
}) {
  const evaluation = review?.evaluation;
  const baseline = review?.baseline;
  const comparison = review?.comparison;
  return <section aria-label={detail ? 'Jev 复核证据' : 'Jev 与原结论对照'} className="min-w-0 space-y-3 text-sm">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{detail ? jevTraceStatusLabel(review) : 'Jev 与原结论对照'}</span>
        <span className="text-xs text-warning">影子复核</span>
        {!detail && <span className="text-xs text-secondary-text">{jevTraceStatusLabel(review)}</span>}
      </div>
      {detail && <div className="flex items-center gap-2">
        <Link to="/jev" className="text-xs text-cyan underline">评估记录</Link>
        {onRefresh && <Button variant="ghost" size="sm" title="刷新 Jev 复核" aria-label="刷新 Jev 复核" onClick={onRefresh}>
          <RefreshCw className="h-4 w-4" />
        </Button>}
      </div>}
    </div>
    {error && detail && <ApiErrorAlert error={error} />}
    {review?.reason && <p className="text-secondary-text">{label(review.reason)}</p>}
    {evaluation && <>
      <div className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-secondary-text">
        <span>{evaluation.stock_code} · {evaluation.as_of}</span>
        <span>{label(baseline?.source)}：{label(baseline?.action)}</span>
        <span>原周期：{label(baseline?.trading_horizon)}</span>
        {evaluation.evaluation_mode === 'historical_replay' && <span className="text-warning">历史重放</span>}
      </div>
      {comparison?.reason && <p className="text-secondary-text">{label(comparison.reason)}</p>}
      {!detail && evaluation.status === 'ok' && <div className="overflow-x-auto">
        <table className="w-full min-w-[610px] text-left text-xs">
          <thead className="border-b border-border text-secondary-text"><tr>
            <th className="py-2 pr-3">交易日</th><th className="p-2">方向</th><th className="p-2">买点复核</th>
            <th className="p-2">下行风险</th><th className="p-2">与原动作对照</th><th className="p-2">后续收盘价变化</th>
          </tr></thead>
          <tbody>{[1, 3, 5].map((horizon) => {
            const row = comparison?.rows.find((item) => item.horizon === horizon);
            const outcome = evaluation.outcomes?.find((item) => item.horizon === horizon);
            return <tr key={horizon} className="border-b border-border/60 align-top">
              <th className="py-3 pr-3 font-medium">{horizon} 日</th>
              <td className="p-2"><Answer value={evaluation.answers?.[`direction_${horizon}`]} /></td>
              <td className="p-2"><Answer value={evaluation.answers?.[`entry_${horizon}`]} /></td>
              <td className="p-2"><Answer value={evaluation.answers?.[`risk_${horizon}`]} /></td>
              <td className="max-w-52 p-2">
                {row?.status === 'divergent' && <AlertTriangle className="mb-1 h-4 w-4 text-warning" />}
                {row?.flags.length ? row.flags.map((flag) => <p key={flag} className="text-warning">{label(flag)}</p>) : label(row?.reason || row?.status)}
              </td>
              <td className="p-2 tabular-nums">{outcome?.price_return_pct != null ? `${outcome.price_return_pct.toFixed(2)}%` : outcome?.status === 'pending' ? '待观察' : label(outcome?.status)}
                <div className="mt-1 text-muted-text">{outcome?.target_date}</div></td>
            </tr>;
          })}</tbody>
        </table>
      </div>}
      {detail && <>
        <dl className="grid grid-cols-2 gap-3 text-xs sm:grid-cols-3">
          <div><dt className="text-muted-text">模型</dt><dd className="break-all">{evaluation.model || '--'}</dd></div>
          <div><dt className="text-muted-text">模型耗时</dt><dd>{evaluation.latency_ms == null ? '--' : `${evaluation.latency_ms} ms`}</dd></div>
          <div><dt className="text-muted-text">输入 / 输出 Token</dt><dd>{evaluation.usage?.input_tokens ?? '--'} / {evaluation.usage?.output_tokens ?? '--'}</dd></div>
          <div><dt className="text-muted-text">行情来源</dt><dd className="break-all">{evaluation.state?.history_source || '--'}</dd></div>
          <div><dt className="text-muted-text">证据快照</dt><dd>{review?.shared_snapshot ? '本轮 Trace' : '历史独立记录'}</dd></div>
          <div><dt className="text-muted-text">缺失维度</dt><dd>{evaluation.state?.missing_evidence.map(label).join('、') || '--'}</dd></div>
        </dl>
        {baseline?.summary && <div className="border-l-2 border-border pl-3"><p className="text-xs text-muted-text">原 Judge 依据</p><p className="mt-1 whitespace-pre-wrap break-words">{baseline.summary}</p></div>}
        {baseline?.reasons?.length ? <div className="border-l-2 border-warning pl-3"><p className="text-xs text-muted-text">原风控约束</p>{baseline.reasons.map((reason, index) => <p key={index} className="mt-1 break-words">{reason}</p>)}</div> : null}
        {evaluation.state?.agent_evidence && Object.entries(evaluation.state.agent_evidence).map(([dimension, items]) => (
          <div key={dimension} className="border-t border-border pt-2"><p className="text-xs text-muted-text">{label(dimension)}证据</p>
            {items.map((item, index) => <p key={index} className="mt-1 break-all text-xs">{item.tool} · {item.summary}</p>)}
          </div>
        ))}
        <Collapsible title="冻结输入与模型答案" defaultOpen={false}>
          <JsonViewer data={evaluation as unknown as Record<string, unknown>} maxHeight="360px" />
        </Collapsible>
      </>}
    </>}
  </section>;
}
