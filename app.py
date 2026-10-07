import os, json, time, sqlite3, traceback, math
from datetime import datetime, timezone

import requests
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

DEX = "https://api.dexscreener.com"
HELIUS = "https://api.helius.xyz"
SOLANA_RPC = "https://api.mainnet-beta.solana.com"
WSOL = "So11111111111111111111111111111111111111112"
VERSION = "5.1.1-learning-fixed"
DB = "radar_v5.db"

# First verified Smart Money wallet supplied by the user.
BCRTEX = "BCrTEXmWutwPz8qv6w1S5gDbaLnSLpXKM5kSGVWyyfxu"

st.set_page_config(page_title="Solana Meme Radar V5", page_icon="🎯", layout="wide")


# ---------- configuration ----------
def secret(name, default=""):
    try:
        v = st.secrets.get(name, os.getenv(name, default))
    except Exception:
        v = os.getenv(name, default)
    return v if v is not None else default

def f(v, d=0.0):
    try: return float(v)
    except Exception: return d

def i(v, d=0):
    try: return int(float(v))
    except Exception: return d

def now():
    return datetime.now(timezone.utc).timestamp()

def iso(ts=None):
    return datetime.fromtimestamp(ts or now(), timezone.utc).isoformat()

def db():
    c = sqlite3.connect(DB, timeout=20)
    c.execute("PRAGMA busy_timeout=20000")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, token TEXT,
      symbol TEXT, name TEXT, price REAL, mc REAL, liq REAL,
      vol5 REAL, vol15 REAL, vol1h REAL, buys5 INT, sells5 INT,
      buys15 INT, sells15 INT, pc5 REAL, pc1h REAL, age_min REAL,
      unique_buyers REAL, unique_sellers REAL, top20_pct REAL,
      mint_authority TEXT, freeze_authority TEXT, security_score REAL,
      smart_hits INT, smart_flow REAL, smart_wallets TEXT,
      volume_accel REAL, buy_sell_ratio REAL, alpha REAL,
      risk REAL, confidence REAL, signal TEXT, url TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_snap_token_ts ON snapshots(token,ts);
    CREATE TABLE IF NOT EXISTS smart_buys(
      id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT, label TEXT,
      weight REAL, token TEXT, ts REAL, signature TEXT, amount REAL,
      UNIQUE(wallet,signature,token)
    );
    CREATE TABLE IF NOT EXISTS outcomes(
      id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_id INT, token TEXT,
      captured_ts REAL, return_5m REAL, return_15m REAL,
      return_1h REAL, return_4h REAL, return_24h REAL,
      mae_1h REAL, mae_4h REAL, mae_24h REAL
    );
    CREATE TABLE IF NOT EXISTS model_runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, horizon TEXT,
      samples INT, positive_rate REAL, auc REAL, brier REAL,
      features TEXT, model_json TEXT
    );
    CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, message TEXT, detail TEXT
    );
    CREATE TABLE IF NOT EXISTS health(
      key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
    );
    """)
    c.commit(); c.close()

def event(kind, msg, detail=""):
    try:
        c=db(); c.execute("INSERT INTO events(ts,kind,message,detail) VALUES(?,?,?,?)",
                          (now(),kind,msg,detail[-4000:])); c.commit(); c.close()
    except Exception: pass

def health(k,v):
    try:
        c=db()
        c.execute("""INSERT INTO health(key,value,updated_at) VALUES(?,?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at""",
                  (k,str(v),iso()))
        c.commit(); c.close()
    except Exception: pass

def hget(k,d="UNKNOWN"):
    try:
        c=db(); r=c.execute("SELECT value FROM health WHERE key=?",(k,)).fetchone(); c.close()
        return r[0] if r else d
    except Exception: return d


# ---------- resilient HTTP ----------
def get_json(url, params=None, headers=None, timeout=15, retries=3, provider="http"):
    last=None
    for n in range(retries):
        try:
            r=requests.get(url,params=params,headers=headers or {},timeout=timeout)
            if r.status_code==429:
                time.sleep(min(8,2**n)); last=RuntimeError("rate limited"); continue
            r.raise_for_status()
            data=r.json(); health(provider,"OK"); return data
        except Exception as e:
            last=e; event("retry",provider,traceback.format_exc())
            time.sleep(min(5,1.6**n))
    health(provider,f"DEGRADED: {last}")
    return None


# ---------- DEX candidate + market data ----------
def dex(path, params=None):
    return get_json(DEX+path, params=params,
                    headers={"User-Agent":f"SolanaMemeRadar/{VERSION}"},
                    provider="dexscreener")

def candidates():
    out={}
    for path in ("/token-profiles/latest/v1","/token-boosts/latest/v1",
                 "/token-boosts/top/v1"):
        data=dex(path) or []
        for x in data:
            if x.get("chainId")=="solana" and x.get("tokenAddress"):
                out[x["tokenAddress"]]=1
    return list(out)[:220]

def hydrate(addr):
    try:
        pairs=dex(f"/token-pairs/v1/solana/{addr}") or []
        pairs=[p for p in pairs if p.get("chainId")=="solana"]
        if not pairs: return None
        p=max(pairs,key=lambda z:f((z.get("liquidity") or {}).get("usd")))
        tx=p.get("txns") or {}; vol=p.get("volume") or {}; ch=p.get("priceChange") or {}
        m5=tx.get("m5") or {}; m15=tx.get("m15") or {}; h1=tx.get("h1") or {}
        created=f(p.get("pairCreatedAt"),now()*1000)
        age=max(0,(now()*1000-created)/60000)
        return {
          "address":addr,
          "symbol":(p.get("baseToken") or {}).get("symbol","?"),
          "name":(p.get("baseToken") or {}).get("name","Unknown"),
          "price":f(p.get("priceUsd")),
          "mc":f(p.get("marketCap") or p.get("fdv")),
          "liq":f((p.get("liquidity") or {}).get("usd")),
          "vol5":f(vol.get("m5")), "vol15":f(vol.get("m15")), "vol1h":f(vol.get("h1")),
          "buys5":i(m5.get("buys")), "sells5":i(m5.get("sells")),
          "buys15":i(m15.get("buys")), "sells15":i(m15.get("sells")),
          "buys1h":i(h1.get("buys")), "sells1h":i(h1.get("sells")),
          "pc5":f(ch.get("m5")), "pc1h":f(ch.get("h1")),
          "age_min":age,
          "url":p.get("url") or f"https://dexscreener.com/solana/{addr}",
          "pair":p.get("pairAddress",""),
          "info":p.get("info") or {}
        }
    except Exception:
        event("hydrate_error",addr,traceback.format_exc())
        return None


# ---------- Smart Money ----------
def load_wallets():
    wallets=[{"address":BCRTEX,"label":"BCrTEX","weight":1.0}]
    raw=secret("SMART_WALLETS","")
    if raw:
        try:
            extra=json.loads(raw)
            for x in extra:
                a=x.get("address")
                if a and a!=BCRTEX:
                    wallets.append({
                      "address":a,
                      "label":x.get("label",a[:8]),
                      "weight":f(x.get("weight"),1.0)
                    })
        except Exception:
            event("config_error","SMART_WALLETS invalid JSON")
    return wallets

def helius_history(wallet, limit=100):
    key=secret("HELIUS_API_KEY")
    if not key: return []
    # Wallet API is preferable to the older enhanced-transactions heuristic.
    data=get_json(f"{HELIUS}/v1/wallet/{wallet}/history",
                  params={"api-key":key,"limit":limit,"tokenAccounts":"balanceChanged"},
                  provider="helius_wallet")
    if isinstance(data,dict): return data.get("data",[])
    return data or []

def parse_wallet_token_changes(row):
    # This is intentionally conservative: a received token is a BUY candidate,
    # not automatically a confirmed swap. Confirmation can be added with Parsed Events.
    changes=row.get("balanceChanges") or []
    out=[]
    for x in changes:
        mint=x.get("mint")
        amt=f(x.get("amount"))
        if mint and mint not in (WSOL,"So11111111111111111111111111111111111111111") and amt>0:
            out.append((mint,amt))
    return out

def collect_smart():
    wallets=load_wallets(); window=f(secret("SMART_WINDOW_MINUTES","30"),30)*60
    cutoff=now()-window; found={}
    key=secret("HELIUS_API_KEY")
    if not key:
        health("smart_money","NOT_CONFIGURED"); return found

    c=db()
    for w in wallets:
        for row in helius_history(w["address"],i(secret("WALLET_LOOKBACK_TX","100"),100)):
            ts=f(row.get("timestamp") or row.get("blockTime"))
            if not ts or ts<cutoff or ts>now()+120: continue
            sig=row.get("signature","")
            for mint,amt in parse_wallet_token_changes(row):
                c.execute("""INSERT OR IGNORE INTO smart_buys
                             (wallet,label,weight,token,ts,signature,amount)
                             VALUES(?,?,?,?,?,?,?)""",
                          (w["address"],w["label"],w["weight"],mint,ts,sig,amt))
                found.setdefault(mint,[]).append(w)
    c.commit(); c.close()
    health("smart_money","OK")
    return found

def smart_summary(token, window_min=30):
    cutoff=now()-window_min*60
    c=db()
    rows=c.execute("""SELECT wallet,label,weight,ts,amount FROM smart_buys
                      WHERE token=? AND ts>? ORDER BY ts DESC""",(token,cutoff)).fetchall()
    c.close()
    unique={r[0]:r for r in rows}
    hits=len(unique)
    weighted=sum(f(r[2],1) for r in unique.values())
    latest=max([r[3] for r in unique.values()],default=0)
    labels=[r[1] for r in unique.values()]
    return hits,weighted,latest,labels


# ---------- on-chain holder/security ----------
def rpc(method, params):
    return get_json(SOLANA_RPC,headers={"Content-Type":"application/json"},
                    timeout=15,provider="solana_rpc") if False else _rpc_post(method,params)

def _rpc_post(method,params):
    try:
        r=requests.post(SOLANA_RPC,json={"jsonrpc":"2.0","id":1,"method":method,"params":params},timeout=15)
        r.raise_for_status(); health("solana_rpc","OK"); return r.json().get("result")
    except Exception as e:
        health("solana_rpc",f"DEGRADED: {e}"); return None

def largest_accounts(mint):
    res=_rpc_post("getTokenLargestAccounts",[mint,{"commitment":"confirmed"}])
    vals=(res or {}).get("value",[]) if isinstance(res,dict) else []
    return vals

def token_supply(mint):
    res=_rpc_post("getTokenSupply",[mint,{"commitment":"confirmed"}])
    return ((res or {}).get("value") or {}) if isinstance(res,dict) else {}

def security_snapshot(mint):
    # Concentration is computed from the top 20 token accounts. Authority checks
    # are intentionally left as a data hook because raw mint parsing varies by
    # legacy SPL vs Token-2022; a connected security provider can fill them later.
    vals=largest_accounts(mint)
    sup=token_supply(mint)
    total=f(sup.get("uiAmount"))
    top20=sum(f(x.get("uiAmount")) for x in vals[:20])
    pct=(top20/total*100) if total else 0
    risk=0
    if pct>=60: risk+=35
    elif pct>=45: risk+=25
    elif pct>=30: risk+=15
    elif pct>=20: risk+=8
    # Very thin liquidity is a hard risk signal in this radar.
    return {
      "top20_pct":pct,
      "mint_authority":"UNKNOWN",
      "freeze_authority":"UNKNOWN",
      "security_score":max(0,100-risk)
    }


# ---------- microstructure + scoring ----------
def prior_volume(token):
    c=db()
    # Latest stored volume before the current observation.
    rows=c.execute("""SELECT ts,vol5,price FROM snapshots
                      WHERE token=? ORDER BY ts DESC LIMIT 8""",(token,)).fetchall()
    c.close()
    return rows

def volume_acceleration(x, prior):
    if x["vol5"]<=0: return 0
    if not prior: return 1.0
    prev=max(f(r[1]) for r in prior[:6])
    if prev<=0: return 1.0
    return x["vol5"]/prev

def alpha_score(x, sm_hits, sm_weight, accel, sec):
    # 100-point precision-oriented score.
    smart=min(30, sm_hits*7 + min(9,sm_weight*3))
    buy_sell=(x["buys5"]+1)/(x["sells5"]+1)
    micro=0
    if buy_sell>=3: micro+=9
    elif buy_sell>=2: micro+=7
    elif buy_sell>=1.35: micro+=4
    if accel>=5: micro+=9
    elif accel>=3: micro+=7
    elif accel>=1.8: micro+=5
    elif accel>=1.25: micro+=2
    if x["buys5"]>=25: micro+=2
    mom=0
    if 0<x["age_min"]<=120:
        if x["pc5"]>=25: mom=15
        elif x["pc5"]>=12: mom=11
        elif x["pc5"]>=5: mom=7
        elif x["pc5"]>=2: mom=3
    else:
        if x["pc5"]>=20: mom=12
        elif x["pc5"]>=8: mom=8
        elif x["pc5"]>=3: mom=4
    liq=10 if x["liq"]>=150000 else 8 if x["liq"]>=75000 else 5 if x["liq"]>=30000 else 2
    dist=10 if sec["top20_pct"]<20 else 8 if sec["top20_pct"]<30 else 5 if sec["top20_pct"]<45 else 2 if sec["top20_pct"]<60 else 0
    security=10 if sec["security_score"]>=90 else 7 if sec["security_score"]>=75 else 3
    # Small age bonus only when market structure is already active.
    age=5 if 2<=x["age_min"]<=45 and x["liq"]>=30000 else 2 if x["age_min"]<=180 else 0
    raw=smart+micro+mom+liq+dist+security+age
    # Hard penalties for obvious low-quality conditions.
    if x["liq"]<15000: raw-=20
    if x["mc"] and x["liq"]/x["mc"]<0.03: raw-=8
    if x["pc5"]<-12: raw-=12
    return max(0,min(100,raw)), buy_sell

def risk_score(x,sec,accel):
    r=0
    if x["liq"]<15000: r+=35
    elif x["liq"]<30000: r+=20
    if sec["top20_pct"]>=60: r+=35
    elif sec["top20_pct"]>=45: r+=25
    elif sec["top20_pct"]>=30: r+=15
    if x["buys5"]+x["sells5"]>80 and x["buys5"]<x["sells5"]*0.65: r+=15
    if accel>12 and x["buys5"]<10: r+=15  # possible volume anomaly
    if x["age_min"]<2: r+=10
    return min(100,r)

def confidence(x, sm_hits, sec, accel):
    completeness=0
    completeness += 25 if secret("HELIUS_API_KEY") else 5
    completeness += 20 if x["liq"]>0 else 0
    completeness += 20 if x["vol5"]>0 else 0
    completeness += 20 if sec["top20_pct"]>0 else 0
    completeness += 15 if x["price"]>0 else 0
    agreement=0
    agreement += 20 if sm_hits>=2 else 10 if sm_hits==1 else 0
    agreement += 20 if accel>=2 else 10 if accel>=1.3 else 0
    agreement += 20 if x["buys5"]>x["sells5"]*1.4 else 8 if x["buys5"]>x["sells5"] else 0
    agreement += 20 if x["pc5"]>5 else 8 if x["pc5"]>2 else 0
    agreement += 20 if sec["top20_pct"]<30 else 8 if sec["top20_pct"]<45 else 0
    return min(99,round((completeness*0.55+agreement*0.45)))

def signal(alpha,risk,conf):
    if risk>=60: return "🔴 DANGER"
    if alpha>=88 and conf>=70 and risk<35: return "🚀 CONFIRMED PUMP"
    if alpha>=76 and conf>=55 and risk<45: return "🟢 EARLY PUMP"
    if alpha>=65 and conf>=45 and risk<55: return "🟡 WATCH"
    return "⚪ LOW"

def reasons(x,sm_hits,accel,sec,bs,alpha,risk):
    pos=[]; neg=[]
    if sm_hits>=2: pos.append(f"{sm_hits} Smart Money wallets converged")
    elif sm_hits==1: pos.append("1 verified Smart Money wallet activity")
    if accel>=2: pos.append(f"volume acceleration {accel:.1f}x")
    if bs>=1.5: pos.append(f"buy/sell {bs:.1f}x")
    if x["pc5"]>=8: pos.append(f"5m momentum +{x['pc5']:.1f}%")
    if x["liq"]>=75000: pos.append(f"liquidity ${x['liq']:,.0f}")
    if sec["top20_pct"]>=45: neg.append(f"top-20 concentration {sec['top20_pct']:.1f}%")
    if x["liq"]<30000: neg.append("thin liquidity")
    if risk>=40: neg.append(f"risk score {risk}/100")
    if x["pc5"]<-5: neg.append("negative short-term momentum")
    return pos[:3],neg[:3]

# ---------- scan + outcome logger ----------
def scan():
    init_db()
    smart=collect_smart()
    rows=[]
    for addr in candidates():
        x=hydrate(addr)
        if not x: continue
        prior=prior_volume(addr)
        accel=volume_acceleration(x,prior)
        hits,weight,latest,labels=smart_summary(addr,30)
        sec=security_snapshot(addr)
        alpha,bs=alpha_score(x,hits,weight,accel,sec)
        risk=risk_score(x,sec,accel)
        conf=confidence(x,hits,sec,accel)
        mlp=ml_probability({
          "alpha":alpha,"confidence":conf,"risk":risk,"smart_hits":hits,"smart_flow":weight,
          "volume_accel":accel,"buy_sell_ratio":bs,"liq":x["liq"],"mc":x["mc"],
          "pc5":x["pc5"],"pc1h":x["pc1h"],"age_min":x["age_min"],
          "top20_pct":sec["top20_pct"],"security_score":sec["security_score"],
          "buys5":x["buys5"],"sells5":x["sells5"],"vol5":x["vol5"],"vol15":x["vol15"],"vol1h":x["vol1h"]
        },"1h")
        learned=learned_score(alpha,mlp)
        sig=signal(learned,risk,conf)
        pos,neg=reasons(x,hits,accel,sec,bs,alpha,risk)
        c=db()
        c.execute("""INSERT INTO snapshots(
          ts,token,symbol,name,price,mc,liq,vol5,vol15,vol1h,buys5,sells5,
          buys15,sells15,pc5,pc1h,age_min,unique_buyers,unique_sellers,
          top20_pct,mint_authority,freeze_authority,security_score,smart_hits,
          smart_flow,smart_wallets,volume_accel,buy_sell_ratio,alpha,risk,confidence,signal,url)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,(
          now(),addr,x["symbol"],x["name"],x["price"],x["mc"],x["liq"],x["vol5"],x["vol15"],x["vol1h"],
          x["buys5"],x["sells5"],x["buys15"],x["sells15"],x["pc5"],x["pc1h"],x["age_min"],
          None,None,sec["top20_pct"],sec["mint_authority"],sec["freeze_authority"],sec["security_score"],
          hits,weight,json.dumps(labels),accel,bs,alpha,risk,conf,sig,x["url"]
        ))
        c.commit(); c.close()
        x.update({"alpha":learned,"raw_alpha":alpha,"ml_probability":mlp,"risk":risk,"confidence":conf,"signal":sig,
                  "smart_hits":hits,"smart_weight":weight,"smart_wallets":", ".join(labels),
                  "volume_accel":accel,"buy_sell_ratio":bs,"top20_pct":sec["top20_pct"],
                  "security_score":sec["security_score"],"pos":pos,"neg":neg})
        rows.append(x)
    health("last_scan",iso()); health("last_scan_rows",len(rows))
    return rows

