"""固定服务地址、有限结果；不传档案/体重/密钥给搜索查询。"""
import json
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import settings


def fetch(url, body=None):
    payload = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=payload, headers={'Content-Type': 'application/json', 'User-Agent': 'Simon-Learning/1.0'})
    with urlopen(request, timeout=12) as response:
        return json.load(response)


def result(status, data=None, sources=None, message=''):
    return {'status': status, 'data': data, 'sources': sources or [], 'message': message,
            'retrieved_at': datetime.now(timezone.utc).isoformat()}


def weather(city: str = '', target_date: str = '', latitude: float | None = None, longitude: float | None = None):
    from app.integrations.weather_providers import fetch_weather
    from app.multi_agent.weather_rules import annotate_weather
    return annotate_weather(fetch_weather(fetch,settings.amap_api_key,city,target_date,latitude,longitude))


def search(query: str):
    if not settings.tavily_api_key:
        return result('not_configured', message='未配置 TAVILY_API_KEY，不能执行联网搜索。')
    if not query.strip():
        return result('missing_input', message='缺少公共搜索关键词。')
    data = fetch('https://api.tavily.com/search', {'api_key': settings.tavily_api_key, 'query': query[:160],
                 'max_results': 4, 'search_depth': 'basic', 'include_answer': False})
    hits = [{'title': r.get('title', ''), 'url': r.get('url', ''), 'snippet': r.get('content', '')[:1200]}
            for r in data.get('results', []) if r.get('url', '').startswith(('https://', 'http://'))]
    return result('ok' if hits else 'empty', hits, [{'title': h['title'], 'url': h['url']} for h in hits],
                  '搜索摘要尚未逐页核实；预约余量和营业时间须核对官方来源。')


def amap_error(code):
    code = str(code)
    if not code.isdigit():
        code = 'unknown'
    reasons = {'10001': 'Key 无效，请核对控制台中有效的 Web 服务 Key',
               '10003': '调用次数超出配额', '10004': '访问频率超限',
               '10005': 'IP 白名单不匹配', '10009': 'Key 平台类型不匹配，需要 Web 服务类型'}
    return f"高德错误 {code}：{reasons.get(code, '请检查服务权限和配额')}。修改 .env 后请重启 run.py。"


def venues(city: str, query: str = '运动场馆'):
    if not settings.amap_api_key:
        return result('not_configured', message='未配置 AMAP_API_KEY，需要高德 Web 服务 Key。')
    if not city.strip():
        return result('missing_input', message='需要城市名称。')
    data = fetch('https://restapi.amap.com/v3/place/text?' + urlencode({
        'key': settings.amap_api_key, 'keywords': query[:80], 'city': city, 'citylimit': 'true', 'offset': 5, 'page': 1, 'extensions': 'base'}))
    if data.get('status') != '1':
        return result('provider_error', message=amap_error(data.get('infocode')))
    hits = [{k: p.get(k) for k in ('name', 'address', 'adname', 'location', 'tel')} for p in data.get('pois', [])]
    return result('ok' if hits else 'empty', hits, [{'title': '高德地点检索', 'url': 'https://www.amap.com/'}],
                  '仅场地候选，不包含实时空位、预约和付款能力。')


def ingredients(query: str = ''):
    foods = [
        {'name': '燕麦', 'group': '主食', 'note': '常见简餐食材，具体用量需结合实际情况。'},
        {'name': '豆腐', 'group': '豆制品', 'note': '含大豆，注意个人忌口。'},
        {'name': '鸡蛋', 'group': '蛋类', 'note': '注意鸡蛋过敏和烹饪条件。'},
        {'name': '西兰花', 'group': '蔬菜', 'note': '按个人喜好选用，可替换其他常见蔬菜。'},
    ]
    return result('ok', {'catalog': foods, 'query': query}, message='内置示例目录；无实时价格、库存、热量估算或购买功能。')


def execute(name, arguments):
    allowed = {'weather': weather, 'search': search, 'venues': venues, 'ingredients': ingredients, 'outing': outing, 'restaurants': restaurants}
    try:
        return allowed[name](**arguments)
    except Exception:
        # 不回传带查询 Key 的 URL 或原始网络异常。
        return result('unavailable', message='工具暂不可用，请稍后重试或手动补充信息。')


GEO_CATEGORIES = {'park': '公园', 'trail': '步道', 'sports': '体育场', 'fitness': '健身中心'}


