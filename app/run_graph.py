"""Run the real LangGraph with scripted fixtures or an explicit paid live mode."""
import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.multi_agent.runners import DeepSeekRunner, DemoRunner
from app.schemas import DailyRecordInput, UserProfileInput
from app.multi_agent.graph import build_graph


def fixture(scenario):
    today = date.today()
    profile = UserProfileInput(
        age=25, height_cm=170, baseline_weight_kg=70, baseline_date=today.isoformat(),
        goal="build_habits", available_activity_days=3, minutes_per_session=20,
        food_preferences=["家常餐"],
        needs_professional_support=None if scenario == "clarify" else scenario == "refer",
    )
    records = [DailyRecordInput(record_date=(today - timedelta(days=1)).isoformat(), weight_kg=70.1),
               DailyRecordInput(record_date=today.isoformat(), activity_minutes=0)]
    previous = None
    if scenario == 'adjustment':
        records[-1].feedback = '最近活动太累，希望降低安排。'
        previous = {'diet_suggestions': ['规律家常餐'], 'activity_days': 3, 'minutes_per_session': 20,
                    'activity_suggestions': ['舒适范围内步行'], 'rationale': '虚构旧计划', 'changes': []}
    return {'previous_plan': previous, 'as_of': today.isoformat(), "profile": profile.model_dump(mode="json"),
            "records": [r.model_dump(mode="json") for r in records],
            "events": [], "coordination_round": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--scenario", choices=["plan", "adjustment", "clarify", "refer"], default="plan")
    parser.add_argument("--decision", choices=["ask", "approve", "reject"], default="ask")
    parser.add_argument('--message', default='', help='本次自然语言需求')
    parser.add_argument('--city', default='', help='本次城市，不修改长期档案')
    parser.add_argument('--allow-external', action='store_true', help='Live 模式允许联网查询')
    args = parser.parse_args()
    try:
        runner = DemoRunner() if args.mode == "demo" else DeepSeekRunner()
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    graph = build_graph(runner, InMemorySaver())
    run_id = uuid4().hex[:12]
    config = {"configurable": {"thread_id": run_id}, "recursion_limit": 40, "max_concurrency": 3}
    print(f"MODE={args.mode}; synthetic profile; thread_id={run_id}")
    print("demo 使用脚本角色；live 才调用真实模型并产生 API 费用。")
    try:
        initial = fixture(args.scenario)
        initial['mode'] = args.mode
        initial.update(user_message=args.message, task_city=args.city, use_external=args.allow_external,
                       dialogue=[{'role': 'user', 'content': args.message}] if args.message else [], clarification_round=0)
        result = graph.invoke(initial, config)
    except Exception as exc:
        print(f"运行未完成：{type(exc).__name__}；未生成已确认计划。")
        raise SystemExit(1) from None
    for event in result.get("events", []):
        print(event)
    while result.get("__interrupt__"):
        payload = result['__interrupt__'][0].value
        if payload.get('kind') == 'clarification':
            print('需要补充：', payload['question'])
            if args.decision != 'ask':
                result = dict(graph.get_state(config).values)
                result['status'] = 'waiting_clarification'
                print('非交互 CLI 不代替用户回答；请去掉 --decision 后重新运行，；Web 中新建的任务支持持久化恢复。')
                break
            reply = input('你的补充：').strip()
            if not reply:
                result = dict(graph.get_state(config).values)
                result['status'] = 'waiting_clarification'
                break
            result = graph.invoke(Command(resume={'message': reply, 'city': args.city}), config)
            continue
        print("\nINTERRUPT：图已暂停，候选计划：")
        print(json.dumps(result["proposal"], ensure_ascii=False, indent=2))
        decision = args.decision
        if decision == "ask":
            decision = "approve" if input("确认输入 y，其他输入拒绝：").strip().lower() == "y" else "reject"
        result = graph.invoke(Command(resume={"approved": decision == "approve"}), config)
    directory = Path(__file__).resolve().parents[1] / "outputs"
    directory.mkdir(exist_ok=True)
    output = directory / f"{run_id}.json"
    output.write_text(json.dumps({"mode": args.mode, "synthetic_data": True, "state": result},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
    print("FINAL STATUS:", result.get("status"))
    print("Output:", output)
    print("检查点保存在内存中：本次进程内可恢复，进程重启恢复尚未实现。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
