from fastapi import APIRouter, Depends
from src.config.database import get_db
from src.middlewares.auth import current_user, writable, admin
from src.schemas.quota import PoolInput, ReservationInput, TransferInput, SimulationInput
from src.services import quota_service as quota
from src.controllers.actions import audit

router = APIRouter()


@router.get('/quota/overview')
def overview(db=Depends(get_db), user=Depends(current_user)):
    return quota.overview(db)


@router.put('/quota/pools')
def upsert_pool(data: PoolInput, db=Depends(get_db), user=Depends(admin)):
    pool = quota.set_pool(db, data.node_id, data.capacity)
    audit(db, user, f'quota:pool:{pool.id}:capacity:{data.capacity}')
    db.commit()
    return quota.overview(db)['pools']


@router.post('/quota/reservations', status_code=201)
def reserve(data: ReservationInput, db=Depends(get_db), user=Depends(writable)):
    out = quota.reserve(db, data.pool_id, data.amount, data.idem_key, actor=user.email)
    audit(db, user, f"quota:reserve:{data.pool_id}:{data.amount}")
    db.commit()
    return out


@router.post('/quota/transfers', status_code=201)
def transfer(data: TransferInput, db=Depends(get_db), user=Depends(writable)):
    out = quota.create_transfer(db, data.from_pool_id, data.to_pool_id, data.amount,
                                data.idem_key, actor=user.email)
    audit(db, user, f"quota:transfer:{out['id']}:{data.amount}")
    db.commit()
    return out


@router.post('/quota/transfers/{transfer_id}/credit')
def credit(transfer_id: int, db=Depends(get_db), user=Depends(writable)):
    out = quota.credit_transfer(db, transfer_id, actor=user.email)
    audit(db, user, f'quota:transfer:{transfer_id}:credit')
    db.commit()
    return out


@router.post('/quota/transfers/{transfer_id}/rollback')
def rollback(transfer_id: int, db=Depends(get_db), user=Depends(writable)):
    out = quota.rollback_transfer(db, transfer_id, actor=user.email)
    audit(db, user, f'quota:transfer:{transfer_id}:rollback')
    db.commit()
    return out


@router.post('/quota/claim')
def claim(db=Depends(get_db), user=Depends(writable)):
    out = quota.claim_pending(db, actor=user.email)
    audit(db, user, f"quota:claim:{len(out['credited'])}")
    db.commit()
    out['overview'] = quota.overview(db)
    return out


@router.post('/quota/nodes/{node_id}/network')
def network(node_id: int, data: SimulationInput, db=Depends(get_db), user=Depends(admin)):
    out = quota.simulate_network(db, node_id, data.available, data.fenced)
    audit(db, user, f"quota:network:{node_id}:{out['available']}:{out['fenced']}")
    db.commit()
    out['overview'] = quota.overview(db)
    return out
