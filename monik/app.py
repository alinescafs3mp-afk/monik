"""Read-only HTTP API, owner authentication, one SSE notification hub."""
from __future__ import annotations
import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections import defaultdict,deque
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import FileResponse,JSONResponse,StreamingResponse,Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .collector import Collector
from .config import expand
from .model import timestamp,dumps
from .storage import Store

WEB=Path(__file__).parent/'web'
CSP="default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self'"


def query(request):
    raw=request.query_params;out={}
    for key in ('profile','thread_id','model','kind','q'):
        value=raw.get(key,'').strip()
        if len(value)>(256 if key=='q' else 128): raise HTTPException(400,'Filter too long')
        if value: out[key]=value
    for key in ('since','until','at'):
        if raw.get(key):
            value=timestamp(raw[key])
            if value is None: raise HTTPException(400,'Use an ISO timestamp with timezone')
            out[key]=value
    for key in ('before','after','limit'):
        if raw.get(key) is not None:
            try: value=int(raw[key])
            except ValueError: raise HTTPException(400,'Invalid integer')
            if not 0<=value<2**63 or (key=='limit' and not 1<=value<=200): raise HTTPException(400,'Integer out of bounds')
            out[key]=value
    if 'before' in out and 'after' in out: raise HTTPException(400,'Choose before or after')
    if out.get('since',0)>out.get('until',float('inf')): raise HTTPException(400,'Invalid time range')
    out['view']=raw.get('view','event');out['search_mode']=raw.get('search_mode','prefix')
    if out['view'] not in ('event','knowledge') or out['search_mode'] not in ('prefix','substring'): raise HTTPException(400,'Unsupported view/search mode')
    return out


