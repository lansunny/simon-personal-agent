"""可安全返回给界面的业务错误；不包含密钥或服务商原始响应。"""
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo


class BudgetExceeded(RuntimeError):
    def __init__(self,kind,used,limit,day,timezone_name):
        self.kind,self.used,self.limit,self.day=kind,used,limit,day
        self.reset_at=(datetime.fromisoformat(day)+timedelta(days=1)).replace(tzinfo=ZoneInfo(timezone_name)).isoformat()
        label='模型请求' if kind=='model' else '外部查询'
        super().__init__(f'今日{label}已使用{used}/{limit}次，达到本项目设置的每日上限。可在“我的 → 自动调整与联网授权”调整上限，或等待次日恢复；这不表示DeepSeek账户余额不足。')

    def details(self):
        return {'code':'daily_budget_exceeded','kind':self.kind,'used':self.used,'limit':self.limit,
                'remaining':max(0,self.limit-self.used),'day':self.day,'reset_at':self.reset_at}
