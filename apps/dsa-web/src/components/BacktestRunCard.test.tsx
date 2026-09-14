import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BacktestRunCard, ChatBacktestEvidence } from './BacktestRunCard';
import { backtestApi } from '../api/backtest';
import type { BacktestRunRecord } from '../types/backtest';

vi.mock('../api/backtest', () => ({ backtestApi: { getRun: vi.fn(), getMarketSnapshot: vi.fn() } }));
const id = 'a'.repeat(32);
describe('program execution evidence', () => {
  beforeEach(() => vi.clearAllMocks());
  it('does not treat an invented run reference as evidence', async () => {
    vi.mocked(backtestApi.getRun).mockRejectedValue(new Error('404'));
    render(<ChatBacktestEvidence content={`[结果](/backtest?run=${id}) 收益 100%`} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('未取得程序证据');
    expect(screen.queryByLabelText('程序运行证据')).not.toBeInTheDocument();
  });
  it('displays server counts and limitations for partial execution and exports evidence', async () => {
    const record = { run_id: id, status: 'partial', created_at: '2026-09-13', sha256: 'hash',
      evidence: { kind: 'ai_report_evaluation', parameters: { eval_window_days: 10 },
        counts: { processed: 1, saved: 1, completed: 0, insufficient: 1, errors: 0 }, limitations: [], items: [] } };
    vi.mocked(backtestApi.getRun).mockResolvedValue(record);
    const createUrl = vi.fn().mockReturnValue('blob:test');
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: vi.fn() }));
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    render(<BacktestRunCard runId={id} />);
    expect(await screen.findByLabelText('程序运行证据')).toHaveTextContent('存在数据不足或错误');
    expect(screen.getByLabelText('程序运行证据')).toHaveTextContent('完成 0');
    fireEvent.click(screen.getByText('参数、数据覆盖与原始证据'));
    expect(screen.getByText(/SHA256/)).toHaveTextContent('hash');
    fireEvent.click(screen.getByText('下载运行证据 JSON'));
    await waitFor(() => expect(createUrl).toHaveBeenCalledOnce());
    expect(click).toHaveBeenCalledOnce();
    click.mockRestore();
    vi.unstubAllGlobals();
  });
  it('keeps AI text without references separate from program evidence', () => {
    render(<ChatBacktestEvidence content="回测成功，收益 100%" />);
    expect(screen.getByText(/正文为 AI 解读/)).toBeInTheDocument();
    expect(backtestApi.getRun).not.toHaveBeenCalled();
  });
  it('replaces the unknown-adjustment claim with the frozen snapshot the run referenced', async () => {
    const snapshotId = 'b'.repeat(32);
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      snapshot_id: snapshotId, data_quality_status: 'verified', input_eligibility: true,
      engine_kind: 'ai_report_evaluation', engine_version: 'v1',
      evidence: { kind: 'ai_report_evaluation', parameters: {}, limitations: [], items: [] },
    });
    vi.mocked(backtestApi.getMarketSnapshot).mockResolvedValue({
      snapshot_id: snapshotId, instrument: '588000', source: 'TencentFetcher',
      price_adjustment: 'provider_default', currency: 'CNY', volume_unit: 'shares',
      resolved_start: '2026-08-01', resolved_end: '2026-09-01', rows: 22, coverage_complete: true,
      data_quality_status: 'verified', input_eligibility: true, quality: { missing: [] },
    });
    render(<BacktestRunCard runId={id} />);
    const block = await screen.findByLabelText('冻结行情快照引用');
    expect(block).toHaveTextContent(snapshotId);
    expect(block).toHaveTextContent('已验证');
    expect(block).toHaveTextContent('满足默认策略收益计算的输入资格');
    expect(screen.getByLabelText('程序运行证据')).not.toHaveTextContent('复权口径未知');
    await waitFor(() => expect(backtestApi.getMarketSnapshot).toHaveBeenCalledWith(snapshotId));
    await waitFor(() => expect(block).toHaveTextContent('provider_default'));
    expect(block).toHaveTextContent('条数 22');
  });
  it('marks an ineligible snapshot as unusable for strategy return conclusions', async () => {
    const snapshotId = 'c'.repeat(32);
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      snapshot_id: snapshotId, data_quality_status: 'partial', input_eligibility: false,
      engine_kind: 'ai_report_evaluation', engine_version: 'v1',
      evidence: { kind: 'ai_report_evaluation', parameters: {}, limitations: [], items: [] },
    });
    vi.mocked(backtestApi.getMarketSnapshot).mockResolvedValue({
      snapshot_id: snapshotId, instrument: '588000', source: 'TencentFetcher',
      price_adjustment: 'provider_default', currency: 'CNY', volume_unit: 'unknown',
      resolved_start: '2026-08-01', resolved_end: '2026-09-01', rows: 22, coverage_complete: true,
      data_quality_status: 'partial', input_eligibility: false, quality: { missing: ['volume_unit'] },
    });
    render(<BacktestRunCard runId={id} />);
    const block = await screen.findByLabelText('冻结行情快照引用');
    expect(block).toHaveTextContent('部分合格');
    expect(block).toHaveTextContent('不满足默认策略收益计算的输入资格');
    await waitFor(() => expect(block).toHaveTextContent('缺失口径项：volume_unit'));
  });
  it('states that adjustment cannot be checked when the run has no frozen snapshot', async () => {
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      engine_kind: 'ai_report_evaluation', engine_version: 'v1',
      evidence: { kind: 'ai_report_evaluation', parameters: {}, limitations: [], items: [] },
    });
    render(<BacktestRunCard runId={id} />);
    const section = await screen.findByLabelText('程序运行证据');
    expect(section).toHaveTextContent('本运行未关联冻结行情快照，复权口径与输入资格无法核对。');
    expect(section).toHaveTextContent('复权口径未知');
    expect(screen.queryByLabelText('冻结行情快照引用')).not.toBeInTheDocument();
    expect(backtestApi.getMarketSnapshot).not.toHaveBeenCalled();
  });
  it('keeps a run visible but flags unchecked adjustment when snapshot metadata is unavailable', async () => {
    const snapshotId = 'd'.repeat(32);
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      snapshot_id: snapshotId, data_quality_status: 'verified', input_eligibility: true,
      engine_kind: 'ai_report_evaluation', engine_version: 'v1',
      evidence: { kind: 'ai_report_evaluation', parameters: {}, limitations: [], items: [] },
    });
    vi.mocked(backtestApi.getMarketSnapshot).mockRejectedValue(new Error('500'));
    render(<BacktestRunCard runId={id} />);
    const block = await screen.findByLabelText('冻结行情快照引用');
    expect(await screen.findByText(/快照元数据暂不可用/)).toBeInTheDocument();
    expect(block).toHaveTextContent(snapshotId);
    expect(block).not.toHaveTextContent('口径：来源');
  });
  it('renders daily_return evidence with a dedicated renderer', async () => {
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      engine_kind: 'daily_return', engine_version: 'v1',
      evidence: {
        kind: 'daily_return', parameters: {}, limitations: [],
        metrics: { bars: 2, observations: 1, total_return: 0.1, annualized_return: null },
        items: [{ date: '2024-01-05', close: 110, simple_return: 0.1 }],
      },
    } as unknown as BacktestRunRecord);
    render(<BacktestRunCard runId={id} />);
    const section = await screen.findByLabelText('程序运行证据');
    expect(section).toHaveTextContent('日收益 10.0000%');
    expect(section).toHaveTextContent('收盘 110');
    expect(section).not.toHaveTextContent('这是单条报告评估');
    expect(backtestApi.getMarketSnapshot).not.toHaveBeenCalled();
  });
  it('does not label a non-report engine as a report evaluation', async () => {
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      engine_kind: 'portfolio_daily', engine_version: 'v1',
      evidence: { kind: 'portfolio_daily', parameters: {}, limitations: [], items: [] },
    });
    render(<BacktestRunCard runId={id} />);
    const section = await screen.findByLabelText('程序运行证据');
    expect(section).toHaveTextContent('程序运行记录 · portfolio_daily');
    expect(section).not.toHaveTextContent('这是单条报告评估');
    expect(backtestApi.getMarketSnapshot).not.toHaveBeenCalled();
  });

});

  it('states that coverage was checked against the trading calendar', async () => {
    const snapshotId = 'e'.repeat(32);
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      snapshot_id: snapshotId, data_quality_status: 'verified', input_eligibility: true,
      engine_kind: 'ai_report_evaluation', engine_version: 'v1',
      evidence: { kind: 'ai_report_evaluation', parameters: {}, limitations: [], items: [] },
    });
    vi.mocked(backtestApi.getMarketSnapshot).mockResolvedValue({
      snapshot_id: snapshotId, instrument: '588000', source: 'TencentFetcher',
      price_adjustment: 'provider_default', currency: 'CNY', volume_unit: 'shares',
      resolved_start: '2026-08-01', resolved_end: '2026-09-01', rows: 21, coverage_complete: true,
      data_quality_status: 'verified', input_eligibility: true,
      quality: { missing: [], coverage: { basis: 'trading_calendar', expected_sessions: 22, rows: 21, deficit: 1 } },
    });
    render(<BacktestRunCard runId={id} />);
    const block = await screen.findByLabelText('冻结行情快照引用');
    await waitFor(() => expect(block).toHaveTextContent('已按交易日历核对区间 session 数'));
    expect(block).toHaveTextContent('应有 22 个 session，缺口 1 个');
  });
  it('does not imply calendar verification when only endpoints were compared', async () => {
    const snapshotId = 'f'.repeat(32);
    vi.mocked(backtestApi.getRun).mockResolvedValue({
      run_id: id, status: 'completed', created_at: '2026-09-13', sha256: 'hash',
      snapshot_id: snapshotId, data_quality_status: 'verified', input_eligibility: true,
      engine_kind: 'ai_report_evaluation', engine_version: 'v1',
      evidence: { kind: 'ai_report_evaluation', parameters: {}, limitations: [], items: [] },
    });
    vi.mocked(backtestApi.getMarketSnapshot).mockResolvedValue({
      snapshot_id: snapshotId, instrument: '588000', source: 'TencentFetcher',
      price_adjustment: 'provider_default', currency: 'CNY', volume_unit: 'shares',
      resolved_start: '2026-08-01', resolved_end: '2026-09-01', rows: 5, coverage_complete: true,
      data_quality_status: 'verified', input_eligibility: true,
      quality: { missing: [], coverage: { basis: 'endpoints_only', expected_sessions: null, rows: 5, deficit: null } },
    });
    render(<BacktestRunCard runId={id} />);
    const block = await screen.findByLabelText('冻结行情快照引用');
    await waitFor(() => expect(block).toHaveTextContent('仅比对首尾日期'));
    expect(block).toHaveTextContent('区间内 session 数未核对');
    expect(block).not.toHaveTextContent('已按交易日历核对');
  });
