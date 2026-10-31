"""将饮食 Agent 的选择绑定真实 POI；店名、路线、参考价不接受模型覆写。"""
import math


def attach_dining(plan, state):
    previous = state.get('previous_plan') or {}
    if 'diet' not in state.get('selected_workers', []):
        plan['dining_recommendations'] = previous.get('dining_recommendations', [])
        plan['dining_note'] = previous.get('dining_note', '')
        return plan
    intent = state.get('intent_plan', {})
    plan['dining_recommendations'] = []
    plan['dining_note'] = ''
    if intent.get('dining_mode') != 'eat_out':
        return plan
    evidence = next((e for e in state.get('external_evidence', []) if e.get('tool') == 'restaurants'), {})
    candidates = {c['id']: c for c in (evidence.get('data') or {}).get('candidates', []) if c.get('id')}
    budget = intent.get('meal_budget_yuan')
    seen = set()
    for choice in state.get('diet', {}).get('restaurant_choices', []):
        candidate = candidates.get(choice['candidate_id'])
        if not candidate or (choice['meal'], candidate['id']) in seen:
            continue
        cost = candidate.get('average_cost_yuan')
        if budget and cost and cost > budget:
            continue
        seen.add((choice['meal'], candidate['id']))
        route = candidate.get('route') or {}
        plan['dining_recommendations'].append({
            'candidate_id': candidate['id'], 'name': candidate['name'], 'address': candidate.get('address'),
            'type': candidate.get('type'), 'meal': choice['meal'], 'reason': choice['reason'],
            'ordering_tips': choice.get('ordering_tips', []), 'budget_yuan': budget,
            'average_cost_yuan': cost, 'price_note': '地图人均参考价，不是本餐报价。' if cost else '地图未提供价格，需向商家确认是否符合预算。',
            'walking_minutes': math.ceil(route['duration_seconds'] / 60) if route.get('status') == 'ok' else None,
            'route_steps': route.get('instructions', []) if route.get('status') == 'ok' else [],
            'source': '高德 POI', 'retrieved_at': evidence.get('retrieved_at'),
            'verification_note': '点餐建议是通用搭配思路；实际菜单、食材、过敏原、营业和价格需向商家确认。未获取可靠营养数据，不估算本餐热量。',
        })
    plan['dining_note'] = ('以下是待你确认的外食候选，没有下单或预约。' if plan['dining_recommendations'] else
        '本轮没有可展示的有效餐馆推荐。' + evidence.get('message', '') + '可调整搜索范围或预算，仍可参考一般饮食建议。')
    return plan
