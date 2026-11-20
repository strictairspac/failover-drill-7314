"""离线库位额度账本。

模型边界：每个数据库节点一个额度池；节点失联时仍可使用本地剩余额度，
节点被隔离时停止一切写入；调拨为两阶段（转出即扣、接收方补领入账），
任何时刻全局「已预留 + 在途转出」都不得超过全局总容量。
"""
import threading
from contextlib import contextmanager
from sqlalchemy import select
from fastapi import HTTPException
from src.config.database import engine
from src.models.entities import Node, QuotaPool, Reservation, Transfer, QuotaEvent, now
from src.utils.serialize import record


def _pool(db, pool_id, lock=True):
    stmt = select(QuotaPool).where(QuotaPool.id == pool_id)
    obj = db.scalar(stmt.with_for_update() if lock else stmt)
    if obj is None:
        raise HTTPException(404, '额度池不存在')
    return obj


def _lock_pools(db, first_id, second_id):
    """按主键顺序加锁，避免对向调拨造成死锁。"""
    rows = db.scalars(select(QuotaPool).where(QuotaPool.id.in_((first_id, second_id)))
                      .order_by(QuotaPool.id).with_for_update()).all()
    by_id = {p.id: p for p in rows}
    if first_id not in by_id: raise HTTPException(404, '源额度池不存在')
    if second_id not in by_id: raise HTTPException(404, '目标额度池不存在')
    return by_id[first_id], by_id[second_id]


_sqlite_lock = threading.RLock()


@contextmanager
def _serialize(db):
    """全局额度账本串行化。

    MySQL：锁定全部池行，保证「各库预留之和 + 在途 ≤ 总容量」在并发下成立。
    SQLite（本地开发/测试）不支持 SELECT ... FOR UPDATE，退化为进程内互斥锁，
    语义相同；两种环境都不允许同一份额度被两边同时使用。
    """
    if engine.dialect.name == 'sqlite':
        # 仅本地开发/测试路径：锁内落盘，下一个线程才能看到本次写入。
        with _sqlite_lock:
            try:
                yield
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return
    _lock_ledger(db)
    yield


def _lock_ledger(db):
    """锁定并强制重读全部池行。加锁读返回最新已提交版本，避免快照读的旧值。"""
    return db.scalars(select(QuotaPool).order_by(QuotaPool.id).with_for_update()
                      .execution_options(populate_existing=True)).all()


def _node(db, node_id):
    node = db.get(Node, node_id)
    if node is None:
        raise HTTPException(404, '节点不存在')
    return node


def node_mode(node):
    """fenced 优先：被隔离的节点停写；失联节点保留本地额度。"""
    if node.fenced:
        return 'fenced'
    return 'online' if node.available else 'offline'


def available(pool):
    return pool.capacity - pool.reserved - pool.in_transit_out


def _sums(rows):
    return (sum(p.capacity for p in rows), sum(p.reserved for p in rows),
            sum(p.in_transit_out for p in rows))


def _global_totals(db):
    """按已加锁的池行求和，避免 REPEATABLE READ 下快照读看不到别的已提交事务。"""
    db.flush()
    return _sums(_lock_ledger(db))


def _assert_global(db):
    capacity, reserved, transit = _global_totals(db)
    if reserved + transit > capacity:
        # 正常路径不可达；出现即说明并发扣减或账本被绕过，必须拒绝写入。
        raise HTTPException(409, f'全局预留 {reserved} + 在途 {transit} 超过总容量 {capacity}，已拒绝写入')


def _event(db, kind, pool_id, amount=0, ref_type=None, ref_id=None, actor=None, detail=None):
    db.add(QuotaEvent(pool_id=pool_id, kind=kind, amount=amount, ref_type=ref_type,
                      ref_id=ref_id, actor=actor, detail=detail))


