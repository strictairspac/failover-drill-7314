import os
from sqlalchemy import select
from src.config.database import Base, engine, Session
from src.models.entities import User, Node, Scenario, QuotaPool
from src.utils.security import hash_password

def seed():
    Base.metadata.create_all(engine)
    if os.getenv('SEED_DEMO','true').lower() != 'true': return
    with Session() as db:
        for role in ('admin','operator','viewer'):
            email = role + '@hengshi.local'
            if not db.scalar(select(User).where(User.email == email)):
                db.add(User(email=email, role=role, password_hash=hash_password('Drill2026!')))
        for name, role, pos in [('hs-primary','leader',8100),('hs-east','follower',8100),('hs-west','follower',8098)]:
            if not db.scalar(select(Node).where(Node.name == name)):
                db.add(Node(name=name, role=role, term=6, log_position=pos, available=True, fenced=False, vote_enabled=True))
        for name, fence in [('晚班隔离演练',True),('缺少隔离证明',False)]:
            if not db.scalar(select(Scenario).where(Scenario.name == name)):
                db.add(Scenario(name=name, fail_primary=True, fence_primary=fence, max_data_loss=0, required_term=7))
        db.flush()
        # 每个节点一个库位额度池；演示总量 900+900+900，可覆盖断网/调拨/补领剧本。
        for name, capacity in [('hs-primary',900),('hs-east',900),('hs-west',900)]:
            node = db.scalar(select(Node).where(Node.name == name))
            if node and not db.scalar(select(QuotaPool).where(QuotaPool.node_id == node.id)):
                db.add(QuotaPool(node_id=node.id, capacity=capacity, reserved=0, in_transit_out=0, in_transit_in=0))
        db.commit()
if __name__ == '__main__': seed()
