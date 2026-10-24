import asyncio
from contextvars import ContextVar

import httpx
import pytest
from fastapi import FastAPI
from nemo_gym.episode_types import EpisodeId

from environment_servers.nanoverse_sdk.context import RequestContextIsolation
from responses_api_models.nanoverse_sdk_openai.app import NativeSDKIngress, parse_capture_key


@pytest.mark.parametrize('key', ('alpha', 'alpha-a1', 'alpha-a12', 'alpha-a0'))
def test_stable_release_capture_keys(key):
    assert parse_capture_key(key) == key


@pytest.mark.parametrize('key', ('', '../alpha', 'alpha/a1', 'alpha-a1-a2', '-alpha'))
def test_invalid_capture_keys(key):
    with pytest.raises(ValueError):
        parse_capture_key(key)


@pytest.mark.asyncio
async def test_episode_capabilities_reject_cross_tenant_calls_before_upstream():
    app = FastAPI()
    forwarded = []
    grants = {EpisodeId(rollout_id='tenant-alpha').capture_key: 'alpha-capability',
              EpisodeId(rollout_id='tenant-beta').capture_key: 'beta-capability'}

    async def authorize(capture, bearer):
        return grants.get(capture) == bearer

    @app.post('/ng-rollout/{capture}/v1/responses')
    async def upstream(capture: str):
        forwarded.append(capture)
        return {'model_call': 'accepted'}

    app.add_middleware(NativeSDKIngress, on_native_request=authorize)
    alpha, beta = grants
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://tenant-test') as client:
        for capture, bearer in [(alpha, None), (alpha, 'wrong'), (alpha, grants[beta]), (beta, grants[alpha])]:
            headers={} if bearer is None else {'authorization': 'Bearer '+bearer}
            response = await client.post(f'/ng-rollout/{capture}/v1/responses', headers=headers, json={'input': []})
            assert response.status_code == 403
        assert forwarded == []
        response = await client.post(f'/ng-rollout/{alpha}/v1/responses', headers={'authorization':'Bearer '+grants[alpha]}, json={'input': []})
        assert response.status_code == 200
        del grants[alpha]
        response = await client.post(f'/ng-rollout/{alpha}/v1/responses', headers={'authorization':'Bearer alpha-capability'}, json={'input': []})
        assert response.status_code == 403
    assert forwarded == [alpha]


@pytest.mark.asyncio
async def test_unbound_model_server_fails_closed():
    app = FastAPI()
    app.add_middleware(NativeSDKIngress)
    episode = EpisodeId(rollout_id='unbound')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/ng-rollout/{episode.capture_key}/v1/responses', json={'input': []})
    assert response.status_code == 403


@pytest.mark.parametrize('drop,include,expected', [
    (False, ['reasoning.encrypted_content'], ['reasoning.encrypted_content']),
    (True, ['reasoning.encrypted_content'], None),
    (True, ['message.output_text.logprobs'], ['message.output_text.logprobs']),
])
async def test_chat_adapter_only_omits_the_unrepresentable_encrypted_state_hint(drop, include, expected):
    from fastapi import Request
    app = FastAPI()

    @app.post('/ng-rollout/{capture}/v1/responses')
    async def echo(capture: str, request: Request):
        return await request.json()

    async def authorize(capture, bearer):
        return bearer == 'owned-fixture'

    app.add_middleware(NativeSDKIngress, on_native_request=authorize, omit_responses_reasoning_hints=drop)
    original={'input': [], 'tools': [], 'include': include, 'instructions': 'Keep the task unchanged',
              'reasoning': {'effort':'xhigh', 'summary':'auto'}}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response=await client.post('/ng-rollout/owned/v1/responses',
                                   headers={'authorization':'Bearer owned-fixture'}, json=original)
    assert response.status_code == 200
    actual=response.json()
    assert actual.get('include') == expected
    assert actual['reasoning'] == ({'effort':'xhigh'} if drop else original['reasoning'])
    assert {k:v for k,v in actual.items() if k not in ('include','reasoning')} == {k:v for k,v in original.items() if k not in ('include','reasoning')}


@pytest.mark.asyncio
async def test_reused_connections_do_not_inherit_dispatcher_or_tenant_context():
    marker = ContextVar('tenant-marker', default=None)
    app = FastAPI()

    @app.get('/{tenant}')
    async def endpoint(tenant: str):
        inherited=marker.get()
        marker.set(tenant)
        await asyncio.sleep(0)
        return {'inherited':inherited, 'own':marker.get()}

    app.add_middleware(RequestContextIsolation)
    marker.set('dispatcher-private-value')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        results=await asyncio.gather(*(client.get('/'+name) for name in ('alpha','beta','alpha','beta')))
    assert [r.json() for r in results] == [{'inherited':None, 'own':name} for name in ('alpha','beta','alpha','beta')]
    assert marker.get() == 'dispatcher-private-value'
