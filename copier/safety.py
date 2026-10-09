"""Durable fail-closed interlock and independently scheduled liquidation worker."""
import asyncio
import json
import secrets
import time
from . import store
from .transport import M, BrokerError, UncertainExecution

DEFAULTS={'max_entries':3,'entry_window':10,'max_positions':5,'max_signal_age':5,
          'protection_timeout':5,'unprotected_timeout':30,'disconnect_timeout':10}

def policy():
    return {**DEFAULTS,**json.loads(store.setting('safety_policy','{}'))}

def trip(reason,account_id=None):
    """Atomic latch: repeat calls coalesce, new entries stop before any broker call."""
    with store.db() as c:
        c.execute("UPDATE settings SET value='true' WHERE key='paused'")
        c.execute("INSERT OR REPLACE INTO settings VALUES('safety_latched','true')")
        followers=c.execute("SELECT id FROM accounts WHERE role='follower'"+(' AND id=?' if account_id else ''),(account_id,) if account_id else ()).fetchall()
        for a in followers:
            c.execute("INSERT OR IGNORE INTO liquidations(account_id,reason,status,created) VALUES(?,?,'queued',?)",(a['id'],reason,time.time()))
        c.execute('INSERT INTO audit(event) VALUES(?)',('SAFETY STOP: '+reason,))

def guard(mapping):
    if store.setting('safety_latched')=='true' or store.setting('paused')!='false':
        raise EntryBlocked('New entries blocked')
    source=store.one('SELECT * FROM master_positions WHERE account_id=? AND position_id=?',(mapping['master_account'],mapping['master_position']))
    if not source or source['closed'] or time.time()-mapping['created']>policy()['max_signal_age']:
        raise EntryBlocked('Signal closed or too old')
    a=store.account(mapping['follower_account'])
    if store.one("SELECT 1 FROM account_removals WHERE account_id IN (?,?) AND status='queued'",(mapping['master_account'],mapping['follower_account'])):raise EntryBlocked('Account removal is pending')
    if not a or not a['enabled']:raise EntryBlocked('Slave disabled')
    # Reservations include pending/in-flight/ambiguous opens, surviving process restarts.
    p=policy()
    recent=store.one("SELECT COUNT(*) n FROM mappings WHERE follower_account=? AND created>=? AND created<=?",(a['id'],mapping['created']-p['entry_window'],mapping['created']))['n']
    count=store.one("SELECT COUNT(*) n FROM mappings WHERE follower_account=? AND status NOT IN ('closed','failed')",(a['id'],))['n']
    broker_count=store.one("SELECT COUNT(*) n FROM positions WHERE account_id=? AND status='open'",(a['id'],))['n']
    reservations=store.one("SELECT COUNT(*) n FROM mappings WHERE follower_account=? AND follower_position IS NULL AND status NOT IN ('closed','failed')",(a['id'],))['n']
    count=max(count,broker_count+reservations)
    if recent>p['max_entries'] or count>p['max_positions']:
        trip('Entry frequency or position limit exceeded',a['id'])
        raise EntryBlocked('Entry frequency or position limit exceeded')

class EntryBlocked(Exception):pass

