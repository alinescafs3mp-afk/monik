"""Chromium integration against an isolated synthetic server, never an installed Codex.
Run: python tests/browser_acceptance.py --output /tmp/monik-browser
Requires optional playwright and a locally installed Chromium. No browser download.
"""
from __future__ import annotations
import argparse,json,socket,tempfile,threading,time,sys,statistics
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
from playwright.sync_api import sync_playwright
from monik.app import create_app
from monik.cli import make_demo
from monik.config import load
from monik.model import dumps

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--output',default='/tmp/monik-browser');ap.add_argument('--chromium',default='/usr/bin/chromium');args=ap.parse_args()
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True);checks=[];errors=[];network=[];latency=[]
    def check(name,condition,detail=None):
        checks.append({'name':name,'ok':bool(condition),'detail':detail})
        if not condition: raise AssertionError(name+': '+str(detail))
    with tempfile.TemporaryDirectory() as directory:
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        path=make_demo(Path(directory)/'demo',port);config=load(path);config['poll_seconds']=0.2;app=create_app(config)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False));thread=threading.Thread(target=server.run,daemon=True);thread.start()
        for _ in range(100):
            if server.started:break
            time.sleep(.05)
        base=f'http://127.0.0.1:{port}';token=Path(config['token_file']).read_text().strip();source=Path(config['profiles'][0]['files'][0]['path'])
        def emit(i,text):
            row={'timestamp':time.time(),'type':'response_item','payload':{'type':'message','role':'assistant','id':'BROWSER-'+str(i),'content':[{'text':text}]}}
            with source.open('a') as f:f.write(dumps(row)+'\n')
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(executable_path=args.chromium,headless=True,args=['--no-sandbox'])
                context=browser.new_context(viewport={'width':1440,'height':1000});page=context.new_page()
                page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:network.append(r.url))
                page.goto(base);page.locator('#login-panel').wait_for(state='visible');page.fill('#token',token);page.get_by_role('button',name='Открыть обсерваторию').click();page.locator('#content .cards').wait_for();page.wait_for_timeout(400)
                check('login_overview',page.locator('#content').inner_text().find('Astra')>=0 and 'Sol' in page.locator('#content').inner_text())
                check('token_not_in_storage',token not in page.evaluate('JSON.stringify(localStorage)') and token not in page.url)
                # A filtered overview must never become the profile catalogue.
                page.locator('#filter-sheet').evaluate('(e)=>e.open=true')
                page.select_option('#profile','sol');page.click('#filters button[type="submit"]');page.wait_for_timeout(350)
                check('profile_sol_only','Sol' in page.locator('#content').inner_text() and 'ASTRA_DEMO_ROOT' not in page.locator('#content').inner_text())
                page.click('#refresh');page.wait_for_timeout(350)
                check('profile_catalogue_survives_refresh',page.locator('#profile option[value="astra"]').count()==1)
                page.select_option('#profile','astra');page.click('#filters button[type="submit"]');page.wait_for_timeout(350)
                check('profile_astra_after_sol','Astra' in page.locator('#content').inner_text() and 'SOL_DEMO_ROOT' not in page.locator('#content').inner_text())
                page.evaluate("S.params.profile='not-configured';syncProfiles();syncFilters();refresh(true)");page.wait_for_timeout(350)
                check('unknown_profile_not_all',page.locator('#profile').input_value()=='not-configured' and page.locator('#content .card').count()==0)
                page.click('#clear');page.locator('#content .cards').wait_for();page.wait_for_timeout(300)
                check('profile_all_restored','Astra' in page.locator('#content').inner_text() and 'Sol' in page.locator('#content').inner_text())
                page.click('#logout');page.locator('#login-panel').wait_for(state='visible')
                check('logout_clears_profile_catalogue',page.locator('#content').inner_text()=='' and page.locator('#profile option').count()==1 and page.locator('#q').input_value()=='')
                page.fill('#token',token);page.get_by_role('button',name='Открыть обсерваторию').click();page.locator('#content .cards').wait_for()
                check('relogin_restores_profile_catalogue',page.locator('#profile option[value="astra"]').count()==1 and page.locator('#profile option[value="sol"]').count()==1)
                page.locator('#content .card').first.evaluate("e=>{const p=document.createElement('p');p.id='long-overview-value';p.className='small separated';p.textContent='X'.repeat(1200);e.append(p)}")
                check('long_overview_value_wraps',page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.locator('#long-overview-value').evaluate('e=>e.remove()')
                for width in (360,390,412,768,1440):
                    page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(100)
                    dimensions=page.evaluate('({scroll:document.documentElement.scrollWidth,width:innerWidth})')
                    check('responsive_'+str(width),dimensions['scroll']<=dimensions['width'],dimensions)
                    touch=page.evaluate("Math.min(...Array.from(document.querySelectorAll('button')).filter(b=>b.getBoundingClientRect().height>0).map(b=>b.getBoundingClientRect().height))")
                    check('touch_targets_'+str(width),touch>=44,touch)
                    if width in (390,1440):page.screenshot(path=str(output/f'overview-{width}.png'),full_page=True)
                page.click('#theme');check('theme_toggle',page.locator('html').get_attribute('data-theme')=='light');page.reload();page.locator('#content .cards').wait_for();check('theme_persist',page.locator('html').get_attribute('data-theme')=='light');page.click('#theme')
                for tab in ('activity','tokens','limits','tree','quality'):
                    page.click('[data-tab="'+tab+'"]');page.wait_for_timeout(350)
                    check('tab_'+tab,len(page.locator('#content').inner_text())>60 and not page.locator('#error').is_visible())
                    check('layout_'+tab,page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                page.click('[data-tab="limits"]');page.wait_for_timeout(250);page.reload();page.wait_for_timeout(350)
                check('last_tab_persisted',page.locator('[data-tab="limits"]').get_attribute('aria-current')=='page' and 'История снимков' in page.locator('#content').inner_text())
                page.click('[data-tab="activity"]');page.locator('#feed').wait_for()
                for i in range(8):
                    marker='LIVE_LATENCY_'+str(i);start=time.monotonic();emit(i,marker);page.get_by_text(marker,exact=True).wait_for(timeout=5000);latency.append(time.monotonic()-start)
                check('live_latency_p95_under_2s',sorted(latency)[-1]<2,latency)
                emit(90,'<img src="https://invalid.example/track" onerror="window.bad=1"><script>window.bad=2</script>');page.get_by_text('<img src=',exact=False).wait_for(timeout=3000)
                check('hostile_text_not_html',page.locator('#feed img,#feed script').count()==0 and page.evaluate('window.bad===undefined'))
                # Create enough visible history to test intentional non-follow scrolling.
                for i in range(100,150):emit(i,'SCROLL_FILLER_'+str(i)+' '+('длинный текст '*20))
                page.wait_for_timeout(1000);page.locator('#feed').evaluate('(e)=>{e.scrollTop=0;e.dispatchEvent(new Event("scroll"));}')
                before=page.locator('#feed').evaluate('(e)=>e.scrollTop');emit(151,'DO_NOT_JUMP');page.wait_for_timeout(900)
                check('scroll_preserved',page.locator('#feed').evaluate('(e)=>e.scrollTop')==before and 'Новых событий' in page.locator('#new-events').inner_text())
                page.click('#new-events');page.get_by_text('DO_NOT_JUMP',exact=True).wait_for(timeout=3000)
                # Pin T, then append a future event: neither T nor the historical DOM may jump.
                page.locator('#filter-sheet summary').click();page.fill('#at','2026-09-09T10:00');page.click('#filters button[type="submit"]');page.wait_for_timeout(500)
                fixed=page.locator('#feed').inner_text();emit(152,'AFTER_FIXED_T');page.wait_for_timeout(700)
                check('past_time_pinned',page.locator('#at').input_value().startswith('2026-09-09T10:00') and page.locator('#feed').inner_text()==fixed and page.locator('#notice').is_visible())
                page.click('#live');page.get_by_text('AFTER_FIXED_T',exact=True).wait_for(timeout=3000)
                page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))")
                emit(1521,'AFTER_SLEEP_RETURN');page.wait_for_timeout(500);check('sleep_holds_dom',page.get_by_text('AFTER_SLEEP_RETURN',exact=True).count()==0)
                page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,get:()=>false});document.dispatchEvent(new Event('visibilitychange'))")
                page.get_by_text('AFTER_SLEEP_RETURN',exact=True).wait_for(timeout=3000)
                check('wake_catches_up',page.get_by_text('AFTER_SLEEP_RETURN',exact=True).count()==1)
                # Deliberate SSE disconnect emulates connection loss, not a physical Android sleep.
                page.evaluate('S.es.close();S.es=null');emit(153,'RECONNECT_CATCHUP');page.wait_for_timeout(350);page.evaluate('connect()');page.get_by_text('RECONNECT_CATCHUP',exact=True).wait_for(timeout=3000)
                check('sse_reconnect_no_duplicate',page.get_by_text('RECONNECT_CATCHUP',exact=True).count()==1)
                # A second authenticated tab must not create a second filesystem worker.
                page2=context.new_page();page2.goto(base);page2.locator('#content > *').first.wait_for();page2.wait_for_timeout(300)
                check('second_tab_restores_last_page',page2.locator('[data-tab="activity"]').get_attribute('aria-current')=='page')
                workers=[t for t in threading.enumerate() if t.name=='monik-collector'];check('one_collector_multiple_tabs',len(workers)==1,len(workers));page2.close()
                page.locator('#feed .event').last.get_by_role('button',name='Детали').click();page.locator('#detail').wait_for();check('paged_safe_details','Provenance' in page.locator('#detail').inner_text());page.click('#close-detail')
                page.locator('#filter-sheet').evaluate('(e)=>e.open=true');page.fill('#q','RECONNECT_CATCHUP');page.select_option('#search_mode','substring');page.click('#filters button[type="submit"]');page.wait_for_timeout(400)
                check('search_ui',page.locator('#feed .event').count()==1)
                context.clear_cookies();page.click('#refresh');page.locator('#login-panel').wait_for(state='visible')
                check('expired_session_clears_private_dom',page.locator('#content').inner_text()=='' and page.locator('#detail-body').inner_text()=='' and page.locator('#profile option').count()==1 and page.locator('#q').input_value()=='')
                check('no_javascript_errors',not errors,errors);check('no_external_requests',all(u.startswith(base) for u in network),[u for u in network if not u.startswith(base)])
                page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(output/'activity-390.png'),full_page=True)
                browser.close()
        except Exception as exc:
            checks.append({'name':'exception','ok':False,'detail':str(exc)})
        finally:
            server.should_exit=True;thread.join(timeout=15)
    result={'ok':all(c['ok'] for c in checks),'checks':checks,'live_latency_seconds':latency,'environment':'Isolated synthetic fixture server; Chromium desktop emulation, not physical Android; library app factory, not production Landlock CLI'}
    (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['ok'] else 1

if __name__=='__main__':raise SystemExit(main())
