import { useEffect, useState } from 'react';
import { backtestApi } from '../api/backtest';
import type { BacktestRunRecord, DailyReturnEvidenceItem, MarketSnapshotRecord } from '../types/backtest';

/**
 * 快照质量等级的用户可见文案。措辞与服务端 ``assess_data_quality`` 的判定保持一致：
 * 来源或复权未知 → unknown（不可用于任何收益口径计算）；仅币种/单位未知或覆盖不足 →
 * partial（可研究但需标注）；全部已知且首尾覆盖完整 → verified。
 */
const qualityLabels: Record<string, string> = {
  verified: '已验证（来源、复权、币种、单位已知且首尾覆盖完整）',
  partial: '部分合格（币种或单位未知、或覆盖不足，仅可用于研究）',
  unknown: '未知（来源或复权口径未知，不可用于收益口径计算）',
};

/** 引擎类型文案：报告评估与策略引擎不能共用同一套结论文案。 */
const engineLabels: Record<string, string> = {
  ai_report_evaluation: 'AI 报告事后评估',
};

/** 覆盖判定依据文案：不让「只比对了首尾日期」看起来像「已核对完整」。 */
const coverageBasisLabels: Record<string, string> = {
  trading_calendar: '覆盖依据：已按交易日历核对区间 session 数',
  endpoints_only: '覆盖依据：仅比对首尾日期（日历不可用，区间内 session 数未核对）',
};

function coverageBasisText(coverage: {
  basis?: string;
  expected_sessions?: number | null;
  deficit?: number | null;
}) {
  const label = coverageBasisLabels[coverage.basis ?? ''] ?? '覆盖依据：未知';
  if (coverage.expected_sessions == null) return `${label}。`;
  return `${label}：应有 ${coverage.expected_sessions} 个 session，缺口 ${coverage.deficit ?? 0} 个。`;
}

function eligibilityText(inputEligibility?: boolean | null) {
  if (inputEligibility === true) return '满足默认策略收益计算的输入资格。';
  if (inputEligibility === false) return '不满足默认策略收益计算的输入资格，不能作为策略收益依据。';
  return '本次运行未记录输入资格。';
}

const statuses: Record<string, string> = {
  running: '运行中或曾中断，尚无完成证据', completed: '评估完成',
  partial: '存在数据不足或错误', failed: '执行失败', empty: '没有符合条件的分析记录',
};

/**
 * 冻结快照引用区块（WP4）。
 *
 * 只陈述服务端可核对的字段，不在前端把「有快照」等同于「数据可信」：
 * 复权 / 币种 / 单位与覆盖都以快照元数据为准，取不到元数据时如实说明尚未核对。
 */
function FrozenSnapshotBlock({ record, detail, detailFailed }: {
  record: BacktestRunRecord;
  detail?: MarketSnapshotRecord | null;
  detailFailed?: boolean;
}) {
  const quality = qualityLabels[record.data_quality_status ?? '']
    ?? `未评估（${record.data_quality_status ?? '无记录'}）`;
  return <div className="space-y-1" aria-label="冻结行情快照引用">
    <p className="break-all">冻结行情快照：{record.snapshot_id}</p>
    <p>数据质量：{quality}</p>
    <p>{eligibilityText(record.input_eligibility)}</p>
    <p>引擎：{record.engine_kind ?? '未记录'} · 版本 {record.engine_version ?? '未记录'}</p>
    {detail && <p>口径：来源 {detail.source ?? '未知'} · 复权 {detail.price_adjustment ?? '未知'} · 币种 {detail.currency ?? '未知'} · 单位 {detail.volume_unit ?? '未知'}</p>}
    {detail && <p>冻结区间：{detail.resolved_start ?? '未知'} 至 {detail.resolved_end ?? '未知'} · 条数 {detail.rows ?? '未知'}{detail.coverage_complete === false ? ' · 首尾未覆盖完整请求区间' : ''}</p>}
    {detail?.quality?.missing?.length ? <p>缺失口径项：{detail.quality.missing.join('、')}</p> : null}
    {detail?.quality?.coverage ? <p>{coverageBasisText(detail.quality.coverage)}</p> : null}
    {detail && <p className="break-all">内容哈希：{detail.payload_hash ?? '未记录'}（仅用于内容核对，不证明数据正确）</p>}
    {!detail && detailFailed && <p>快照元数据暂不可用；该快照的复权、币种与单位口径仍未核对。</p>}
    {!detail && !detailFailed && <p role="status">正在核对冻结快照元数据…</p>}
  </div>;
}