def _reservation_out(db, r, pool=None, replayed=False):
    out = record(r)
    if pool is not None:
        out['pool_capacity'] = pool.capacity
        out['pool_remaining'] = available(pool)
    out['replayed'] = replayed
    return out


def _transfer_out(db, t, replayed=False):
    out = record(t)
    source = db.get(QuotaPool, t.from_pool_id)
    target = db.get(QuotaPool, t.to_pool_id)
    source_node = db.get(Node, source.node_id) if source else None
    target_node = db.get(Node, target.node_id) if target else None
    out['from_node'] = source_node.name if source_node else None
    out['to_node'] = target_node.name if target_node else None
    out['stuck_reason'] = stuck_reason(db, t)
    out['replayed'] = replayed
    return out


def stuck_reason(db, t):
    if t.status != 'pending':
        # 已到账/已回滚不再“卡住”，直接展示结果说明。
        return t.detail
    source = db.get(QuotaPool, t.from_pool_id)
    target = db.get(QuotaPool, t.to_pool_id)
    source_node = db.get(Node, source.node_id) if source else None
    target_node = db.get(Node, target.node_id) if target else None
    if target_node is None:
        return '接收节点已删除，只能回滚'
    target_mode = node_mode(target_node)
    if target_mode == 'fenced':
        return '接收节点已隔离停写，恢复后可补领入账'
    if target_mode == 'offline':
        return '接收节点失联，转出额度已扣、尚未到账，恢复后可补领入账'
    if source_node is not None and node_mode(source_node) == 'fenced':
        return '转出节点已隔离停写，接收方仍可补领入账'
    return '等待接收方补领入账'


def set_pool(db, node_id, capacity):
    with _serialize(db):
        node = _node(db, node_id)
        pool = db.scalar(select(QuotaPool).where(QuotaPool.node_id == node.id)
                         .with_for_update())
        if pool is None:
            pool = QuotaPool(node_id=node.id, capacity=capacity, reserved=0, in_transit_out=0, in_transit_in=0)
            db.add(pool)
            db.flush()
            _event(db, 'pool:create', pool.id, detail=f'容量 {capacity}')
        else:
            if capacity < pool.reserved + pool.in_transit_out:
                raise HTTPException(409, f'容量不能低于已预留 {pool.reserved} + 在途转出 {pool.in_transit_out}')
            _event(db, 'pool:capacity', pool.id, amount=capacity - pool.capacity,
                   detail=f'{pool.capacity} -> {capacity}')
            pool.capacity = capacity
        db.flush()
        return pool


def reserve(db, pool_id, amount, idem_key=None, actor=None):
    with _serialize(db):
        return _reserve(db, pool_id, amount, idem_key, actor)


def _reserve(db, pool_id, amount, idem_key=None, actor=None):
    pool = _pool(db, pool_id)
    if idem_key:
        existing = db.scalar(select(Reservation).where(
            Reservation.pool_id == pool_id, Reservation.idem_key == idem_key))
        if existing:
            if existing.amount != amount:
                raise HTTPException(409, '幂等键已用于不同金额的预留，请换一个键')
            return _reservation_out(db, existing, pool, replayed=True)
    node = _node(db, pool.node_id)
    mode = node_mode(node)
    if mode == 'fenced':
        raise HTTPException(409, f'{node.name} 已被隔离，停止写入，不能新增预留')
    room = available(pool)
    if amount > room:
        raise HTTPException(409, f'{node.name} 剩余额度 {room}，不足预留 {amount}；'
                                f'可用容量 {pool.capacity} - 已预留 {pool.reserved} - 在途转出 {pool.in_transit_out}')
    pool.reserved += amount
    _assert_global(db)
    obj = Reservation(pool_id=pool.id, amount=amount, status='active', idem_key=idem_key)
    db.add(obj)
    db.flush()
    _event(db, 'reservation:active', pool.id, amount, 'reservation', obj.id, actor,
           f'节点 {node.name}，模式 {mode}')
    db.flush()
    return _reservation_out(db, obj, pool)


