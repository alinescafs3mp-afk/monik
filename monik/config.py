"""Owner-maintained local configuration. No source configuration or credentials are read."""
from __future__ import annotations
import ipaddress
import json
import os
import secrets
from pathlib import Path


def expand(path): return Path(path).expanduser().absolute()

def forbidden(path):
    return Path(path).name.casefold()=='auth.json' or any(part.casefold().replace('_',' ').replace('-',' ')=='pandora box' for part in Path(path).parts)

def defaults(home=None):
    home=Path(home or Path.home());runtime=home/'.jericho/runtime'
    bases={'astra':home/'.codex','sol':home/'.config/codex-multi/profiles/solgoodman'}
    return {'version':1,'data_dir':str(home/'.local/share/monik'),'bind':'127.0.0.1','port':8666,
        'tls_cert':None,'tls_key':None,'poll_seconds':0.5,'discovery_seconds':5,
        'profiles':[{'name':name,'root_id':None,'state_db':str(base/'state_5.sqlite'),
            'registry':str(runtime/'subagent-lifecycle/state'/name/'registry.json'),
            'lifecycle_events':str(runtime/'subagent-lifecycle/state'/name/'events.jsonl'),
            'peer_events':str(runtime/('sol-link-main.events.jsonl' if name=='astra' else 'sol-link-solgoodman.events.jsonl')),
            'rollout_dirs':[str(base/'sessions'),str(base/'archived_sessions')]} for name,base in bases.items()]}


def load(path):
    path=expand(path)
    if path.stat().st_mode & 0o077: raise ValueError('Configuration must have permissions 0600')
    if path.stat().st_size>131072: raise ValueError('Configuration too large')
    config=json.loads(path.read_text());config['_path']=str(path)
    if config.get('version')!=1: raise ValueError('Unsupported configuration version')
    addr=ipaddress.ip_address(config.get('bind','127.0.0.1'))
    if addr.is_unspecified or addr.is_multicast or (not addr.is_loopback and not addr.is_private):
        raise ValueError('Bind must be one explicit loopback or private LAN address')
    if addr.version!=4: raise ValueError('This release supports explicit IPv4 binds only')
    port=config.get('port',8666)
    if type(port) is not int or not 1<=port<=65535: raise ValueError('Invalid port')
    if not addr.is_loopback and not (config.get('tls_cert') and config.get('tls_key')):
        raise ValueError('LAN requires TLS certificate and key; plaintext LAN serving is disabled')
    profiles=config.get('profiles')
    if not isinstance(profiles,list) or not 1<=len(profiles)<=16: raise ValueError('Expected 1..16 explicit profiles')
    names=[p.get('name') for p in profiles]
    if len(set(names))!=len(names) or any(not isinstance(n,str) or not n.isidentifier() for n in names): raise ValueError('Profile names must be unique identifiers')
    data=expand(config['data_dir']).resolve()
    if forbidden(data) or data in (Path('/'),Path.home()): raise ValueError('Unsafe own data directory')
    roots=[]
    for p in profiles:
        for key in ('registry','lifecycle_events','peer_events','state_db'):
            if p.get(key):
                source=expand(p[key])
                if forbidden(source): raise ValueError('Excluded source path')
                roots.append(source.resolve())
        for item in p.get('rollout_dirs',[]):
            source=expand(item)
            if forbidden(source): raise ValueError('Excluded source directory')
            roots.append(source.resolve())
        for item in p.get('files',[]):
            source=expand(item['path']);roots.append(source.resolve())
            if forbidden(source): raise ValueError('Excluded source path')
    for source in roots:
        if data==source or data.is_relative_to(source) or source.is_relative_to(data):
            raise ValueError('Own data and source trees must not overlap')
    config['data_dir']=str(data)
    config['poll_seconds']=max(0.2,min(10,float(config.get('poll_seconds',0.5))))
    config['discovery_seconds']=max(1,min(60,float(config.get('discovery_seconds',5))))
    return config


def init(path,home=None):
    path=expand(path)
    if path.exists(): return path
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    config=defaults(home);token=path.parent/'owner-token.txt';config['token_file']=str(token)
    if not token.exists():
        fd=os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f: f.write(secrets.token_urlsafe(36)+'\n')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(config,f,ensure_ascii=False,indent=2);f.write('\n')
    return path
