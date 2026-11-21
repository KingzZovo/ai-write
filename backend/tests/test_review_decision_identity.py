import httpx
import pytest
from fastapi import FastAPI, Request
from app import main


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['valid', 'invalid', 'missing', 'disabled'])
async def test_auth_middleware_only_supplies_verified_identity(monkeypatch, mode):
    monkeypatch.setenv('DISABLE_AUTH', '1' if mode == 'disabled' else '0')
    checked = []
    def verify(token):
        checked.append(token)
        if mode == 'invalid': raise ValueError('bad token')
        return 'verified-author'
    monkeypatch.setattr(main, 'verify_token', verify)
    app = FastAPI()
    @app.get('/api/decision-identity')
    async def identity(request: Request):
        return {'actor': getattr(request.state, 'auth_username', None)}
    app.add_middleware(main.AuthMiddleware)
    headers = {} if mode == 'missing' else {'Authorization': 'Bearer test-token', 'X-Username': 'forged'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/decision-identity', headers=headers)
    if mode in {'invalid', 'missing'}:
        assert response.status_code == 401
    else:
        assert response.status_code == 200
        assert response.json()['actor'] == ('verified-author' if mode == 'valid' else None)
    assert checked == (['test-token'] if mode in {'valid', 'invalid'} else [])
