"""Legacy options and gateway transport contract shared by both client modes."""
import json

import httpx
import pytest

from openviking_cli.client._http_compat import AsyncHTTPClient, SyncHTTPClient


@pytest.mark.parametrize('sync', [False, True])
def test_legacy_search_accepts_keywords_and_preserves_explicit_options(sync):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'ok', 'result': {}})

    c = SyncHTTPClient(url='http://test') if sync else AsyncHTTPClient(url='http://test')
    async_client = c._async_client if sync else c
    async_client._http = httpx.AsyncClient(base_url='http://test', transport=httpx.MockTransport(handler))
    from openviking_sdk._utils import run_async
    try:
        if sync:
            c.find('query', context_type='resource', tags=['manual'])
            c.search('query', options={'telemetry': True}, context_type=['resource'])
        else:
            run_async(c.find('query', context_type='resource', tags=['manual']))
            run_async(c.search('query', options={'telemetry': True}, context_type=['resource']))
    finally:
        c.close() if sync else run_async(c.close())
    assert requests[0]['context_type'] == 'resource'
    assert requests[0]['tags'] == ['manual']
    assert requests[0]['telemetry'] is False
    assert requests[1]['telemetry'] is True
    assert requests[1]['context_type'] == ['resource']


async def test_legacy_search_rejects_unknown_and_duplicate_options():
    c = AsyncHTTPClient(url='http://test')
    with pytest.raises(TypeError, match='Unknown option'):
        await c.find('query', made_up=True)
    with pytest.raises(ValueError, match='specified twice'):
        await c.search('query', options={'context_type': 'skill'}, context_type='resource')


@pytest.mark.parametrize('url,explicit,expected', [
    ('http://configured', None, ['gateway-secret']),
    ('http://different', None, []),
    ('http://configured', 'manual-secret', ['manual-secret']),
    ('http://different', 'manual-secret', ['manual-secret']),
])
async def test_legacy_gateway_challenge_respects_url_and_explicit_header(tmp_path, monkeypatch, url, explicit, expected):
    config = tmp_path / 'ovcli.conf'
    config.write_text(json.dumps({'url': 'http://configured', 'gateway_token': 'gateway-secret'}))
    monkeypatch.setenv('OPENVIKING_CLI_CONFIG_FILE', str(config))
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1 and explicit is None:
            return httpx.Response(401, headers={'X-VikingBot-Gateway': 'true'}, json={'detail': 'challenge'})
        return httpx.Response(200, json={'status': 'ok'})

    c = AsyncHTTPClient(url=url, extra_headers={'x-gateway-token': explicit} if explicit else {})
    await c.initialize()
    await c._http.aclose()
    headers = {'x-gateway-token': explicit} if explicit else {}
    c._http = httpx.AsyncClient(base_url=url, headers=headers, transport=httpx.MockTransport(handler))
    try:
        response = await c._request('GET', '/health')
        assert response.status_code == (401 if url == 'http://different' and explicit is None else 200)
    finally:
        await c.close()
    assert [r.headers['X-Gateway-Token'] for r in requests if 'X-Gateway-Token' in r.headers] == expected
