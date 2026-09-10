"""Pure normalization of persisted Codex records. No I/O."""
from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any

FIELDS = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens', 'total_tokens')
SAFE_VALUE_BYTES = 65536
SAFE_STRING_BYTES = 32768
MAX_STORED_TEXT_BYTES = 16384
SENSITIVE = {'encrypted_content','authorization','cookie','set-cookie','password','passwd','access_token','refresh_token','id_token','api_key','secret','private_key','environment','env','headers'}
PATTERNS = [
    (r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)','[PRIVATE KEY REMOVED]'),
    (r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}','Bearer [REDACTED]'),
    (r'\bsk-[A-Za-z0-9_-]{12,}','[API KEY REMOVED]'),
    (r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})','[API KEY REMOVED]'),
    (r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}','[JWT REMOVED]'),
    (r'''(?i)((?:password|passwd|api[_-]?key|access[_-]?token|refresh[_-]?token|secret)\s*[=:]\s*["']?)[^\s"',;}]+''',r'\1[REDACTED]'),
    (r'(https?://[^\s/?#]+[^\s?#]*)\?[^\s\"<>]+',r'\1?[QUERY REDACTED]'),
]

def dumps(value: Any) -> str:
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def digest(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value,str) else dumps(value)).encode()).hexdigest()

def timestamp(value: Any) -> float | None:
    try:
        if type(value) in (int,float):
            t=float(value)/(1000 if value>100_000_000_000 else 1)
        elif isinstance(value,str):
            dt=datetime.fromisoformat(value.replace('Z','+00:00'))
            if dt.tzinfo is None: return None
            t=dt.timestamp()
        else: return None
        return t if math.isfinite(t) and 0<=t<=32_503_680_000 else None
    except (ValueError,TypeError,OverflowError): return None

def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat() if value is not None else None

def redact(text: str) -> str:
    for pattern,replacement in PATTERNS: text=re.sub(pattern,replacement,text)
    return text

def safe(value: Any, budget: int=SAFE_VALUE_BYTES) -> Any:
    """Best-effort redaction, byte/node/depth limits; no unredacted raw copy."""
    left=[budget,2048]
    def visit(v,depth=0):
        left[1]-=1
        if depth>14 or min(left)<=0: return '[TRUNCATED]'
        if isinstance(v,str):
            raw=redact(v).encode();size=min(left[0],SAFE_STRING_BYTES);left[0]-=min(size,len(raw))
            return raw[:size].decode('utf-8','ignore')+(f' [TRUNCATED; original_bytes={len(v.encode())}]' if len(raw)>size else '')
        if isinstance(v,dict):
            out={}
            for key,item in list(v.items())[:128]:
                key=redact(str(key))[:200]
                out[key]='[REMOVED]' if key.lower() in SENSITIVE else visit(item,depth+1)
                if min(left)<=0: out['_truncated']=True;break
            if len(v)>128: out['_truncated_keys']=len(v)-128
            return out
        if isinstance(v,list):
            out=[visit(x,depth+1) for x in v[:128] if min(left)>0]
            if len(v)>128: out.append('[TRUNCATED ARRAY]')
            return out
        if v is None or type(v) in (bool,int): return v
        if isinstance(v,float): return v if math.isfinite(v) else None
        return '[UNSUPPORTED]'
    return visit(value)


def bounded_text(value: Any, limit: int=MAX_STORED_TEXT_BYTES) -> str:
    raw=str(value).encode()
    if len(raw)<=limit: return raw.decode()
    suffix=b' [TRUNCATED]'
    return raw[:limit-len(suffix)].decode('utf-8','ignore')+suffix.decode()


def compact_persisted(kind: str, value: Any) -> Any:
    """Apply the current durable payload bound and remove redundant UI output."""
    if kind=='unknown:CommandExecution' and isinstance(value,dict):
        fields=('type','id','status','exit_code','duration','process_id','source','cwd','command','parsed_cmd')
        value={key:value.get(key) for key in fields if key in value}
        value['bulk_output_omitted']=True
    return safe(value)