def create_transfer(db, from_pool_id, to_pool_id, amount, idem_key=None, actor=None):
    if from_pool_id == to_pool_id:
        raise HTTPException(409, '源池与目标池不能相同')
    with _serialize(db):
        return _create_transfer(db, from_pool_id, to_pool_id, amount, idem_key, actor)


def _create_transfer(db, from_pool_id, to_pool_id, amount, idem_key=None, actor=None):
    source, target = _lock_pools(db, from_pool_id, to_pool_id)
    if idem_key:
        existing = db.scalar(select(Transfer).where(
            Transfer.from_pool_id == from_pool_id, Transfer.idem_key == idem_key))
        if existing:
            if existing.amount != amount or existing.to_pool_id != to_pool_id:
                raise HTTPException(409, '幂等键已用于不同的调拨请求，请换一个键')
            return _transfer_out(db, existing, replayed=True)
    source_node = _node(db, source.node_id)
    target_node = _node(db, target.node_id)
    for node in (source_node, target_node):
        mode = node_mode(node)
        if mode == 'fenced':
            raise HTTPException(409, f'{node.name} 已被隔离，停止写入，不能发起调拨')
        if mode == 'offline':
            raise HTTPException(409, f'{node.name} 失联，不能参与调拨；失联节点只能使用本地额度')
    room = available(source)
    if amount > room:
        raise HTTPException(409, f'{source_node.name} 剩余额度 {room}，不足调出 {amount}；'
                                f'在途转出 {source.in_transit_out} 仍占用容量')
    source.in_transit_out += amount
    target.in_transit_in += amount
    _assert_global(db)
    obj = Transfer(from_pool_id=source.id, to_pool_id=target.id, amount=amount,
                   status='pending', idem_key=idem_key)
    db.add(obj)
    db.flush()
    _event(db, 'transfer:pending', source.id, amount, 'transfer', obj.id, actor,
           f'{source_node.name} -> {target_node.name}，转出即扣、等待补领')
    db.flush()
    return _transfer_out(db, obj)


def credit_transfer(db, transfer_id, actor=None, system=False):
    """接收方补领入账。已入账的调拨直接返回原结果，绝不重复增加容量。"""
    with _serialize(db):
        return _credit_transfer(db, transfer_id, actor, system)


def _credit_transfer(db, transfer_id, actor=None, system=False):
    obj = db.scalar(select(Transfer).where(Transfer.id == transfer_id).with_for_update())
    if obj is None:
        raise HTTPException(404, '调拨单不存在')
    if obj.status == 'credited':
        return _transfer_out(db, obj, replayed=True)
    if obj.status != 'pending':
        raise HTTPException(409, f'调拨单状态为 {obj.status}，不能补领')
    source, target = _lock_pools(db, obj.from_pool_id, obj.to_pool_id)
    source_node = _node(db, source.node_id)
    target_node = _node(db, target.node_id)
    target_mode = node_mode(target_node)
    if target_mode == 'fenced':
        raise HTTPException(409, f'{target_node.name} 已被隔离，停止写入，待恢复后补领')
    if target_mode == 'offline':
        raise HTTPException(409, f'{target_node.name} 失联，转出额度已扣但未到账，恢复后可补领')
    if source.in_transit_out < obj.amount:
        raise HTTPException(409, '源池在途转出账本不一致，拒绝入账')
    source.in_transit_out -= obj.amount
    source.capacity -= obj.amount
    target.in_transit_in -= obj.amount
    target.capacity += obj.amount
    obj.status = 'credited'
    obj.detail = f'已到账 {obj.amount}，源池容量 {source.capacity}，目标池容量 {target.capacity}'
    obj.updated_at = now()
    _event(db, 'transfer:credited', target.id, obj.amount, 'transfer', obj.id, actor,
           f'{source_node.name} -> {target_node.name}，接收方已到账')
    _assert_global(db)
    db.flush()
    return _transfer_out(db, obj)


