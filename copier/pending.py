"""Pending entry mirroring, sharing durable position mappings and command intents."""
import json
import secrets
import time
from . import store, safety
from .transport import M, BrokerError
from .protocol import OpenApiModelMessages_pb2 as Model

FIELDS=('limitPrice','stopPrice','stopLoss','takeProfit','relativeStopLoss','relativeTakeProfit','expirationTimestamp','slippageInPoints','stopTriggerMethod')

def master_event(engine,account,order,position=None,allow_new=False):
    if order.orderType not in (2,3,6) or order.closingOrder or order.tradeData.symbolId!=account['symbol_id']:return
    aid=account['id'];oid=order.orderId;stamp=order.utcLastUpdateTimestamp or order.tradeData.openTimestamp
    old=store.one('SELECT * FROM master_orders WHERE account_id=? AND order_id=?',(aid,oid))
    if old and stamp<old['updated']:return
    store.execute('INSERT OR REPLACE INTO master_orders VALUES(?,?,?,?,?)',(aid,oid,order.SerializeToString(),order.orderStatus,stamp))
    linked=store.rows('SELECT * FROM mappings WHERE master_account=? AND master_order=?',(aid,oid))
    if order.HasField('positionId') and (order.executedVolume or order.orderStatus==2):
        # Move the existing reservation to the real master position BEFORE update_master.
        store.execute('UPDATE mappings SET master_position=? WHERE master_account=? AND master_order=?',(order.positionId,aid,oid))
        if position is not None and linked:engine.update_master(account,position,allow_new=False)
        for m in linked:engine.schedule(m['id'])
        return
    if order.orderStatus!=1:
        store.execute('UPDATE master_positions SET closed=1,volume=0 WHERE account_id=? AND position_id=?',(aid,-oid))
        for m in linked:engine.schedule(m['id'])
        return
    price=order.limitPrice if order.orderType==2 else order.stopPrice
    side=order.tradeData.tradeSide;sign=1 if side==1 else -1
    sl=order.stopLoss if order.HasField('stopLoss') else price-sign*order.relativeStopLoss/100000 if order.HasField('relativeStopLoss') else None
    tp=order.takeProfit if order.HasField('takeProfit') else price+sign*order.relativeTakeProfit/100000 if order.HasField('relativeTakeProfit') else None
    store.execute('''INSERT INTO master_positions VALUES(?,?,?,?,?,?,?,?,?,0,1)
        ON CONFLICT(account_id,position_id) DO UPDATE SET volume=excluded.volume,sl=excluded.sl,tp=excluded.tp,price=excluded.price,updated=excluded.updated''',
        (aid,-oid,order.tradeData.volume,order.tradeData.volume,side,sl,tp,price,stamp))
    fresh=allow_new and not old and store.setting('paused')=='false' and engine.trading_enabled() and time.time()*1000-stamp<=safety.policy()['max_signal_age']*1000
    if fresh:
        from .engine import lots_to_volume
        for follower in store.rows("SELECT * FROM accounts WHERE role='follower' AND enabled=1"):
            if follower['id'] not in engine.ready:continue
            volume=lots_to_volume(follower['lots'],json.loads(follower['symbol_json']))
            mid=secrets.token_hex(12)
            store.execute('''INSERT OR IGNORE INTO mappings(id,master_account,master_position,master_order,follower_account,initial_master,initial_follower,label,status,created)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',(mid,aid,-oid,oid,follower['id'],order.tradeData.volume,volume,'gimm:'+mid,'pending',time.time()))
    for m in store.rows("SELECT id FROM mappings WHERE master_account=? AND master_order=? AND status NOT IN ('closed','failed')",(aid,oid)):engine.schedule(m['id'])

def follower_event(engine,account,order,position=None):
    if order.orderType not in (2,3,6) or order.closingOrder:return
    m=store.one('SELECT * FROM mappings WHERE follower_account=? AND label=? AND master_order IS NOT NULL',(account['id'],order.tradeData.label))
    if not m:return
    store.execute('UPDATE mappings SET follower_order=? WHERE id=?',(order.orderId,m['id']))
    if position is not None and position.positionStatus in (1,2):
        engine.save_position(account,position)
        store.execute("UPDATE mappings SET follower_position=?,status='active',error=NULL WHERE id=?",(position.positionId,m['id']))
    elif order.orderStatus in (3,4,5) and not order.executedVolume and not m['follower_position']:
        store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(m['id'],))
    elif order.orderStatus==1 and (m['status']=='opening' or store.one("SELECT 1 FROM commands WHERE mapping_id=? AND kind='open' AND status IN ('sending','uncertain')",(m['id'],))):
        store.execute("UPDATE mappings SET status='active',error=NULL WHERE id=? AND status IN ('opening','uncertain')",(m['id'],))
    store.execute("UPDATE commands SET status='reconciled',error=NULL WHERE mapping_id=? AND kind='open' AND status IN ('sending','uncertain')",(m['id'],))
    for cmd in store.rows("SELECT * FROM commands WHERE mapping_id=? AND kind='pending_amend' AND status='uncertain'",(m['id'],)):
        payload=json.loads(cmd['payload'])
        prices=('stopLoss','takeProfit','relativeStopLoss','relativeTakeProfit')
        same=all(order.HasField(k)==(k in payload) and (k not in payload or getattr(order,k)==payload[k]) for k in prices)
        same=same and all(order.HasField(k) and getattr(order,k)==payload[k] for k in FIELDS if k in payload and k not in prices)
        if same:
            store.execute("UPDATE commands SET status='reconciled',error=NULL WHERE id=?",(cmd['id'],))
            store.execute("UPDATE mappings SET status='active',error=NULL WHERE id=?",(m['id'],))
    engine.schedule(m['id'])

async def sync(engine,mapping,source,follower):
    """Return True while this copy is an unfilled pending order."""
    if not mapping['master_order']:return False
    mid=mapping['id'];saved=store.one('SELECT * FROM master_orders WHERE account_id=? AND order_id=?',(mapping['master_account'],mapping['master_order']))
    if not saved:return True
    order=Model.ProtoOAOrder.FromString(saved['payload'])
    if mapping['follower_position']:
        # A partial fill can leave a live remainder. Cancel that before closing
        # the filled position, or when the master cancels its own remainder.
        if mapping['follower_order'] and (source['closed'] or order.orderStatus in (3,4,5)):
            if store.one("SELECT 1 FROM commands WHERE mapping_id=? AND kind='cancel' AND status IN ('sending','uncertain')",(mid,)):return False
            details=await engine.conn(follower).request(M.ProtoOAOrderDetailsReq(ctidTraderAccountId=follower['broker_id'],orderId=mapping['follower_order']))
            if details.order.orderStatus==1:
                response=await engine.command(mapping,'cancel',M.ProtoOACancelOrderReq(ctidTraderAccountId=follower['broker_id'],orderId=mapping['follower_order']))
                if response and response.HasField('order'):follower_event(engine,follower,response.order,response.position if response.HasField('position') else None)
        return False
    if mapping['follower_order']:
        if source['closed']:
            if store.one("SELECT 1 FROM commands WHERE mapping_id=? AND kind='cancel' AND status IN ('sending','uncertain')",(mid,)):return True
            response=await engine.command(mapping,'cancel',M.ProtoOACancelOrderReq(ctidTraderAccountId=follower['broker_id'],orderId=mapping['follower_order']))
            if response and response.HasField('order'):follower_event(engine,follower,response.order,response.position if response.HasField('position') else None)
            return True
        # Details establish current broker fields, including fills racing an amendment.
        details=await engine.conn(follower).request(M.ProtoOAOrderDetailsReq(ctidTraderAccountId=follower['broker_id'],orderId=mapping['follower_order']))
        actual=details.order
        if actual.orderStatus!=1:
            await engine.reconcile(follower)
            if actual.orderStatus in (3,4,5) and not actual.executedVolume:
                store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(mid,))
            return True
        request=M.ProtoOAAmendOrderReq(ctidTraderAccountId=follower['broker_id'],orderId=mapping['follower_order'],trailingStopLoss=False)
        changed=False
        for field in FIELDS:
            if order.HasField(field):
                setattr(request,field,getattr(order,field))
                if not actual.HasField(field) or getattr(order,field)!=getattr(actual,field):changed=True
            elif field in ('stopLoss','takeProfit','relativeStopLoss','relativeTakeProfit') and actual.HasField(field):
                changed=True
        if changed:
            response=await engine.command(mapping,'pending_amend',request)
            if response is None:return True
            if response.HasField('position') and response.HasField('order'):
                follower_event(engine,follower,response.order,response.position)
                return True
            if response.HasField('order'):
                for field in ('stopLoss','takeProfit','relativeStopLoss','relativeTakeProfit'):
                    if order.HasField(field)!=response.order.HasField(field) or (order.HasField(field) and getattr(order,field)!=getattr(response.order,field)):
                        raise BrokerError('Pending order protection not confirmed')
            else:
                store.execute("UPDATE mappings SET status='uncertain',error='Pending amendment confirmation missing' WHERE id=?",(mid,))
        return True
    if source['closed'] or mapping['master_position']>0 or store.setting('paused')!='false' or not follower['enabled']:
        store.execute("UPDATE mappings SET status='closed',error='Pending entry no longer eligible' WHERE id=?",(mid,));return True
    request=M.ProtoOANewOrderReq(ctidTraderAccountId=follower['broker_id'],symbolId=follower['symbol_id'],orderType=order.orderType,tradeSide=order.tradeData.tradeSide,volume=mapping['initial_follower'],label=mapping['label'],clientOrderId=mid,timeInForce=order.timeInForce if order.HasField('timeInForce') else 2,trailingStopLoss=False)
    for field in FIELDS:
        if order.HasField(field):setattr(request,field,getattr(order,field))
    store.execute("UPDATE mappings SET status='opening' WHERE id=?",(mid,))
    try:response=await engine.command(mapping,'open',request)
    except BrokerError as exc:
        store.execute("UPDATE mappings SET status='failed',error=? WHERE id=?",(str(exc),mid));raise
    if not response.HasField('order'):
        store.execute("UPDATE mappings SET status='uncertain',error='Pending order confirmation missing' WHERE id=?",(mid,));return True
    follower_event(engine,follower,response.order,response.position if response.HasField('position') else None)
    if response.order.orderStatus==1 and not response.HasField('position'):
        for field in ('stopLoss','takeProfit','relativeStopLoss','relativeTakeProfit'):
            if request.HasField(field) and (not response.order.HasField(field) or getattr(request,field)!=getattr(response.order,field)):
                raise BrokerError('Pending order protection not confirmed')
    store.audit('Pending order copied: '+follower['name']+' · Master order #'+str(order.orderId))
    return True

async def reconcile(engine,account,snapshot,connection):
    if account['role']=='master':
        live={o.orderId for o in snapshot.order}
        positions={p.positionId:p for p in snapshot.position}
        for order in snapshot.order:master_event(engine,account,order)
        for saved in store.rows('SELECT * FROM master_orders WHERE account_id=? AND status=1',(account['id'],)):
            if saved['order_id'] in live:continue
            details=await connection.request(M.ProtoOAOrderDetailsReq(ctidTraderAccountId=account['broker_id'],orderId=saved['order_id']))
            master_event(engine,account,details.order,positions.get(details.order.positionId))
            if details.order.orderStatus==2 and details.order.positionId not in positions:
                pid=details.order.positionId
                store.execute('''INSERT INTO master_positions VALUES(?,?,?,?,?,?,?,?,?,1,0)
                    ON CONFLICT(account_id,position_id) DO UPDATE SET closed=1,volume=0''',
                    (account['id'],pid,details.order.tradeData.volume,0,details.order.tradeData.tradeSide,None,None,details.order.executionPrice,details.order.utcLastUpdateTimestamp))
                for m in store.rows('SELECT id FROM mappings WHERE master_account=? AND master_position=?',(account['id'],pid)):engine.schedule(m['id'])
    else:
        for order in snapshot.order:follower_event(engine,account,order)
        live={o.orderId for o in snapshot.order}
        positions={p.positionId:p for p in snapshot.position}
        for m in store.rows("SELECT * FROM mappings WHERE follower_account=? AND master_order IS NOT NULL AND follower_order IS NOT NULL AND follower_position IS NULL AND status NOT IN ('closed','failed')",(account['id'],)):
            if m['follower_order'] in live:continue
            details=await connection.request(M.ProtoOAOrderDetailsReq(ctidTraderAccountId=account['broker_id'],orderId=m['follower_order']))
            follower_event(engine,account,details.order,positions.get(details.order.positionId))
            if details.order.orderStatus==2 and details.order.positionId not in positions:
                store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(m['id'],))
            if details.order.orderStatus in (2,3,4,5):
                store.execute("UPDATE commands SET status='reconciled',error=NULL WHERE mapping_id=? AND kind IN ('pending_amend','cancel') AND status IN ('sending','uncertain')",(m['id'],))
