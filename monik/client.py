"""Bounded client of the configured monik HTTP server, never a Codex client."""
from __future__ import annotations

import json
import os
from pathlib import Path
import ssl
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .config import expand
from .snapshot import open_regular


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Owner credentials must not follow a server redirect to another service.
        return None


def read_token(path):
    path=expand(path)
    fd=open_regular(path)
    try:
        s=os.fstat(fd)
        if s.st_mode & 0o077 or s.st_uid!=os.geteuid() or s.st_nlink!=1 or s.st_size>512:
            raise ValueError('Файл токена должен принадлежать пользователю, иметь права 0600 и не быть ссылкой.')
        with os.fdopen(fd,'r',encoding='utf-8',closefd=False) as stream:
            token=stream.read(513).strip()
        if not 32<=len(token)<=256:
            raise ValueError('Некорректная длина токена monik.')
        return token
    finally:
        os.close(fd)


class Client:
    def __init__(self, config, ca_file=None):
        self.token=read_token(config['token_file'])
        scheme='https' if config.get('tls_cert') else 'http'
        self.base=f"{scheme}://{config['bind']}:{config['port']}/api/v1/"
        handlers=[ProxyHandler({}), NoRedirect()]
        if scheme=='https':
            ca=ca_file or config.get('tls_ca')
            if not ca:
                candidate=expand(config['tls_cert']).parent/'monik-ca.crt'
                if candidate.is_file(): ca=str(candidate)
            context=ssl.create_default_context(cafile=str(expand(ca)) if ca else None)
            handlers.append(HTTPSHandler(context=context))
        self.opener=build_opener(*handlers)

    def get(self, resource, params=None):
        if resource not in ('overview','events','usage','limits','threads','health/sources') and not (
            resource.startswith('events/') and len(resource)==71 and all(c in '0123456789abcdef' for c in resource[7:])
        ):
            raise ValueError('Неизвестный read-only маршрут monik.')
        params={k:v for k,v in (params or {}).items() if v is not None and v!=''}
        request=Request(self.base+resource+'?'+urlencode(params),headers={'Authorization':'Bearer '+self.token,'Accept':'application/json'})
        try:
            with self.opener.open(request,timeout=2) as response:
                if response.status!=200:
                    raise ValueError('Сервер вернул неожиданный HTTP-статус.')
                body=response.read(8*1024*1024+1)
                if len(body)>8*1024*1024:
                    raise ValueError('Ответ monik превышает лимит размера. Сузь фильтр.')
                return json.loads(body)
        except HTTPError as exc:
            messages={401:'Токен не принят. Проверь monik token.',429:'Слишком много подключений.',503:'Хранилище сервера временно недоступно.'}
            raise ValueError(messages.get(exc.code,f'Ошибка HTTP {exc.code}; перенаправления не разрешены.')) from None
        except (URLError,TimeoutError,OSError) as exc:
            raise ValueError('Нет связи с monik. Проверь службу, адрес и доверие TLS-сертификату.') from None
