import os
import pathlib
import tempfile

TEST_DB = pathlib.Path(tempfile.gettempdir()) / 'hengshi-quota-api-test.db'
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ['DATABASE_URL'] = f'sqlite:///{TEST_DB}'
os.environ['JWT_SECRET'] = 'test-secret'
os.environ['REDIS_URL'] = 'redis://127.0.0.1:6399/0'

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.config.database import Base, engine, Session
from src.main import app
from src.models.entities import Node, QuotaPool, User
from src.utils.security import hash_password


@pytest.fixture()
def client():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        for name, role in [('hs-primary', 'leader'), ('hs-east', 'follower'), ('hs-west', 'follower')]:
            db.add(Node(name=name, role=role, term=6, log_position=8100,
                        available=True, fenced=False, vote_enabled=True))
        for role in ('admin', 'operator', 'viewer'):
            db.add(User(email=f'{role}@hengshi.local', role=role,
                        password_hash=hash_password('Drill2026!')))
        db.commit()
        for node in db.scalars(select(Node)):
            db.add(QuotaPool(node_id=node.id, capacity=900, reserved=0, in_transit_out=0, in_transit_in=0))
        db.commit()
    return TestClient(app)


def auth(client, role='operator'):
    r = client.post('/auth/login', json={'email': f'{role}@hengshi.local', 'password': 'Drill2026!'})
    assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['access_token']}


def pools(client, headers):
    return {p['node']: p for p in client.get('/quota/overview', headers=headers).json()['pools']}


def test_viewer_cannot_write(client):
    h = auth(client, 'viewer')
    east = pools(client, h)['hs-east']['id']
    assert client.post('/quota/reservations', json={'pool_id': east, 'amount': 10}, headers=h).status_code == 403
    assert client.post('/quota/claim', headers=h).status_code == 403
    assert client.post(f'/quota/nodes/1/network', json={'available': False}, headers=h).status_code == 403


def test_operator_can_reserve_but_not_simulate_network(client):
    h = auth(client, 'operator')
    east = pools(client, h)['hs-east']['id']
    assert client.post('/quota/reservations', json={'pool_id': east, 'amount': 20}, headers=h).status_code == 201
    assert client.post('/quota/nodes/1/network', json={'available': False}, headers=h).status_code == 403


def test_network_simulation_and_offline_reservation(client):
    h = auth(client, 'admin')
    east_node = next(n for n in client.get('/nodes', headers=h).json() if n['name'] == 'hs-east')
    r = client.post(f"/quota/nodes/{east_node['id']}/network", json={'available': False}, headers=h)
    assert r.status_code == 200 and r.json()['mode'] == 'offline'
    east = pools(client, h)['hs-east']['id']
    r = client.post('/quota/reservations', json={'pool_id': east, 'amount': 30}, headers=h)
    assert r.status_code == 201, r.text
    assert r.json()['pool_remaining'] == 870


def test_transfer_interrupt_and_claim_flow(client):
    h = auth(client, 'operator')
    p = pools(client, h)
    primary, east, east_node = p['hs-primary']['id'], p['hs-east']['id'], p['hs-east']['node_id']
    created = client.post('/quota/transfers', json={
        'from_pool_id': primary, 'to_pool_id': east, 'amount': 200, 'idem_key': 'api-tr-0001'}, headers=h)
    assert created.status_code == 201, created.text
    tid = created.json()['id']
    # 接收方在补领前失联：额度停在在途
    a = auth(client, 'admin')
    assert client.post(f'/quota/nodes/{east_node}/network', json={'available': False}, headers=a).status_code == 200
    assert client.post(f'/quota/transfers/{tid}/credit', headers=h).status_code == 409
    ov = client.get('/quota/overview', headers=h).json()
    stuck = next(t for t in ov['transfers'] if t['id'] == tid)
    assert '失联' in stuck['stuck_reason']
    assert stuck['status'] == 'pending'
    # 恢复后批量补领；重复调用不重复到账
    assert client.post(f'/quota/nodes/{east_node}/network', json={'available': True}, headers=a).status_code == 200
    first = client.post('/quota/claim', headers=h)
    assert first.status_code == 200 and len(first.json()['credited']) == 1
    second = client.post('/quota/claim', headers=h)
    assert second.json()['credited'] == []
    after = pools(client, h)
    assert after['hs-primary']['capacity'] == 700
    assert after['hs-east']['capacity'] == 1100
    assert after['hs-primary']['reserved'] + after['hs-east']['reserved'] + after['hs-west']['reserved'] <= 2700


def test_duplicate_reservation_key_returns_original(client):
    h = auth(client, 'operator')
    east = pools(client, h)['hs-east']['id']
    body = {'pool_id': east, 'amount': 40, 'idem_key': 'api-rs-0001'}
    first = client.post('/quota/reservations', json=body, headers=h)
    second = client.post('/quota/reservations', json=body, headers=h)
    assert first.status_code == second.status_code == 201
    assert first.json()['id'] == second.json()['id']
    assert second.json()['replayed'] is True
    assert pools(client, h)['hs-east']['reserved'] == 40


def test_overview_reports_global_invariant(client):
    h = auth(client, 'operator')
    p = pools(client, h)
    for name, amount in [('hs-primary', 900), ('hs-east', 900), ('hs-west', 900)]:
        assert client.post('/quota/reservations', json={'pool_id': p[name]['id'], 'amount': amount}, headers=h).status_code == 201
    summary = client.get('/quota/overview', headers=h).json()['summary']
    assert summary['total_reserved'] == 2700 and summary['total_remaining'] == 0 and summary['invariant_ok'] is True
    assert client.post('/quota/reservations', json={'pool_id': p['hs-east']['id'], 'amount': 1}, headers=h).status_code == 409
