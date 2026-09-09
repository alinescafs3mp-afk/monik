"""Actual loopback HTTP/SSE integration using synthetic sources, not real TUI sessions."""
from __future__ import annotations
import argparse,json,queue,socket,tempfile,threading,time,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx,uvicorn
from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import dumps,normalize


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',default='/tmp/monik-wire.json');args=ap.parse_args();checks=[];latencies=[]
    def check(name,ok,detail=None):
        checks.append({'name':name,'ok':bool(ok),'detail':detail})
        if not ok:raise AssertionError(name+': '+str(detail))
    with tempfile.TemporaryDirectory() as d:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        path=make_demo(Path(d)/'demo',port);config=load(path);config['poll_seconds']=.2;app=create_app(config)
        # Existing history must not delay the single notification hub after restart.
        with app.state.store.connect(write=True) as db:
            for i in range(3000):
                e=normalize({'timestamp':100+i,'type':'response_item','payload':{'type':'message','id':'OLD-'+str(i),'role':'assistant','content':[{'text':'old history'}]}},'astra','ASTRA_DEMO_ROOT',str(i))[0]
                app.state.store.put(db,e,{'source':'synthetic-history','delivery':str(i)})
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False));worker=threading.Thread(target=server.run,daemon=True);worker.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.03)
        base=f'http://127.0.0.1:{port}';token=Path(config['token_file']).read_text().strip();source=Path(config['profiles'][0]['files'][0]['path'])
        def emit(i):
            with source.open('a') as f:f.write(dumps({'timestamp':time.time(),'type':'response_item','payload':{'type':'message','id':'WIRE-'+str(i),'role':'assistant','content':[{'text':'WIRE_MARKER_'+str(i)}]}})+'\n')
        def one_stream(cursor,received,connected,*,event='committed',header=False):
            try:
                headers={'Authorization':'Bearer '+token}
                if header:headers['Last-Event-ID']=str(cursor)
                with httpx.Client(base_url=base,trust_env=False,timeout=8) as client:
                    with client.stream('GET','/api/v1/stream?after='+str(0 if header else cursor),headers=headers) as response:
                        if response.status_code!=200:raise RuntimeError(str(response.status_code))
                        connected.set();block={}
                        for line in response.iter_lines():
                            if line=='':
                                if block.get('event')==event:received.put({'event':block,'time':time.monotonic()});return
                                block={}
                            elif ': ' in line:
                                k,v=line.split(': ',1);block[k]=v
            except Exception as exc:received.put({'error':str(exc)});connected.set()
        try:
            with httpx.Client(base_url=base,trust_env=False,timeout=5) as client:
                check('unauthenticated_http',client.get('/api/v1/overview').status_code==401)
                check('login_cookie_http',client.post('/api/v1/login',headers={'Origin':base},json={'token':token}).status_code==200)
                time.sleep(.15);cursor=client.get('/api/v1/overview').json()['cursor']
                for i in range(8):
                    q=queue.Queue();ready=threading.Event();t=threading.Thread(target=one_stream,args=(cursor,q,ready),kwargs={'header':bool(i%2)},daemon=True);t.start();ready.wait(3)
                    start=time.monotonic();emit(i);answer=q.get(timeout=5);check('sse_delivery_'+str(i),'error' not in answer,answer.get('error'));latencies.append(answer['time']-start)
                    event=answer['event'];data=json.loads(event['data']);check('cursor_advance_'+str(i),data['cursor']>cursor and len(set(data['ids']))==len(data['ids']));cursor=data['cursor'];t.join(1)
                check('local_wire_p95_under_2_seconds',max(latencies)<2,latencies)
                # Two independent consumers receive the same durable commit; no extra collector.
                consumers=[]
                for _ in range(2):
                    q=queue.Queue();ready=threading.Event();t=threading.Thread(target=one_stream,args=(cursor,q,ready),daemon=True);t.start();ready.wait(3);consumers.append((q,t))
                emit(50);results=[q.get(timeout=5) for q,t in consumers]
                check('fanout_same_ids',all('event' in r for r in results) and results[0]['event']['data']==results[1]['event']['data'])
                check('one_collector_for_clients',len([t for t in threading.enumerate() if t.name=='monik-collector'])==1)
                for q,t in consumers:t.join(1)
                # A cursor from a newer database causes explicit reset, not a silent empty stream.
                q=queue.Queue();ready=threading.Event();t=threading.Thread(target=one_stream,args=(2**40,q,ready),kwargs={'event':'reset'},daemon=True);t.start();answer=q.get(timeout=5);check('future_cursor_reset',answer.get('event',{}).get('id')=='0');t.join(1)
                check('http_method_boundary',client.post('/api/v1/tool/execute').status_code==405)
        except Exception as exc:checks.append({'name':'exception','ok':False,'detail':str(exc)})
        finally:server.should_exit=True;worker.join(12)
    result={'ok':all(c['ok'] for c in checks),'checks':checks,'append_to_sse_seconds':latencies,'measurement':'8 synthetic appends, pre-existing 3000-event history, loopback Python HTTP client, poll 0.2s; not a physical-device or production-TUI measurement'};Path(args.output).parent.mkdir(parents=True,exist_ok=True);Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
