import os
import pathlib
import tempfile

TEST_DB = pathlib.Path(tempfile.gettempdir()) / 'hengshi-quota-test.db'
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ['DATABASE_URL'] = f'sqlite:///{TEST_DB}'
os.environ['JWT_SECRET'] = 'test-secret'

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.config.database import Base, engine, Session
from src.models.entities import Node, QuotaPool, User
from src.utils.security import hash_password
from src.services import quota_service as quota


@pytest.fixture()
def db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as session:
        for name, role in [('hs-primary', 'leader'), ('hs-east', 'follower'), ('hs-west', 'follower')]:
            session.add(Node(name=name, role=role, term=6, log_position=8100,
                             available=True, fenced=False, vote_enabled=True))
        for role in ('admin', 'operator', 'viewer'):
            session.add(User(email=f'{role}@hengshi.local', role=role,
                             password_hash=hash_password('Drill2026!')))
        session.commit()
        nodes = {n.name: n.id for n in session.scalars(select(Node))}
        for node_id in nodes.values():
            quota.set_pool(session, node_id, 900)
        session.commit()
        yield session


def pool_id(db, name):
    node = db.scalar(select(Node).where(Node.name == name))
    return db.scalar(select(QuotaPool).where(QuotaPool.node_id == node.id)).id


def node_id(db, name):
    return db.scalar(select(Node).where(Node.name == name)).id


def set_network(db, name, available=None, fenced=None):
    quota.simulate_network(db, node_id(db, name), available, fenced)
    db.commit()


def test_offline_node_keeps_local_quota(db):
    set_network(db, 'hs-east', available=False)
    out = quota.reserve(db, pool_id(db, 'hs-east'), 100)
    db.commit()
    assert out['amount'] == 100
    assert out['pool_remaining'] == 800
    east = next(p for p in quota.overview(db)['pools'] if p['node'] == 'hs-east')
    assert east['mode'] == 'offline' and east['writable'] is True


def test_fenced_node_stops_writes(db):
    set_network(db, 'hs-east', fenced=True)
    with pytest.raises(HTTPException) as exc:
        quota.reserve(db, pool_id(db, 'hs-east'), 1)
    assert exc.value.status_code == 409 and '隔离' in exc.value.detail
    set_network(db, 'hs-east', fenced=False)
    assert quota.reserve(db, pool_id(db, 'hs-east'), 1)['amount'] == 1


def test_per_node_and_global_capacity_never_exceeded(db):
    for name in ('hs-primary', 'hs-east', 'hs-west'):
        quota.reserve(db, pool_id(db, name), 900)
    db.commit()
    for name in ('hs-primary', 'hs-east', 'hs-west'):
        with pytest.raises(HTTPException):
            quota.reserve(db, pool_id(db, name), 1)
    summary = quota.overview(db)['summary']
    assert summary['total_reserved'] == 2700
    assert summary['total_remaining'] == 0
    assert summary['invariant_ok'] is True


def test_transfer_pending_deducts_source_only_then_credit_after_recovery(db):
    primary, east = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east')
    # 接收方断网时先切断调拨入口，模拟“转出已扣、接收未到账”的中断现场。
    out = quota.create_transfer(db, primary, east, 200, idem_key='tr-0001')
    db.commit()
    assert out['status'] == 'pending'
    pools = {p['node']: p for p in quota.overview(db)['pools']}
    assert pools['hs-primary']['remaining'] == 700
    assert pools['hs-east']['remaining'] == 900       # 未到账，不增加可用额度
    assert pools['hs-east']['in_transit_in'] == 200
    # 中断：接收节点失联，补领被拒但额度仍挂在途
    set_network(db, 'hs-east', available=False)
    with pytest.raises(HTTPException) as exc:
        quota.credit_transfer(db, out['id'])
    db.rollback()
    assert '失联' in exc.value.detail
    stuck = next(t for t in quota.overview(db)['transfers'] if t['id'] == out['id'])
    assert stuck['stuck_reason'].startswith('接收节点失联')
    # 恢复后补领到账
    set_network(db, 'hs-east', available=True)
    credited = quota.credit_transfer(db, out['id'])
    db.commit()
    assert credited['status'] == 'credited'
    pools = {p['node']: p for p in quota.overview(db)['pools']}
    assert pools['hs-primary']['capacity'] == 700
    assert pools['hs-east']['capacity'] == 1100
    assert pools['hs-east']['remaining'] == 1100


