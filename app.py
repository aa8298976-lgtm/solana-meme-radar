import os, re, sqlite3, time, requests
from datetime import datetime, timezone
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

DEX='https://api.dexscreener.com'; HELIUS='https://api.helius.xyz'
BCRTEX='BCrTEXmWutwPz8qv6w1S5gDbaLnSLpXKM5kSGVWyyfxu'
DB='radar.db'

st.set_page_config(page_title='Solana Meme Radar V3',page_icon='🚨',layout='wide')

def secret(name, default=''):
    try:
        return st.secrets.get(name, os.getenv(name, default))
    except Exception:
        return os.getenv(name, default)

def get_json(url, params=None, headers=None, timeout=20):
    r=requests.get(url,params=params,headers=headers or {},timeout=timeout)
    r.raise_for_status(); return r.json()

def init_db():
    c=sqlite3.connect(DB)
    c.execute('''CREATE TABLE IF NOT EXISTS tokens(address TEXT PRIMARY KEY,symbol TEXT,name TEXT,price REAL,mc REAL,liq REAL,vol5 REAL,vol1h REAL,buys5 INT,sells5 INT,pc5 REAL,pc1h REAL,age_min REAL,boosts REAL,smart_hits INT,smart_flow INT,score REAL,url TEXT,last_seen TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS wallet_buys(id INTEGER PRIMARY KEY AUTOINCREMENT,wallet TEXT,label TEXT,token TEXT,symbol TEXT,ts REAL,signature TEXT,amount REAL)''')
    c.commit(); c.close()

def dex(path): return get_json(DEX+path,headers={'User-Agent':'SolanaMemeRadar/3.0'})

def candidates():
    s={}
    for src in (dex('/token-profiles/latest/v1') or [],dex('/token-boosts/latest/v1') or []):
        for x in src:
            if x.get('chainId')=='solana' and x.get('tokenAddress'): s[x['tokenAddress']]=1
    return list(s)[:150]

def hydrate(addr):
    ps=[p for p in (dex(f'/token-pairs/v1/solana/{addr}') or []) if p.get('chainId')=='solana']
    if not ps:return None
    p=max(ps,key=lambda z:float((z.get('liquidity') or {}).get('usd') or 0))
    created=p.get('pairCreatedAt'); now=datetime.now(timezone.utc).timestamp()*1000
    age=(now-created)/60000 if created else 999999
    tx=(p.get('txns') or {}).get('m5') or {}; vol=p.get('volume') or {}; ch=p.get('priceChange') or {}
    return {'address':addr,'symbol':(p.get('baseToken') or {}).get('symbol','?'),'name':(p.get('baseToken') or {}).get('name','Unknown'),'price':float(p.get('priceUsd') or 0),'mc':float(p.get('marketCap') or p.get('fdv') or 0),'liq':float((p.get('liquidity') or {}).get('usd') or 0),'vol5':float(vol.get('m5') or 0),'vol1h':float(vol.get('h1') or 0),'buys5':int(tx.get('buys') or 0),'sells5':int(tx.get('sells') or 0),'pc5':float(ch.get('m5') or 0),'pc1h':float(ch.get('h1') or 0),'age_min':max(0,age),'boosts':float((p.get('boosts') or {}).get('active') or 0),'url':p.get('url') or f'https://dexscreener.com/solana/{addr}'}

def helius_txs(wallet,limit=20):
    key=secret('HELIUS_API_KEY')
    if not key:return []
    try:return get_json(f'{HELIUS}/v0/addresses/{wallet}/transactions',params={'api-key':key,'limit':limit}) or []
    except Exception:return []

def incoming(tx,wallet):
    if not isinstance(tx,dict) or tx.get('type')!='SWAP':return []
    out=[]
    for t in tx.get('tokenTransfers') or []:
        mint=t.get('mint'); to=t.get('toUserAccount') or t.get('to'); amt=t.get('tokenAmount') or t.get('amount') or 0
        if mint and to and str(to).startswith(wallet[:8]):
            try: amt=float(amt)
            except Exception: amt=0
            out.append((mint,amt))
    return out

def wallet_buys():
    now=datetime.now(timezone.utc).timestamp(); window=float(secret('SMART_WINDOW_MINUTES','15'))*60
    c=sqlite3.connect(DB)
    for wallet,label in [(BCRTEX,'BCrTEX')]:
        for tx in helius_txs(wallet,int(secret('WALLET_LOOKBACK_TX','30'))):
            ts=tx.get('timestamp') or tx.get('blockTime') or now
            if now-ts>window:continue
            sig=tx.get('signature')
            for mint,amt in incoming(tx,wallet):
                if mint=='So11111111111111111111111111111111111111111':continue
                if not c.execute('SELECT 1 FROM wallet_buys WHERE wallet=? AND signature=? AND token=?',(wallet,sig,mint)).fetchone():
                    c.execute('INSERT INTO wallet_buys(wallet,label,token,symbol,ts,signature,amount) VALUES(?,?,?,?,?,?,?)',(wallet,label,mint,'?',ts,sig,amt))
    c.commit()
    rows=c.execute('SELECT token,COUNT(DISTINCT wallet),MAX(ts) FROM wallet_buys WHERE ts>? GROUP BY token',(now-window,)).fetchall(); c.close()
    return {r[0]:(int(r[1]),float(r[2])) for r in rows}

def birdeye_tokens():
    key=secret('BIRDEYE_API_KEY')
    if not key:return set()
    try:
        raw=get_json('https://public-api.birdeye.so/smart-money/v1/token/list',headers={'X-API-KEY':key,'x-chain':'solana'})
        data=raw.get('data',raw) if isinstance(raw,dict) else raw
        if isinstance(data,dict):data=data.get('tokens',data.get('items',[]))
        out=set()
        for x in data or []:
            if isinstance(x,str):out.add(x)
            elif isinstance(x,dict):
                a=x.get('address') or x.get('tokenAddress') or x.get('token_address')
                if a:out.add(a)
        return out
    except Exception:return set()