def evaluate_outcomes():
    """Safely label snapshots using later snapshots of the same token."""
    init_db()
    horizons=[("5m",300),("15m",900),("1h",3600),("4h",14400),("24h",86400)]
    c=db()
    # Older builds did not declare snapshot_id UNIQUE. Add a unique index so
    # INSERT ... ON CONFLICT(snapshot_id) is valid and duplicate labels are avoided.
    try:
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_outcomes_snapshot_id ON outcomes(snapshot_id)")
    except Exception:
        pass
    raw_snaps=c.execute("SELECT id,token,ts,price FROM snapshots WHERE price IS NOT NULL AND price>0 ORDER BY token,ts").fetchall()
    raw_existing=c.execute("SELECT snapshot_id,return_5m,return_15m,return_1h,return_4h,return_24h FROM outcomes").fetchall()
    c.close()

    snaps=[]
    for sid,token,ts,p0 in raw_snaps:
        try:
            snaps.append((int(sid),str(token),float(ts),float(p0)))
        except (TypeError,ValueError):
            continue

    existing={}
    for row in raw_existing:
        try:
            existing[int(row[0])] = row
        except (TypeError,ValueError):
            continue

    by={}
    for row in snaps:
        by.setdefault(row[1],[]).append(row)

    updated=0
    c=db()
    for sid,token,ts,p0 in snaps:
        old=existing.get(sid)
        vals=list(old) if old else [sid,None,None,None,None,None]
        for idx,(_,secs) in enumerate(horizons,1):
            if vals[idx] is not None:
                continue
            target=ts+float(secs)
            future=None
            for z in by.get(token,[]):
                try:
                    if float(z[2]) >= target and int(z[0]) > sid:
                        future=z
                        break
                except (TypeError,ValueError,IndexError):
                    continue
            if future is not None:
                try:
                    future_price=float(future[3])
                    if future_price > 0 and p0 > 0:
                        vals[idx]=(future_price/p0-1.0)*100.0
                except (TypeError,ValueError,ZeroDivisionError):
                    pass

        if any(v is not None for v in vals[1:]):
            c.execute("""INSERT INTO outcomes(snapshot_id,token,captured_ts,return_5m,return_15m,return_1h,return_4h,return_24h)
                         VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(snapshot_id) DO UPDATE SET
                         return_5m=COALESCE(excluded.return_5m,outcomes.return_5m),
                         return_15m=COALESCE(excluded.return_15m,outcomes.return_15m),
                         return_1h=COALESCE(excluded.return_1h,outcomes.return_1h),
                         return_4h=COALESCE(excluded.return_4h,outcomes.return_4h),
                         return_24h=COALESCE(excluded.return_24h,outcomes.return_24h)""",
                      (sid,token,now(),vals[1],vals[2],vals[3],vals[4],vals[5]))
            updated+=1
    c.commit(); c.close()
    return updated

