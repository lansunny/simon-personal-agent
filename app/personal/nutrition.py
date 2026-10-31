"""只把用户提供并可追溯的餐食模板绑定到计划，绝不从餐馆名推算营养。"""
import copy
from datetime import date
from app.multi_agent.quantification import nutrition_total


def bind_meals(plan,choices,templates):
    lookup={t['id']:t for t in templates};warnings=[]
    for choice in choices:
        template=lookup.get(choice.get('template_id'))
        if not template:warnings.append('忽略不存在的餐食模板ID。');continue
        servings=choice.get('servings',1)
        weekday=choice.get('weekday')
        if type(servings) not in (int,float) or not 0.25<=servings<=3 or type(weekday) is not int or not 0<=weekday<=6:
            warnings.append('忽略无效份数或星期。');continue
        for meal in plan.get('meal_schedule',[]):
            if meal.get('execution_status') in ('completed','partial','skipped'):continue
            if meal['meal']!=choice.get('meal') or date.fromisoformat(meal['date']).weekday()!=weekday:continue
            if meal.get('event_id'):continue
            items=copy.deepcopy(template['items'])
            for item in items:item['grams']*=servings
            meal.update(template_id=template['id'],template_revision=template['revision'],servings=servings,
                        nutrition=nutrition_total(items),title=template['name'],
                        basis='计划分量与用户提供标签的计算，尚未实际摄入。')
    plan['nutrition_warnings']=list(dict.fromkeys(warnings))
    return plan
