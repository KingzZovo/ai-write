"""Real PG regression: a library entry is not an author's default choice."""
import uuid
from datetime import datetime, timezone, timedelta

import pytest
pytestmark = pytest.mark.external_services
from sqlalchemy import update

from app.db.session import async_session_factory
from app.models.project import StyleProfile
from app.services.style_runtime import resolve_active_profile


@pytest.mark.asyncio
async def test_library_does_not_become_default_and_explicit_selection_survives():
    async with async_session_factory() as db:
        await db.execute(update(StyleProfile).where(
            StyleProfile.bind_level == 'default_global'
        ).values(is_active=0))
        legacy = StyleProfile(name='旧仙侠画像', bind_level='global')
        library = StyleProfile(name='新资料画像')
        db.add_all([legacy, library])
        await db.flush()
        pid = uuid.uuid4()
        assert library.bind_level == 'library'
        assert await resolve_active_profile(db, pid, settings_json={}) is None
        for profile in (legacy, library):
            assert await resolve_active_profile(db, pid, settings_json={
                'style_profile_id': str(profile.id),
            }) is profile
        old = StyleProfile(name='较早明确默认', bind_level='default_global',
            config_json={'default_selected_at': '2026-10-05T00:00:00+00:00'},
            updated_at=datetime.now(timezone.utc) + timedelta(days=1))
        new = StyleProfile(name='最近明确默认', bind_level='default_global',
            config_json={'default_selected_at': '2026-10-06T00:00:00+00:00'})
        db.add_all([old, new])
        await db.flush()
        assert await resolve_active_profile(db, pid, settings_json={}) is new
        assert await resolve_active_profile(db, pid, settings_json={
            'style_reference': {'author_name': '明确选择的作者'},
        }) is None
        new.is_active = 0
        await db.flush()
        assert await resolve_active_profile(db, pid, settings_json={}) is old
        await db.rollback()


@pytest.mark.asyncio
async def test_api_default_binding_and_unbinding(auth_client):
    created = await auth_client.post('/api/styles', json={'name': 'default-regression-' + uuid.uuid4().hex})
    assert created.status_code == 201
    profile_id = created.json()['id']
    try:
        assert created.json()['bind_level'] == 'library'
        response = await auth_client.post(f'/api/styles/{profile_id}/bind', json={
            'bind_level': 'global', 'bind_target_id': str(uuid.uuid4()),
        })
        assert response.status_code == 200
        assert response.json()['bind_level'] == 'default_global'
        assert response.json()['bind_target_id'] is None
        assert response.json()['config_json']['default_selected_at']
        for target in (None, 'invalid'):
            invalid = await auth_client.post(f'/api/styles/{profile_id}/bind', json={
                'bind_level': 'chapter', 'bind_target_id': target,
            })
            assert invalid.status_code == 400
        missing = await auth_client.post(f'/api/styles/{profile_id}/bind', json={
            'bind_level': 'book', 'bind_target_id': str(uuid.uuid4()),
        })
        assert missing.status_code == 404
        response = await auth_client.post(f'/api/styles/{profile_id}/bind', json={'bind_level': 'library'})
        assert response.status_code == 200
        assert 'default_selected_at' not in response.json()['config_json']
    finally:
        await auth_client.delete(f'/api/styles/{profile_id}')