FEATURES=["alpha","confidence","risk","smart_hits","smart_flow","volume_accel",
          "buy_sell_ratio","liq","mc","pc5","pc1h","age_min","top20_pct",
          "security_score","buys5","sells5","vol5","vol15","vol1h"]

def train_models(min_samples=120):
    try:
        from sklearn.pipeline import Pipeline
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score,brier_score_loss
    except Exception as ex:
        event("ml_error","scikit-learn unavailable",str(ex)); return {}
    evaluate_outcomes()
    c=db()
    df=pd.read_sql_query("""SELECT s.ts,s.token,s.alpha,s.confidence,s.risk,s.smart_hits,s.smart_flow,
        s.volume_accel,s.buy_sell_ratio,s.liq,s.mc,s.pc5,s.pc1h,s.age_min,s.top20_pct,
        s.security_score,s.buys5,s.sells5,s.vol5,s.vol15,s.vol1h,
        o.return_15m,o.return_1h,o.return_4h
        FROM snapshots s JOIN outcomes o ON o.snapshot_id=s.id ORDER BY s.ts ASC""",c)
    c.close()
    models={}
    for horizon,col in [("15m","return_15m"),("1h","return_1h"),("4h","return_4h")]:
        d=df.dropna(subset=[col]).copy()
        if len(d)<min_samples: continue
        y=(d[col]>=50).astype(int)
        if y.nunique()<2: continue
        cut=max(int(len(d)*.75),len(d)-max(30,int(len(d)*.25)))
        train,val=d.iloc[:cut],d.iloc[cut:]
        model=Pipeline([("imp",SimpleImputer(strategy="median")),
                        ("scale",StandardScaler()),
                        ("clf",LogisticRegression(max_iter=2000,class_weight="balanced"))])
        model.fit(train[FEATURES],(train[col]>=50).astype(int))
        prob=model.predict_proba(val[FEATURES])[:,1]
        yt=(val[col]>=50).astype(int)
        auc=float(roc_auc_score(yt,prob)) if yt.nunique()>1 else None
        brier=float(brier_score_loss(yt,prob))
        imp=model.named_steps["imp"]; sc=model.named_steps["scale"]; clf=model.named_steps["clf"]
        payload={"intercept":float(clf.intercept_[0]),"coef":[float(x) for x in clf.coef_[0]],
                 "features":FEATURES,"median":imp.statistics_.tolist(),
                 "mean":sc.mean_.tolist(),"scale":sc.scale_.tolist()}
        c=db()
        c.execute("""INSERT INTO model_runs(ts,horizon,samples,positive_rate,auc,brier,features,model_json)
                     VALUES(?,?,?,?,?,?,?,?)""",
                  (now(),horizon,len(d),float(y.mean()),auc,brier,json.dumps(FEATURES),json.dumps(payload)))
        c.commit(); c.close()
        models[horizon]=(payload,len(d),auc,brier)
    return models

