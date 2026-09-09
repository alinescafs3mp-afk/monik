#!/usr/bin/env python3
"""Offline validator for the sanitized Observatory fixtures. Standard library only."""
from __future__ import annotations
import argparse, collections, json
from pathlib import Path

def lines(path, tolerate_final=False):
 rows=[]; errors=[]
 data=path.read_bytes().splitlines()
 for n,raw in enumerate(data,1):
  try: rows.append(json.loads(raw))
  except Exception as exc:
   if tolerate_final and n==len(data): errors.append({'line':n,'kind':'incomplete_final_record'})
   else: errors.append({'line':n,'kind':type(exc).__name__})
 return rows,errors

def main():
 ap=argparse.ArgumentParser();ap.add_argument('package',nargs='?',default=str(Path(__file__).resolve().parent));ap.add_argument('--output');a=ap.parse_args()
 root=Path(a.package); fx=root/'fixtures'; expected=json.loads((fx/'expected_aggregates.json').read_text())
 checks=[]
 def check(name,actual,want):
  ok=actual==want;checks.append({'name':name,'ok':ok,'actual':actual,'expected':want})
  return ok
 # Response usage is a per-response delta. Deduplicate only stable identity, never text/timestamp.
 deliveries,_=lines(fx/'usage_deliveries.jsonl'); canonical={}; duplicates=0; conflicts=0
 for row in deliveries:
  u=row['usage']; assert u['cached_input_tokens']<=u['input_tokens'];assert u['reasoning_output_tokens']<=u['output_tokens'];assert u['total_tokens']==u['input_tokens']+u['output_tokens']
  key=(row['profile'],row['thread_id'],row['response_id'])
  if key in canonical:
   if canonical[key]['usage']==u: duplicates+=1
   else: conflicts+=1
  else: canonical[key]=row
 agg={}
 for row in canonical.values():
  p=row['profile'];out=agg.setdefault(p,collections.Counter());u=row['usage']
  for k in ('input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens','total_tokens'):out[k]+=u[k]
  out['uncached_input_tokens']+=u['input_tokens']-u['cached_input_tokens']
 agg={p:dict(v) for p,v in agg.items()};combined=dict(sum((collections.Counter(v) for v in agg.values()),collections.Counter()))
 check('usage.unique_records',len(canonical),expected['usage']['unique_records']);check('usage.duplicate_deliveries',duplicates,expected['usage']['duplicate_deliveries']);check('usage.conflicts',conflicts,expected['usage']['conflicts']);check('usage.by_profile',agg,expected['usage']['by_profile']);check('usage.combined',combined,expected['usage']['combined'])
 # Cumulative counters require a baseline per epoch. A negative change is explicit anomaly, not max(0, delta).
 rows,_=lines(fx/'cumulative_snapshots.jsonl');prev={};positive=baselines=repeats=negative=0
 for row in rows:
  key=(row['profile'],row['thread_id'],row['counter_epoch']);value=row['value']
  if key not in prev:baselines+=1
  else:
   delta=value-prev[key]
   if delta>0:positive+=delta
   elif delta==0:repeats+=1
   else:negative+=1
  prev[key]=value
 check('cumulative',{'known_positive_delta':positive,'baselines':baselines,'repeats':repeats,'negative_changes':negative},expected['cumulative'])
 # Incomplete tail must stay pending while prior complete rows survive.
 rows,errs=lines(fx/'incomplete_last_line.jsonl',True);check('incomplete_jsonl',{'valid_records':len(rows),'incomplete_records':len(errs)},expected['incomplete_jsonl'])
 # Rotation overlap is delivered twice but one stable item.
 allrot=[]
 for name in ('rotation_before.jsonl','rotation_after.jsonl'):allrot.extend(lines(fx/name)[0])
 ids=[x['payload']['id'] for x in allrot];check('rotation',{'delivered_records':len(ids),'unique_item_ids':len(set(ids)),'duplicates':len(ids)-len(set(ids))},expected['rotation'])
 # Lifecycle tree from registry.
 reg=json.loads((fx/'lifecycle_registry.json').read_text());child=reg['assignments']['A-CHILD#1'];grand=reg['assignments']['A-GRANDCHILD#1']
 life={'root':child['owner_thread_id'],'child':child['child_thread_id'],'grandchild':grand['child_thread_id'],'all_closed':all(x['state']=='CLOSED' for x in reg['assignments'].values())};check('lifecycle',life,expected['lifecycle'])
 # Rate snapshots are observations. Validate sequences only; no inferred summed spend.
 rates,_=lines(fx/'rate_limits_real_sanitized.jsonl');seq=collections.defaultdict(list)
 for row in rates:seq[row['_fixture']['profile']].append(row['payload']['rate_limits']['primary']['used_percent'])
 rate={'astra_used_sequence':seq['astra'],'sol_used_sequence':seq['sol'],'do_not_sum_drops':True};check('rate_limits',rate,expected['rate_limits'])
 # All other JSON/JSONL fixtures parse, except the documented final fragment.
 parse_errors=[]
 for p in sorted(fx.iterdir()):
  if p.suffix=='.json':
   try:json.loads(p.read_text())
   except Exception as exc:parse_errors.append({'file':p.name,'error':type(exc).__name__})
  elif p.suffix=='.jsonl' and p.name!='incomplete_last_line.jsonl':
   _,es=lines(p)
   if es:parse_errors.append({'file':p.name,'errors':es})
 check('other_fixture_parse_errors',parse_errors,[])
 result={'schema_version':1,'ok':all(c['ok'] for c in checks),'checks':checks,'semantics':{'cached_is_subset_of_input':True,'reasoning_is_subset_of_output':True,'dedupe_key':'profile,thread_id,response_id','text_is_never_a_dedupe_key':True,'first_cumulative_value_is_baseline':True,'negative_cumulative_change_is_not_clamped':True}}
 out=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
 if a.output:Path(a.output).write_text(out)
 else:print(out,end='')
 raise SystemExit(0 if result['ok'] else 1)
if __name__=='__main__':main()
