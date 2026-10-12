"""Single-owner event-driven copier. No trading commands originate in the web process."""
import asyncio
import fcntl
import json
import logging
import os
import secrets
import signal
import time
from decimal import Decimal, ROUND_FLOOR
from . import store, oauth, safety, risk
from . import pending as pending_orders
from .transport import Connection, BrokerError, UncertainExecution, M, to_dict

log=logging.getLogger('copier')

def lots_to_volume(lots,symbol):
    lot=int(symbol.get('lotSize',0)); step=int(symbol.get('stepVolume',0))
    minimum=int(symbol.get('minVolume',0)); maximum=int(symbol.get('maxVolume',0))
    if not lot or not step or not minimum or not maximum: raise ValueError('Broker symbol metadata lacks complete volume rules.')
    volume=int((Decimal(str(lots))*lot/step).to_integral_value(rounding=ROUND_FLOOR))*step
    if not minimum<=volume<=maximum: raise ValueError('Selected lots exceed broker volume limits.')
    return volume

def remaining_volume(initial_follower,remaining_master,initial_master,step):
    if remaining_master<=0:return 0
    return int(Decimal(initial_follower)*remaining_master/initial_master/step)*step

class Engine:
    def __init__(self,connection_factory=Connection):
        self.connection_factory=connection_factory; self.connections={};self.authenticated={};self.grants={}
        self.events=asyncio.Queue();self.sync_tasks={};self.dirty=set();self.ready=set();self.started={}
        self.stop=asyncio.Event();self.discovered_versions={};self.history_time={};self.history_cursor={}
        self.account_errors={};self.reconciling=set();self.buffered={};self.reconcile_locks={}
    def event(self,environment,message):
        self.events.put_nowait((environment,message))
    def conn(self,account):
        c=self.connections.get(account['environment'])
        if not c or not c.alive or account['id'] not in self.ready: raise ConnectionError('Account not synchronized')
        return c
    async def ensure_connections(self):
        tokens=store.rows('SELECT id,expires FROM oauth_tokens')
        if not tokens:return
        environments={'live'}|{a['environment'] for a in store.rows('SELECT environment FROM accounts')}
        for environment in environments:
            old=self.connections.get(environment)
            if old and old.alive:continue
            if old:await old.close()
            self.ready={i for i in self.ready if store.account(i) and store.account(i)['environment']!=environment}
            self.authenticated={i:v for i,v in self.authenticated.items() if store.account(i) and store.account(i)['environment']!=environment}
            connection=self.connection_factory(environment,self.event)
            try:
                client,secret=oauth.credentials();await connection.connect(client,secret)
                self.connections[environment]=connection
                store.set_status(environment,'connected')
            except Exception:
                await connection.close();store.set_status(environment,'disconnected')
                raise
        for token in tokens:
            try:
                data=oauth.load_token(token['id'])
                if token['expires']<time.time()+120:
                    data=await asyncio.to_thread(oauth.token_request,grant_type='refresh_token',refresh_token=data['refreshToken'])
                    oauth.save_token(data,token['id']);data=oauth.load_token(token['id'])
                access=data['accessToken'];self.grants[token['id']]=access
                if self.discovered_versions.get(token['id'])==access:continue
                response=await self.connections['live'].request(M.ProtoOAGetAccountListByAccessTokenReq(accessToken=access))
                if response.permissionScope!=1: # SCOPE_TRADE = 1
                    raise ValueError('cTrader authorization does not have trading permission.')
                for a in response.ctidTraderAccount:
                    environment='live' if a.isLive else 'demo';aid=environment+':'+str(a.ctidTraderAccountId)
                    store.execute('INSERT OR REPLACE INTO discovered VALUES(?,?,?,?,?,?)',(aid,a.ctidTraderAccountId,environment,token['id'],str(a.traderLogin),a.brokerTitleShort))
                    store.execute('UPDATE accounts SET token_id=? WHERE id=?',(token['id'],aid))
                self.discovered_versions[token['id']]=access
                store.set_status('oauth_error',None)
            except Exception as exc:
                # Error class only; access tokens never enter logs.
                store.set_status('oauth_error',str(exc) if isinstance(exc,ValueError) else 'Could not load account authorization.')
    async def initialize_account(self,account):
        aid=account['id']
        if not store.account(aid):return
        connection=self.connections.get(account['environment']);access=self.grants.get(account['token_id'])
        if not connection or not connection.alive or not access:return
        if self.authenticated.get(aid)==access and aid in self.ready and account['status']=='connected':return
        self.ready.discard(aid);self.started[aid]=int(time.time()*1000)
        if self.authenticated.get(aid)!=access:
            try:
                await connection.request(M.ProtoOAAccountAuthReq(ctidTraderAccountId=account['broker_id'],accessToken=access))
            except BrokerError as exc:
                # This account is already authorized on this connection. Still
                # verify trader data and reconcile before marking it ready.
                if str(exc)!='ALREADY_LOGGED_IN':raise
            self.authenticated[aid]=access
        trader=(await connection.request(M.ProtoOATraderReq(ctidTraderAccountId=account['broker_id']))).trader
        if trader.accountType!=0:raise ValueError('Only hedging accounts are supported; netting can merge positions.')
        if trader.isLimitedRisk:raise ValueError('This account requires guaranteed protection and does not support entry without SL.')
        assets=await connection.request(M.ProtoOAAssetListReq(ctidTraderAccountId=account['broker_id']))
        currency=next((a.name for a in assets.asset if a.assetId==trader.depositAssetId),None)
        if not currency:raise ValueError('Could not determine account currency.')
        symbols=await connection.request(M.ProtoOASymbolsListReq(ctidTraderAccountId=account['broker_id']))
        normalize=lambda x:x.upper().replace('/','').replace(' ','')
        match=[s for s in symbols.symbol if normalize(s.symbolName)==normalize(account['symbol_name'])]
        if len(match)!=1:raise ValueError('XAU/USD symbol is ambiguous or missing. Enter the exact broker symbol in account settings.')
        # Check semantic asset pair, not just an arbitrary editable symbol name.
        asset_names={a.assetId:a.name.upper() for a in assets.asset}
        light=match[0]
        if asset_names.get(light.baseAssetId)!='XAU' or asset_names.get(light.quoteAssetId)!='USD':
            raise ValueError('The selected symbol is not XAU/USD.')
        symbol=(await connection.request(M.ProtoOASymbolByIdReq(ctidTraderAccountId=account['broker_id'],symbolId=[light.symbolId]))).symbol[0]
        if account['role']=='follower':lots_to_volume(account['lots'],to_dict(symbol))
        digits=trader.moneyDigits if trader.HasField('moneyDigits') else 2
        store.execute("UPDATE accounts SET currency=?,balance=?,symbol_id=?,symbol_json=?,account_type=?,status='connected',error=NULL WHERE id=?",(currency,trader.balance/(10**digits),symbol.symbolId,json.dumps(to_dict(symbol)),trader.accountType,aid))
        self.authenticated[aid]=access
        await self.reconcile(store.account(aid),connection)
        self.ready.add(aid)
        store.audit('cTrader connected: '+account['name'])
        for mapping in store.rows("SELECT id FROM mappings WHERE (follower_account=? OR master_account=?) AND status NOT IN ('closed','failed')",(aid,aid)):
            self.schedule(mapping['id'])
    def save_position(self,account,p):
        if p.positionStatus not in (1,2):return False
        d=to_dict(p);td=d['tradeData'];pid=int(d['positionId']);closed=d.get('positionStatus',1)!=1
        updated=int(d.get('utcLastUpdateTimestamp',td.get('openTimestamp',0)))
        old=store.one('SELECT updated,status FROM positions WHERE account_id=? AND position_id=?',(account['id'],pid))
        if old and ((updated<old['updated'] and not closed) or (old['status']=='closed' and not closed)):return False
        if old and closed:updated=max(updated,old['updated'])
        store.execute('''INSERT INTO positions(account_id,position_id,volume,side,price,sl,tp,label,opened,updated,status,symbol_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,position_id) DO UPDATE SET
            volume=excluded.volume,side=excluded.side,price=excluded.price,sl=excluded.sl,tp=excluded.tp,label=excluded.label,
            updated=excluded.updated,status=excluded.status,symbol_id=excluded.symbol_id''',
            (account['id'],pid,0 if closed else int(td['volume']),td['tradeSide'],d.get('price'),d.get('stopLoss'),d.get('takeProfit'),td.get('label',''),int(td.get('openTimestamp',0)),updated,'closed' if closed else 'open',int(td['symbolId'])))
        return True
    def update_master(self,account,p,allow_new=False):
        if p.positionStatus not in (1,2) or p.tradeData.symbolId!=account['symbol_id']:return
        pid=p.positionId;old=store.one('SELECT * FROM master_positions WHERE account_id=? AND position_id=?',(account['id'],pid))
        stamp=p.utcLastUpdateTimestamp or p.tradeData.openTimestamp
        if old and stamp<old['updated']:
            if p.positionStatus==2:stamp=old['updated']
            else:return
        if old and old['closed'] and p.positionStatus==1:return
        closed=p.positionStatus!=1;volume=0 if closed else p.tradeData.volume
        eligible=old['eligible'] if old else int(allow_new and not closed and store.setting('paused')=='false' and self.trading_enabled() and time.time()*1000-stamp<=safety.policy()['max_signal_age']*1000)
        initial=max(volume,old['initial_volume'] if old else volume)
        store.execute('''INSERT INTO master_positions VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id,position_id) DO UPDATE SET
            initial_volume=excluded.initial_volume,volume=excluded.volume,side=excluded.side,sl=excluded.sl,tp=excluded.tp,
            price=excluded.price,updated=excluded.updated,closed=excluded.closed''',
            (account['id'],pid,initial,volume,p.tradeData.tradeSide,p.stopLoss if p.HasField('stopLoss') else None,p.takeProfit if p.HasField('takeProfit') else None,p.price,stamp,int(closed),eligible))
        if not old and eligible:
            for follower in store.rows("SELECT * FROM accounts WHERE role='follower' AND enabled=1"):
                if follower['id'] not in self.ready:
                    store.audit('Copy skipped: '+follower['name']+' is not synchronized.');continue
                try:volume_f=lots_to_volume(follower['lots'],json.loads(follower['symbol_json']))
                except ValueError as exc:store.audit(follower['name']+': '+str(exc));continue
                mid=secrets.token_hex(12);label='gimm:'+mid
                store.execute('''INSERT OR IGNORE INTO mappings(id,master_account,master_position,follower_account,initial_master,initial_follower,label,status,created)
                    VALUES(?,?,?,?,?,?,?,?,?)''',(mid,account['id'],pid,follower['id'],initial,volume_f,label,'pending',time.time()))
        store.execute('UPDATE mappings SET initial_master=MAX(initial_master,?) WHERE master_account=? AND master_position=?',(initial,account['id'],pid))
        for mapping in store.rows("SELECT id FROM mappings WHERE master_account=? AND master_position=? AND status NOT IN ('closed','failed')",(account['id'],pid)):
            self.schedule(mapping['id'])
    @staticmethod
    def trading_enabled():return os.environ.get('ENGINE_TRADING_ENABLED','true').lower()=='true'
    def schedule(self,mid):
        self.dirty.add(mid)
        task=self.sync_tasks.get(mid)
        if not task or task.done():self.sync_tasks[mid]=asyncio.create_task(self.sync_loop(mid))
    async def sync_loop(self,mid):
        try:
            while mid in self.dirty:
                self.dirty.discard(mid)
                await self.sync_mapping(mid)
        except safety.EntryBlocked as exc:
            store.execute("UPDATE mappings SET status='closed',error=? WHERE id=?",(str(exc),mid))
        except (ConnectionError,UncertainExecution):
            pass # reconnect/reconcile will schedule again; uncertain commands remain blocked.
        except Exception as exc:
            message=str(exc) if isinstance(exc,(ValueError,BrokerError)) else 'Internal synchronization error: '+type(exc).__name__
            store.execute('UPDATE mappings SET error=? WHERE id=?',(message,mid));store.audit('Synchronization failed: '+mid+' · '+message)
            log.error('Mapping %s: %s',mid,type(exc).__name__)
            m=store.one('SELECT * FROM mappings WHERE id=?',(mid,))
            if m and (m['follower_position'] or m['follower_order']):safety.trip(message,m['follower_account'])
    async def command(self,mapping,kind,message):
        def entry_guard():
            safety.guard(mapping)
            for aid in (mapping['master_account'],mapping['follower_account']):
                a=store.account(aid);c=self.connections.get(a['environment'])
                if aid not in self.ready or not c or not c.alive or time.time()-getattr(c,'last_received',time.time())>25:
                    raise safety.EntryBlocked('Required broker connection is not healthy')
            if hasattr(self,'watchdog'):
                heartbeat=store.one("SELECT value FROM engine_status WHERE key='safety_heartbeat'")
                if not heartbeat or time.time()-json.loads(heartbeat['value'])>5:raise safety.EntryBlocked('Protection monitor is not current')
        if kind=='open':entry_guard()
        command_id=secrets.token_hex(12)
        store.execute('INSERT INTO commands VALUES(?,?,?,?,?,?,?,?)',(command_id,mapping['id'],kind,json.dumps(to_dict(message)),'sending',None,time.time(),None))
        try:
            # Commands are serialized per mapping; other followers run independently.
            response=await self.conn(store.account(mapping['follower_account'])).request(message,trade=True,timeout=5,guard=entry_guard if kind=='open' else None,expected_remaining=(store.one('SELECT close_target FROM mappings WHERE id=?',(mapping['id'],)) or {}).get('close_target') if kind=='close' else None)
        except safety.EntryBlocked:
            store.execute("UPDATE commands SET status='cancelled',completed=? WHERE id=?",(time.time(),command_id))
            raise
        except BrokerError as exc:
            store.execute("UPDATE commands SET status='rejected',error=?,completed=? WHERE id=?",(str(exc),time.time(),command_id))
            if kind in ('cancel','pending_amend') and str(exc) in ('ORDER_NOT_FOUND','POSITION_NOT_FOUND'):
                await self.reconcile(store.account(mapping['follower_account']))
                actual=store.one('SELECT * FROM mappings WHERE id=?',(mapping['id'],))
                if actual['status']=='closed' or actual['follower_position']:
                    store.execute("UPDATE commands SET status='reconciled',error=NULL WHERE id=?",(command_id,))
                    self.dirty.add(mapping['id'])
                    return None
            # A broker-side SL/TP can close the position between our snapshot
            # and a close/amend request. Prove absence before treating it as done.
            if kind in ('close','amend') and str(exc)=='POSITION_NOT_FOUND':
                follower=store.account(mapping['follower_account'])
                snapshot=await self.reconcile(follower)
                if not any(p.positionId==mapping['follower_position'] for p in snapshot.position):
                    store.execute("UPDATE commands SET status='reconciled',error=NULL WHERE id=?",(command_id,))
                    store.audit('Copy already closed by broker: '+follower['name'])
                    return None
            raise
        except (Exception,asyncio.CancelledError):
            store.execute("UPDATE commands SET status='uncertain',error='Broker confirmation pending',completed=? WHERE id=?",(time.time(),command_id))
            store.execute("UPDATE mappings SET status='uncertain',error='Uncertain command; reconciliation required' WHERE id=?",(mapping['id'],))
            raise UncertainExecution('Uncertain broker command') from None
        store.execute("UPDATE commands SET status='confirmed',completed=? WHERE id=?",(time.time(),command_id))
        return response
    async def sync_mapping(self,mid):
        mapping=store.one('SELECT * FROM mappings WHERE id=?',(mid,))
        if not mapping or mapping['status'] in ('closed','failed'):return
        if store.one('SELECT 1 FROM liquidations WHERE account_id=?',(mapping['follower_account'],)):return
        follower=store.account(mapping['follower_account']);master=store.account(mapping['master_account'])
        if not self.trading_enabled():return
        connection=self.conn(follower)
        source=store.one('SELECT * FROM master_positions WHERE account_id=? AND position_id=?',(mapping['master_account'],mapping['master_position']))
        if not source:return
        if master['id'] not in self.ready and not source['closed']:return
        if mapping['status']=='uncertain' and not source['closed']:return
        if mapping['master_order'] and not mapping['follower_position'] and store.one("SELECT 1 FROM commands WHERE mapping_id=? AND status IN ('sending','uncertain')",(mid,)):return
        if source['closed'] and store.one("SELECT 1 FROM commands WHERE mapping_id=? AND kind='close' AND status IN ('sending','uncertain')",(mid,)):return
        if await pending_orders.sync(self,mapping,source,follower):return
        if not mapping['follower_position']:
            # Never open a previously paused or already-closed signal after a restart.
            if source['closed'] or store.setting('paused')!='false' or not follower['enabled']:
                store.execute("UPDATE mappings SET status='closed',error='Entry closed or paused before execution' WHERE id=?",(mid,));return
            store.execute("UPDATE mappings SET status='opening' WHERE id=?",(mid,))
            request=M.ProtoOANewOrderReq(ctidTraderAccountId=follower['broker_id'],symbolId=follower['symbol_id'],orderType=1,tradeSide=source['side'],volume=mapping['initial_follower'],label=mapping['label'],clientOrderId=mid)
            # Master protection is included as relative distance where possible, then set to absolute master prices after fill.
            sign=1 if source['side']==1 else -1
            for field,key in [('relativeStopLoss','sl'),('relativeTakeProfit','tp')]:
                if source[key] is not None:
                    distance=(source['price']-source[key])*sign if key=='sl' else (source[key]-source['price'])*sign
                    if distance>0:setattr(request,field,max(1,round(distance*100000)))
            started=time.monotonic()
            try:response=await self.command(mapping,'open',request)
            except BrokerError as exc:
                store.execute("UPDATE mappings SET status='failed',error=? WHERE id=?",(str(exc),mid));raise
            if not response.HasField('position'):
                store.execute("UPDATE mappings SET status='uncertain',error='Execution without position mapping' WHERE id=?",(mid,));return
            self.save_position(follower,response.position)
            store.execute("UPDATE mappings SET follower_position=?,initial_follower=?,status='active',error=NULL,latency_ms=? WHERE id=?",(response.position.positionId,response.position.tradeData.volume,(time.monotonic()-started)*1000,mid))
            store.audit('Trade copied: '+follower['name']+' · Master #'+str(source['position_id']))
            self.dirty.add(mid);return
        # Position was closed independently: never resurrect it.
        position=store.one('SELECT * FROM positions WHERE account_id=? AND position_id=?',(follower['id'],mapping['follower_position']))
        if not position or position['status']=='closed':
            store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(mid,));return
        symbol=json.loads(follower['symbol_json']);step=int(symbol['stepVolume']);minimum=int(symbol['minVolume'])
        target=remaining_volume(mapping['initial_follower'],source['volume'],mapping['initial_master'],step)
        # A positive residual below broker minimum is retained until the master closes.
        if source['volume']>0 and target<minimum:target=minimum
        delta=position['volume']-target
        if delta>0 and (target==0 or delta>=minimum):
            store.execute('UPDATE mappings SET close_target=? WHERE id=?',(target,mid))
            response=await self.command(mapping,'close',M.ProtoOAClosePositionReq(ctidTraderAccountId=follower['broker_id'],positionId=mapping['follower_position'],volume=delta))
            if response is None:return
            if response.HasField('position'):self.save_position(follower,response.position)
            else:
                # Never assume a close filled if the response lacks its resulting position.
                store.execute("UPDATE mappings SET status='uncertain',error='Close without position confirmation' WHERE id=?",(mid,));return
            actual=store.one('SELECT * FROM positions WHERE account_id=? AND position_id=?',(follower['id'],mapping['follower_position']))
            if actual['status']=='closed' or actual['volume']==0:
                store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(mid,))
                store.execute("UPDATE commands SET status='reconciled',error=NULL WHERE mapping_id=? AND kind='amend' AND status='uncertain'",(mid,))
            else:self.dirty.add(mid)
            return
        if source['closed']:return
        desired={k:position[k] if mapping[k+'_override'] else source[k] for k in ('sl','tp')}
        if position['sl']!=desired['sl'] or position['tp']!=desired['tp']:
            request=M.ProtoOAAmendPositionSLTPReq(ctidTraderAccountId=follower['broker_id'],positionId=mapping['follower_position'],trailingStopLoss=False)
            # Omitted optional prices remove protection; always send the complete desired SL/TP pair.
            if desired['sl'] is not None:request.stopLoss=desired['sl']
            if desired['tp'] is not None:request.takeProfit=desired['tp']
            response=await self.command(mapping,'amend',request)
            if response is None:return
            if response.HasField('position'):
                self.save_position(follower,response.position)
                confirmed=store.one('SELECT sl,tp FROM positions WHERE account_id=? AND position_id=?',(follower['id'],mapping['follower_position']))
                if confirmed['sl']!=desired['sl'] or confirmed['tp']!=desired['tp']:
                    store.execute("UPDATE mappings SET error='Broker protection prices differ from requested prices' WHERE id=?",(mid,));return
            else:
                store.execute("UPDATE mappings SET status='uncertain',error='SL/TP without position confirmation' WHERE id=?",(mid,));return
            store.execute('UPDATE mappings SET sl=?,tp=?,error=NULL WHERE id=?',(desired['sl'],desired['tp'],mid))
            # The master could have moved while the broker was processing the amend.
            latest=store.one('SELECT updated,closed,volume,sl,tp FROM master_positions WHERE account_id=? AND position_id=?',(master['id'],source['position_id']))
            if any(latest[k]!=source[k] for k in latest):self.dirty.add(mid)
        store.execute('UPDATE mappings SET protection_initialized=1,sl=?,tp=? WHERE id=?',(desired['sl'],desired['tp'],mid))
    def detect_override(self,account,p):
        if account['role']!='follower' or p.positionStatus!=1:return
        m=store.one('SELECT * FROM mappings WHERE follower_account=? AND follower_position=? AND protection_initialized=1',(account['id'],p.positionId))
        if not m:return
        previous=store.one('SELECT * FROM positions WHERE account_id=? AND position_id=?',(account['id'],p.positionId))
        stamp=p.utcLastUpdateTimestamp or p.tradeData.openTimestamp
        if previous and stamp<previous['updated']:return
        pending=store.one("SELECT 1 FROM commands WHERE mapping_id=? AND kind='amend' AND status IN ('sending','uncertain')",(m['id'],))
        if pending:return
        values={k:getattr(p,field) if p.HasField(field) else None for k,field in [('sl','stopLoss'),('tp','takeProfit')]}
        changed=[k for k in values if not m[k+'_override'] and values[k]!=m[k]]
        if changed:
            store.execute('UPDATE mappings SET '+','.join(k+'_override=1' for k in changed)+' WHERE id=?',(m['id'],))
            store.audit('Manual slave protection remains independent: '+account['name']+' · '+','.join(changed).upper())
    def request_reconcile(self,account):
        if not hasattr(self,'recovery_tasks'):self.recovery_tasks={}
        task=self.recovery_tasks.get(account['id'])
        if task and not task.done():return
        async def recover():
            try:await self.reconcile(store.account(account['id']))
            except Exception:store.audit('Position reconciliation pending: '+account['name'])
        self.recovery_tasks[account['id']]=asyncio.create_task(recover())
    def save_deal(self,account,deal):
        if deal.dealStatus not in (2,3):return
        closing=deal.HasField('closePositionDetail');detail=deal.closePositionDetail
        digits=deal.moneyDigits if deal.HasField('moneyDigits') else 2
        dd=detail.moneyDigits if detail.HasField('moneyDigits') else digits
        # Brokers report conversion charges with either sign. They are a cost,
        # never a rebate added to trading profit.
        net=(detail.grossProfit+detail.swap+detail.commission-abs(detail.pnlConversionFee))/(10**dd) if closing else None
        store.execute('INSERT OR REPLACE INTO deals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(account['id'],deal.dealId,deal.positionId,deal.orderId,deal.symbolId,deal.executionTimestamp,deal.tradeSide,deal.filledVolume,deal.executionPrice,net,deal.commission/(10**digits),detail.swap/(10**dd) if closing else 0,int(closing),account['currency']))
    async def consume(self):
        while not self.stop.is_set():
            environment,message=await self.events.get()
            try:
                if isinstance(message,(M.ProtoOAClientDisconnectEvent,M.ProtoOAAccountDisconnectEvent,M.ProtoOAAccountsTokenInvalidatedEvent)):
                    self.connections[environment].alive=False;continue
                account_id=getattr(message,'ctidTraderAccountId',None)
                account=store.one('SELECT * FROM accounts WHERE broker_id=? AND environment=?',(account_id,environment)) if account_id else None
                if not account:continue
                if account['id'] in self.reconciling:
                    self.buffered.setdefault(account['id'],[]).append((environment,message));continue
                if isinstance(message,M.ProtoOAExecutionEvent):
                    if message.HasField('order'):
                        pos=message.position if message.HasField('position') else None
                        if account['role']=='master':
                            fresh=account['id'] in self.ready and message.executionType==2 and (message.order.utcLastUpdateTimestamp or message.order.tradeData.openTimestamp)>=self.started.get(account['id'],0)
                            pending_orders.master_event(self,account,message.order,pos,allow_new=fresh)
                        else:pending_orders.follower_event(self,account,message.order,pos)
                    if message.HasField('deal'):
                        self.save_deal(account,message.deal)
                        if message.deal.HasField('closePositionDetail'):self.request_reconcile(account)
                    if message.HasField('position') and (message.executionType in (3,4,9,11) or message.position.positionStatus==2 or (message.executionType==2 and store.one('SELECT 1 FROM positions WHERE account_id=? AND position_id=?',(account['id'],message.position.positionId)))):
                        if message.position.positionStatus==2 and message.HasField('deal'):
                            message.position.utcLastUpdateTimestamp=max(message.position.utcLastUpdateTimestamp,message.deal.executionTimestamp)
                        self.detect_override(account,message.position)
                        if not self.save_position(account,message.position):
                            if message.position.positionStatus==2:self.request_reconcile(account)
                            continue
                        if account['role']=='master':
                            signal_time=message.deal.executionTimestamp if message.HasField('deal') else message.position.utcLastUpdateTimestamp
                            fresh=account['id'] in self.ready and message.executionType in (3,11) and signal_time>=self.started.get(account['id'],0)
                            self.update_master(account,message.position,allow_new=fresh)
                        else:
                            p=message.position
                            mapping=store.one('SELECT * FROM mappings WHERE follower_account=? AND (follower_position=? OR label=?)',(account['id'],p.positionId,p.tradeData.label))
                            if mapping:
                                if not mapping['follower_position']:
                                    store.execute("UPDATE mappings SET follower_position=?,status='active',error=NULL WHERE id=?",(p.positionId,mapping['id']))
                                if p.positionStatus in (1,2):
                                    store.execute("UPDATE commands SET status='reconciled',completed=? WHERE mapping_id=? AND kind='open' AND status='uncertain'",(time.time(),mapping['id']))
                                self.schedule(mapping['id'])
                elif isinstance(message,M.ProtoOATrailingSLChangedEvent):
                    source=store.one('SELECT * FROM master_positions WHERE account_id=? AND position_id=?',(account['id'],message.positionId))
                    if source and message.utcLastUpdateTimestamp>=source['updated']:
                        store.execute('UPDATE master_positions SET sl=?,updated=? WHERE account_id=? AND position_id=?',(message.stopPrice,message.utcLastUpdateTimestamp,account['id'],message.positionId))
                        store.execute('UPDATE positions SET sl=?,updated=? WHERE account_id=? AND position_id=?',(message.stopPrice,message.utcLastUpdateTimestamp,account['id'],message.positionId))
                        for mapping in store.rows('SELECT id FROM mappings WHERE master_account=? AND master_position=?',(account['id'],message.positionId)):self.schedule(mapping['id'])
            except Exception as exc:
                log.error('Event processing failed: %s',type(exc).__name__)
                store.audit('Could not process broker event: '+type(exc).__name__)
            finally:self.events.task_done()
    async def reconcile(self,account,connection=None):
        lock=self.reconcile_locks.setdefault(account['id'],asyncio.Lock())
        async with lock:
            if store.account(account['id']):return await self._reconcile(account,connection)
    async def _reconcile(self,account,connection=None):
        aid=account['id'];connection=connection or self.conn(account)
        self.reconciling.add(aid)
        try:
            response=await connection.request(M.ProtoOAReconcileReq(ctidTraderAccountId=account['broker_id']))
            live_ids={p.positionId for p in response.position}
            margin=sum(p.usedMargin/(10**(p.moneyDigits if p.HasField('moneyDigits') else 2)) for p in response.position)
            store.execute('UPDATE accounts SET margin=? WHERE id=?',(margin,aid))
            store.set_status('reconciled:'+aid,time.time())
            await pending_orders.reconcile(self,account,response,connection)
            for p in response.position:
                self.detect_override(account,p)
                self.save_position(account,p)
                if account['role']=='master':self.update_master(account,p,allow_new=False)
                else:
                    mapping=store.one('SELECT * FROM mappings WHERE follower_account=? AND label=?',(aid,p.tradeData.label))
                    pending_open=store.one("SELECT id FROM commands WHERE mapping_id=? AND kind='open' AND status IN ('sending','uncertain')",(mapping['id'],)) if mapping else None
                    if mapping and (not mapping['follower_position'] or pending_open):
                        store.execute("UPDATE mappings SET follower_position=?,status='active',error=NULL WHERE id=?",(p.positionId,mapping['id']))
                        store.execute("UPDATE commands SET status='reconciled',completed=? WHERE mapping_id=? AND kind='open' AND status IN ('sending','uncertain')",(time.time(),mapping['id']))
            for p in store.rows("SELECT * FROM positions WHERE account_id=? AND status='open'",(aid,)):
                if p['position_id'] not in live_ids:
                    store.execute("UPDATE positions SET status='closed',volume=0 WHERE account_id=? AND position_id=?",(aid,p['position_id']))
            if account['role']=='master':
                for source in store.rows('SELECT * FROM master_positions WHERE account_id=? AND closed=0 AND position_id>0',(aid,)):
                    if source['position_id'] not in live_ids:
                        store.execute('UPDATE master_positions SET closed=1,volume=0 WHERE account_id=? AND position_id=?',(aid,source['position_id']))
                        for mapping in store.rows('SELECT id FROM mappings WHERE master_account=? AND master_position=?',(aid,source['position_id'])):self.schedule(mapping['id'])
            else:
                for mapping in store.rows("SELECT * FROM mappings WHERE follower_account=?",(aid,)):
                    if mapping['follower_position'] and mapping['follower_position'] not in live_ids:
                        store.execute("UPDATE mappings SET status='closed',error=NULL WHERE id=?",(mapping['id'],))
                        store.execute("UPDATE commands SET status='reconciled',completed=? WHERE mapping_id=? AND status='uncertain'",(time.time(),mapping['id']))
                    elif mapping['status']=='uncertain' and mapping['follower_position']:
                        pending=store.one("SELECT * FROM commands WHERE mapping_id=? AND status IN ('sending','uncertain') ORDER BY created DESC LIMIT 1",(mapping['id'],))
                        actual=store.one('SELECT * FROM positions WHERE account_id=? AND position_id=?',(aid,mapping['follower_position']))
                        if pending and actual:
                            payload=json.loads(pending['payload']);resolved=pending['kind']=='amend' and actual['sl']==payload.get('stopLoss') and actual['tp']==payload.get('takeProfit')
                            resolved=resolved or (pending['kind']=='close' and mapping['close_target'] is not None and actual['volume']<=mapping['close_target'])
                            if resolved:
                                store.execute("UPDATE commands SET status='reconciled',completed=? WHERE id=?",(time.time(),pending['id']))
                                store.execute("UPDATE mappings SET status='active',error=NULL WHERE id=?",(mapping['id'],))
                    self.schedule(mapping['id'])
            return response
        finally:
            self.reconciling.discard(aid)
            for item in self.buffered.pop(aid,[]):self.events.put_nowait(item)
    async def snapshot(self,account):
        connection=self.conn(account);aid=account['id']
        trader=(await connection.request(M.ProtoOATraderReq(ctidTraderAccountId=account['broker_id']))).trader
        pnls=await connection.request(M.ProtoOAGetPositionUnrealizedPnLReq(ctidTraderAccountId=account['broker_id']))
        digits=trader.moneyDigits if trader.HasField('moneyDigits') else 2
        pd=pnls.moneyDigits if pnls.HasField('moneyDigits') else 2
        unrealized=sum(p.netUnrealizedPnL for p in pnls.positionUnrealizedPnL)/(10**pd)
        balance=trader.balance/(10**digits);equity=balance+unrealized
        for p in pnls.positionUnrealizedPnL:store.execute('UPDATE positions SET pnl=? WHERE account_id=? AND position_id=?',(p.netUnrealizedPnL/(10**pd),aid,p.positionId))
        store.execute("UPDATE accounts SET balance=?,equity=?,unrealized=?,updated=?,status='connected',error=NULL WHERE id=?",(balance,equity,unrealized,time.time(),aid))
        risk.record_day(aid,balance)
        store.execute('INSERT OR IGNORE INTO equity_samples VALUES(?,?,?,?)',(aid,int(time.time()//60)*60000,equity,balance))
    async def history(self,account):
        aid=account['id'];now=int(time.time()*1000)
        if time.time()-self.history_time.get(aid,0)<60:return
        latest=store.one('SELECT MAX(timestamp) AS timestamp FROM deals WHERE account_id=?',(aid,))['timestamp']
        start=self.history_cursor.get(aid) or max(0,(latest-86400000) if latest else now-90*86400000)
        end=min(now,start+7*86400000)
        response=await self.conn(account).request(M.ProtoOADealListReq(ctidTraderAccountId=account['broker_id'],fromTimestamp=start,toTimestamp=end,maxRows=1000),historical=True)
        for deal in response.deal:self.save_deal(account,deal)
        if response.hasMore:
            # Walk forward with overlap; deduplication is by broker deal ID.
            # cTrader can return newest first; halve saturated windows instead of skipping older records.
            if end-start>1000:
                self.history_cursor[aid]=start
                await self.history_window(account,start,end)
            else:raise ValueError('History window has too many deals; import is incomplete.')
        self.history_cursor[aid]=end if end<now else None
        self.history_time[aid]=time.time() if end>=now else 0
    async def history_window(self,account,start,end):
        if end-start<=1:raise ValueError('History import could not be fully paginated.')
        midpoint=(start+end)//2
        for lo,hi in [(start,midpoint),(midpoint,end)]:
            response=await self.conn(account).request(M.ProtoOADealListReq(ctidTraderAccountId=account['broker_id'],fromTimestamp=lo,toTimestamp=hi,maxRows=1000),historical=True)
            for deal in response.deal:self.save_deal(account,deal)
            if response.hasMore:await self.history_window(account,lo,hi)
    async def remove_accounts(self):
        for job in store.rows("SELECT * FROM account_removals WHERE status='queued'"):
            aid=job['account_id'];a=store.account(aid)
            if not a:store.execute('DELETE FROM account_removals WHERE account_id=?',(aid,));continue
            try:
                connection=self.conn(a)
                lock=self.reconcile_locks.setdefault(aid,asyncio.Lock())
                async with lock:
                    snapshot=await self._reconcile(a,connection)
                    if snapshot.position or snapshot.order:raise ValueError('Close all positions and cancel pending orders in cTrader before removing this account.')
                    with store.db() as c:
                        unresolved=c.execute("SELECT 1 FROM mappings m WHERE (m.master_account=? OR m.follower_account=?) AND (m.status NOT IN ('closed','failed') OR EXISTS(SELECT 1 FROM commands cmd WHERE cmd.mapping_id=m.id AND cmd.status IN ('sending','uncertain')))",(aid,aid)).fetchone()
                        uncertain=c.execute("SELECT 1 FROM safety_actions WHERE account_id=? AND status IN ('sending','uncertain')",(aid,)).fetchone()
                        if unresolved or uncertain:raise ValueError('Resolve active copy mappings and unconfirmed broker commands before removing this account.')
                        liquidation=c.execute('SELECT status FROM liquidations WHERE account_id=?',(aid,)).fetchone()
                        if liquidation and liquidation['status']!='flat':raise ValueError('Wait for the emergency stop to confirm this account is flat.')
                        c.execute('DELETE FROM accounts WHERE id=?',(aid,))
                        c.execute('DELETE FROM risk_limits WHERE account_id=?',(aid,));c.execute('DELETE FROM risk_days WHERE account_id=?',(aid,))
                        c.execute('DELETE FROM liquidations WHERE account_id=?',(aid,));c.execute('DELETE FROM account_removals WHERE account_id=?',(aid,))
                        c.execute("DELETE FROM engine_status WHERE key=?",('reconciled:'+aid,))
                        c.execute('INSERT INTO audit(event) VALUES(?)',('Account removed from copier: '+a['name']+'; broker account and recorded history retained.',))
                    self.ready.discard(aid);self.authenticated.pop(aid,None);self.started.pop(aid,None)
                    task=getattr(self,'history_tasks',{}).pop(aid,None)
                    if task:task.cancel()
            except Exception as exc:
                error=str(exc) if isinstance(exc,ValueError) else 'Broker verification failed. Reconnect the account and retry removal.'
                store.execute("UPDATE account_removals SET status='blocked',error=? WHERE account_id=?",(error,aid))
    async def maintenance(self):
        last_snapshot={};last_reconcile={}
        while not self.stop.is_set():
            store.set_status('heartbeat',time.time());store.set_status('trading_enabled',self.trading_enabled())
            try:
                await self.ensure_connections()
                await self.remove_accounts()
                for account in store.rows("SELECT * FROM accounts ORDER BY CASE role WHEN 'follower' THEN 0 ELSE 1 END"):
                    aid=account['id']
                    try:
                        await self.initialize_account(account)
                        if aid not in self.ready:continue
                        account=store.account(aid)
                        if time.time()-last_reconcile.get(aid,0)>(3 if account['role']=='master' else 10):
                            await self.reconcile(account);last_reconcile[aid]=time.time()
                        if time.time()-last_snapshot.get(aid,0)>5:
                            await self.snapshot(account);last_snapshot[aid]=time.time()
                        # Historical imports run separately, outside the low-latency event path.
                        task=getattr(self,'history_tasks',{}).get(aid)
                        if not hasattr(self,'history_tasks'):self.history_tasks={}
                        if not task or task.done():
                            if task:
                                try:task.result()
                                except Exception:store.set_status('history_error','History import will be retried.')
                            self.history_tasks[aid]=asyncio.create_task(self.history(account))
                    except Exception as exc:
                        self.ready.discard(aid)
                        error=str(exc) if isinstance(exc,(ValueError,BrokerError)) else 'Broker connection interrupted'
                        store.execute("UPDATE accounts SET status='error',error=? WHERE id=?",(error,aid))
                store.set_status('error',None)
            except Exception as exc:
                store.set_status('error',str(exc) if isinstance(exc,ValueError) else 'cTrader connection is currently unavailable.')
            for environment,connection in self.connections.items():
                if not connection.alive:
                    store.execute("UPDATE accounts SET status='disconnected' WHERE environment=?",(environment,))
                    self.ready={aid for aid in self.ready if store.account(aid)['environment']!=environment}
            try:await asyncio.wait_for(self.stop.wait(),2)
            except asyncio.TimeoutError:pass
    async def run(self):
        # A crash after durable send intent is ambiguous until reconcile proves execution.
        store.execute("UPDATE mappings SET status='closed',error='Entry not sent before restart; no late copy' WHERE status='pending'")
        store.execute("UPDATE commands SET status='uncertain' WHERE status='sending'")
        store.execute("UPDATE mappings SET status='uncertain',error='Restart during broker command; reconciliation pending' WHERE status='opening' OR id IN (SELECT mapping_id FROM commands WHERE status='uncertain')")
        async def pulse():
            while not self.stop.is_set():
                store.set_status('heartbeat',time.time())
                store.set_status('trading_enabled',self.trading_enabled())
                for env,c in self.connections.items():store.set_status('connection:'+env,c.alive and time.time()-getattr(c,'last_received',0)<25)
                await asyncio.sleep(2)
        async def master_monitor():
            while not self.stop.is_set():
                master=store.one("SELECT * FROM accounts WHERE role='master'")
                if master and master['id'] in self.ready and master['id'] not in self.reconciling:
                    try:await self.reconcile(master)
                    except Exception:store.set_status('master_reconcile_error','Master reconciliation pending')
                await asyncio.sleep(3)
        monitor=asyncio.create_task(master_monitor())
        self.watchdog=safety.Watchdog(self)
        protector=asyncio.create_task(self.watchdog.run())
        heartbeat=asyncio.create_task(pulse())
        consumer=asyncio.create_task(self.consume());maintenance=asyncio.create_task(self.maintenance())
        await self.stop.wait()
        maintenance.cancel();consumer.cancel();heartbeat.cancel();protector.cancel();monitor.cancel()
        for task in self.watchdog.tasks.values():task.cancel()
        for task in list(self.sync_tasks.values())+list(getattr(self,'history_tasks',{}).values())+list(getattr(self,'recovery_tasks',{}).values()):task.cancel()
        await asyncio.gather(maintenance,consumer,heartbeat,protector,monitor,*self.watchdog.tasks.values(),*getattr(self,'recovery_tasks',{}).values(),*self.sync_tasks.values(),*getattr(self,'history_tasks',{}).values(),return_exceptions=True)
        for connection in self.connections.values():await connection.close()
        store.set_status('heartbeat',0)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    store.init()
    lock=open(store.DB+'.engine.lock','a')
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise SystemExit('A copy engine is already running on this database.')
    async def run():
        engine=Engine();loop=asyncio.get_running_loop()
        for sig in (signal.SIGTERM,signal.SIGINT):loop.add_signal_handler(sig,engine.stop.set)
        await engine.run()
    asyncio.run(run())

if __name__=='__main__':main()