def latest_models():
    c=db()
    rows=c.execute("""SELECT horizon,model_json,samples,auc,brier FROM model_runs
                      WHERE id IN (SELECT MAX(id) FROM model_runs GROUP BY horizon)""").fetchall()
    c.close()
    out={}
    for h,m,s,a,b in rows:
        try: out[h]=(json.loads(m),s,a,b)
        except Exception: pass
    return out

def ml_probability(row,horizon="1h"):
    m=latest_models().get(horizon)
    if not m or m[1]<120: return None
    payload=m[0]; xs=[]
    for k,med,mu,sd in zip(payload["features"],payload["median"],payload["mean"],payload["scale"]):
        try: v=float(row.get(k,0) or 0)
        except Exception: v=med
        if not math.isfinite(v): v=med
        xs.append((v-mu)/(sd if sd else 1))
    z=payload["intercept"]+sum(a*b for a,b in zip(payload["coef"],xs))
    z=max(-30,min(30,z))
    return 1/(1+math.exp(-z))

def learned_score(alpha,prob):
    return alpha if prob is None else max(0,min(100,.70*alpha+30*prob))



# ---------- V6 CLEAN UI ----------
init_db()
interval = max(45, i(secret("DEX_SCAN_SECONDS", "60"), 60))
st_autorefresh(interval=interval * 1000, key="v6refresh")

