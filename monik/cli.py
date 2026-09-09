"""Owner CLI. Configuration changes belong to monik, never to Codex."""
from __future__ import annotations
import argparse
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime,timezone,timedelta
from pathlib import Path
from .config import init,load,expand,defaults
from .model import dumps

DEFAULT=str(Path.home()/'.config/monik/config.json')

def write_config(path,value):
    path=expand(path);tmp=path.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: json.dump(value,f,ensure_ascii=False,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)


def make_demo(directory,port=8866):
    directory=expand(directory)
    if directory.exists() and any(directory.iterdir()): raise ValueError('Demo directory must be empty or new')
    directory.mkdir(parents=True,mode=0o700,exist_ok=True);source=directory/'sources';source.mkdir(mode=0o700)
    token=directory/'owner-token.txt';token.write_text(secrets.token_urlsafe(36)+'\n');token.chmod(0o600)
    config=defaults();config.update(data_dir=str(directory/'data'),port=port,token_file=str(token),profiles=[],demo=True)
    now=datetime.now(timezone.utc)-timedelta(seconds=30)
    for index,name in enumerate(('astra','sol')):
        root=name.upper()+'_DEMO_ROOT';path=source/(name+'.jsonl');model='gpt-6-astra' if name=='astra' else 'gpt-5.6-sol'
        def row(n,typ,p): return {'timestamp':(now+timedelta(seconds=n)).isoformat(),'type':typ,'payload':p}
        records=[row(0,'session_meta',{'id':root,'source':'cli','thread_source':'user'}),row(1,'turn_context',{'model':model,'effort':'ultra','service_tier':'default'}),
            row(2,'response_item',{'type':'message','id':root+'-U','role':'user','content':[{'text':'Проверь изоляцию источников и исторические снимки.'}]}),
            row(3,'event_msg',{'type':'task_started'}),row(4,'response_item',{'type':'function_call','call_id':root+'-CALL','name':'read_fixture','arguments':'{"path":"tests/example.py"}'}),
            row(5,'response_item',{'type':'function_call_output','call_id':root+'-CALL','output':'PASS: source files unchanged. Русский поиск: надёжность.'}),
            row(6,'response_item',{'type':'message','id':root+'-A','role':'assistant','content':[{'text':'Проверка завершена. Монитор ничего не записывает в источники.'}]}),
            row(7,'token_usage_record',{'thread_id':root,'response_id':root+'-RESP','usage':{'input_tokens':12000+index*8000,'cached_input_tokens':10000+index*6000,'output_tokens':600,'reasoning_output_tokens':150,'total_tokens':12600+index*8000}}),
            row(8,'event_msg',{'type':'token_count','info':None,'rate_limits':{'limit_id':'codex','plan_type':'pro','primary':{'used_percent':23+index,'window_minutes':10080,'resets_at':int((now+timedelta(days=4)).timestamp())}}}),
            row(9,'event_msg',{'type':'task_complete'})]
        path.write_text('\n'.join(dumps(x) for x in records)+'\n')
        config['profiles'].append({'name':name,'root_id':root,'files':[{'path':str(path),'thread_id':root}]})
    config_path=directory/'config.json';write_config(config_path,config);return config_path


def serve(path):
    if os.geteuid()==0: raise ValueError('Run the service as your ordinary user, never root')
    os.umask(0o077);config=load(path)
    data=Path(config['data_dir']);data.mkdir(parents=True,exist_ok=True,mode=0o700)
    if data.stat().st_mode & 0o077: raise ValueError('Own data directory must have mode 0700')
    temp=data/'tmp';temp.mkdir(exist_ok=True,mode=0o700)
    os.environ['TMPDIR']=str(temp);sys.dont_write_bytecode=True
    from .sandbox import enforce
    config['landlock_abi']=enforce(data)
    from .app import create_app
    import uvicorn
    app=create_app(config)
    uvicorn.run(app,host=config['bind'],port=config['port'],workers=1,access_log=False,proxy_headers=False,
                ssl_certfile=config.get('tls_cert'),ssl_keyfile=config.get('tls_key'),limit_concurrency=64,timeout_keep_alive=5)


def main():
    parser=argparse.ArgumentParser(prog='monik',description='Локальный read-only Codex Observatory')
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('init','serve','doctor','token','backup','roots'):
        cmd=sub.add_parser(name);cmd.add_argument('--config',default=DEFAULT)
        if name=='backup': cmd.add_argument('target')
        if name=='roots': cmd.add_argument('--astra');cmd.add_argument('--sol')
    demo=sub.add_parser('demo');demo.add_argument('--directory',default=str(Path.home()/'.local/share/monik-demo'));demo.add_argument('--port',type=int,default=8866);demo.add_argument('--prepare-only',action='store_true')
    args=parser.parse_args()
    try:
        if args.command=='init': print('Конфигурация:',init(args.config));return
        if args.command=='demo':
            path=make_demo(args.directory,args.port);print('DEMO config:',path);print('Токен:',path.parent/'owner-token.txt')
            if not args.prepare_only: serve(path)
            return
        config=load(args.config)
        if args.command=='serve': serve(args.config)
        elif args.command=='token': print(expand(config['token_file']).read_text().strip())
        elif args.command=='doctor':
            db=sqlite3.connect(':memory:');db.execute('CREATE VIRTUAL TABLE fts_test USING fts5(text)');db.close()
            from .collector import read_json
            profiles=[]
            for p in config['profiles']:
                root=p.get('root_id');state='explicit' if root else 'unknown'
                if p.get('registry') and not root:
                    try: root=read_json(p['registry']).get('designated_root_thread_id');state='registry' if root else 'unknown'
                    except (OSError,ValueError): pass
                profiles.append({'profile':p['name'],'root_id':root,'identity':state,'state_db_readable':os.access(expand(p['state_db']),os.R_OK) if p.get('state_db') else None})
            print(json.dumps({'python':sys.version.split()[0],'sqlite':sqlite3.sqlite_version,'fts5':True,'landlock_abi':__import__('monik.sandbox',fromlist=['abi']).abi(),'listen':f"{config['bind']}:{config['port']}",'tls':bool(config.get('tls_cert')),'profiles':profiles,'source_writes':0,'rpc_calls':0},ensure_ascii=False,indent=2))
        elif args.command=='backup':
            from .storage import Store
            Store(Path(config['data_dir'])/'observatory.sqlite').backup(expand(args.target));print('Проверенная резервная копия:',args.target)
        elif args.command=='roots':
            for p in config['profiles']:
                value=getattr(args,p['name'],None)
                if value:
                    if len(value)>128 or any(c.isspace() for c in value): raise ValueError('Invalid thread identifier')
                    p['root_id']=value
            config.pop('_path',None);write_config(args.config,config);print('Корни monik обновлены. Перезапусти только monik.')
    except (OSError,ValueError,RuntimeError,sqlite3.Error) as exc:
        parser.exit(1,f'monik: {exc}\n')

if __name__=='__main__': main()