def rollback_transfer(db, transfer_id, actor=None):
    """回滚未入账的调拨：释放源池在途额度，接收方不增加容量。"""
    with _serialize(db):
        return _rollback_transfer(db, transfer_id, actor)


def _rollback_transfer(db, transfer_id, actor=None):
    obj = db.scalar(select(Transfer).where(Transfer.id == transfer_id).with_for_update())
    if obj is None:
        raise HTTPException(404, '调拨单不存在')
    if obj.status == 'failed':
        return _transfer_out(db, obj, replayed=True)
    if obj.status != 'pending':
        raise HTTPException(409, f'调拨单已 {obj.status}，入账后不能回滚')
    source, target = _lock_pools(db, obj.from_pool_id, obj.to_pool_id)
    source.in_transit_out -= obj.amount
    target.in_transit_in -= obj.amount
    obj.status = 'failed'
    obj.updated_at = now()
    obj.detail = '中途回滚'
    _event(db, 'transfer:rolledback', source.id, obj.amount, 'transfer', obj.id, actor,
           '未到账调拨回滚，源池额度恢复')
    db.flush()
    return _transfer_out(db, obj)


def claim_pending(db, actor=None):
    """恢复后批量补领：只处理两端健康且仍挂起的调拨，可重复调用。"""
    ids = db.scalars(select(Transfer.id).where(Transfer.status == 'pending').order_by(Transfer.id)).all()
    claimed, stuck = [], []
    for transfer_id in ids:
        try:
            claimed.append(credit_transfer(db, transfer_id, actor=actor, system=True))
        except HTTPException as exc:
            # 单个调拨卡住不应滚掉已成功入账的调拨；先落盘再继续。
            db.commit()
            stuck.append({'transfer_id': transfer_id, 'reason': exc.detail})
    return {'credited': claimed, 'stuck': stuck}


def simulate_network(db, node_id, available_flag=None, fenced_flag=None):
    node = _node(db, node_id)
    if fenced_flag is not None:
        node.fenced = fenced_flag
    if available_flag is not None:
        node.available = available_flag
    db.flush()
    return {'node_id': node.id, 'name': node.name, 'available': node.available,
            'fenced': node.fenced, 'mode': node_mode(node)}


def overview(db):
    pools = db.scalars(select(QuotaPool).order_by(QuotaPool.id)).all()
    items = []
    for pool in pools:
        node = db.get(Node, pool.node_id)
        mode = node_mode(node) if node else 'missing'
        items.append({
            'id': pool.id, 'node_id': pool.node_id, 'node': node.name if node else None,
            'mode': mode, 'available': node.available if node else False,
            'fenced': node.fenced if node else False,
            'capacity': pool.capacity, 'reserved': pool.reserved,
            'in_transit_out': pool.in_transit_out, 'in_transit_in': pool.in_transit_in,
            'remaining': available(pool),
            'writable': mode != 'fenced',
        })
    capacity, reserved, transit = _sums(pools)
    transfers = db.scalars(select(Transfer).order_by(Transfer.id.desc()).limit(30)).all()
    events = db.scalars(select(QuotaEvent).order_by(QuotaEvent.id.desc()).limit(30)).all()
    reservations = db.scalars(select(Reservation).order_by(Reservation.id.desc()).limit(30)).all()
    return {
        'pools': items,
        'summary': {
            'total_capacity': capacity,
            'total_reserved': reserved,
            'total_in_transit': transit,
            'total_remaining': capacity - reserved - transit,
            'invariant_ok': reserved + transit <= capacity,
        },
        'transfers': [_transfer_out(db, t) for t in transfers],
        'reservations': [_reservation_out(db, r) for r in reservations],
        'events': [record(e) for e in events],
        'model_version': 'offline-quota-ledger-v1',
        'boundary': '受控演练账本；节点可达性与隔离为页面模拟输入，不代表真实网络探测。',
    }
