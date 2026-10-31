"""确定性的身份/问候答复，无需为固定产品事实启动两轮模型工具调用。"""
import re


def local_reply(message):
    text=re.sub(r'[\s，,。！？!?、~～]+','',message).lower()
    if text in {'你是谁','你叫什么','你叫什么名字','介绍一下你自己','你好你是谁','请问你是谁','你是哪个助手'}:
        return '我是Simon，你的健康生活助手。可以结合你确认的档案、位置、天气和日程，协助安排运动、饮食和睡眠；计划调整会遵循你设置的授权。'
    if text in {'你好','您好','嗨','hi','hello','你好simon','simon你好'}:
        return '你好，我是Simon。今天想聊聊，还是一起调整运动、饮食或作息？'
    return None
