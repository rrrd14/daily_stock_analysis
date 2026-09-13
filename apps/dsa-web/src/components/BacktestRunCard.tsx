import { useEffect, useState } from 'react';
import { backtestApi } from '../api/backtest';
import type { BacktestRunRecord } from '../types/backtest';

const statuses: Record<string, string> = {
  running: '运行中或曾中断，尚无完成证据', completed: '评估完成',
  partial: '存在数据不足或错误', failed: '执行失败', empty: '没有符合条件的分析记录',
};

export function BacktestRunCard({ runId }: { runId: string }) {
  const [state, setState] = useState<{ id: string; record?: BacktestRunRecord; error?: boolean }>({ id: runId });
  useEffect(() => {
    let active = true;
    backtestApi.getRun(runId).then(record => {
      if (active) setState({ id: runId, record });
    }).catch(() => { if (active) setState({ id: runId, error: true }); });
    return () => { active = false; };
  }, [runId]);
  const record = state.id === runId ? state.record : undefined;
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
  return <section className="my-3 rounded-xl border border-white/10 p-4 text-sm space-y-2" aria-label="程序运行证据">
    <strong>程序运行记录 · AI 报告事后评估</strong>
    <p>{statuses[record.status] ?? '未知状态'} · {record.created_at}</p>
    <p className="break-all">编号：{record.run_id}</p>
    <p>这是单条报告评估，不是组合净值或三年策略收益；未建模资金、手续费和滑点。</p>
    <p>复权口径未知；数据条数不代表交易日连续，也未验证历史时点可得性。</p>
    {evidence?.counts && <p>处理 {evidence.counts.processed} · 写入 {evidence.counts.saved} · 完成 {evidence.counts.completed} · 数据不足 {evidence.counts.insufficient} · 错误 {evidence.counts.errors}</p>}
    <details><summary className="cursor-pointer">参数、数据覆盖与原始证据</summary>
      <pre className="overflow-auto py-2">{JSON.stringify(evidence?.parameters, null, 2)}</pre>
      <ul>{evidence?.items.map(item => <li key={item.analysis_history_id}>
        {item.code} #{item.analysis_history_id} · {item.result.eval_status} · 后续行情 {item.forward_bars.length}/{item.requested_forward_bars} 条
        · {item.start_bar?.date ?? '无起始行情'} 至 {item.forward_bars.at(-1)?.date ?? '无后续行情'}
        {item.start_date_matches_request === false && ' · 起始行情早于分析日，需检查是否为正常休市或陈旧数据'}
        · 来源：{[...new Set([item.start_bar?.data_source, ...item.forward_bars.map(bar => bar.data_source)].map(source => source || '未知'))].join(', ')}
      </li>)}</ul>
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
