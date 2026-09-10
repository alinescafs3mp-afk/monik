"""Render graph in Chromium. Report native navigation and offline DOM separately.

Never changes browser policies. The labelled DOM bridge substitutes fetch/SSE and
history URL writes only after native navigation fails. Synthetic own DB only.
"""
from __future__ import annotations
import argparse,json,math,re,socket,sys,tempfile,threading,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx,uvicorn
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import normalize

ROOT=Path(__file__).resolve().parents[1]
BRIDGE=r'''
window.graphObserverCount=0;
const NativeResizeObserver=window.ResizeObserver;
window.ResizeObserver=class extends NativeResizeObserver{constructor(cb){super(cb);window.graphObserverCount++;}};
history.replaceState=()=>{}; // Opaque about:blank cannot exercise URL navigation.
window.fetch=async(url,options={})=>{
 if(options.signal?.aborted)throw new DOMException('Aborted','AbortError');
 const r=await window.graphRequest(String(url));
 if(options.signal?.aborted)throw new DOMException('Aborted','AbortError');
 return new Response(r.text,{status:r.status,headers:r.headers});
};
window.EventSource=class{
 constructor(url){this.cursor=Number(new URL(url,'http://offline.test').searchParams.get('after')||0);this.listeners={};setTimeout(()=>{this.onopen?.();this.poll();},20);}
 addEventListener(n,f){(this.listeners[n]??=[]).push(f);}
 async poll(){if(this.closed)return;const ids=await window.graphCommits(this.cursor);if(this.closed)return;if(ids.length){this.cursor=ids.at(-1);for(const f of this.listeners.committed||[])f({data:JSON.stringify({ids,cursor:this.cursor})});}this.timer=setTimeout(()=>this.poll(),100);}
 close(){this.closed=true;clearTimeout(this.timer);}
};
'''


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',default='/tmp/monik-graph-browser');ap.add_argument('--chromium',default='/usr/bin/chromium');args=ap.parse_args()
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True);checks=[];errors=[];requests=[];native={'status':'not_run'}
    def check(name,condition,detail=None):
        checks.append({'name':name,'ok':bool(condition),'detail':detail})
        if not condition:raise AssertionError(name+': '+str(detail))
    with tempfile.TemporaryDirectory() as folder:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        cfg=load(make_demo(Path(folder)/'demo',port));app=create_app(cfg,collect=False);store=app.state.store
        end=time.time();anchor=int(end//300)*300
        def record(i,profile,at,amount):
            event=normalize({'timestamp':at,'type':'token_usage_record','payload':{
                'thread_id':profile+'-synthetic','response_id':str(i),
                'usage':{'input_tokens':amount,'cached_input_tokens':amount//2,'output_tokens':amount//10,
                         'reasoning_output_tokens':amount//30,'total_tokens':amount+amount//10}}},profile,profile+'-synthetic',str(i))[0]
            with store.connect(write=True) as db:store.put(db,event,{'source':'synthetic-graph','delivery':str(i),'ingested_at':at+1})
        for j,profile in enumerate(('astra','sol')):
            for i in range(144):
                if i in (50,51,99):continue
                amount=int(8000+9000*(1+math.sin(i*.28+j))+(50000 if i in (90,120-j*7) else 0))
                record(f'{profile}-{i}',profile,anchor-(144-i)*300+90,amount)
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.02)
        origin=f'http://127.0.0.1:{port}';token=Path(cfg['token_file']).read_text().strip()
        try:
            with httpx.Client(base_url=origin,trust_env=False) as wire:
                wire.post('/api/v1/login',json={'token':token},headers={'Origin':origin}).raise_for_status()
                first=wire.get('/api/v1/usage-series?hours=6');check('real_http_series',first.status_code==200)
                check('real_http_two_profiles',len(first.json()['series'])==2)
                before=first.json()['series'][0]['totals']['total_tokens']
                record('HTTP-APPEND','astra',time.time()-2,1000)
                newer=wire.get('/api/v1/usage-series?hours=6').json()
                check('real_http_fresh_commit',newer['series'][0]['totals']['total_tokens']==before+1100)
            client=TestClient(app,base_url=origin)  # Server already owns the one app lifespan.
            with sync_playwright() as pw:
                client.post('/api/v1/login',json={'token':token},headers={'Origin':origin}).raise_for_status()
                browser=pw.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
                page=browser.new_page(viewport={'width':1440,'height':1100});native_requests=[];page.on('request',lambda request:native_requests.append(request.url))
                try:
                    page.goto(origin,timeout=6000);page.locator('#login-panel').wait_for(state='visible')
                    page.fill('#token',token);page.get_by_role('button',name='Открыть обсерваторию').click();page.locator('#content .cards').wait_for()
                    page.goto(origin+'/token-graph',timeout=6000);page.locator('#graph-cards h2').first.wait_for(timeout=6000)
                    first_native=page.locator('#graph-cards').inner_text();record('NATIVE-APPEND','astra',time.time()-2,3000)
                    page.wait_for_function("old => document.querySelector('#graph-cards').innerText !== old",arg=first_native,timeout=5000)
                    if not all(url.startswith(origin) for url in native_requests):raise AssertionError('Native graph requested an external origin')
                    native={'status':'full_passed','login':True,'authenticated_series':True,'sse_commit':True,
                            'only_local_requests':all(url.startswith(origin) for url in native_requests)}
                except Exception as exc:native={'status':'blocked_or_unavailable','error':str(exc)}
                page.close();page=browser.new_page(viewport={'width':1440,'height':1100});page.on('pageerror',lambda e:errors.append(str(e)))
                def request(url):
                    requests.append(url);response=client.get(url)
                    return {'status':response.status_code,'text':response.text,'headers':dict(response.headers)}
                page.expose_function('graphRequest',request);page.expose_function('graphCommits',store.stream_batch)
                html=(ROOT/'monik/web/token-graph.html').read_text()
                html=re.sub(r'<link[^>]*>','',html);html=re.sub(r'<script\b[^>]*>[\s\S]*?</script>','',html)
                page.set_content(html)
                page.add_style_tag(content=(ROOT/'monik/web/style.css').read_text()+(ROOT/'monik/web/token-graph.css').read_text())
                page.add_script_tag(content=BRIDGE);page.add_script_tag(content=(ROOT/'monik/web/token-graph.js').read_text())
                page.locator('#graph-cards h2').first.wait_for();page.wait_for_timeout(200)
                check('two_profile_cards',page.locator('#graph-cards h2').count()==2)
                check('actual_svg_points',page.locator('#graph-svg .graph-dot').count()>20)
                check('demo_badge',page.locator('#graph-demo').is_visible())
                for hours in (24,12,6,3,2,1):
                    page.click(f'[data-hours="{hours}"]');page.wait_for_timeout(120)
                    check('window_'+str(hours),page.locator(f'[data-hours="{hours}"]').get_attribute('aria-pressed')=='true' and f'за {hours} ч' in page.locator('#graph-cards').inner_text())
                page.click('[data-hours="6"]');page.wait_for_timeout(150)
                check('one_resize_observer_after_all_presets',page.evaluate('window.graphObserverCount')==1)
                for width in (360,390,412,768,1440):
                    page.set_viewport_size({'width':width,'height':1100});page.wait_for_timeout(150)
                    check('layout_'+str(width),page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                    size=page.evaluate("Math.min(...[...document.querySelectorAll('button')].filter(e=>e.getBoundingClientRect().height>0).map(e=>e.getBoundingClientRect().height))")
                    check('touch_'+str(width),size>=44,size)
                    vb=page.locator('#graph-svg').get_attribute('viewBox').split()
                    actual=page.locator('#graph-svg').bounding_box()['width'];check('svg_legibility_'+str(width),abs(float(vb[2])-actual)<2)
                    if width in (390,1440):page.screenshot(path=str(out/f'graph-dark-{width}.png'),full_page=True)
                page.click('#graph-theme');check('light_theme',page.locator('html').get_attribute('data-theme')=='light');page.screenshot(path=str(out/'graph-light-1440.png'),full_page=True)
                page.select_option('#graph-metric','cached_input_tokens');check('metric_switch','Ввод из кэша' in page.locator('#graph-cards').inner_text())
                page.select_option('#graph-metric','total_tokens')
                page.locator('#graph-legend button').first.click();check('toggle_profile',page.locator('#graph-legend button').first.get_attribute('aria-pressed')=='false');page.locator('#graph-legend button').first.click()
                page.locator('#graph-slider').fill('3');page.locator('#graph-slider').dispatch_event('input');check('keyboard_inspector','токенов' in page.locator('#graph-inspector').inner_text())
                page.click('#graph-pause');old=page.locator('#graph-cards').inner_text()
                record('DOM-APPEND','astra',time.time()-2,1000);page.wait_for_timeout(1100)
                check('pause_preserves_snapshot',page.locator('#graph-cards').inner_text()==old)
                page.click('#graph-pause');page.wait_for_timeout(200)
                check('resume_updates_snapshot',page.locator('#graph-cards').inner_text()!=old)
                old=page.locator('#graph-cards').inner_text();record('LIVE-APPEND','astra',time.time()-2,2000);page.wait_for_timeout(1300)
                check('commit_notification_updates_dom',page.locator('#graph-cards').inner_text()!=old)
                page.locator('.graph-table summary').click();check('numeric_table',page.locator('#graph-table-body tr').count()>50)
                client.cookies.clear();page.click('#graph-refresh');page.locator('#graph-login').wait_for(state='visible')
                check('expired_session_clears_sensitive_dom',page.locator('#graph-cards').inner_text()=='' and page.locator('#graph-table-body').inner_text()=='' and page.locator('#graph-legend').inner_text()=='')
                page.select_option('#graph-metric','output_tokens');page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(150)
                check('resize_and_metric_cannot_restore_logged_out_data',page.locator('#graph-cards').inner_text()=='' and page.locator('#graph-svg .graph-dot').count()==0)
                check('no_js_errors',not errors,errors)
                check('only_local_series_fetches',all(u.startswith('/api/v1/usage-series?') for u in requests))
                browser.close()
        except Exception as exc:checks.append({'name':'exception','ok':False,'detail':str(exc)})
        finally:server.should_exit=True;thread.join(timeout=8);sock.close()
    result={'dom_and_http_ok':all(c['ok'] for c in checks),'checks':checks,'native_browser':native,
            'scope':'Synthetic own DB. Real loopback HTTP checked separately; DOM fetch/SSE/history use a labelled bridge. No physical Android or Codex host acceptance.'}
    (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['dom_and_http_ok'] else 1
if __name__=='__main__':raise SystemExit(main())
