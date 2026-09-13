import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BacktestRunCard, ChatBacktestEvidence } from './BacktestRunCard';
import { backtestApi } from '../api/backtest';

vi.mock('../api/backtest', () => ({ backtestApi: { getRun: vi.fn() } }));
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
});
