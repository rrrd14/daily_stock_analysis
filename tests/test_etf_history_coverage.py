import pandas as pd
from types import SimpleNamespace
from unittest.mock import Mock
from data_provider.base import DataFetcherManager


def fetcher(name, priority, start, rows=3):
    return SimpleNamespace(name=name,priority=priority,get_daily_data=Mock(return_value=pd.DataFrame({'date':pd.date_range(start,periods=rows),'close':[1.0]*rows})))


def test_long_etf_prefers_specialized_akshare():
    bao=fetcher('BaostockFetcher',0,'2026-01-05')
    ak=fetcher('AkshareFetcher',1,'2023-09-13')
    _,source=DataFetcherManager([bao,ak]).get_daily_data('588000',start_date='2023-09-13',end_date='2026-09-13')
    assert source=='AkshareFetcher'
    bao.get_daily_data.assert_not_called()


def test_partial_etf_history_continues_to_complete_source():
    ak=fetcher('AkshareFetcher',0,'2026-01-05')
    bao=fetcher('BaostockFetcher',1,'2023-09-13')
    _,source=DataFetcherManager([ak,bao]).get_daily_data('588000',start_date='2023-09-13',end_date='2026-09-13')
    assert source=='BaostockFetcher'


def test_partial_etf_history_retained_if_all_sources_short():
    ak=fetcher('AkshareFetcher',0,'2026-01-05',4)
    bao=fetcher('BaostockFetcher',1,'2026-02-05',3)
    df,source=DataFetcherManager([ak,bao]).get_daily_data('588000',start_date='2023-09-13',end_date='2026-09-13')
    assert source=='AkshareFetcher' and len(df)==4


def test_short_etf_and_stock_priority_unchanged():
    for code,start in [('588000','2026-08-01'),('601398','2023-09-13')]:
        bao=fetcher('BaostockFetcher',0,start)
        ak=fetcher('AkshareFetcher',1,start)
        _,source=DataFetcherManager([bao,ak]).get_daily_data(code,start_date=start,end_date='2026-09-13')
        assert source=='BaostockFetcher'
        ak.get_daily_data.assert_not_called()


def test_explicit_source_is_exclusive_and_reports_attempts():
    ak = fetcher('AkshareFetcher', 1, '2023-09-13', 10)
    bao = fetcher('BaostockFetcher', 0, '2026-01-05', 3)
    attempts = []
    _, source = DataFetcherManager([ak, bao]).get_daily_data(
        '588000', source='BaostockFetcher', min_records=750, diagnostics=attempts)
    assert source == 'BaostockFetcher'
    ak.get_daily_data.assert_not_called()
    assert attempts == [{'source': 'BaostockFetcher', 'status': 'insufficient_data', 'records': 3}]


def test_row_shortage_falls_back_even_with_early_start():
    ak = fetcher('AkshareFetcher', 0, '2023-09-13', 3)
    bao = fetcher('BaostockFetcher', 1, '2023-09-13', 10)
    attempts = []
    df, source = DataFetcherManager([ak, bao]).get_daily_data(
        '588000', start_date='2023-09-13', end_date='2026-09-13', min_records=10, diagnostics=attempts)
    assert source == 'BaostockFetcher' and len(df) == 10
    assert attempts[0]['status'] == 'insufficient_data'


def test_duplicate_dates_do_not_satisfy_count():
    ak = fetcher('AkshareFetcher', 0, '2023-09-13', 1)
    ak.get_daily_data.return_value = pd.DataFrame({'date': ['2023-09-13'] * 10, 'close': [1] * 10})
    bao = fetcher('BaostockFetcher', 1, '2023-09-13', 10)
    _, source = DataFetcherManager([ak, bao]).get_daily_data('588000', min_records=10)
    assert source == 'BaostockFetcher'


def test_failed_provider_diagnostics_do_not_include_exception_secrets():
    ak = fetcher('AkshareFetcher', 0, '2023-09-13')
    ak.get_daily_data.side_effect = RuntimeError('private-token')
    yf = fetcher('YfinanceFetcher', 1, '2023-09-13', 10)
    attempts = []
    _, source = DataFetcherManager([ak, yf]).get_daily_data('588000', min_records=10, diagnostics=attempts)
    assert source == 'YfinanceFetcher'
    assert attempts[0] == {'source': 'AkshareFetcher', 'status': 'error', 'error_type': 'RuntimeError'}


def test_explicit_source_bypasses_cache():
    from unittest.mock import patch
    from src.services.history_loader import load_history_df
    manager = SimpleNamespace(get_daily_data=Mock(return_value=(
        pd.DataFrame({'date': ['2026-09-11'], 'close': [1.0]}), 'YfinanceFetcher')))
    with patch('src.storage.get_db') as db, patch('src.services.history_loader._get_fetcher_manager', return_value=manager):
        _, source = load_history_df('588000', days=750, source='YfinanceFetcher')
    db.assert_not_called()
    assert source == 'YfinanceFetcher'
    assert manager.get_daily_data.call_args.kwargs['source'] == 'YfinanceFetcher'
