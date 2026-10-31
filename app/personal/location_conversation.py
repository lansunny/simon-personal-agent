"""位置相关的只读问答分流；不把附近地点查询变成完整健康计划。"""
import re


def nearby_requests(text):
    local=bool(re.search(r'附近|周边|周围|当前(?:位置|起点)|我的(?:位置|坐标)|已(?:授权|保存).{0,5}(?:位置|坐标)|这里|我这[边里]',text))
    # 无地名的简短天气问句可直接使用当前位置；指定其他地区的问句仍由协调者理解。
    simple_weather=bool(re.fullmatch(r'(?:请问|帮我看看|查一下|现在|今天|明天|当地|这边|的|天气|气温|如何|怎么样|怎样|下雨|会|吗|呢|是否|适合|跑步|运动|[，,。！？?！\s])+',text)) and bool(re.search(r'天气|气温|下雨',text))
    if not local and not simple_weather:return []
    # 完整规划仍交给健康图，场地信息查询不应被追问每周运动天数/开始时间。
    if re.search(r'规划|制定|计划|安排|日程|预约|提醒',text):return []
    queries=[]
    if local and re.search(r'跑步|慢跑|散步|步行|公园|绿道|步道|操场|运动|场地|健身',text):
        queries.append({'purpose':'outing','categories':['park','trail','sports']})
    if re.search(r'餐馆|饭店|餐厅|吃饭|吃什么|晚餐|午餐|早餐|美食|饮食',text):
        queries.append({'purpose':'restaurants','categories':[]})
    if re.search(r'天气|气温|下雨|降雨',text):queries.append({'purpose':'weather'})
    return queries


def asks_location_status(text):
    return bool(re.search(r'(?:看到|看见|识别|获取|知道|读取|访问|收到|有).{0,10}(?:我的位置|我的坐标|我现在的位置|当前(?:的)?位置|定位信息|我的定位)|(?:我的位置|我的定位|位置授权).{0,8}(?:成功|生效|有用|识别|收到|了吗)',text)) and not nearby_requests(text)


def location_reply(context):
    if context['status']!='available':return context['note']
    accuracy=context.get('accuracy_m')
    precision=f'，精度约{accuracy:g}米' if accuracy is not None else ''
    text=f'能，我已经收到你授权保存的位置{precision}。'
    if not context.get('can_search_nearby'):return text+'目前精度不足以提供可靠的具体路线，请重新定位或确认实际起点。'
    text+='可以直接按这个起点查附近适合运动的场地、餐馆和天气，不需要再填写城市。'
    if not context['environment_queries_allowed']:text+='目前联网查询尚未开启，请在“自动调整与联网授权”中允许环境查询。'
    return text+'这只是你上次主动提供的位置，我不会后台持续跟踪。'


def collect_nearby(environment,queries,today):
    evidence=[]
    for query in queries[:3]:
        item=environment.query(**query,target_date=today,radius_m=2000)
        evidence.append(item)
        # 小半径真实空结果才扩到5km；鉴权、配额或网络失败不重复请求。
        if item.get('status')=='empty' and query['purpose'] in ('outing','restaurants'):
            evidence.append(environment.query(**query,target_date=today,radius_m=5000))
    return evidence
