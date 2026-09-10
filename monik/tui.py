"""Terminal observer of the existing monik server. No collector or Codex controls.

Rendering is pure and reusable in --once mode. Network requests run in one client
thread so a missing server cannot block key handling or terminal restoration.
"""
from __future__ import annotations

import curses
from datetime import datetime
import json
import locale
import math
import queue
import re
import sys
import threading
import time
import unicodedata

from .client import Client
from .i18n import label, labels
from .model import timestamp

PAGES=('overview','activity','tokens','limits','tree','quality')
OSC=re.compile(r'(?:\x1b\]|\x9d)[\s\S]*?(?:\x07|\x1b\\|\x9c|$)')
CSI=re.compile(r'(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]')


def terminal_text(value):
    """Strip terminal protocols, bidi controls and control characters from data."""
    text=CSI.sub('',OSC.sub('',str(value)))
    return ''.join(c for c in text if c in '\n\t' or not unicodedata.category(c).startswith('C')).expandtabs(4)


def clip(text,width):
    result=[];used=0
    for c in terminal_text(text).replace('\n',' '):
        size=0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W','F') else 1
        if used+size>width: break
        result.append(c);used+=size
    return ''.join(result)


def number(value):
    if type(value) not in (int,float) or (type(value) is float and not math.isfinite(value)):
        return 'нет измерения'
    return f'{value:,}'.replace(',',' ')


def date(value):
    if value is None: return 'время неизвестно'
    try: return datetime.fromtimestamp(value).astimezone().strftime('%d.%m %H:%M:%S %Z')
    except (ValueError,OverflowError,TypeError,OSError): return 'время некорректно'


def render(page,data,width=100,selected=None):
    """Human labels are Russian; models, IDs, code and messages stay verbatim."""
    lines=[]
    if page=='overview':
        for p in data.get('profiles',[]):
            settings=(p.get('settings') or {}).get('data',{});usage=p.get('usage',{})
            lines += [p['profile'].upper(),
                f"  Модель: {settings.get('model') or 'неизвестна'} | reasoning_effort: {settings.get('effort') or 'неизвестен'} | service_tier: {settings.get('service_tier') or 'неизвестен'}",
                f"  Токены профиля: {number(usage.get('total_tokens'))} | Ответов: {number(usage.get('responses'))} | Спорных: {number(usage.get('conflicts',0))}",
                f"  Сама root-сессия: {number((p.get('own_usage') or {}).get('total_tokens'))}",
                f"  root: {p.get('root_id') or 'не установлен'}"]
            task=p.get('last_task')
            if task: lines.append(f"  Последнее свидетельство: {label('events',task['kind'])}, {date(task['time'])}")
            else: lines.append('  Текущая активность не подтверждена.')
            if p.get('goal'): lines.append('  Цель: '+p['goal']['text'][:1000])
            lines.append('')
        lines.append('Охват частичный. Соединение с monik не подтверждает активность Codex.')
    elif page=='activity':
        for index,e in enumerate(data.get('items',[])):
            lines.append(('> ' if index==selected else '  ')+f"{date(e['event_at'] if e['event_at'] is not None else e['time'])} | {e['profile']} | {label('events',e['kind'])}")
            lines.append('  thread: '+e['thread_id'])
            for text in terminal_text(e.get('text','')).splitlines()[:12]:
                # Long lines remain inspectable through the detail screen and web UI.
                lines.append('  '+text)
            lines.append('')
        if data.get('next_before'): lines.append('[ Более ранняя страница: [ ]')
        lines.append('j/k: выбрать событие; Enter: открыть выбранное событие.')
    elif page=='tokens':
        for key,name in [('input_tokens','Ввод'),('cached_input_tokens','Ввод из кэша'),('uncached_input_tokens','Ввод без кэша'),('output_tokens','Вывод'),('reasoning_output_tokens','Reasoning в выводе'),('total_tokens','Всего')]:
            lines.append(f'{name}: {number(data.get(key))}')
        lines += [f"Ответов: {number(data.get('responses'))} | Некорректных: {number(data.get('invalid'))} | Спорных: {number(data.get('conflicts'))}",
                  'Кэш входит во ввод; reasoning входит в вывод. Конфликты исключены из суммы.','']
        for row in data.get('groups',[])[:200]:
            lines.append(f"{row['profile']} | {row['model'] or 'модель неизвестна'} | {row['thread_id']} | {number(row['total_tokens'])}")
        if data.get('groups_capped') or len(data.get('groups',[]))>200: lines.append('Показана ограниченная выборка групп; сузь фильтр.')
    elif page=='limits':
        for item in data.get('latest',[]):
            remaining='нет измерения' if item.get('remaining_percent') is None else number(item['remaining_percent'])+'%'
            lines += [f"{item['profile']} | {item['limit_id']} | {label('windows',item['role'])}",
                      f"  Осталось: {remaining} | окно: {number(item.get('window_minutes'))} мин",
                      f"  Сброс: {date(item.get('resets_at'))} | возраст: {int(item['age_seconds'])} с"+(' | УСТАРЕЛО' if item['stale'] else ''),'']
        lines.append('Проценты не пересчитываются в токены. Причина изменения снимка неизвестна.')
    elif page=='tree':
        for item in data.get('items',[]):
            lines += [f"{item['profile']} | {item.get('nickname') or item['thread_id']}",
                f"  {label('states',item['state'])}"+(' | УСТАРЕЛО' if item['stale'] else ''),
                f"  thread: {item['thread_id']} | parent: {item.get('parent_thread_id') or 'не подтверждён'}",'']
        if data.get('capped'): lines.append('Достигнут предел выборки дерева.')
    elif page=='quality':
        collector=data.get('collector',{})
        lines += ['Текущее состояние сервера, не исторический срез.',
            f"Последний успешный цикл: {date(collector.get('last_success'))}",
            f"Ошибка цикла: {collector.get('last_error') or 'не зафиксирована'}",
            f"Свободно: {number(data.get('disk',{}).get('free_bytes'))} байт",'']
        for source in data.get('sources',[]):
            lines += [f"{source['profile']} | {label('states',source['status'])} | {source['kind']}",
                      f"  {source['source']} | {source.get('detail','')}"]
    elif page=='detail':
        lines += [f"Событие: {data['uid']}",f"Профиль: {data['profile']} | thread: {data['thread_id']}",
            f"Событие: {date(data['event_at'])} | Приём: {date(data['ingested_at'])}",
            'Очищенный сохранённый фрагмент (не оригинал):',data.get('payload_page',''),
            'Происхождение записи:',json.dumps(data.get('provenance',[]),ensure_ascii=False,indent=2)]
        if data.get('next_offset') is not None: lines.append('Следующая страница данных: n. Назад: Esc.')
    if not lines: lines=['Нет доступных данных для выбранного фильтра.']
    wrapped=[]
    for block in lines:
        for line in terminal_text(block).splitlines():
            if not line: wrapped.append('');continue
            # Each logical line is already sanitized. Walk it once instead of
            # sanitizing the entire remaining suffix for every terminal row.
            start=0;cells=0;limit=max(2,width)
            for index,char in enumerate(line):
                size=0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in ('W','F') else 1
                if cells+size>limit:
                    wrapped.append(line[start:index]);start=index;cells=0
                cells+=size
            wrapped.append(line[start:])
    return wrapped


