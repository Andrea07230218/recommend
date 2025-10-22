# core/place_filters.py
from typing import List

BAN_QUICK_STOPS = True

_BRANDS = [
    "50嵐","50lan","coco","都可","清心","清心福全","迷客夏","milksha","星巴克","starbucks",
    "再睡5分鐘","康青龍","可不可","kebuke","一芳","yifang","珍煮丹","tigersugar","老虎堂","tiger sugar",
    "鹿角巷","the alley","comebuy","chatime","日出茶太","麻古","macu","麥當勞","mcdonald",
    "kfc","肯德基","7-eleven","7-11","7 eleven","全家","familymart","茶湯會","珍奶","手搖","飲料"
]

_QUICK_TYPES = {"convenience_store", "fast_food"}

def is_quick_stop(name: str = "", types: List[str] = None) -> bool:
    n = (name or "").lower()
    if any(k.lower() in n for k in _BRANDS):
        return True
    ts = set((types or []) or [])
    return len(_QUICK_TYPES & ts) > 0

def scrub_quick_stops(places: List[dict]) -> List[dict]:
    return [p for p in (places or []) if not is_quick_stop(p.get("name",""), p.get("types", []))]
