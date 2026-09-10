"""Read-only graph routes; installed inside the existing authenticated app."""
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from .model import timestamp
from .series import usage_series

router = APIRouter()
WEB = Path(__file__).parent / 'web'


@router.get('/token-graph')
def page():
    return FileResponse(WEB / 'token-graph.html', media_type='text/html')


@router.get('/token-graph.js')
def script():
    return FileResponse(WEB / 'token-graph.js', media_type='text/javascript')


@router.get('/token-graph.css')
def style():
    return FileResponse(WEB / 'token-graph.css', media_type='text/css')


@router.get('/api/v1/usage-series')
def series(request: Request):
    q = request.query_params
    if set(q) - {'hours', 'profile', 'at'} or any(len(q.getlist(k)) != 1 for k in q):
        raise HTTPException(400, 'Неизвестный или повторяющийся параметр графика.')
    raw = q.get('hours', '6')
    if raw not in ('24', '12', '6', '3', '2', '1'):
        raise HTTPException(400, 'Допустимые интервалы: 24/12/6/3/2/1 ч.')
    profile = q.get('profile')
    if profile is not None and len(profile) > 64:
        raise HTTPException(400, 'Слишком длинное имя профиля.')
    at = None
    if 'at' in q:
        if len(q['at']) > 64 or (at := timestamp(q['at'])) is None:
            raise HTTPException(400, 'Для момента T укажи ISO-дату с часовым поясом.')
    try:
        result = usage_series(request.app.state.store, request.app.state.config['profiles'],
                              hours=int(raw), profile=profile, at=at)
        result['demo'] = bool(request.app.state.config.get('demo'))
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