class Watchdog:
    def __init__(self,engine):self.engine=engine;self.since={};self.tasks={}
    def expired(self,key,bad,seconds):
        name=json.dumps(key)
        if not bad:
            self.since.pop(key,None);store.execute('DELETE FROM safety_timers WHERE id=?',(name,));return False
        store.execute('INSERT OR IGNORE INTO safety_timers VALUES(?,?)',(name,time.time()))
        first=store.one('SELECT started FROM safety_timers WHERE id=?',(name,))['started']
        return time.time()-first>=seconds or time.monotonic()-self.since.setdefault(key,time.monotonic())>=seconds
    async def tick(self):
        p=policy();engine=self.engine
        for m in store.rows("SELECT * FROM mappings WHERE status NOT IN ('closed','failed')"):
            follower=store.account(m['follower_account']);master=store.account(m['master_account'])
            unhealthy=False
            for a in (master,follower):
                c=engine.connections.get(a['environment'])
                unhealthy=unhealthy or not c or not c.alive or a['id'] not in engine.ready or time.time()-getattr(c,'last_received',time.time())>25 or bool(a['updated'] and time.time()-a['updated']>30)
            reason='Unconfirmed broker command' if m['status']=='uncertain' else None
            if self.expired((m['id'],'connection'),unhealthy,p['disconnect_timeout']):reason='Broker/account connection failed or account data is stale'
            pos=store.one("SELECT * FROM positions WHERE account_id=? AND position_id=? AND status='open'",(follower['id'],m['follower_position']))
            src=store.one('SELECT * FROM master_positions WHERE account_id=? AND position_id=?',(master['id'],m['master_position']))
            if pos and src:
                mismatch=any(not m[k+'_override'] and pos[k]!=src[k] for k in ('sl','tp'))
                if self.expired((m['id'],'protection'),mismatch,p['protection_timeout']):reason='SL/TP not confirmed before deadline'
                if self.expired((m['id'],'unprotected'),pos['sl'] is None,p['unprotected_timeout']):reason='Slave without stop loss: protection deadline exceeded'
            if m['error'] and m['status']=='active':reason='Synchronization error: '+m['error']
            if reason and not store.one('SELECT 1 FROM liquidations WHERE account_id=?',(follower['id'],)):trip(reason,follower['id'])
        for a in store.rows('SELECT account_id FROM liquidations'):
            aid=a['account_id'];task=self.tasks.get(aid)
            if not task or task.done():
                if task:
                    try:task.result()
                    except Exception:store.execute("UPDATE liquidations SET status='waiting',error='Protection request will be checked again' WHERE account_id=?",(aid,))
                self.tasks[aid]=asyncio.create_task(self.liquidate(aid))
        store.set_status('safety_heartbeat',time.time())
    async def liquidate(self,aid):
        e=self.engine;a=store.account(aid);c=e.connections.get(a['environment'])
        if not e.trading_enabled():
            store.execute("UPDATE liquidations SET status='waiting',error='ENGINE_TRADING_ENABLED=false: trading commands disabled' WHERE account_id=?",(aid,));return
        if not c or not c.alive or (aid not in e.ready and aid not in e.authenticated):
            store.execute("UPDATE liquidations SET status='waiting',error='Broker disconnected: close waits for reconnection' WHERE account_id=?",(aid,));return
        # Normal copies finish their acknowledgement first. The latch already blocks queued opens.
        running=[t for mid,t in e.sync_tasks.items() if not t.done() and store.one('SELECT 1 FROM mappings WHERE id=? AND follower_account=?',(mid,aid))]
        if running:return
        snapshot=await c.request(M.ProtoOAReconcileReq(ctidTraderAccountId=a['broker_id']),timeout=5)
        open_ids={p.positionId for p in snapshot.position}
        for pos in snapshot.position:e.save_position(a,pos)
        for m in store.rows('SELECT id,follower_position FROM mappings WHERE follower_account=?',(aid,)):
            if m['follower_position'] and m['follower_position'] not in open_ids:
                store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(m['id'],))
                store.execute("UPDATE commands SET status='reconciled' WHERE mapping_id=? AND kind!='open' AND status IN ('sending','uncertain')",(m['id'],))
        store.set_status('reconciled:'+aid,time.time())
        order_ids={o.orderId for o in snapshot.order}
        for action in store.rows("SELECT * FROM safety_actions WHERE account_id=? AND status IN ('sending','uncertain')",(aid,)):
            absent=action['target'] not in (open_ids if action['kind']=='close' else order_ids)
            if absent:store.execute("UPDATE safety_actions SET status='confirmed' WHERE id=?",(action['id'],))
        # Cancel pending orders first; any fill racing cancellation is found on the next pass.
        targets=[('cancel',o.orderId,M.ProtoOACancelOrderReq(ctidTraderAccountId=a['broker_id'],orderId=o.orderId)) for o in snapshot.order]
        targets += [('close',p.positionId,M.ProtoOAClosePositionReq(ctidTraderAccountId=a['broker_id'],positionId=p.positionId,volume=p.tradeData.volume)) for p in snapshot.position]
        for kind,target,request in targets:
            failures=store.one("SELECT COUNT(*) n FROM safety_actions WHERE account_id=? AND kind=? AND target=? AND status='rejected'",(aid,kind,target))['n']
            if failures>=3:
                store.execute("UPDATE liquidations SET status='blocked',error='Broker repeatedly rejected protection request; close manually in cTrader' WHERE account_id=?",(aid,));return
            ambiguous=store.one("SELECT 1 FROM safety_actions WHERE account_id=? AND kind=? AND target=? AND status IN ('sending','uncertain')",(aid,kind,target))
            normal_ambiguous=store.one("SELECT 1 FROM commands c JOIN mappings m ON m.id=c.mapping_id WHERE m.follower_account=? AND m.follower_position=? AND c.kind='close' AND c.status IN ('sending','uncertain')",(aid,target)) if kind=='close' else None
            if ambiguous or normal_ambiguous:
                store.execute("UPDATE liquidations SET status='uncertain',error='Unconfirmed close request: no blind retries' WHERE account_id=?",(aid,));continue
            action=secrets.token_hex(12)
            store.execute('INSERT INTO safety_actions VALUES(?,?,?,?,?,?,?,?)',(action,aid,kind,target,'sending',None,time.time(),None))
            try:
                response=await c.request(request,trade=True,timeout=5,emergency=True,expected_remaining=0 if kind=='close' else None)
                if kind=='close':
                    if not response.HasField('position'):raise UncertainExecution('Position confirmation missing')
                    e.save_position(a,response.position)
                    if response.position.positionStatus==2:store.execute("UPDATE mappings SET status='closed',error=NULL WHERE follower_account=? AND follower_position=?",(aid,target))
                store.execute("UPDATE safety_actions SET status='confirmed',completed=? WHERE id=?",(time.time(),action))
            except BrokerError as exc:
                store.execute("UPDATE safety_actions SET status='rejected',error=?,completed=? WHERE id=?",(str(exc),time.time(),action))
                store.execute("UPDATE liquidations SET status='waiting',error=? WHERE account_id=?",('Broker rejected protection request: '+str(exc),aid))
                return
            except (Exception,asyncio.CancelledError):
                store.execute("UPDATE safety_actions SET status='uncertain',error='Broker confirmation missing' WHERE id=?",(action,))
                store.execute("UPDATE liquidations SET status='uncertain',error='Broker confirmation missing' WHERE account_id=?",(aid,));return
        # Only a NEW broker snapshot can confirm flat; late fills keep being checked while latched.
        if not targets:
            unresolved=store.one("SELECT 1 FROM commands c JOIN mappings m ON m.id=c.mapping_id WHERE m.follower_account=? AND c.kind='open' AND c.status IN ('sending','uncertain')",(aid,))
            store.execute("UPDATE liquidations SET status=?,error=?,verified=? WHERE account_id=?",('uncertain' if unresolved else 'flat','Uncertain entry: a late fill is possible' if unresolved else None,time.time(),aid))
        elif not store.one("SELECT 1 FROM safety_actions WHERE account_id=? AND status='uncertain'",(aid,)):
            store.execute("UPDATE liquidations SET status='closing' WHERE account_id=?",(aid,))
    async def run(self):
        while not self.engine.stop.is_set():
            try:
                await self.tick()
                store.set_status('safety_error',None)
            except Exception:
                store.set_status('safety_error','Protection monitor failed; new copies blocked')
                trip('Protection monitor error')
            await asyncio.sleep(1)
