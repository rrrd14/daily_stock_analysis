from unittest.mock import Mock, patch
import pytest
from data_provider.polygon_fetcher import PolygonFetcher
from data_provider.base import DataFetchError


@pytest.mark.parametrize('status',['OK','DELAYED'])
def test_polygon_accepts_historical_aggregates(status):
    response=Mock()
    response.json.return_value={'status':status,'results':[{'t':1700000000000,'o':10,'h':12,'l':9,'c':11,'v':100}]}
    with patch.dict('os.environ',{'POLYGON_API_KEY':'unit-test-key'}), patch('data_provider.polygon_fetcher.requests.get',return_value=response):
        df=PolygonFetcher().get_daily_data('AAPL','2023-09-13','2026-09-13')
    assert len(df)==1 and df.iloc[0]['close']==11
    assert df['date'].notna().all()
    assert df.index.name != 'date'


def test_polygon_rejects_real_api_error():
    response=Mock()
    response.json.return_value={'status':'ERROR','error':'subscription missing'}
    with patch.dict('os.environ',{'POLYGON_API_KEY':'unit-test-key'}), patch('data_provider.polygon_fetcher.requests.get',return_value=response):
        with pytest.raises(DataFetchError,match='ERROR'):
            PolygonFetcher()._fetch_raw_data('AAPL','2023-09-13','2026-09-13')
