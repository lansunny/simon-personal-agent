"""透明的离线脚本演示：规则生成角色结果，不模拟为真实模型推理。"""
from pydantic import BaseModel
from app.multi_agent.tools import role_tools
from app.multi_agent.routing import demo_intent


class DemoRunner:
    mode = 'demo'

    def run(self, role: str, context: dict, output_type: type[BaseModel], trace=None, retrieval=None, extra_bindings=None):
        trace = trace if trace is not None else []
        evidence = {}
        for name, tool in role_tools(role, context).items():
            evidence[name] = tool()
            trace.append(f'{role}: [demo] tool={name}')
        profile = context.get('profile', {})
        previous = context.get('previous_plan')
        if role == 'router':
            result = demo_intent(context)
        elif role == 'advisor':
            items = context.get('external_evidence', [])
            history = next((r for r in items if r.get('tool') == 'history'), None)
            if history:
                import json
                text = '本机历史查询（离线，不作模型推断）：\n' + json.dumps(history.get('data', {}), ensure_ascii=False, indent=2)
            else:
                details = ['工具 %s：%s；%s' % (r['tool'], r.get('status'), r.get('message', '')) for r in items]
                text = '离线信息查询演示。' + ('；'.join(details) if details else '未获取外部证据，不能核实实时情况。')
            result = {'text': text, 'evidence_ids': [r['id'] for r in items if r.get('status') == 'ok']}
        elif role == 'analysis':
            support = profile.get('needs_professional_support')
            route = 'clarify' if support is None else 'professional_support' if support else 'plan'
            result = {'route': route, 'reason': '脚本按已确认档案分流。'}
        elif role == 'diet':
            preferences = '、'.join(profile.get('food_preferences', [])) or '日常可获得的食物'
            suggestions = [f'围绕{preferences}安排规律餐食，记录饮食感受。']
            if any(r.get('meal_notes') for r in context.get('records', [])):
                suggestions.append('已有饮食记录，继续标记容易坚持的餐食，不因缺少活动额外限制饮食。')
            result = {'suggestions': suggestions, 'rationale': '脚本演示：使用当前偏好和记录完整度。'}
            templates=context.get('meal_templates',[])
            if templates and context.get('intent_plan',{}).get('dining_mode')!='eat_out':
                result['meal_choices']=[{'template_id':templates[0]['id'],'weekday':n,'meal':'dinner','servings':1,
                                         'reason':'离线示例：展示用户模板一份，不代表模型个性化推荐。'} for n in range(7)]
            dining = next(((e.get('data') or {}) for e in context.get('external_evidence', []) if e.get('tool') == 'restaurants'), {})
            candidates = dining.get('candidates', [])
            result['restaurant_choices'] = [{'candidate_id': c['id'], 'meal': 'dinner',
                'reason': '离线脚本展示候选，不代表模型比较结果。',
                'ordering_tips': ['若菜单提供，可组合蔬菜、蛋白质来源与主食，先确认食材和价格。']} for c in candidates[:2]]
        elif role == 'activity':
            days = min(previous['activity_days'], profile['available_activity_days']) if previous else profile['available_activity_days']
            minutes = min(previous['minutes_per_session'], profile['minutes_per_session']) if previous else profile['minutes_per_session']
            # 首次规划故意多一天，以展示校验回流；调整任务不注入该冲突。
            if not previous and days > 0 and minutes > 0:
                days = min(7, days + 1)
            if not days or not minutes:
                days = minutes = 0
            result = {'days': days, 'minutes': minutes, 'suggestions': ['可在自身舒适范围内选择轻松步行；不适时停止。' if days else '本轮暂不安排活动，之后根据可用时间更新。']}
            # Demo 只演示消费证据的规则，不能冒充 Agent 智能选址。
            outing = next((e.get('data') or {} for e in context.get('external_evidence', []) if e.get('tool') == 'outing'), {})
            viable = [c for c in outing.get('candidates', []) if c.get('route', {}).get('status') == 'ok'
                      and c.get('return_route', {}).get('status') == 'ok'
                      and (c['route']['duration_seconds'] + c['return_route']['duration_seconds']) / 60 + 5 < minutes]
            candidate = min(viable, key=lambda c: c['route']['duration_seconds'] + c['return_route']['duration_seconds']) if viable else None
            result['venue_choices'] = [{'weekday': day, 'candidate_id': candidate['id'] if candidate else '',
                'activity': context.get('intent_plan', {}).get('activity_type', 'walk') if candidate else 'indoor',
                'reason': '离线规则演示：选往返耗时最少的候选。' if candidate else '离线演示：无可用路线，采用室内备选。'} for day in range(7)]
        elif role == 'sleep':
            result = {'suggestions': [f"按已设置作息预留 {profile.get('sleep_bedtime','23:00')} 至次日 {profile.get('sleep_wake_time','07:00')} 的休息窗口；真实睡眠以记录为准。"], 'rationale': '离线脚本：读取作息设置与记录，不作睡眠诊断。'}
        elif role == 'review':
            summary = evidence['summarize_week']
            # 仅演示明确词的匹配，不宣称具备自然语言理解或医学判断能力。
            difficult = any(any(word in f['text'] for word in ('太累', '难坚持', '时间不够')) for f in summary['feedback'])
            adjustment = 'reduce_activity' if difficult else 'maintain' if summary['recorded_days'] >= 3 else 'collect_more'
            result = {'evidence_status': 'available' if summary['recorded_days'] >= 3 else 'limited',
                      'observations': [f"七天内记录 {summary['recorded_days']} 天，体重样本 {summary['weight_samples']} 个。", '描述性观测，不判断脂肪变化。'],
                      'adjustment': adjustment,
                      'recommendations': ['反馈出现执行困难，可先减少活动安排并等待用户确认。' if difficult else '保留可执行安排，补充记录，不因缺失自动加练或少吃。']}
        elif role == 'coordinator':
            activity = context['activity']
            repairing = bool(context.get('validation_errors'))
            limits = evidence['get_time_limits']
            days = min(activity['days'], limits['days']) if repairing else activity['days']
            minutes = min(activity['minutes'], limits['minutes'])
            changes = ['将活动安排收回时间预算内。'] if repairing else []
            if previous and context['review'].get('adjustment') == 'reduce_activity':
                days = min(days, previous['activity_days'])
                minutes = min(minutes, max(0, previous['minutes_per_session'] - 5))
                changes.append('脚本演示规则：反馈出现执行困难，单次活动尝试减少 5 分钟，待用户确认。')
            if not days or not minutes:
                days = minutes = 0
            if previous:
                for key, value, label in [('activity_days', days, '每周天数'), ('minutes_per_session', minutes, '单次分钟')]:
                    if previous[key] != value:
                        changes.append(f'{label}由 {previous[key]} 改为 {value}。')
            result = {'sleep_suggestions': context.get('sleep', {}).get('suggestions', []), 'diet_suggestions': context['diet']['suggestions'], 'activity_days': days,
                      'minutes_per_session': minutes,
                      'activity_suggestions': activity['suggestions'] if days else ['本轮暂不安排活动，之后根据反馈更新。'],
                      'rationale': '脚本协调：结合时间预算、旧计划与复盘反馈；缺失记录不当作未执行，不用少吃补偿运动。',
                      'changes': changes or ['首次建立候选计划。' if not previous else '活动安排保持原计划，继续记录后复盘。']}
        else:
            raise ValueError('Unknown agent role')
        result = output_type.model_validate(result).model_dump()
        trace.append(f'{role}: [demo] submit_result')
        return result