def _discover(city='', place_name='', start_place='', latitude=None, longitude=None,
           categories=None, radius_m=2000, dining=False):
    """发现真实候选及往返路线。工具不替 Agent 选第一条结果。"""
    from concurrent.futures import ThreadPoolExecutor
    catalog = RESTAURANT_CATEGORIES if dining else GEO_CATEGORIES
    categories = list(dict.fromkeys(categories or list(catalog)[:3]))[:3]
    categories = [c for c in categories if c in catalog]
    radius_m = max(300, min(5000, int(radius_m)))
    data = {'weather': {}, 'candidates': [], 'route': {'status': 'not_selected'},
            'search': {'categories': categories, 'radius_m': radius_m, 'place_name': place_name}}
    sources = []
    if not dining:
        data['weather']=execute('weather',{'city':city,'latitude':latitude,'longitude':longitude})
        sources.extend(data['weather'].get('sources',[]))
    if not settings.amap_api_key:
        return result('partial', data, sources, '未配置高德 Web 服务 Key，无法查询真实场地。')

    def amap(path, params):
        response = fetch('https://restapi.amap.com/v3/' + path + '?' + urlencode({'key': settings.amap_api_key, **params}))
        if response.get('status') != '1':
            # 只暴露数值错误码，不回传请求地址或密钥。
            code = str(response.get('infocode', ''))
            raise ValueError(code if code.isdigit() else 'unknown')
        return response

    try:
        origin = None
        if latitude is not None and longitude is not None:
            origin = amap('assistant/coordinate/convert', {'locations': f'{longitude},{latitude}', 'coordsys': 'gps'}).get('locations')
        elif start_place and city:
            geo = amap('geocode/geo', {'address': start_place, 'city': city}).get('geocodes', [])
            if len(geo) == 1:
                origin = geo[0]['location']
        if not origin:
            return result('missing_input', data, sources, '无法唯一确定起点，请授权定位或补充城市和具体起点。')
        queries = [('specified', place_name.replace('用户提到的', ''))] if place_name else [(c, catalog[c]) for c in categories]
        candidates = []
        seen = set()
        for category, query in queries:
            rows = amap('place/around', {'location': origin, 'keywords': query[:80], 'radius': radius_m,
                        'sortrule': 'distance', 'offset': 2, 'page': 1, 'extensions': 'all', **({'types': '050000'} if dining else {})}).get('pois', [])
            for row in rows:
                if not row.get('id') or not row.get('location') or row['id'] in seen:
                    continue
                seen.add(row['id'])
                candidates.append({'id': row['id'], 'name': row.get('name', ''), 'category': category,
                    'type': row.get('type', ''), 'address': row.get('address', ''), 'distance_m': row.get('distance'),
                    'location': row['location'], 'opening_status': 'unknown', 'booking_status': 'unknown',
                    'indoor_status':'unknown','public_access_status':'unknown',
                    'access_note':'校园/机构场地，未核实公众进入许可，不应作为默认可执行方案。' if any(w in row.get('name','') for w in ('学校','中学','小学','校区','校园','部队')) else '是否室内、公众开放与当前可进入状态需核实。'})
                if dining:
                    biz = row.get('biz_ext') or {}
                    cost = str(biz.get('cost', '')) if isinstance(biz, dict) else ''
                    candidates[-1].update(average_cost_yuan=float(cost) if cost.replace('.', '', 1).isdigit() and float(cost) > 0 else None,
                                          menu_status='unknown', allergens_status='unknown', nutrition_status='unknown')

        def route_between(a, b):
            try:
                paths = amap('direction/walking', {'origin': a, 'destination': b}).get('route', {}).get('paths', [])
                if not paths:
                    return {'status': 'empty'}
                path = paths[0]
                return {'status': 'ok', 'distance_m': int(path['distance']), 'duration_seconds': int(path['duration']),
                        'instructions': [step.get('instruction', '') for step in path.get('steps', [])][:30]}
            except Exception:
                return {'status': 'unavailable'}

        def attach_routes(candidate):
            candidate['route'] = route_between(origin, candidate['location'])
            candidate['return_route'] = route_between(candidate['location'], origin)
            return candidate

        # 最多六个候选；网络并发仅用于缩短等待，不改变 Agent 的选择职责。
        with ThreadPoolExecutor(max_workers=6) as pool:
            data['candidates'] = list(pool.map(attach_routes, candidates[:6]))
        data['origin'] = origin
        sources.append({'title': '高德 POI 与双向步行路径', 'url': 'https://www.amap.com/'})
        return result('ok' if candidates else 'empty', data, sources,
                      f'找到 {len(candidates)} 个真实候选，由' + ('饮食' if dining else '运动') + ' Agent 比较选择。营业、菜单、费用和预约情况需向商家确认。')
    except ValueError as exc:
        return result('provider_error', data, sources, amap_error(str(exc)))
    except Exception:
        return result('unavailable', data, sources, '地图服务暂不可用，请稍后重试；不会编造场地。')


RESTAURANT_CATEGORIES = {'chinese': '中餐', 'light': '轻食', 'noodles': '面馆', 'vegetarian': '素食', 'restaurant': '餐厅'}


def outing(city='', place_name='', start_place='', latitude=None, longitude=None, categories=None, radius_m=2000):
    return _discover(city, place_name, start_place, latitude, longitude, categories, radius_m)


def restaurants(city='', start_place='', latitude=None, longitude=None, categories=None, radius_m=2000):
    """只传公开类别和起点；饮食限制、体重等留在 Agent 内部。"""
    return _discover(city=city, start_place=start_place, latitude=latitude, longitude=longitude,
                     categories=categories or ['chinese', 'light'], radius_m=radius_m, dining=True)
