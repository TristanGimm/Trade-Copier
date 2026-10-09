"""English display text for legacy messages persisted before the language update."""
import json
from pathlib import Path
_TRANSLATIONS=sorted(json.loads(Path(__file__).with_name('english.json').read_text()).items(),key=lambda pair:-len(pair[0]))
def display(value):
    if not isinstance(value,str):return value
    for old,new in _TRANSLATIONS:value=value.replace(old,new)
    return value
