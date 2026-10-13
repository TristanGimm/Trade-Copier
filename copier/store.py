"""Durable broker state. A single writer engine owns all trading commands."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB = os.environ.get('DATABASE_PATH', 'data/copier.db')

@contextmanager
def db():
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=20000')
    connection.execute('PRAGMA foreign_keys=ON')
    try:
        with connection:
            yield connection
    finally:
        connection.close()

def init():
    with db() as c:
        c.execute('PRAGMA journal_mode=WAL')
        old = c.execute("SELECT name FROM sqlite_master WHERE name='accounts'").fetchone()
        if old and 'broker_id' not in [x[1] for x in c.execute('PRAGMA table_info(accounts)')]:
            # Preserve old configuration for inspection, never promote example data to live data.
            c.execute('ALTER TABLE accounts RENAME TO legacy_demo_accounts')
            c.execute("DELETE FROM settings WHERE key='paused'")
            c.execute("DELETE FROM audit WHERE event LIKE 'Demo%'")
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,email TEXT UNIQUE,salt TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER,expires REAL,csrf TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,created TEXT DEFAULT CURRENT_TIMESTAMP,event TEXT);
        CREATE TABLE IF NOT EXISTS oauth_tokens(id TEXT PRIMARY KEY,secret TEXT NOT NULL,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS oauth_pending(state TEXT PRIMARY KEY,session_token TEXT,expires REAL);
        CREATE TABLE IF NOT EXISTS discovered(id TEXT PRIMARY KEY,broker_id INTEGER,environment TEXT,token_id TEXT,login TEXT,broker TEXT);
        CREATE TABLE IF NOT EXISTS accounts(
          id TEXT PRIMARY KEY,broker_id INTEGER NOT NULL,environment TEXT NOT NULL,token_id TEXT NOT NULL,
          name TEXT NOT NULL,provider TEXT NOT NULL,kind TEXT NOT NULL,role TEXT NOT NULL,lots REAL NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0,symbol_name TEXT NOT NULL DEFAULT 'XAUUSD',symbol_id INTEGER,
          symbol_json TEXT,currency TEXT,balance REAL,equity REAL,unrealized REAL,margin REAL,
          status TEXT NOT NULL DEFAULT 'pending',error TEXT,updated REAL,account_type INTEGER,
          UNIQUE(broker_id,environment));
        CREATE UNIQUE INDEX IF NOT EXISTS single_master ON accounts(role) WHERE role='master';
        CREATE UNIQUE INDEX IF NOT EXISTS single_overlord ON accounts(role) WHERE role='overlord';
        CREATE TABLE IF NOT EXISTS positions(
          account_id TEXT,position_id INTEGER,volume INTEGER,side INTEGER,price REAL,sl REAL,tp REAL,
          label TEXT,opened INTEGER,updated INTEGER,status TEXT,pnl REAL,symbol_id INTEGER,
          PRIMARY KEY(account_id,position_id));
        CREATE TABLE IF NOT EXISTS master_positions(
          account_id TEXT,position_id INTEGER,initial_volume INTEGER,volume INTEGER,side INTEGER,
          sl REAL,tp REAL,price REAL,updated INTEGER,closed INTEGER DEFAULT 0,eligible INTEGER DEFAULT 0,
          PRIMARY KEY(account_id,position_id));
        CREATE TABLE IF NOT EXISTS mappings(
          id TEXT PRIMARY KEY,master_account TEXT,master_position INTEGER,slave_account TEXT,
          slave_position INTEGER,initial_master INTEGER,initial_slave INTEGER,label TEXT UNIQUE,
          status TEXT,sl REAL,tp REAL,close_target INTEGER,error TEXT,latency_ms REAL,created REAL,
          UNIQUE(master_account,master_position,slave_account));
        CREATE TABLE IF NOT EXISTS commands(
          id TEXT PRIMARY KEY,mapping_id TEXT,kind TEXT,payload TEXT,status TEXT,error TEXT,created REAL,completed REAL);
        CREATE TABLE IF NOT EXISTS deals(
          account_id TEXT,deal_id INTEGER,position_id INTEGER,order_id INTEGER,symbol_id INTEGER,
          timestamp INTEGER,side INTEGER,volume INTEGER,price REAL,net REAL,commission REAL,swap REAL,
          is_close INTEGER,currency TEXT,PRIMARY KEY(account_id,deal_id));
        CREATE TABLE IF NOT EXISTS equity_samples(
          account_id TEXT,timestamp INTEGER,equity REAL,balance REAL,PRIMARY KEY(account_id,timestamp));
        CREATE TABLE IF NOT EXISTS account_removals(account_id TEXT PRIMARY KEY,status TEXT,error TEXT,created REAL);
        CREATE TABLE IF NOT EXISTS risk_limits(account_id TEXT PRIMARY KEY,initial_balance REAL,max_loss REAL,daily_loss REAL,warning_pct REAL DEFAULT 20);
        CREATE TABLE IF NOT EXISTS risk_days(account_id TEXT,day TEXT,balance REAL,source TEXT,PRIMARY KEY(account_id,day));
        CREATE TABLE IF NOT EXISTS safety_timers(id TEXT PRIMARY KEY,started REAL);
        CREATE TABLE IF NOT EXISTS liquidations(account_id TEXT PRIMARY KEY,reason TEXT,status TEXT,created REAL,error TEXT,verified REAL);
        CREATE TABLE IF NOT EXISTS safety_actions(id TEXT PRIMARY KEY,account_id TEXT,kind TEXT,target INTEGER,status TEXT,error TEXT,created REAL,completed REAL);
        CREATE TABLE IF NOT EXISTS engine_status(key TEXT PRIMARY KEY,value TEXT);
        ''')
        c.execute('BEGIN IMMEDIATE')
        columns={r[1] for r in c.execute('PRAGMA table_info(mappings)')}
        # Migrate existing installations without losing saved copy mappings.
        for previous,current in (('follower_account','slave_account'),('follower_position','slave_position'),('initial_follower','initial_slave'),('follower_order','slave_order')):
            if previous in columns and current not in columns:
                c.execute('ALTER TABLE mappings RENAME COLUMN '+previous+' TO '+current)
                columns.remove(previous);columns.add(current)
        c.execute("UPDATE accounts SET role='slave' WHERE role='follower'")
        for name in ('protection_initialized','sl_override','tp_override'):
            if name not in columns:c.execute('ALTER TABLE mappings ADD COLUMN '+name+' INTEGER NOT NULL DEFAULT 0')
        for name in ('master_order','slave_order'):
            if name not in columns:c.execute('ALTER TABLE mappings ADD COLUMN '+name+' INTEGER')
        c.execute('''CREATE TABLE IF NOT EXISTS master_orders(
            account_id TEXT,order_id INTEGER,payload BLOB,status INTEGER,updated INTEGER,
            PRIMARY KEY(account_id,order_id))''')
        c.execute("INSERT OR IGNORE INTO settings VALUES('paused','true')")

def rows(query, args=()):
    with db() as c:
        return [dict(x) for x in c.execute(query, args)]

def one(query, args=()):
    items = rows(query, args)
    return items[0] if items else None

def execute(query, args=()):
    with db() as c:
        return c.execute(query, args).rowcount

def copy_targets(role):
    """Return the enabled next hop for this source in the fixed two-tier topology."""
    if role=='overlord':return rows("SELECT * FROM accounts WHERE role='master' AND enabled=1")
    if role=='master':return rows("SELECT * FROM accounts WHERE role='slave' AND enabled=1")
    return []

def audit(event):
    execute('INSERT INTO audit(event) VALUES(?)', (event,))

def set_status(key, value):
    execute('INSERT OR REPLACE INTO engine_status VALUES(?,?)',(key,json.dumps(value)))

def setting(key, default=None):
    result=one('SELECT value FROM settings WHERE key=?',(key,))
    return result['value'] if result else default

def account(account_id):
    return one('SELECT * FROM accounts WHERE id=?',(account_id,))

def performance(account,now=None):
    """Uncapped trading returns since connection; transfers never become profit."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now=time.time() if now is None else now
    today=datetime.fromtimestamp(now,ZoneInfo('Europe/Berlin')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()*1000
    first=one('SELECT * FROM equity_samples WHERE account_id=? ORDER BY timestamp LIMIT 1',(account['id'],))
    result={}
    for key,cutoff in [('today',today),('30',(now-30*86400)*1000),('90',(now-90*86400)*1000),('365',(now-365*86400)*1000),('all',0)]:
        reference=one('SELECT * FROM equity_samples WHERE account_id=? AND timestamp<=? ORDER BY timestamp DESC LIMIT 1',(account['id'],cutoff)) or first
        if not reference or account['unrealized'] is None:
            result[key]=None;continue
        start=max(cutoff,reference['timestamp'])
        realized=one('SELECT COALESCE(SUM(net),0) total FROM deals WHERE account_id=? AND is_close=1 AND timestamp>=?',(account['id'],start))['total']
        net=realized+account['unrealized']-(reference['equity']-reference['balance'])
        result[key]=dict(net=net,capital=reference['equity'],since=start,reference_at=reference['timestamp'])
    return result

def public_state():
    accounts=rows('SELECT * FROM accounts ORDER BY role DESC,name')
    from . import risk
    for a in accounts:
        a['performance']=performance(a)
        a['risk']=risk.view(a) if a['role']=='slave' else None
        for key in ('token_id','symbol_json'):
            a.pop(key,None)
    status={r['key']:json.loads(r['value']) for r in rows('SELECT * FROM engine_status')}
    status['stale']=time.time()-status.get('heartbeat',0)>20
    relevant=[a for a in accounts if a['role'] in ('overlord','master') or a['enabled'] or one("SELECT 1 FROM positions WHERE account_id=? AND status='open'",(a['id'],))]
    has_overlord=any(a['role']=='overlord' for a in relevant)
    checks=[{'label':'Copy-Engine','ok':not status['stale']},
            {'label':'Protection monitor','ok':time.time()-status.get('safety_heartbeat',0)<5 and not status.get('safety_error')},
            {'label':'Copy chain assigned','ok':any(a['role']=='master' for a in relevant) and any(a['role']=='slave' for a in relevant) and (not has_overlord or any(a['role']=='master' and a['enabled'] for a in relevant))},
            {'label':'Trade execution','ok':bool(status.get('trading_enabled'))},
            {'label':'Protection prices confirmed','ok':not one("SELECT 1 FROM mappings m JOIN master_positions s ON s.account_id=m.master_account AND s.position_id=m.master_position JOIN positions p ON p.account_id=m.slave_account AND p.position_id=m.slave_position WHERE m.status='active' AND p.status='open' AND ((m.sl_override=0 AND p.sl IS NOT s.sl) OR (m.tp_override=0 AND p.tp IS NOT s.tp))")},
            {'label':'No safety stop','ok':setting('safety_latched')!='true'},
            {'label':'No unconfirmed commands','ok':not one("SELECT 1 FROM commands WHERE status IN ('sending','uncertain')")},
            {'label':'No synchronization errors','ok':not one("SELECT 1 FROM mappings WHERE status NOT IN ('closed','failed') AND error IS NOT NULL")}]
    for a in relevant:
        checks.append({'label':a['name'],'ok':a['status']=='connected' and bool(a['updated']) and time.time()-a['updated']<30 and time.time()-status.get('reconciled:'+a['id'],0)<25 and status.get('connection:'+a['environment'],False)})
    healthy=all(c['ok'] for c in checks)
    from .safety import policy
    result=dict(accounts=accounts,positions=rows("SELECT * FROM positions WHERE status='open'"),
                deals=rows('SELECT * FROM deals ORDER BY timestamp DESC LIMIT 5000'),
                equity=rows('SELECT * FROM equity_samples ORDER BY timestamp DESC LIMIT 20000'),
                mappings=rows('SELECT * FROM mappings ORDER BY created DESC LIMIT 200'),
                audit=rows('SELECT * FROM audit ORDER BY id DESC LIMIT 100'),
                commands=rows("SELECT id,mapping_id,kind,status,error,created,completed FROM commands ORDER BY created DESC LIMIT 100"),
                discovered=rows('SELECT id,broker_id,environment,login,broker FROM discovered'),
                paused=setting('paused')!='false',engine=status,
                configured=bool(os.environ.get('CTRADER_CLIENT_ID') and os.environ.get('CTRADER_CLIENT_SECRET') and os.environ.get('TOKEN_ENCRYPTION_KEY')),
                removals=rows('SELECT * FROM account_removals'),mode='broker',brokerConnected=healthy,health=checks,safety=dict(latched=setting('safety_latched')=='true',policy=policy(),liquidations=rows('SELECT * FROM liquidations'),actions=rows('SELECT * FROM safety_actions ORDER BY created DESC LIMIT 100')))

    from .english import display
    for item in result['accounts']+result['mappings']+result['commands']+result['removals']+result['safety']['liquidations']+result['safety']['actions']:
        for key in ('error','reason'):
            if key in item:item[key]=display(item[key])
    for item in result['audit']:item['event']=display(item['event'])
    for key in ('error','oauth_error','history_error','safety_error','master_reconcile_error'):
        if key in result['engine']:result['engine'][key]=display(result['engine'][key])
    return result