st.markdown("""
<style>
.block-container {padding-top: 1.4rem; padding-bottom: 2rem; max-width: 1450px;}
[data-testid="stMetric"] {border: 1px solid rgba(128,128,128,.20); border-radius: 16px; padding: 12px 14px;}
.radar-card {
    border: 1px solid rgba(128,128,128,.22);
    border-radius: 18px;
    padding: 18px;
    margin: 8px 0 14px 0;
    background: rgba(128,128,128,.035);
}
.radar-score {font-size: 34px; font-weight: 800; line-height: 1;}
.radar-muted {opacity: .68; font-size: 13px;}
.radar-pill {
    display:inline-block; padding:5px 10px; border-radius:999px;
    border:1px solid rgba(128,128,128,.25); font-size:12px; margin-right:5px;
}
.radar-kpi {font-size: 16px; font-weight: 700;}
</style>
""", unsafe_allow_html=True)

with st.sidebar:
    st.header("⚙️ Radar")
    st.caption(f"V6 Clean UI · auto scan هر {interval} ثانیه")

    if st.button("🔄 Scan now", use_container_width=True):
        with st.spinner("در حال اسکن..."):
            scan()
        st.success("اسکن انجام شد.")

    with st.expander("🧠 Learning", expanded=False):
        if st.button("Evaluate outcomes", use_container_width=True):
            n = evaluate_outcomes()
            st.success(f"{n} snapshot بررسی شد.")
        if st.button("Train learning model", use_container_width=True):
            with st.spinner("آموزش مدل..."):
                m = train_models()
            st.success("مدل‌های فعال: " + (", ".join(m.keys()) if m else "داده کافی نیست"))

    with st.expander("🩺 Provider Health", expanded=False):
        st.json({
            "DEX Screener": hget("dexscreener"),
            "Helius Wallet API": hget("helius_wallet", "NOT_CONFIGURED"),
            "Solana RPC": hget("solana_rpc"),
            "Smart Money": hget("smart_money", "NOT_CONFIGURED"),
            "Last scan": hget("last_scan", "—")
        })

    with st.expander("ℹ️ About", expanded=False):
        st.caption("Research/alert engine؛ نه مشاوره مالی و نه اجرای خودکار معامله.")

