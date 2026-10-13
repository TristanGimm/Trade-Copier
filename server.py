"""Private dashboard/API. Broker execution runs in `python -m copier.engine`."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from copier import store, oauth

DB=os.environ.get('DATABASE_PATH','data/copier.db')
ORIGIN=os.environ.get('APP_ORIGIN','http://localhost:8080').rstrip('/')
SECURE=ORIGIN.startswith('https://')
SESSION_TTL=365*24*60*60
ATTEMPTS={}
ATTEMPT_LOCK=threading.Lock()

def connect():return store.db()
def init():
    store.DB=DB;store.init()
def password_hash(password,salt):
    return hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),600000).hex()

def validate_account(body,c):
    discovered=c.execute('SELECT * FROM discovered WHERE id=?',(body.get('id'),)).fetchone()
    if not discovered:raise ValueError('Authorize cTrader first, then select a discovered account.')
    name=str(body.get('name','')).strip();provider=body.get('provider');kind=body.get('kind');role=body.get('role')
    if not name or len(name)>60:raise ValueError('Account name must contain 1 to 60 characters.')
    if provider not in ('FTMO','Pepperstone') or kind not in ('real','simulated') or role not in ('overlord','master','follower'):raise ValueError('Invalid account settings.')
    if provider=='FTMO' and kind!='simulated':raise ValueError('FTMO capital is classified as simulated capital.')
    if role=='overlord' and c.execute("SELECT 1 FROM accounts WHERE role='overlord'").fetchone():raise ValueError('An Overlord source is already configured.')
    if role=='master' and c.execute("SELECT 1 FROM accounts WHERE role='master'").fetchone():raise ValueError('A master is already configured.')
    lots=float(body.get('lots',.1))
    if not math.isfinite(lots) or not .01<=lots<=100:raise ValueError('Lots must be between 0.01 and 100.')
    symbol=str(body.get('symbol','XAUUSD')).strip()
    if not symbol or len(symbol)>40:raise ValueError('Invalid symbol name.')
    return discovered,name,provider,kind,role,lots,symbol

class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,directory=str(Path(__file__).parent/'public'),**kwargs)
    def log_message(self,fmt,*args):
        # Callback query contains a short-lived authorization code. Never log it.
        print('%s %s %s'%(self.address_string(),self.command,urlparse(self.path).path))
    def end_headers(self):
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        if SECURE:self.send_header('Strict-Transport-Security','max-age=31536000')
        super().end_headers()
    def output(self,data,status=200,cookie=None):
        raw=json.dumps(data,allow_nan=False).encode();self.send_response(status)
        self.send_header('Content-Type','application/json');self.send_header('Cache-Control','no-store')
        self.send_header('Content-Length',str(len(raw)))
        if cookie or getattr(self,'renewed_session_cookie',None):self.send_header('Set-Cookie',cookie or self.renewed_session_cookie)
        self.end_headers();self.wfile.write(raw)
    def session(self):
        cookie=SimpleCookie()
        try:cookie.load(self.headers.get('Cookie',''))
        except Exception:return None
        token=cookie.get('session');token=token.value if token else ''
        session=store.one('SELECT sessions.*,users.email FROM sessions JOIN users ON users.id=sessions.user_id WHERE token=? AND expires>?',(hashlib.sha256(token.encode()).hexdigest(),time.time()))
        if session and session['expires']<time.time()+SESSION_TTL*2/3:
            expires=time.time()+SESSION_TTL
            with store.db() as c:c.execute('UPDATE sessions SET expires=? WHERE token=?',(expires,session['token']))
            self.renewed_session_cookie='session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(SESSION_TTL)+('; Secure' if SECURE else '')
        return session
    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/health':
            heartbeat=store.one("SELECT value FROM engine_status WHERE key='heartbeat'")
            return self.output({'ok':True,'engineRunning':bool(heartbeat and time.time()-json.loads(heartbeat['value'])<20)})
        if path=='/api/session':
            session=self.session();setup=not store.one('SELECT 1 FROM users')
            return self.output({'authenticated':bool(session),'setupRequired':setup,'email':session['email'] if session else None,'csrf':session['csrf'] if session else None})
        if path.startswith('/api/'):
            if not self.session():return self.output({'error':'Please sign in.'},401)
            if path=='/api/state':return self.output(store.public_state())
            return self.output({'error':'Not found.'},404)
        if path in ('/','/ctrader/callback'):self.path='/index.html'
        return super().do_GET()
    def do_POST(self):
        if self.headers.get('Origin')!=ORIGIN:return self.output({'error':'Origin not allowed.'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384:return self.output({'error':'Invalid request size.'},413)
            body=json.loads(self.rfile.read(length),parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body,dict):raise ValueError()
        except (ValueError,UnicodeDecodeError):return self.output({'error':'Invalid request.'},400)
        path=urlparse(self.path).path
        try:
            if path in ('/api/setup','/api/login'):return self.login(path,body)
            session=self.session()
            if not session:return self.output({'error':'Please sign in.'},401)
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),session['csrf']):return self.output({'error':'Invalid session.'},403)
            if path=='/api/ctrader/connect':return self.output(oauth.begin(session['token'],ORIGIN))
            if path=='/api/ctrader/exchange':return self.output(oauth.exchange(body.get('code'),body.get('state'),session['token'],ORIGIN))
            if path=='/api/emergency':
                from copier.safety import trip
                trip('Manual emergency stop: all slave positions and pending orders')
                return self.output({'ok':True,'status':'queued'})
            with store.db() as c:
                if path=='/api/logout':
                    c.execute('DELETE FROM sessions WHERE token=?',(session['token'],))
                    c.commit() # Invalidate before responding; a following request may use another thread.
                    return self.output({'ok':True},cookie='session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'+('; Secure' if SECURE else ''))
                if path=='/api/pause':
                    if not isinstance(body.get('paused'),bool):raise ValueError('Invalid pause setting.')
                    paused=body['paused']
                    if not paused:
                        state=store.public_state()
                        if state['safety']['latched']:raise ValueError('Review and reset the safety stop first.')
                        if c.execute("SELECT 1 FROM account_removals WHERE status='queued'").fetchone():raise ValueError('Wait for account removal to complete.')
                        if not state['brokerConnected']:raise ValueError('Connections, protection monitor or broker confirmations are not healthy.')
                        if state['engine']['stale']:raise ValueError('Copy engine is unreachable.')
                        if not state['engine'].get('trading_enabled'):raise ValueError('Trade execution is disabled in the server configuration.')
                        if not any(a['role']=='master' and a['status']=='connected' for a in state['accounts']):raise ValueError('A synchronized master is required.')
                        if any(a['role']=='overlord' for a in state['accounts']) and not any(a['role']=='master' and a['enabled'] and a['status']=='connected' for a in state['accounts']):raise ValueError('Enable and synchronize the master relay before starting the copy chain.')
                        if any(a['role']=='overlord' for a in state['accounts']) and not any(a['role']=='overlord' and a['status']=='connected' for a in state['accounts']):raise ValueError('The Overlord source must be synchronized before starting the copy chain.')
                        if not any(a['role']=='follower' and a['enabled'] and a['status']=='connected' for a in state['accounts']):raise ValueError('At least one enabled, synchronized slave is required.')
                    c.execute("UPDATE settings SET value=? WHERE key='paused'",('true' if paused else 'false',))
                    event='New copies '+('paused; existing positions remain managed.' if paused else 'enabled.')
                elif path=='/api/safety/reset':
                    c.execute('BEGIN IMMEDIATE')
                    state=store.public_state()
                    if state['engine']['stale'] or any(not x['ok'] for x in state['health'] if x['label']!='No safety stop'):raise ValueError('All connections and broker confirmations must be healthy.')
                    if any(x['status']!='flat' or time.time()-(x['verified'] or 0)>5 for x in state['safety']['liquidations']):raise ValueError('All affected slaves need a fresh broker confirmation of no positions or pending orders.')
                    c.execute('DELETE FROM liquidations')
                    c.execute("INSERT OR REPLACE INTO settings VALUES('safety_latched','false')")
                    event='Safety stop reset; new copies remain paused.'
                elif path=='/api/safety/policy':
                    from copier.safety import DEFAULTS
                    limits={'max_entries':(1,20),'entry_window':(1,60),'max_positions':(1,50),'max_signal_age':(1,30),'protection_timeout':(1,15),'unprotected_timeout':(1,300),'disconnect_timeout':(1,30)}
                    values={}
                    for key,default in DEFAULTS.items():
                        value=body.get(key,default)
                        if isinstance(value,bool) or not isinstance(value,int) or not limits[key][0]<=value<=limits[key][1]:raise ValueError('Invalid protection rule: '+key)
                        values[key]=value
                    c.execute("INSERT OR REPLACE INTO settings VALUES('safety_policy',?)",(json.dumps(values),))
                    event='Protection rules updated.'
                elif path=='/api/accounts':
                    c.execute('BEGIN IMMEDIATE')
                    d,name,provider,kind,role,lots,symbol=validate_account(body,c)
                    c.execute('''INSERT INTO accounts(id,broker_id,environment,token_id,name,provider,kind,role,lots,enabled,symbol_name)
                        VALUES(?,?,?,?,?,?,?,?,?,0,?)''',(d['id'],d['broker_id'],d['environment'],d['token_id'],name,provider,kind,role,lots,symbol))
                    event='cTrader account added: '+name+' · '+role
                elif path=='/api/rules':
                    account=c.execute('SELECT * FROM accounts WHERE id=?',(body.get('id'),)).fetchone()
                    if not account or account['role'] not in ('master','follower'):raise ValueError('Copy target account not found.')
                    if account['role']=='master' and not c.execute("SELECT 1 FROM accounts WHERE role='overlord'").fetchone():raise ValueError('A master copy rule is only available when an Overlord source is configured.')
                    lots=float(body.get('lots'));enabled=body.get('enabled')
                    if not isinstance(enabled,bool) or not math.isfinite(lots) or not .01<=lots<=100:raise ValueError('Invalid lots or activation setting.')
                    if enabled and c.execute("SELECT 1 FROM account_removals WHERE account_id=? AND status='queued'",(account['id'],)).fetchone():raise ValueError('Account removal is pending.')
                    if enabled and account['status']!='connected':raise ValueError('The copy target must synchronize successfully first.')
                    if account['symbol_json']:
                        from copier.engine import lots_to_volume
                        lots_to_volume(lots,json.loads(account['symbol_json']))
                    c.execute('UPDATE accounts SET lots=?,enabled=? WHERE id=?',(lots,int(enabled),account['id']))
                    event='Copy rule saved: '+account['name']+' · '+str(lots)+' Lots · '+('enabled' if enabled else 'paused')
                elif path=='/api/risk':
                    from copier.risk import day_key
                    account=c.execute("SELECT * FROM accounts WHERE id=? AND role='follower'",(body.get('id'),)).fetchone()
                    if not account:raise ValueError('Slave not found.')
                    def amount(key,optional=False):
                        value=body.get(key)
                        if optional and value in (None,''):return None
                        value=float(value)
                        if not math.isfinite(value) or value<=0:raise ValueError('Amounts must be positive: '+key)
                        return value
                    initial=amount('initial_balance');maximum=amount('max_loss',True);daily=amount('daily_loss',True)
                    warning=float(body.get('warning_pct',20))
                    if not math.isfinite(warning) or not 1<=warning<=100:raise ValueError('Warning threshold must be between 1 and 100 percent.')
                    if maximum is not None and maximum>initial:raise ValueError('Max Loss cannot exceed the initial balance.')
                    c.execute('INSERT OR REPLACE INTO risk_limits VALUES(?,?,?,?,?)',(account['id'],initial,maximum,daily,warning))
                    if body.get('day_balance') not in (None,''):
                        c.execute("INSERT OR REPLACE INTO risk_days VALUES(?,?,?,'manual')",(account['id'],day_key(),amount('day_balance')))
                    event='Loss display saved: '+account['name']+' · warnings only, no trading restriction'
                elif path=='/api/mapping/resync':
                    mapping=c.execute("SELECT * FROM mappings WHERE id=? AND status='active'",(body.get('id'),)).fetchone()
                    if not mapping:raise ValueError('Active copy not found.')
                    c.execute('UPDATE mappings SET sl_override=0,tp_override=0,protection_initialized=0 WHERE id=?',(mapping['id'],))
                    event='Master SL/TP restored: '+mapping['id']
                elif path=='/api/account/remove':
                    c.execute('BEGIN IMMEDIATE')
                    account=c.execute('SELECT * FROM accounts WHERE id=?',(body.get('id'),)).fetchone()
                    if not account:raise ValueError('Account not found.')
                    state=store.public_state()
                    if state['engine']['stale']:raise ValueError('Start the copy engine so it can verify the broker account before removal.')
                    c.execute("UPDATE settings SET value='true' WHERE key='paused'")
                    c.execute('UPDATE accounts SET enabled=0 WHERE id=?',(account['id'],))
                    c.execute("INSERT INTO account_removals VALUES(?,'queued',NULL,?) ON CONFLICT(account_id) DO UPDATE SET status='queued',error=NULL,created=excluded.created",(account['id'],time.time()))
                    event='Account removal requested: '+account['name']+'; new copies paused pending broker verification.'
                elif path=='/api/account/update':
                    account=c.execute('SELECT * FROM accounts WHERE id=?',(body.get('id'),)).fetchone()
                    if not account:raise ValueError('Account not found.')
                    name=str(body.get('name',account['name'])).strip();symbol=str(body.get('symbol',account['symbol_name'])).strip()
                    if not name or len(name)>60 or not symbol or len(symbol)>40:raise ValueError('Invalid account name or symbol name.')
                    if symbol!=account['symbol_name']:
                        if c.execute("SELECT 1 FROM mappings WHERE (master_account=? OR follower_account=?) AND status NOT IN ('closed','failed')",(account['id'],account['id'])).fetchone():raise ValueError('The symbol cannot be changed while active position mappings exist.')
                        c.execute("UPDATE accounts SET name=?,symbol_name=?,status='pending',enabled=0 WHERE id=?",(name,symbol,account['id']))
                    else:c.execute('UPDATE accounts SET name=? WHERE id=?',(name,account['id']))
                    event='Account settings updated: '+name
                elif path=='/api/password':
                    user=c.execute('SELECT * FROM users WHERE id=?',(session['user_id'],)).fetchone()
                    old=str(body.get('current',''));new=str(body.get('password',''))
                    if not hmac.compare_digest(password_hash(old,user['salt']),user['hash']):raise ValueError('Current password is incorrect.')
                    if not 12<=len(new)<=512:raise ValueError('New password must contain 12 to 512 characters.')
                    salt=secrets.token_hex(16);c.execute('UPDATE users SET salt=?,hash=? WHERE id=?',(salt,password_hash(new,salt),user['id']))
                    c.execute('DELETE FROM sessions WHERE user_id=? AND token!=?',(user['id'],session['token']))
                    event='Administrator password changed; other sessions ended.'
                else:return self.output({'error':'Not found.'},404)
                c.execute('INSERT INTO audit(event) VALUES(?)',(event,))
            self.output({'ok':True})
        except (ValueError,TypeError):
            import sys
            error=sys.exc_info()[1]
            return self.output({'error':str(error) or 'Invalid input.'},400)
        except __import__('sqlite3').IntegrityError:return self.output({'error':'Account or user already exists.'},409)
    def login(self,path,body):
        now=time.time();ip=self.client_address[0]
        with ATTEMPT_LOCK:
            attempts=ATTEMPTS.setdefault(ip,[]);attempts[:]=[x for x in attempts if x>now-300]
            if len(attempts)>=10:return self.output({'error':'Too many attempts. Try again in five minutes.'},429)
            attempts.append(now)
        email=str(body.get('email','')).strip().lower();password=str(body.get('password',''))
        if len(email)>254 or len(password)>512:raise ValueError('Credentials are too long.')
        with store.db() as c:
            if path=='/api/setup':
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT 1 FROM users').fetchone():return self.output({'error':'Setup already completed.'},409)
                secret=os.environ.get('SETUP_TOKEN','')
                if not secret or not hmac.compare_digest(str(body.get('setupToken','')),secret):return self.output({'error':'Invalid setup token.'},403)
                if len(password)<12 or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):raise ValueError('A valid email and a password of at least 12 characters are required.')
                salt=secrets.token_hex(16);c.execute('INSERT INTO users(email,salt,hash) VALUES(?,?,?)',(email,salt,password_hash(password,salt)))
            user=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            salt=user['salt'] if user else '0'*32
            valid=hmac.compare_digest(password_hash(password,salt),user['hash'] if user else '0'*64)
            if not user or not valid:return self.output({'error':'Invalid credentials.'},401)
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            c.execute('DELETE FROM sessions WHERE expires<?',(now,))
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],now+SESSION_TTL,csrf))
        return self.output({'ok':True,'csrf':csrf},cookie='session='+token+'; HttpOnly; SameSite=Strict; Path=/; Max-Age='+str(SESSION_TTL)+('; Secure' if SECURE else ''))

if __name__=='__main__':
    init();ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