def usage(value: Any) -> dict:
    raw=value if isinstance(value,dict) else {}
    out={k:raw.get(k) if type(raw.get(k)) is int and 0<=raw[k]<10**12 else None for k in FIELDS}
    errors=['missing_or_invalid:'+k for k in ('input_tokens','output_tokens','total_tokens') if out[k] is None]
    i,c,o,r,t=(out[k] for k in FIELDS)
    if i is not None and c is not None and c>i: errors.append('cached_exceeds_input')
    if o is not None and r is not None and r>o: errors.append('reasoning_exceeds_output')
    if None not in (i,o,t) and i+o!=t: errors.append('total_mismatch')
    out.update(valid=not errors,errors=errors,uncached_input_tokens=i-c if i is not None and c is not None and c<=i else None)
    return out

def limit_identity(value: Any) -> str:
    return value if isinstance(value,str) and value else 'unknown'


def rate_windows(value: Any) -> list[dict]:
    if not isinstance(value,dict): return []
    if isinstance(value.get('rateLimitsByLimitId'),dict):
        buckets=dict(value['rateLimitsByLimitId']);legacy=value.get('rateLimits')
        if isinstance(legacy,dict): buckets.setdefault(limit_identity(legacy.get('limitId','codex')),legacy)
    else:
        legacy=value.get('rateLimits',value)
        if not isinstance(legacy,dict): return []
        buckets={limit_identity(legacy.get('limit_id',legacy.get('limitId','codex'))):legacy}
    out=[]
    for ident,b in buckets.items():
        if not isinstance(b,dict): continue
        common={'limit_id':str(ident),'limit_name':b.get('limit_name',b.get('limitName')),'plan':b.get('plan_type',b.get('planType')),'cause':'unknown'}
        for role in ('primary','secondary'):
            w=b.get(role)
            if not isinstance(w,dict): continue
            u=w.get('used_percent',w.get('usedPercent'))
            valid=type(u) in (float,int) and 0<=u<=100
            if not valid: u=None
            minutes=w.get('window_minutes',w.get('windowDurationMins'))
            if type(minutes) is not int or minutes<=0: minutes=None
            reset=timestamp(w.get('resets_at',w.get('resetsAt')))
            out.append({**common,'role':role,'used_percent':u,'remaining_percent':100-u if valid else None,'window_minutes':minutes,'resets_at':reset,'valid':valid,'raw':w})
        if not any(isinstance(b.get(role),dict) for role in ('primary','secondary')):
            out.append({**common,'role':'unavailable','used_percent':None,'remaining_percent':None,'valid':False,'raw':b})
    return out

def text_content(v):
    if isinstance(v,str): return v
    if isinstance(v,list): return '\n'.join(text_content(x) for x in v)
    if isinstance(v,dict):
        for k in ('text','output','input','arguments','summary','content'):
            if k in v: return text_content(v[k])
        return dumps(v)
    return '' if v is None else str(v)