st.markdown("## 🎯 Solana Meme Radar")
st.caption("Smart Money Intelligence · Clean V6")

if "last_scan" not in st.session_state or time.time() - st.session_state.last_scan > interval - 5:
    try:
        scan()
        st.session_state.last_scan = time.time()
    except Exception as ex:
        st.warning(f"Scan error: {ex}")

c = db()
df = pd.read_sql_query("""
    SELECT * FROM snapshots
    WHERE id IN (SELECT MAX(id) FROM snapshots GROUP BY token)
    ORDER BY alpha DESC, confidence DESC
    LIMIT 100
""", c)
wdf = pd.read_sql_query("""
    SELECT wallet,label,weight,token,datetime(ts,'unixepoch') AS time,
           amount,signature
    FROM smart_buys ORDER BY ts DESC LIMIT 100
""", c)
edf = pd.read_sql_query("""
    SELECT token,COUNT(*) alerts,AVG(return_5m) avg_return_5m,
           MAX(return_5m) max_return_5m
    FROM outcomes GROUP BY token ORDER BY avg_return_5m DESC LIMIT 30
""", c)
c.close()

# Top counters
m1, m2, m3, m4 = st.columns(4)
m1.metric("Tracked", len(df))
m2.metric("🚀 Confirmed", int((df.alpha >= 88).sum()) if not df.empty else 0)
m3.metric("🟢 Early", int(((df.alpha >= 76) & (df.alpha < 88)).sum()) if not df.empty else 0)
m4.metric("🐋 Smart Money", int(df.smart_hits.sum()) if not df.empty else 0)

