import re

from nonebot import get_bots, get_driver, get_plugin_config, on_command, on_message
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.exception import IgnoredException
from nonebot.message import event_preprocessor
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from feedback_hub.config import Settings
from feedback_hub.runtime import Runtime
from feedback_hub.web import create_router

__plugin_meta__ = PluginMetadata(
    name="游戏反馈管理",
    description="AI 分类、FAQ、黑名单、问题优先级与 WebUI",
    usage="/反馈 设备：安卓 浏览器：Edge 内容：进入战斗黑屏；/反馈管理 帮助",
    type="application",
    config=Settings,
    supported_adapters={"~onebot.v11"},
)

driver = get_driver()
settings = get_plugin_config(Settings)


async def send(payload: dict):
    bots = get_bots()
    bot_id = payload.get("bot", "")
    if bot_id:
        bot = bots.get(bot_id)
        if not isinstance(bot, Bot):
            raise RuntimeError("反馈所属机器人不在线")
    else:
        bot = next((b for b in bots.values() if isinstance(b, Bot)), None)
        if bot is None:
            raise RuntimeError("OneBot 机器人不在线")
    message = Message()
    if payload.get("qq"):
        message += MessageSegment.at(payload["qq"])
        message += MessageSegment.text(" ")
    # Never parse model/user text as CQ codes.
    message += MessageSegment.text(payload["text"])
    await bot.send_group_msg(group_id=int(payload["group"]), message=message)


runtime = Runtime(settings, {str(v).rsplit(":", 1)[-1] for v in driver.config.superusers}, send)
if not hasattr(driver, "server_app"):
    raise RuntimeError("反馈插件需要 FastAPI 驱动：DRIVER=~fastapi")
driver.server_app.include_router(create_router(runtime))
driver.on_startup(runtime.start)
driver.on_shutdown(runtime.stop)


@event_preprocessor
async def enforce_blacklist(event: MessageEvent):
    if runtime.db.blocked(str(event.user_id)):
        raise IgnoredException("反馈系统黑名单：阻止该 QQ 与机器人交互")


async def allowed(event: GroupMessageEvent) -> bool:
    return str(event.group_id) in settings.feedback_groups


feedback = on_command("反馈", rule=allowed, priority=10, block=True)
listener = on_message(rule=allowed, priority=90, block=False)
admin = on_command("反馈管理", permission=SUPERUSER, priority=1, block=True)


async def submit(bot: Bot, event: GroupMessageEvent, explicit: bool):
    text = event.get_plaintext()
    result = runtime.service.ingest(
        qq=str(event.user_id),
        group=str(event.group_id),
        bot=bot.self_id,
        message_id=str(event.message_id),
        text=text,
        segments=[{"type": seg.type, "data": seg.data} for seg in event.message],
        explicit=explicit,
    )
    if result not in {"queued", "ignored", "duplicate"}:
        await bot.send(event, MessageSegment.text(result), at_sender=True)


@feedback.handle()
async def handle_feedback(bot: Bot, event: GroupMessageEvent):
    await submit(bot, event, True)


@listener.handle()
async def handle_message(bot: Bot, event: GroupMessageEvent):
    if str(event.user_id) == bot.self_id:
        return
    # Commands belong to their respective plugins, not the passive AI listener.
    prefixes = tuple(p for p in driver.config.command_start if p)
    if prefixes and event.get_plaintext().startswith(prefixes):
        return
    await submit(bot, event, False)


HELP = """仅机器人 SUPERUSERS 可执行：
/反馈管理 拉黑 QQ 原因
/反馈管理 解封 QQ
/反馈管理 删除 QQ all 或 反馈ID,反馈ID
/反馈管理 标注 问题ID 评论内容
/反馈管理 重开 问题ID
/反馈管理 确认修复 建议ID
/反馈管理 列表
完整原文、FAQ、修复依据及失败重试请使用 /feedback/ WebUI。"""


@admin.handle()
async def handle_admin(event: MessageEvent, args: Message = CommandArg()):  # noqa: B008
    parts = args.extract_plain_text().strip().split(maxsplit=2)
    actor = str(event.user_id)
    response = HELP
    try:
        if parts and parts[0] in {"拉黑", "解封", "删除"}:
            if len(parts) < 2 or not re.fullmatch(r"[0-9]{5,20}", parts[1]):
                raise ValueError("请输入有效 QQ 号")
            qq = parts[1]
            if parts[0] == "拉黑" and len(parts) == 3:
                with runtime.db.transaction():
                    runtime.service.ban(qq, parts[2], actor)
                response = "已拉黑。仅机器人管理员可手动解封。"
            elif parts[0] == "解封":
                runtime.service.unban(qq, actor)
                response = "已解封。"
            elif parts[0] == "删除" and len(parts) == 3:
                ids = None if parts[2] == "all" else [int(i) for i in parts[2].split(",")]
                count = runtime.service.delete_reports(qq, ids, actor)
                response = f"已删除 {count} 条反馈。"
        elif parts and parts[0] == "标注" and len(parts) == 3:
            runtime.service.comment(int(parts[1]), parts[2], "deferred", actor)
            response = "已标注；后续同类反馈会自动回复此评论。"
        elif parts and parts[0] == "重开" and len(parts) >= 2:
            runtime.service.reopen(int(parts[1]), actor)
            response = "问题已重新打开。"
        elif parts and parts[0] == "确认修复" and len(parts) >= 2:
            with runtime.db.transaction():
                runtime.service.resolve(int(parts[1]), actor)
            response = "已确认修复，通知已加入发送队列。"
        elif parts and parts[0] == "列表":
            rows = runtime.db.issues("", 10)
            response = "\n".join(
                f"#{r['id']} [{r['status']}] {r['title']} · {r['reporters']} 人" for r in rows
            )
            response = response or "暂无反馈。"
    except ValueError as exc:
        response = str(exc)
    await admin.finish(MessageSegment.text(response))