def filter_overview(page, data, params):
    """The overview API returns all cards; apply the requested display scope."""
    profile=params.get('profile')
    if page=='overview' and profile:
        return {**data,'profiles':[card for card in data.get('profiles',[]) if card.get('profile')==profile]}
    return data


class Fetcher:
    """One bounded mailbox, no unbounded task queue or duplicate collectors."""
    def __init__(self,client):
        self.client=client;self.requests=queue.Queue(maxsize=1);self.results=queue.Queue(maxsize=1)
        self.stop=threading.Event();self.thread=threading.Thread(target=self.run,name='monik-tui-http',daemon=True)
        self.thread.start()

    @staticmethod
    def replace(mailbox,value):
        try: mailbox.get_nowait()
        except queue.Empty: pass
        try: mailbox.put_nowait(value)
        except queue.Full: pass

    def request(self,version,page,params):
        self.replace(self.requests,(version,page,dict(params)))

    def run(self):
        routes={'activity':'events','tokens':'usage','tree':'threads','quality':'health/sources'}
        while not self.stop.is_set():
            try: version,page,params=self.requests.get(timeout=.1)
            except queue.Empty: continue
            try:
                resource='events/'+params.pop('_event') if page=='detail' else routes.get(page,page)
                result=filter_overview(page,self.client.get(resource,params),params);error=None
            except Exception as exc:
                result=None;error=terminal_text(str(exc))
            self.replace(self.results,(version,result,error))

    def close(self):
        self.stop.set();self.thread.join(timeout=2.2)