st.markdown("### 🔥 Top Signals")

if df.empty:
    st.info("هنوز داده‌ای نیست؛ چند ثانیه صبر کن یا Scan now را بزن.")
else:
    top = df.head(8)

    for _, r in top.iterrows():
        sig = str(r["signal"])
        alpha = float(r["alpha"])
        conf = float(r["confidence"])
        risk = float(r["risk"])
        symbol = str(r["symbol"])
        name = str(r["name"])

        if "DANGER" in sig:
            badge = "🔴 DANGER"
        elif "CONFIRMED" in sig:
            badge = "🚀 CONFIRMED"
        elif "EARLY" in sig:
            badge = "🟢 EARLY PUMP"
        elif "WATCH" in sig:
            badge = "🟡 WATCH"
        else:
            badge = "⚪ LOW"

        left, right = st.columns([4, 1])
        with left:
            st.markdown(f"""
            <div class="radar-card">
              <div class="radar-muted">{badge}</div>
              <div style="font-size:25px;font-weight:800;margin-top:4px;">${symbol}</div>
              <div class="radar-muted">{name}</div>
              <div style="margin-top:16px;">
                <span class="radar-score">{alpha:.0f}</span>
                <span class="radar-muted"> / 100 Alpha</span>
              </div>
              <div style="margin-top:12px;">
                <span class="radar-pill">Confidence {conf:.0f}%</span>
                <span class="radar-pill">Risk {risk:.0f}</span>
                <span class="radar-pill">🐋 {int(r['smart_hits'])} Smart Money</span>
              </div>
            </div>
            """, unsafe_allow_html=True)

            p1, p2, p3, p4 = st.columns(4)
            p1.metric("Volume", f"{float(r['volume_accel']):.1f}x")
            p2.metric("Buy / Sell", f"{float(r['buy_sell_ratio']):.2f}x")
            p3.metric("Liquidity", f"${float(r['liq']):,.0f}")
            p4.metric("5m", f"{float(r['pc5']):+.1f}%")

            pos = []
            neg = []
            if int(r["smart_hits"]) >= 2:
                pos.append(f"{int(r['smart_hits'])} کیف پول Smart Money همگرا شده‌اند")
            elif int(r["smart_hits"]) == 1:
                pos.append("فعالیت Smart Money تأیید شده")
            if float(r["volume_accel"]) >= 2:
                pos.append(f"شتاب حجم {float(r['volume_accel']):.1f}x")
            if float(r["buy_sell_ratio"]) >= 1.5:
                pos.append(f"نسبت خرید/فروش {float(r['buy_sell_ratio']):.1f}x")
            if float(r["pc5"]) >= 5:
                pos.append(f"مومنتوم ۵ دقیقه‌ای +{float(r['pc5']):.1f}%")
            if float(r["liq"]) >= 75000:
                pos.append(f"نقدینگی ${float(r['liq']):,.0f}")

            if float(r["top20_pct"]) >= 45:
                neg.append(f"تمرکز Top20: {float(r['top20_pct']):.1f}%")
            if float(r["liq"]) < 30000:
                neg.append("نقدینگی پایین")
            if risk >= 40:
                neg.append(f"Risk = {risk:.0f}")
            if float(r["pc5"]) < -5:
                neg.append("مومنتوم کوتاه‌مدت منفی")

            with st.expander("🔎 Why this token?", expanded=False):
                st.write("**مثبت:** " + (" • ".join(pos[:4]) if pos else "داده مثبت کافی نیست"))
                st.write("**ریسک:** " + (" • ".join(neg[:3]) if neg else "پرچم منفی مهمی ثبت نشده"))
                st.link_button("باز کردن در DEX Screener", str(r["url"]))

        with right:
            st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