def test_credit_replay_does_not_double_credit(db):
    primary, east = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east')
    out = quota.create_transfer(db, primary, east, 200, idem_key='tr-replay')
    db.commit()
    quota.credit_transfer(db, out['id']); db.commit()
    again = quota.credit_transfer(db, out['id']); db.commit()
    assert again['replayed'] is True
    pools = {p['node']: p for p in quota.overview(db)['pools']}
    assert pools['hs-east']['capacity'] == 1100
    assert pools['hs-primary']['in_transit_out'] == 0


def test_transfer_idempotency_key_replays_without_second_deduction(db):
    primary, east = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east')
    first = quota.create_transfer(db, primary, east, 120, idem_key='tr-idem')
    db.commit()
    second = quota.create_transfer(db, primary, east, 120, idem_key='tr-idem')
    db.commit()
    assert second['id'] == first['id'] and second['replayed'] is True
    assert next(p for p in quota.overview(db)['pools'] if p['node'] == 'hs-primary')['in_transit_out'] == 120
    with pytest.raises(HTTPException):
        quota.create_transfer(db, primary, east, 121, idem_key='tr-idem')


def test_reservation_idempotency_key_is_idempotent(db):
    east = pool_id(db, 'hs-east')
    first = quota.reserve(db, east, 50, idem_key='rs-000001')
    db.commit()
    second = quota.reserve(db, east, 50, idem_key='rs-000001')
    db.commit()
    assert second['replayed'] is True and second['id'] == first['id']
    assert next(p for p in quota.overview(db)['pools'] if p['node'] == 'hs-east')['reserved'] == 50


def test_in_transit_quota_cannot_be_spent_on_both_sides(db):
    primary, east = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east')
    quota.create_transfer(db, primary, east, 300)
    db.commit()
    # 源池在途转出仍占容量，不能再预留
    with pytest.raises(HTTPException) as exc:
        quota.reserve(db, primary, 601)
    assert '在途转出' in exc.value.detail
    # 目标池未到账前也不能提前使用
    with pytest.raises(HTTPException):
        quota.reserve(db, east, 901)
    assert quota.reserve(db, east, 900)['amount'] == 900


def test_claim_pending_is_restart_safe_and_skips_stuck(db):
    primary, east, west = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east'), pool_id(db, 'hs-west')
    quota.create_transfer(db, primary, east, 100)
    quota.create_transfer(db, primary, west, 100)
    db.commit()
    set_network(db, 'hs-east', available=False)
    result = quota.claim_pending(db, actor='system:reconcile')
    db.commit()
    assert [c['to_node'] for c in result['credited']] == ['hs-west']
    assert len(result['stuck']) == 1
    # 节点恢复后再次补领，只处理剩余那一单
    set_network(db, 'hs-east', available=True)
    again = quota.claim_pending(db)
    db.commit()
    assert [c['to_node'] for c in again['credited']] == ['hs-east']
    assert quota.claim_pending(db)['credited'] == []
    pools = {p['node']: p for p in quota.overview(db)['pools']}
    assert pools['hs-east']['capacity'] == 1000
    assert pools['hs-west']['capacity'] == 1000
    assert pools['hs-primary']['capacity'] == 700


def test_rollback_restores_source_quota(db):
    primary, east = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east')
    out = quota.create_transfer(db, primary, east, 150)
    db.commit()
    quota.rollback_transfer(db, out['id']); db.commit()
    pools = {p['node']: p for p in quota.overview(db)['pools']}
    assert pools['hs-primary']['remaining'] == 900
    assert pools['hs-east']['capacity'] == 900
    with pytest.raises(HTTPException):
        quota.credit_transfer(db, out['id'])


def test_fenced_or_offline_nodes_cannot_transfer(db):
    primary, east, west = pool_id(db, 'hs-primary'), pool_id(db, 'hs-east'), pool_id(db, 'hs-west')
    set_network(db, 'hs-west', available=False)
    with pytest.raises(HTTPException) as exc:
        quota.create_transfer(db, primary, west, 10)
    assert '失联' in exc.value.detail
    set_network(db, 'hs-west', available=True, fenced=True)
    with pytest.raises(HTTPException) as exc:
        quota.create_transfer(db, primary, west, 10)
    assert '隔离' in exc.value.detail
    assert quota.create_transfer(db, primary, east, 10)['status'] == 'pending'