/**
 * daily_return 引擎的独立证据渲染：只陈列确定性数值（总回报/年化/逐日观测），
 * 不复用报告条目的 `eval_status`/`forward_bars` 结构，避免读到 undefined 属性。
 */
function DailyReturnEvidence({ evidence }: { evidence: NonNullable<BacktestRunRecord['evidence']> }) {
  const raw = evidence as unknown as { metrics?: Record<string, unknown>; items?: DailyReturnEvidenceItem[] };
  const metrics = raw.metrics;
  const items = raw.items ?? [];
  return <div className="space-y-1">
    {metrics && <pre className="overflow-auto py-2">{JSON.stringify(metrics, null, 2)}</pre>}
    {items.length
      ? <ul>{items.map((item, index) => <li key={item.date ?? index}>
          {item.date ?? '无日期'} · 收盘 {item.close ?? '未知'} · 日收益 {item.simple_return == null ? '未知' : `${(item.simple_return * 100).toFixed(4)}%`}
        </li>)}</ul>
      : <p>没有可展示的收益观测。</p>}
  </div>;
}

export function BacktestRunCard({ runId }: { runId: string }) {
  const [state, setState] = useState<{
    id: string; record?: BacktestRunRecord; snapshot?: MarketSnapshotRecord | null;
    snapshotError?: boolean; error?: boolean;
  }>({ id: runId });
  useEffect(() => {
    let active = true;
    backtestApi.getRun(runId).then(async record => {
      if (!active) return;
      setState({ id: runId, record });
      // 快照元数据只做补充核对：取不到也不影响运行记录本身的展示。
      if (!record.snapshot_id) return;
      try {
        const snapshot = await backtestApi.getMarketSnapshot(record.snapshot_id);
        if (active) setState({ id: runId, record, snapshot });
      } catch {
        if (active) setState({ id: runId, record, snapshotError: true });
      }
    }).catch(() => { if (active) setState({ id: runId, error: true }); });
    return () => { active = false; };
  }, [runId]);
  const record = state.id === runId ? state.record : undefined;
  const snapshot = state.id === runId ? state.snapshot : undefined;
  const snapshotFailed = state.id === runId && Boolean(state.snapshotError);
  if (state.id === runId && state.error) return <p role="alert">无法读取运行记录 {runId}，未取得程序证据。</p>;
  if (!record) return <p role="status">正在查询程序运行记录…</p>;
  const evidence = record.evidence;
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(record, null, 2)], { type: 'application/json' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `backtest-${record.run_id}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const isReportEvaluation = !record.engine_kind || record.engine_kind === 'ai_report_evaluation';
  return <section className="my-3 rounded-xl border border-white/10 p-4 text-sm space-y-2" aria-label="程序运行证据">
    <strong>程序运行记录 · {engineLabels[record.engine_kind ?? ''] ?? record.engine_kind ?? '未记录引擎'}</strong>
    <p>{statuses[record.status] ?? '未知状态'} · {record.created_at}</p>
    <p className="break-all">编号：{record.run_id}</p>
    {isReportEvaluation
      ? <p>这是单条报告评估，不是组合净值或三年策略收益；未建模资金、手续费和滑点。</p>
      : <p>这不是单条报告评估引擎的运行记录；卡片只陈列冻结输入与执行证据，不把其中的数字当作组合净值。</p>}
    {record.snapshot_id
      ? <p>快照覆盖判定只看首尾日期，数据条数不代表交易日连续，也未验证历史时点可得性。</p>
      : <p>复权口径未知；数据条数不代表交易日连续，也未验证历史时点可得性。</p>}
    {record.snapshot_id
      ? <FrozenSnapshotBlock record={record} detail={snapshot} detailFailed={snapshotFailed} />
      : <p>本运行未关联冻结行情快照，复权口径与输入资格无法核对。</p>}
    {evidence?.counts && <p>处理 {evidence.counts.processed} · 写入 {evidence.counts.saved} · 完成 {evidence.counts.completed} · 数据不足 {evidence.counts.insufficient} · 错误 {evidence.counts.errors}</p>}
    <details><summary className="cursor-pointer">参数、数据覆盖与原始证据</summary>
      <pre className="overflow-auto py-2">{JSON.stringify(evidence?.parameters, null, 2)}</pre>
      {isReportEvaluation
        ? <ul>{(evidence?.items ?? []).map(item => <li key={item.analysis_history_id}>
              {item.code} #{item.analysis_history_id} · {item.result.eval_status} · 后续行情 {item.forward_bars.length}/{item.requested_forward_bars} 条
              · {item.start_bar?.date ?? '无起始行情'} 至 {item.forward_bars.at(-1)?.date ?? '无后续行情'}
              {item.start_date_matches_request === false && ' · 起始行情早于分析日，需检查是否为正常休市或陈旧数据'}
              · 来源：{[...new Set([item.start_bar?.data_source, ...item.forward_bars.map(bar => bar.data_source)].map(source => source || '未知'))].join(', ')}
            </li>)}</ul>
        : record.engine_kind === 'daily_return' && evidence
          ? <DailyReturnEvidence evidence={evidence} />
          : <pre className="overflow-auto">{JSON.stringify(evidence?.items ?? [], null, 2)}</pre>}
      <p className="break-all">SHA256：{record.sha256 ?? '未完成，尚无最终哈希'}</p>
      <p>哈希用于内容核对，不证明数据或模型判断正确。</p>
      <pre className="max-h-80 overflow-auto">{JSON.stringify(evidence, null, 2)}</pre>
    </details>
    <button type="button" className="underline" onClick={download}>下载运行证据 JSON</button>
  </section>;
}

export function ChatBacktestEvidence({ content }: { content: string }) {
  const ids = [...new Set([...content.matchAll(/\/backtest\?run=([a-f0-9]{32})(?![a-f0-9])/g)].map(match => match[1]))].slice(0, 3);
  return <div><p className="text-xs opacity-70">{ids.length
    ? '正文为 AI 解读；下方记录仅核验对应程序执行，不核验整段回答。'
    : '正文为 AI 解读，未关联可核验的回测运行记录。'}</p>
    {ids.map(id => <BacktestRunCard key={id} runId={id} />)}
  </div>;
}

export function RecentBacktestRuns() {
  const [runs, setRuns] = useState<BacktestRunRecord[]>([]);
  const [error, setError] = useState(false);
  useEffect(() => {
    let active = true;
    async function load() {
      try { const rows = await backtestApi.getRuns(); if (active) setRuns(rows); }
      catch { if (active) setError(true); }
    }
    void load();
    return () => { active = false; };
  }, []);
  return <details className="my-3 text-sm"><summary>最近的程序运行记录</summary>
    {error ? <p>运行列表暂不可用。</p> : runs.length ? runs.map(run => <p key={run.run_id}>
      <a className="underline" href={`/backtest?run=${run.run_id}`}>{run.created_at} · {statuses[run.status] ?? run.status}</a>
    </p>) : <p>暂无运行记录。</p>}
  </details>;
}
