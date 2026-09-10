"""Shared presentation labels. Never translate protocol values or source payloads."""
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def labels():
    return json.loads((Path(__file__).parent / 'web' / 'labels.ru.json').read_text(encoding='utf-8'))


def label(group, value):
    return labels().get(group, {}).get(value, value)