def score(x,hits=0,flow=False):
    s=0; liq=x['liq']; mc=x['mc']
    s += 15 if liq>=250000 else 12 if liq>=100000 else 8 if liq>=30000 else 4 if liq>=10000 else 0
    ratio=liq/mc if mc else 0; s += 10 if ratio>=.35 else 7 if ratio>=.20 else 4 if ratio>=.10 else 0
    age=x['age_min']; s += 10 if 0<age<=15 else 8 if age<=60 else 4 if age<=360 else 0
    pc5=x['pc5']; pc1=x['pc1h']; s += 12 if pc5>=30 else 9 if pc5>=15 else 5 if pc5>=5 else -8 if pc5<-10 else 0
    s += 10 if pc1>=80 else 7 if pc1>=30 else 4 if pc1>=10 else 0
    buys=x['buys5']; sells=x['sells5']; tx=buys+sells; s += 8 if tx>=150 else 5 if tx>=60 else 2 if tx>=20 else 0
    s += 8 if buys>sells*1.5 and tx>=10 else 4 if buys>sells else 0
    v=x['vol5']; s += 8 if v>=100000 else 5 if v>=25000 else 2 if v>=5000 else 0
    s += min(3,int(x['boosts'])) + min(25,hits*5) + (5 if flow else 0)
    if mc>5000000:s-=12
    if liq<10000:s-=15
    return max(0,min(100,s))

def run_scan():
    init_db(); conv=wallet_buys(); sm=birdeye_tokens(); rows=[]
    for addr in candidates():
        try:
            x=hydrate(addr)
            if not x:continue
            hits,_=conv.get(addr,(0,0)); flow=addr in sm or hits>0; x['smart_hits']=hits; x['smart_flow']=flow; x['score']=score(x,hits,flow); rows.append(x)
        except Exception:continue
    c=sqlite3.connect(DB)
    for x in rows:
        c.execute('''INSERT INTO tokens VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(address) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,price=excluded.price,mc=excluded.mc,liq=excluded.liq,vol5=excluded.vol5,vol1h=excluded.vol1h,buys5=excluded.buys5,sells5=excluded.sells5,pc5=excluded.pc5,pc1h=excluded.pc1h,age_min=excluded.age_min,boosts=excluded.boosts,smart_hits=excluded.smart_hits,smart_flow=excluded.smart_flow,score=excluded.score,url=excluded.url,last_seen=excluded.last_seen''',(x['address'],x['symbol'],x['name'],x['price'],x['mc'],x['liq'],x['vol5'],x['vol1h'],x['buys5'],x['sells5'],x['pc5'],x['pc1h'],x['age_min'],x['boosts'],x['smart_hits'],int(x['smart_flow']),x['score'],x['url'],datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close(); return rows

init_db()
with st.sidebar:
    st.header('⚙️ Radar')
    seconds=int(secret('DEX_SCAN_SECONDS','60'))
    st.write(f'Auto-scan: هر {seconds} ثانیه')
    if st.button('🔄 Scan now',use_container_width=True):
        with st.spinner('در حال اسکن...'): run_scan()
    st.info('برای بررسی خریدهای واقعی BCrTEX، کلید HELIUS_API_KEY را در Secrets وارد کن.')

st.title('🚨 Solana Meme Radar V3')
st.caption('On-chain Smart Money + DEX momentum | research/alert tool — not auto-trading')
st_autorefresh(interval=seconds*1000,key='radar_refresh')
if 'last_auto_scan' not in st.session_state or time.time()-st.session_state.last_auto_scan>=max(30,seconds-5):
    try: run_scan()
    except Exception as e: st.warning(f'Scan error: {e}')
    st.session_state.last_auto_scan=time.time()

c=sqlite3.connect(DB); df=pd.read_sql_query('SELECT * FROM tokens ORDER BY score DESC,last_seen DESC LIMIT 100',c); wdf=pd.read_sql_query("SELECT wallet,label,token,symbol,datetime(ts,'unixepoch') time,amount,signature FROM wallet_buys ORDER BY ts DESC LIMIT 100",c); c.close()
def bucket(s): return '🚨 EXTREME' if s>=93 else '🔥 ALERT' if s>=85 else '🟢 STRONG WATCH' if s>=75 else '🟡 WATCH' if s>=65 else '⚪ LOW'
if df.empty: st.warning('هنوز داده‌ای نیست؛ Scan now را بزن یا چند ثانیه صبر کن.')
else:
    df['signal']=df.score.apply(bucket); a,b,d,e=st.columns(4); a.metric('Tracked',len(df)); b.metric('Alerts',int((df.score>=85).sum())); d.metric('Strong Watch',int((df.score>=75).sum())); e.metric('Smart Hits',int(df.smart_hits.sum()))
    st.dataframe(df[['signal','symbol','name','score','mc','liq','pc5','pc1h','vol5','buys5','sells5','smart_hits','smart_flow','age_min','url']],use_container_width=True,hide_index=True,column_config={'url':st.column_config.LinkColumn('DEX')})
st.subheader('🧠 آخرین خریدهای Smart Money')
if wdf.empty: st.info('برای ثبت خریدهای واقعی کیف‌پول، HELIUS_API_KEY لازم است.')
else: st.dataframe(wdf,use_container_width=True,hide_index=True,column_config={'signature':st.column_config.LinkColumn('TX')})