st.markdown("---")

with st.expander("🐋 Smart Money Feed", expanded=False):
    if wdf.empty:
        st.info("برای Smart Money واقعی، HELIUS_API_KEY لازم است.")
    else:
        st.dataframe(
            wdf,
            use_container_width=True,
            hide_index=True,
            column_config={"signature": st.column_config.LinkColumn("TX")}
        )

with st.expander("📈 Outcome / Backtest", expanded=False):
    if edf.empty:
        st.info("هنوز نتیجه‌ای ثبت نشده؛ با جمع شدن snapshotها می‌توانی Evaluate outcomes را اجرا کنی.")
    else:
        st.dataframe(edf, use_container_width=True, hide_index=True)

with st.expander("🧠 Learning Engine", expanded=False):
    lm = latest_models()
    if not lm:
        st.info("مدل هنوز فعال نیست؛ پس از جمع شدن داده کافی، Train learning model را اجرا کن.")
    else:
        model_rows = [
            {"Horizon": h, "Samples": v[1], "Validation AUC": v[2], "Brier": v[3]}
            for h, v in lm.items()
        ]
        st.dataframe(pd.DataFrame(model_rows), use_container_width=True, hide_index=True)
        st.caption("مدل فقط Alpha را تنظیم می‌کند و جایگزین Risk/Security نیست.")
