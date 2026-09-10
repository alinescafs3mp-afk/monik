"""Owner-maintained local configuration. No source configuration or credentials are read."""
from __future__ import annotations
import ipaddress
import json
import math
import stat
import re
import os
import secrets
import tempfile
from pathlib import Path


def expand(path): return Path(path).expanduser().absolute()

def forbidden(path):
    return any(part.casefold()=='auth.json' or re.sub(r'[ _-]+','',part).casefold()=='pandorabox' for part in Path(path).parts)


def _reject_symlink_components(path):
    path=expand(path)
    for part in (path,*path.parents):
        try:
            if part.is_symlink(): raise ValueError('Private monik paths must not use symbolic links')
        except OSError as exc:
            raise ValueError('Cannot verify private monik path') from exc


def private_directory(path,*,create=False):
    """Validate one monik-owned private directory without changing shared parents."""
    path=expand(path);_reject_symlink_components(path)
    if create: path.mkdir(parents=True,exist_ok=True,mode=0o700)
    try: info=path.lstat()
    except FileNotFoundError: raise ValueError('Private monik directory does not exist') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Private monik directory must be owner-only (0700)')
    return path


def private_file(path,*,maximum=None,label='Private monik file'):
    """Reject links, aliases, foreign ownership and group/world permissions."""
    path=expand(path);_reject_symlink_components(path)
    try: info=path.lstat()
    except FileNotFoundError: raise
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or
            info.st_uid!=os.geteuid() or info.st_mode & 0o077):
        raise ValueError(f'{label} must be an owner-only regular file (0600), not a link')
    if maximum is not None and info.st_size>maximum: raise ValueError(f'{label} is too large')
    return path


def atomic_private_write(path,data,*,replace=True):
    """Durably replace an own file through a unique 0600 temporary file."""
    path=expand(path);parent=private_directory(path.parent,create=True)
    old=None
    try:
        old=private_file(path).lstat()
        if not replace: raise FileExistsError(path)
    except FileNotFoundError:
        pass
    fd,name=tempfile.mkstemp(prefix='.'+path.name+'.',suffix='.tmp',dir=parent)
    temp=Path(name)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        if old is not None:
            current=private_file(path).lstat()
            if (current.st_dev,current.st_ino)!=(old.st_dev,old.st_ino):
                raise ValueError('Private monik file changed during atomic update')
        if old is not None and replace:
            os.replace(temp,path)
        else:
            os.link(temp,path,follow_symlinks=False);temp.unlink()
        directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
        try: os.fsync(directory)
        finally: os.close(directory)
    except BaseException:
        try: os.close(fd)
        except OSError: pass
        try: temp.unlink()
        except FileNotFoundError: pass
        raise
    return path

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
    path=expand(path);private_directory(path.parent)
    path=private_file(path,maximum=131072,label='Configuration')
    config=json.loads(path.read_text())
    if not isinstance(config,dict): raise ValueError('Configuration must be a JSON object')
    config['_path']=str(path)
    if any(not isinstance(config.get(k),str) or not config[k] for k in ('data_dir','token_file')):
        raise ValueError('Configuration requires data_dir and token_file paths')
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
    if any(not isinstance(p,dict) for p in profiles): raise ValueError('Profiles must be JSON objects')
    names=[p.get('name') for p in profiles]
    if any(not isinstance(n,str) or not n.isidentifier() or len(n)>64 for n in names) or len(set(names))!=len(names): raise ValueError('Profile names must be unique identifiers')
    data=expand(config['data_dir'])
    if any(p.is_symlink() for p in (data,*data.parents)): raise ValueError('Own data directory must not use symbolic links')
    data=data.resolve()
    if forbidden(data) or data in (Path('/'),Path.home()): raise ValueError('Unsafe own data directory')
    roots=[]
    for p in profiles:
        for key in ('registry','lifecycle_events','peer_events','state_db'):
            if p.get(key) is not None and not isinstance(p[key],str): raise ValueError('Source path must be a string')
        if not isinstance(p.get('rollout_dirs',[]),list) or any(not isinstance(x,str) for x in p.get('rollout_dirs',[])):
            raise ValueError('rollout_dirs must be a list of paths')
        if not isinstance(p.get('files',[]),list) or len(p.get('files',[]))>4096:
            raise ValueError('files must be a bounded list')
        for item in p.get('files',[]):
            if not isinstance(item,dict) or not isinstance(item.get('path'),str) or not isinstance(item.get('thread_id'),str) or not 1<=len(item['thread_id'])<=128:
                raise ValueError('Each explicit source needs path and thread_id')
            if item.get('kind','rollout') not in ('rollout','lifecycle','peer'): raise ValueError('Unknown explicit source kind')
        if p.get('root_id') is not None and (not isinstance(p['root_id'],str) or not 1<=len(p['root_id'])<=128 or any(c.isspace() for c in p['root_id'])):
            raise ValueError('Invalid configured root_id')
        for key in ('registry','lifecycle_events','peer_events','state_db'):
            if p.get(key):
                source=expand(p[key])
                if forbidden(source) or forbidden(source.resolve()): raise ValueError('Excluded source path')
                roots.append(source.resolve())
        for item in p.get('rollout_dirs',[]):
            source=expand(item)
            if forbidden(source) or forbidden(source.resolve()): raise ValueError('Excluded source directory')
            roots.append(source.resolve())
        for item in p.get('files',[]):
            source=expand(item['path']);roots.append(source.resolve())
            if forbidden(source) or forbidden(source.resolve()): raise ValueError('Excluded source path')
    for source in roots:
        if data==source or data.is_relative_to(source) or source.is_relative_to(data):
            raise ValueError('Own data and source trees must not overlap')
    config['data_dir']=str(data)
    for key,low,high,default in (('poll_seconds',0.2,10,0.5),('discovery_seconds',1,60,5)):
        value=float(config.get(key,default))
        if not math.isfinite(value): raise ValueError('Polling interval must be finite')
        config[key]=max(low,min(high,value))
    budget=config.get('state_snapshot_bytes',64*1024*1024)
    if type(budget) is not int or not 1048576<=budget<=512*1024*1024: raise ValueError('Invalid metadata snapshot byte budget')
    config['state_snapshot_bytes']=budget
    return config


def init(path,home=None):
    path=expand(path)
    try:
        path.lstat()
    except FileNotFoundError:
        config=None
    else:
        config=load(path)
    if config is not None:
        token=private_file(config['token_file'],maximum=512,label='Owner token').read_text().strip()
        if not 32<=len(token)<=256: raise ValueError('Owner token must contain 32..256 characters')
        return path
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(path.parent,0o700);private_directory(path.parent)
    config=defaults(home);token=path.parent/'owner-token.txt';config['token_file']=str(token)
    try:
        value=private_file(token,maximum=512,label='Owner token').read_text().strip()
        if not 32<=len(value)<=256: raise ValueError('Owner token must contain 32..256 characters')
    except FileNotFoundError:
        atomic_private_write(token,(secrets.token_urlsafe(36)+'\n').encode(),replace=False)
    encoded=(json.dumps(config,ensure_ascii=False,indent=2)+'\n').encode()
    atomic_private_write(path,encoded,replace=False)
    return path