def normalize(row: dict,profile: str,thread: str,delivery: str,model: str|None=None) -> list[dict]:
    outer=row.get('type',row.get('kind','unknown'));p=row.get('payload',row)
    if not isinstance(p,dict): p={'value':p}
    if outer=='event_msg' and p.get('type')=='item_completed' and isinstance(p.get('item'),dict):
        p=p['item'];outer='response_item'
    nested=p.get('type',outer);at=timestamp(row.get('timestamp',row.get('at',row.get('observed_at',row.get('ts')))))
    tid=p.get('thread_id') or thread
    if outer=='session_meta': tid=p.get('id',p.get('session_id',thread))
    meta=p.get('internal_chat_message_metadata_passthrough',{})
    if not isinstance(meta,dict): meta={}
    base={'profile':profile,'thread_id':str(tid or 'unknown'),'at':at,'timestamp_kind':'source_record' if at is not None else 'observer_clock','model':model,'turn_id':p.get('turn_id',meta.get('turn_id')),'call_id':p.get('call_id'),'response_id':p.get('response_id')}
    out=[]
    def add(kind,data,native=None,text=None):
        clean=compact_persisted(kind,data)
        # Derive both indexed text and display text from the same redacted projection.
        # Otherwise structured secrets removed from data could survive in the FTS text.
        shown = clean.get('content', []) if kind.startswith('message:') else clean
        if kind == 'reasoning_summary': shown = clean.get('summary', [])
        identity = str(native) if native else delivery+':'+kind
        if len(identity) > 512: identity = 'sha256:'+digest(identity)
        fields = {k:(v if not isinstance(v, str) or len(v)<=512 else 'sha256:'+digest(v)) for k,v in base.items()}
        for field in ('turn_id','call_id','response_id','model'):
            if fields.get(field) is not None and not isinstance(fields[field], str): fields[field]=None
        out.append({**fields,'kind':kind,'data':clean,'native':identity,'text':bounded_text(text_content(shown))})
    if outer=='token_usage_record' or ('usage' in row and 'response_id' in row):
        response_id=p.get('response_id')
        identified=isinstance(response_id,str) and bool(response_id)
        add('usage' if identified else 'unsupported_usage',usage(p.get('usage')) if identified else p,response_id if identified else None)
        if isinstance(p.get('thread_token_usage'),dict): add('cumulative',{'value':p['thread_token_usage'].get('total_tokens'),'counter_epoch':'thread'})
    elif outer=='turn_context' or nested=='thread_settings_applied':
        s=p.get('settings',p)
        if not isinstance(s,dict): s={}
        values={k:s.get(k) if isinstance(s.get(k),str) and 0<len(s[k])<=128 else None for k in ('model','effort','reasoning_effort','service_tier')}
        values['effort']=values['effort'] or values['reasoning_effort'];base['model']=values['model'] or model
        add('settings',values)
    elif outer=='session_meta':
        source=p.get('source',{});sub=source.get('subagent',{}) if isinstance(source,dict) else {};spawn=sub.get('thread_spawn',{}) if isinstance(sub,dict) else {}
        parent=p.get('parent_thread_id') or (spawn.get('parent_thread_id') if isinstance(spawn,dict) else None)
        add('thread',{'id':tid,'parent_thread_id':parent,'cli_version':p.get('cli_version'),'identity_basis':'root_tree','root_evidence':p.get('thread_source')=='user' or p.get('source')=='cli'},tid)
    elif outer=='response_item':
        native=p.get('id') or p.get('call_id')
        if nested=='message': add('message:'+str(p.get('role','unknown')),p,native,text_content(p.get('content')))
        elif nested=='reasoning': add('reasoning_summary',{'summary':p.get('summary',[]),'hidden_reasoning':'not_collected'},native,text_content(p.get('summary',[])))
        elif nested in ('function_call','custom_tool_call'): add('tool_call',p,p.get('call_id') or native)
        elif nested in ('function_call_output','custom_tool_call_output'): add('tool_output',p,p.get('call_id') or native)
        else: add('unknown:'+str(nested),p,native)
    elif nested=='token_count':
        for w in rate_windows(p.get('rate_limits')): add('limit',w,delivery+':limit:'+w['limit_id']+':'+w['role'])
        info=p.get('info')
        if isinstance(info,dict):
            if isinstance(info.get('total_token_usage'),dict): add('cumulative',{'value':info['total_token_usage'].get('total_tokens'),'counter_epoch':'thread'})
            if info.get('model_context_window') is not None: add('context_window',{'model_context_window':info['model_context_window'],'usage_semantics':'capacity_not_consumption'})
    elif outer=='event_msg' and nested in ('task_started','task_complete','turn_aborted','thread_goal_updated','agent_message','user_message','error','warning','turn_started','turn_completed'):
        add('goal' if nested=='thread_goal_updated' else str(nested),p,p.get('event_id'))
    elif 'assignment' in row: add('lifecycle',row,row.get('event_id'))
    elif outer in ('compacted','world_state','inter_agent_communication_metadata'): add(str(outer),p)
    else: add('unknown:'+str(nested),p,p.get('event_id'))
    return out
