"""并发验证：并发预留/调拨下，全局预留+在途不超过总容量，调拨不会被重复到账。"""
import os
import pathlib
import tempfile
import threading

TEST_DB = pathlib.Path(tempfile.gettempdir()) / 'hengshi-quota-concurrency.db'
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ['DATABASE_URL'] = f'sqlite:///{TEST_DB}'
os.environ['JWT_SECRET'] = 'test-secret'

import pytest
from sqlalchemy import select

from src.config.database import Base, engine, Session
from src.models.entities import Node, QuotaPool
from src.services import quota_service as quota


@pytest.fixture()
def seeded():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session() as db:
        for name, role in [('hs-primary', 'leader'), ('hs-east', 'follower'), ('hs-west', 'follower')]:
            db.add(Node(name=name, role=role, term=6, log_position=8100,
                        available=True, fenced=False, vote_enabled=True))
        db.commit()
        ids = {}
        for node in db.scalars(select(Node)):
            pool = quota.set_pool(db, node.id, 300)
            db.flush()
            ids[node.name] = pool.id
        db.commit()
        return ids


def _pool_id(name):
    with Session() as db:
        node = db.scalar(select(Node).where(Node.name == name))
        return db.scalar(select(QuotaPool).where(QuotaPool.node_id == node.id)).id


def _attempt(fn, results, errors):
    try:
        with Session() as db:
            results.append(fn(db))
            db.commit()
    except Exception as exc:  # 业务拒绝（容量不足）是预期结果
        errors.append(exc)


def _run(fns):
    results, errors = [], []
    threads = [threading.Thread(target=_attempt, args=(fn, results, errors)) for fn in fns]
    for t in threads: t.start()
    for t in threads: t.join()
    return results, errors


def _totals():
    with Session() as db:
        return quota.overview(db)['summary']


def test_parallel_reservations_never_oversell(seeded):
    pool = seeded['hs-east']
    # 20 个线程各抢 30，容量 300：最多 10 个成功
    fns = [lambda db, p=pool, i=i: quota.reserve(db, p, 30, idem_key=f'par-{i}') for i in range(20)]
    results, errors = _run(fns)
    assert len(results) == 10, (len(results), len(errors))
    summary = _totals()
    assert summary['total_reserved'] == 300 and summary['invariant_ok'] is True
    east = next(p for p in _overview()['pools'] if p['node'] == 'hs-east')
    assert east['remaining'] == 0


def _overview():
    with Session() as db:
        return quota.overview(db)


def test_same_pool_quota_cannot_be_transferred_twice(seeded):
    primary, east, west = seeded['hs-primary'], seeded['hs-east'], seeded['hs-west']
    # 同一源池 300 额度，两个方向并发各调 200：只能有一笔成立
    fns = [lambda db: quota.create_transfer(db, primary, east, 200, idem_key='a'),
           lambda db: quota.create_transfer(db, primary, west, 200, idem_key='b')]
    results, errors = _run(fns)
    assert len(results) == 1, (len(results), [str(e) for e in errors])
    summary = _totals()
    assert summary['total_in_transit'] == 200 and summary['invariant_ok'] is True
    pending = [t for t in _overview()['transfers'] if t['status'] == 'pending']
    assert len(pending) == 1


def test_reservation_and_transfer_cannot_both_claim_same_quota(seeded):
    primary, east = seeded['hs-primary'], seeded['hs-east']
    # 预留与调拨并发争用同一份 300 额度，各要 250：只能成功一个
    fns = [lambda db: quota.reserve(db, primary, 250, idem_key='rs-race'),
           lambda db: quota.create_transfer(db, primary, east, 250, idem_key='tr-race')]
    results, errors = _run(fns)
    assert len(results) == 1, (len(results), [str(e) for e in errors])
    summary = _totals()
    assert summary['total_reserved'] + summary['total_in_transit'] == 250
    assert summary['invariant_ok'] is True


def test_parallel_credit_only_credits_once(seeded):
    primary, east = seeded['hs-primary'], seeded['hs-east']
    with Session() as db:
        out = quota.create_transfer(db, primary, east, 120, idem_key='once')
        db.commit()
        tid = out['id']
    fns = [lambda db, t=tid: quota.credit_transfer(db, t) for _ in range(8)]
    results, errors = _run(fns)
    assert errors == [], [str(e) for e in errors]
    assert sum(1 for r in results if not r['replayed']) == 1
    with Session() as db:
        ov = quota.overview(db)
    target = next(p for p in ov['pools'] if p['node'] == 'hs-east')
    source = next(p for p in ov['pools'] if p['node'] == 'hs-primary')
    assert target['capacity'] == 420 and source['capacity'] == 180
    assert ov['summary']['invariant_ok'] is True
