"""程序检查用户约束，并计算候选方案相对旧计划的真实变更。"""


def validate_plan(plan, profile, review, previous, selected_sections=None):
    selected_sections = selected_sections or ['diet', 'activity']
    errors = list(plan.get('schedule_conflicts', []))
    if plan['activity_days'] > profile['available_activity_days']:
        errors.append('活动天数超过用户可用天数')
    if plan['minutes_per_session'] > profile['minutes_per_session']:
        errors.append('单次活动时长超过用户可用时长')
    if (plan['activity_days'] == 0) != (plan['minutes_per_session'] == 0):
        errors.append('未安排活动时，活动天数和单次分钟数须同时为零')
    if 'diet' in selected_sections and not plan['diet_suggestions']:
        errors.append('所选饮食分支的建议不得为空')
    if ('activity' in selected_sections or plan['activity_days'] > 0) and not plan['activity_suggestions']:
        errors.append('所选活动分支的说明不得为空')
    if not plan['rationale'].strip():
        errors.append('协调依据不得为空')
    if previous and review.get('adjustment') == 'reduce_activity':
        for key in ('activity_days', 'minutes_per_session'):
            if plan[key] > previous[key]:
                errors.append(f'复盘建议降低活动负担，不得增加旧计划的 {key}')
    return errors


def diff_plans(previous, proposal):
    fields = {'activity_days': '每周活动天数', 'minutes_per_session': '单次分钟数',
              'diet_suggestions': '饮食建议', 'activity_suggestions': '活动建议'}
    return [{'field': key, 'label': label, 'before': previous.get(key) if previous else None,
             'after': proposal[key]} for key, label in fields.items()
            if previous is None or previous.get(key) != proposal[key]]