def create_app(config,*,collect=True):
    token_path=expand(config['token_file'])
    if token_path.stat().st_mode & 0o077 or token_path.stat().st_uid!=os.geteuid(): raise ValueError('Owner token must be owned by service user, mode 0600')
    token=token_path.read_text().strip()
    if not 32<=len(token)<=256: raise ValueError('Owner token must contain 32..256 characters')
    token_hash=hashlib.sha256(token.encode()).digest();del token
    data=Path(config['data_dir']);data.mkdir(mode=0o700,parents=True,exist_ok=True)
    if data.stat().st_mode & 0o077: raise ValueError('Own data directory must have permissions 0700')
    store=Store(data/'observatory.sqlite');collector=Collector(config,store)
    sessions={};failures=defaultdict(deque);streams=set();condition=asyncio.Condition();latest=[0];closing=asyncio.Event()
    secure=bool(config.get('tls_cert'));scheme='https' if secure else 'http'
    allowed_origins={f"{scheme}://{config['bind']}:{config['port']}"}
    if config['bind']=='127.0.0.1': allowed_origins.add(f"{scheme}://localhost:{config['port']}")

    async def hub():
        while not closing.is_set():
            try:
                maximum=await asyncio.to_thread(store.sequence)
                if maximum!=latest[0]:
                    latest[0]=maximum
                    async with condition: condition.notify_all()
            except sqlite3.Error: pass
            await asyncio.sleep(0.25)

    @asynccontextmanager
    async def lifespan(app):
        lock=open(data/'collector.lock','a')
        try:
            fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close();raise RuntimeError('Another monik instance is using this data directory')
        closing.clear()
        if collect: collector.start()
        task=asyncio.create_task(hub())
        try: yield
        finally:
            closing.set();task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            await asyncio.to_thread(collector.close)
            lock.close()

    app=FastAPI(title='monik',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store=store;app.state.collector=collector;app.state.config=config
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=[config['bind'],'127.0.0.1','localhost'])

    def authorized(request):
        sid=request.cookies.get('monik_session','');expires=sessions.get(sid,0)
        if expires>time.time(): return True
        auth=request.headers.get('authorization','')
        if auth.startswith('Bearer ') and len(auth)<=300:
            return hmac.compare_digest(hashlib.sha256(auth[7:].encode()).digest(),token_hash)
        return False

    @app.middleware('http')
    async def boundary(request,call_next):
        if len(str(request.url))>4096: result=JSONResponse({'detail':'Request too large'},414)
        elif request.method not in ('GET','HEAD') and request.url.path not in ('/api/v1/login','/api/v1/logout'):
            result=JSONResponse({'detail':'Read-only service'},405)
        elif request.url.path.startswith('/api/') and request.url.path!='/api/v1/login' and not authorized(request):
            result=JSONResponse({'detail':'Owner authentication required'},401)
        else:
            try: result=await call_next(request)
            except sqlite3.Error: result=JSONResponse({'detail':'Observer storage unavailable; sources are unaffected'},503)
        result.headers.update({'Content-Security-Policy':CSP,'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cache-Control':'no-store','X-Frame-Options':'DENY','Permissions-Policy':'camera=(), microphone=(), geolocation=()'})
        return result

    @app.post('/api/v1/login')
    async def login(request:Request):
        if request.headers.get('origin') not in allowed_origins: raise HTTPException(403,'Same-origin login required')
        ip=request.client.host if request.client else 'unknown';now=time.time()
        for key in list(failures):
            if not failures[key] or failures[key][-1]<now-60: failures.pop(key,None)
        bucket=failures[ip]
        while bucket and bucket[0]<now-60: bucket.popleft()
        if len(bucket)>=5 or len(failures)>1024: raise HTTPException(429,'Too many login attempts; retry in one minute')
        bucket.append(now);body=bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>4096: raise HTTPException(413,'Login body too large')
        try:
            payload=json.loads(body);value=payload.get('token')
            if not isinstance(value,str) or len(value)>256: raise ValueError()
        except (ValueError,TypeError,AttributeError): raise HTTPException(400,'Expected owner token')
        if not hmac.compare_digest(hashlib.sha256(value.encode()).digest(),token_hash): raise HTTPException(401,'Invalid owner token')
        failures.pop(ip,None)
        for sid,expiry in list(sessions.items()):
            if expiry<now: sessions.pop(sid,None)
        if len(sessions)>=128: raise HTTPException(429,'Too many active sessions')
        sid=secrets.token_urlsafe(32);sessions[sid]=now+43200
        result=JSONResponse({'ok':True});result.set_cookie('monik_session',sid,max_age=43200,httponly=True,secure=secure,samesite='strict',path='/')
        return result

    @app.post('/api/v1/logout')
    async def logout(request:Request):
        if request.headers.get('origin') not in allowed_origins: raise HTTPException(403,'Same-origin request required')
        sessions.pop(request.cookies.get('monik_session',''),None)
        result=JSONResponse({'ok':True});result.delete_cookie('monik_session',path='/');return result

    @app.get('/')
    def index(): return FileResponse(WEB/'index.html',media_type='text/html')
    @app.get('/app.js')
    def javascript(): return FileResponse(WEB/'app.js',media_type='text/javascript')
    @app.get('/style.css')
    def stylesheet(): return FileResponse(WEB/'style.css',media_type='text/css')
    @app.get('/favicon.ico')
    def favicon(): return Response(status_code=204)

    @app.get('/api/v1/overview')
    def overview(request:Request):
        result=store.overview(query(request),collector.profiles);result['demo']=bool(config.get('demo'));return result
    @app.get('/api/v1/events')
    @app.get('/api/v1/search')
    def events(request:Request): return store.events(query(request))
    @app.get('/api/v1/events/{uid}')
    def detail(uid:str,request:Request):
        if len(uid)!=64 or any(x not in '0123456789abcdef' for x in uid): raise HTTPException(400,'Invalid event identifier')
        result=store.detail(uid,query(request))
        if not result: raise HTTPException(404,'No event in this time view')
        # Large redacted bodies are deliberately paged, even on demand.
        try: offset=int(request.query_params.get('offset','0'))
        except ValueError: raise HTTPException(400,'Invalid offset')
        if not 0<=offset<=2_000_000: raise HTTPException(400,'Invalid offset')
        raw=dumps({'data':result.pop('data'),'text':result.pop('text')})
        result.update(payload_page=raw[offset:offset+32768],next_offset=offset+32768 if offset+32768<len(raw) else None,payload_characters=len(raw),offset=offset)
        return result
    @app.get('/api/v1/usage')
    def usage(request:Request): return store.usage(query(request))
    @app.get('/api/v1/limits')
    def limits(request:Request): return store.limits(query(request))
    @app.get('/api/v1/cumulative')
    def cumulative(request:Request): return store.cumulative(query(request))
    @app.get('/api/v1/threads')
    @app.get('/api/v1/assignments')
    def threads(request:Request): return store.threads(query(request))
    @app.get('/api/v1/coverage')
    @app.get('/api/v1/health/sources')
    def health():
        result=store.coverage();result['collector']=collector.status();result['filesystem_boundary']={'landlock_abi':config.get('landlock_abi'),'production_serve_requires_abi':3};s=os.statvfs(data)
        result['disk']={'free_bytes':s.f_bavail*s.f_frsize,'database_bytes':sum(p.stat().st_size for p in data.glob('observatory.sqlite*'))}
        return result
    @app.get('/api/v1/export')
    def export(request:Request): return JSONResponse(store.events(query(request)),headers={'Content-Disposition':'attachment; filename="monik-events.json"'})

    @app.get('/api/v1/stream')
    async def stream(request:Request):
        if len(streams)>=24: raise HTTPException(429,'Too many live browser streams')
        try: cursor=int(request.headers.get('last-event-id') or request.query_params.get('after','0'))
        except ValueError: raise HTTPException(400,'Invalid event cursor')
        if not 0<=cursor<2**63: raise HTTPException(400,'Invalid event cursor')
        tag=object();streams.add(tag)
        async def generate():
            nonlocal cursor
            try:
                with store.connect() as db: maximum=db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
                if cursor>maximum:
                    cursor=0;yield 'id: 0\nevent: reset\ndata: {}\n\n'
                while not closing.is_set() and not await request.is_disconnected() and authorized(request):
                    try: ids=await asyncio.to_thread(store.stream_batch,cursor)
                    except sqlite3.Error:
                        yield 'event: degraded\ndata: {"storage":"unavailable"}\n\n';await asyncio.sleep(2);continue
                    if ids:
                        cursor=ids[-1]
                        yield f'id: {cursor}\nevent: committed\ndata: '+dumps({'ids':ids,'cursor':cursor})+'\n\n'
                        await asyncio.sleep(0)
                    else:
                        try:
                            async with condition:
                                await asyncio.wait_for(condition.wait_for(lambda:latest[0]>cursor or closing.is_set()),timeout=10)
                        except TimeoutError: yield 'event: pulse\ndata: {}\n\n'
            finally: streams.discard(tag)
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no','Cache-Control':'no-store'})
    from .chart_api import router as chart_router
    app.include_router(chart_router)
    return app