def run(config,*,once=False,page='overview',profile=None,at=None,ca_file=None):
    if page not in PAGES: raise ValueError('Неизвестный раздел TUI.')
    if profile and profile not in {p['name'] for p in config['profiles']}:
        raise ValueError('Профиль отсутствует в конфигурации monik.')
    params={}
    if profile: params['profile']=profile
    if at:
        if timestamp(at) is None: raise ValueError('Для момента T укажи ISO-дату с часовым поясом.')
        params['at']=at
    client=Client(config,ca_file)
    if once:
        route={'activity':'events','tokens':'usage','tree':'threads','quality':'health/sources'}.get(page,page)
        print('\n'.join(render(page,filter_overview(page,client.get(route,params),params),120)))
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError('Интерактивному TUI нужен терминал. Для разового вывода используй --once.')
    try: locale.setlocale(locale.LC_ALL,'')
    except locale.Error: pass
    fetcher=Fetcher(client)
    try: curses.wrapper(_screen,fetcher,config,page,params)
    except KeyboardInterrupt: pass
    except curses.error as exc: raise ValueError('Не удалось открыть TUI: проверь TERM и поддержку терминала.') from exc
    finally: fetcher.close()


def _screen(screen,fetcher,config,page,params):
    try: curses.curs_set(0)
    except curses.error: pass
    screen.timeout(100);screen.keypad(True)
    version=0;dirty=True;paused=False;scroll=0;selected=0;data={};error=None
    pending=False;last=0;last_ok=None;prompt=None;text='';history=[];return_page=page
    profiles=['']+[p['name'] for p in config['profiles']]
    help_open=False
    seek_selected=False
    rendered_data=None;rendered_key=None;rendered_lines=[]

    def draw(y,text,style=0):
        height,width=screen.getmaxyx()
        if 0<=y<height and width>1:
            try: screen.addstr(y,0,clip(text,width-1),style)
            except curses.error: pass

    while True:
        if not help_open and prompt is None and (dirty or (not paused and not params.get('at') and not params.get('before') and page!='detail' and time.monotonic()-last>=2 and not pending)):
            version+=1;fetcher.request(version,page,params);dirty=False;pending=True;last=time.monotonic()
        try:
            received,result,failure=fetcher.results.get_nowait()
            if received==version:
                pending=False
                if failure: error=failure
                else: data=result;error=None;last_ok=date(time.time())
        except queue.Empty: pass
        screen.erase();height,width=screen.getmaxyx()
        draw(0,'monik | '+label('pages',page)+' | '+('ПАУЗА ЭКРАНА' if paused else 'LIVE')+(' | ПРОШЛОЕ T' if params.get('at') else ''),curses.A_BOLD)
        draw(1,'  '.join(f'{i+1}:{label("pages",p)}'+('*' if page==p else '') for i,p in enumerate(PAGES)))
        draw(2,f"Профиль: {params.get('profile') or 'все'} | Поиск: {params.get('q') or 'нет'} | Обновлено: {last_ok or 'ожидание'}")
        draw(3,error or (f"T: {params['at']}" if params.get('at') else 'Пауза и выход не останавливают сервер или Codex.'))
        # Key polling and scrolling do not change the formatted snapshot. Retain
        # the actual data object, not only id(data), so object-ID reuse is safe.
        render_key=(page,max(1,width-1),selected,bool(pending) if not data else False)
        if rendered_data is not data or rendered_key!=render_key:
            rendered_lines=render(page,data,max(1,width-1),selected=selected) if data else ['Загрузка...' if pending else 'Нет данных.']
            rendered_data=data;rendered_key=render_key
        lines=rendered_lines
        if help_open:
            lines=['Управление monik',
                '1–6 или Tab: раздел. p: переключить профиль.',
                'Space: заморозить экран. r: один новый снимок.',
                'Стрелки: прокрутка с паузой. j/k: выбрать событие.',
                '[ и ]: старые и новые страницы активности.',
                'Enter: выбранное событие. n: следующая часть данных.',
                't: исторический момент. l: вернуться к LIVE.',
                '/: буквальный поиск. Esc: назад / очистить поиск.',
                'q: выход. Сервер и Codex продолжают работу.',
                '? или Esc: закрыть справку.']
        if page=='activity' and data.get('items') and not help_open:
            selected=min(selected,len(data['items'])-1)
            draw(3,error or f'Выбрано событие {selected+1}/{len(data["items"])}: {data["items"][selected]["kind"]} | j/k выбрать, Enter открыть')
        visible=max(0,height-7)
        if seek_selected and page=='activity' and not help_open:
            index=next((i for i,line in enumerate(lines) if line.startswith('> ')),0)
            if index<scroll or index>=scroll+visible: scroll=index
            seek_selected=False
        scroll=min(scroll,max(0,len(lines)-visible))
        for row,line in enumerate(lines[(0 if help_open else scroll):(0 if help_open else scroll)+visible],4): draw(row,line,curses.A_REVERSE if page=='activity' and line.startswith('> ') else 0)
        draw(height-3,'q: выход  ?: помощь  Space: пауза  p: профиль  /: поиск  t: T  l: LIVE  r: обновить')
        draw(height-2,'↑↓/PgUp/PgDn: прокрутка  [ ]: страницы  Enter: детали  Esc: назад  ?: справка')
        draw(height-1,(prompt+': '+text) if prompt else ('Запрос...' if pending else f'Строки {scroll+1}–{min(len(lines),scroll+visible)} из {len(lines)}'))
        screen.refresh()
        try: key=screen.get_wch()
        except curses.error: continue
        if prompt:
            if key=='\x1b': prompt=None;continue
            if key in ('\n','\r'):
                if prompt=='Поиск':
                    params['q']=text;params['search_mode']='substring';page='activity';params.pop('_event',None);params.pop('offset',None);params.pop('before',None);history=[]
                elif prompt=='Момент T (ISO с часовым поясом)':
                    if timestamp(text) is None: error='Некорректный момент T. Пример: 2026-09-10T12:00:00+03:00';prompt=None;continue
                    params['at']=text
                prompt=None;scroll=0;data={};dirty=True;continue
            if key in ('\b','\x7f',curses.KEY_BACKSPACE): text=text[:-1]
            elif isinstance(key,str) and key.isprintable() and len(text)<256: text+=key
            continue
        if key in ('q','й'): return
        if key=='?':
            help_open=not help_open
            if help_open: version+=1;pending=False
            continue
        if help_open:
            if key=='\x1b': help_open=False
            continue
        if key==' ': paused=not paused;version+=1;pending=False;dirty=not paused;continue
        if key in ('r','к'): dirty=True;continue
        if key in ('/','t'):
            version+=1;pending=False
            prompt='Поиск' if key=='/' else 'Момент T (ISO с часовым поясом)';text=params.get('q' if key=='/' else 'at','');continue
        if key in ('l','д'):
            if page=='detail':
                page=return_page;params.pop('_event',None);params.pop('offset',None);data={}
            params.pop('at',None);params.pop('before',None);history=[];paused=False;scroll=0;dirty=True;continue
        if key in ('p','з'):
            current=params.get('profile','');params['profile']=profiles[(profiles.index(current)+1)%len(profiles)] if current in profiles else ''
            params.pop('before',None);history=[];data={};scroll=0;dirty=True;continue
        if key=='\t' or isinstance(key,str) and key in '123456':
            page=PAGES[(PAGES.index(page)+1)%len(PAGES)] if key=='\t' and page in PAGES else PAGES[int(key)-1] if key!='\t' else 'overview'
            return_page=page;params.pop('_event',None);params.pop('offset',None);params.pop('before',None);history=[];scroll=0;selected=0;data={};dirty=True;continue
        if key in (curses.KEY_UP,curses.KEY_DOWN,curses.KEY_PPAGE,curses.KEY_NPAGE):
            scroll=max(0,scroll+({curses.KEY_UP:-1,curses.KEY_DOWN:1,curses.KEY_PPAGE:-max(1,visible),curses.KEY_NPAGE:max(1,visible)}[key]));paused=True;version+=1;pending=False;continue
        if page=='activity' and key in ('j','k'):
            selected=max(0,min(len(data.get('items',[]))-1,selected+(1 if key=='j' else -1)));paused=True;version+=1;pending=False;seek_selected=True;continue
        if page=='activity' and key=='[' and data.get('next_before'):
            history.append(params.get('before'));params['before']=data['next_before'];scroll=0;selected=0;paused=True;data={};dirty=True;continue
        if page=='activity' and key==']' and history:
            params['before']=history.pop();scroll=0;selected=0;data={};dirty=True;continue
        if page=='activity' and key in ('\n','\r') and data.get('items'):
            return_page=page;params['_event']=data['items'][selected]['uid'];page='detail';params['offset']=0;scroll=0;data={};dirty=True;continue
        if page=='detail' and key=='n' and data.get('next_offset') is not None:
            params['offset']=data['next_offset'];scroll=0;data={};dirty=True;continue
        if key=='\x1b':
            if page=='detail': page=return_page;params.pop('_event',None);params.pop('offset',None)
            else: params.pop('q',None)
            scroll=0;data={};dirty=True
