"""Offline DOM/responsive acceptance when browser navigation is administratively disabled.

No navigation policy is changed. HTML/CSS/JS render in about:blank. A Python bridge
provides the actual ASGI API, and a labelled in-memory EventSource stand-in provides
commit notifications. This is NOT a native browser-network/SSE or Android test.
"""
from __future__ import annotations
import argparse,json,tempfile,time,sys,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from fastapi.testclient import TestClient
from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import dumps

ROOT=Path(__file__).resolve().parents[1]
BRIDGE_JS=r'''
window.fetch=async(url,options={})=>{const r=await window.asgiRequest(String(url),options);return new Response(r.text,{status:r.status,headers:r.headers});};
window.EventSource=class{
 constructor(url){this.cursor=Number(new URL(url,'http://offline.test').searchParams.get('after')||0);this.listeners={};this.closed=false;setTimeout(()=>{if(this.onopen)this.onopen();this.poll();},30);}
 addEventListener(name,cb){(this.listeners[name]??=[]).push(cb);}
 async poll(){if(this.closed)return;const result=await window.committedEvents(this.cursor);if(this.closed)return;if(result.length){this.cursor=result.at(-1);for(const cb of this.listeners.committed||[])cb({data:JSON.stringify({ids:result,cursor:this.cursor})});}this.timer=setTimeout(()=>this.poll(),250);}
 close(){this.closed=true;clearTimeout(this.timer);}
};
'''

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',default='/tmp/monik-offline-ui');args=ap.parse_args();out=Path(args.output);out.mkdir(parents=True,exist_ok=True);checks=[];errors=[]
    def check(name,ok,detail=None):
        checks.append({'name':name,'ok':bool(ok),'detail':detail})
        if not ok:raise AssertionError(name+': '+str(detail))
    with tempfile.TemporaryDirectory() as d:
        path=make_demo(Path(d)/'demo');config=load(path);config['poll_seconds']=.2;app=create_app(config);source=Path(config['profiles'][0]['files'][0]['path'])
        def emit(i,text):
            with source.open('a') as f:f.write(dumps({'timestamp':time.time(),'type':'response_item','payload':{'type':'message','role':'assistant','id':'UI-'+str(i),'content':[{'text':text}]}})+'\n')
        with TestClient(app,base_url='http://127.0.0.1:8866') as client, sync_playwright() as pw:
            def request(url,options):
                headers=options.get('headers',{});headers['Origin']='http://127.0.0.1:8866'
                r=client.request(options.get('method','GET'),url,content=options.get('body'),headers=headers)
                return {'status':r.status_code,'text':r.text,'headers':dict(r.headers)}
            b=pw.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox']);page=b.new_page(viewport={'width':1440,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.expose_function('asgiRequest',request);page.expose_function('committedEvents',app.state.store.stream_batch)
                html=(ROOT/'monik/web/index.html').read_text().replace('<link rel="stylesheet" href="/style.css">','').replace('<script src="/app.js" defer></script>','')
                page.set_content(html);page.add_style_tag(content=(ROOT/'monik/web/style.css').read_text());page.add_script_tag(content=BRIDGE_JS);page.add_script_tag(content=(ROOT/'monik/web/app.js').read_text())
                page.locator('#login-panel').wait_for(state='visible');page.fill('#token',Path(config['token_file']).read_text().strip());page.get_by_role('button',name='Открыть обсерваторию').click();page.locator('#content .cards').wait_for();page.wait_for_timeout(400)
                check('overview_profiles',all(x in page.locator('#content').inner_text() for x in ('Astra','Sol')))
                check('profile_select_options',page.locator('#profile option').evaluate_all('(items)=>items.map(x=>x.value)')==['','astra','sol'])
                for width in (360,390,412,768,1440):
                    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(100)
                    dims=page.evaluate('({scroll:document.documentElement.scrollWidth,width:innerWidth})');check('width_'+str(width),dims['scroll']<=dims['width'],dims)
                    minimum=page.evaluate("Math.min(...Array.from(document.querySelectorAll('button')).filter(b=>b.getBoundingClientRect().height>0).map(b=>b.getBoundingClientRect().height))");check('touch_'+str(width),minimum>=44,minimum)
                    if width in (390,1440):page.screenshot(path=str(out/f'overview-{width}.png'),full_page=True)
                page.click('#theme');check('light_theme',page.locator('html').get_attribute('data-theme')=='light');page.screenshot(path=str(out/'overview-light.png'),full_page=True);page.click('#theme')
                for tab in ('activity','tokens','limits','tree','quality'):
                    page.click('[data-tab="'+tab+'"]');page.wait_for_timeout(350);check('tab_'+tab,len(page.locator('#content').inner_text())>60 and not page.locator('#error').is_visible())
                    page.set_viewport_size({'width':360,'height':840});check('mobile_'+tab,page.evaluate('document.documentElement.scrollWidth<=innerWidth'));page.set_viewport_size({'width':1440,'height':1000})
                page.click('[data-tab="activity"]');page.locator('#feed').wait_for();emit(1,'BRIDGED_UPDATE');page.get_by_text('BRIDGED_UPDATE',exact=True).wait_for(timeout=4000);check('commit_updates_dom',True)
                emit(2,'<img src="https://invalid.example/track" onerror="window.bad=1"><script>window.bad=2</script>');page.get_by_text('<img src=',exact=False).wait_for(timeout=3000);check('xss_plain_text',page.locator('#feed img,#feed script').count()==0 and page.evaluate('window.bad===undefined'))
                for i in range(10,60):emit(i,'FILLER_'+str(i)+' '+('содержимое '*25))
                page.wait_for_timeout(900);page.locator('#feed').evaluate('(e)=>{e.scrollTop=0;e.dispatchEvent(new Event("scroll"));}');emit(61,'NO_SCROLL_JUMP');page.wait_for_timeout(800)
                check('history_scroll_pinned',page.locator('#feed').evaluate('(e)=>e.scrollTop')==0 and 'Новых событий' in page.locator('#new-events').inner_text());page.click('#new-events');page.get_by_text('NO_SCROLL_JUMP',exact=True).wait_for(timeout=3000)
                page.locator('#filter-sheet summary').click();page.fill('#at','2026-09-09T10:00');page.click('#filters button[type="submit"]');page.wait_for_timeout(450);prior=page.locator('#feed').inner_text();emit(62,'LATER_THAN_T');page.wait_for_timeout(700)
                check('historical_time_does_not_jump',page.locator('#feed').inner_text()==prior and page.locator('#notice').is_visible());page.click('#live');page.get_by_text('LATER_THAN_T',exact=True).wait_for(timeout=3000)
                # Native EventSource behavior is deliberately not claimed by this stand-in test.
                page.evaluate('S.es.close();S.es=null');emit(63,'UI_CATCHUP');page.wait_for_timeout(350);page.evaluate('connect()');page.get_by_text('UI_CATCHUP',exact=True).wait_for(timeout=3000);check('notification_catchup_dom_once',page.get_by_text('UI_CATCHUP',exact=True).count()==1)
                page.locator('#feed .event').last.get_by_role('button',name='Детали').click();page.locator('#detail').wait_for();check('detail_paging_ui','Provenance' in page.locator('#detail').inner_text());page.click('#close-detail')
                page.locator('#filter-sheet').evaluate('(e)=>e.open=true');page.fill('#q','UI_CATCHUP');page.select_option('#search_mode','substring');page.click('#filters button[type="submit"]');page.wait_for_timeout(400);check('search_ui',page.locator('#feed .event').count()==1)
                page.fill('#q','');page.click('#filters button[type="submit"]');page.wait_for_timeout(350);page.locator('.compare summary').click();page.get_by_role('button',name='Сравнить',exact=True).click();page.locator('.compare table').wait_for();check('comparison_ui',page.locator('.compare tbody tr').count()==2)
                page.click('#clear');page.click('[data-tab="activity"]');page.wait_for_timeout(300);client.cookies.clear();page.evaluate("api('overview').catch(()=>{})");page.locator('#login-panel').wait_for(state='visible');page.fill('#token',Path(config['token_file']).read_text().strip());page.get_by_role('button',name='Открыть обсерваторию').click();page.locator('#workspace').wait_for(state='visible');emit(99,'RELOGIN_LIVE');page.get_by_text('RELOGIN_LIVE',exact=True).wait_for(timeout=5000);check('relogin_restores_live_stream',True)
                check('javascript_clean',not errors,errors);check('single_collector',len([t for t in threading.enumerate() if t.name=='monik-collector'])==1)
                page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(out/'activity-390.png'),full_page=True)
            except Exception as exc:checks.append({'name':'exception','ok':False,'detail':str(exc)})
            finally:b.close()
    result={'ok':all(c['ok'] for c in checks),'checks':checks,'mode':'Offline DOM + real ASGI API bridge + EventSource stand-in. No browser network, no physical phone, no production Landlock validation.'};(out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ok'] else 1
if __name__=='__main__':raise SystemExit(main())
