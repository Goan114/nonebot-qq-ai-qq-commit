import os
import subprocess
import sys


def test_plugins_load_real_nonebot_and_blacklist(tmp_path):
    code = r"""
import asyncio
import nonebot
from nonebot.adapters.onebot.v11 import Adapter, Message, MessageSegment, GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import IgnoredException
from nonebot.permission import SUPERUSER
nonebot.init(driver="~fastapi", feedback_database="test.sqlite", superusers={"10000"}, feedback_groups={"88888"})
nonebot.get_driver().register_adapter(Adapter)
assert nonebot.load_plugin("nonebot_plugin_commit_monitor")
import nonebot_plugin_feedback_manager as plugin
assert plugin.runtime.monitor_enabled
assert [{"type":s.type,"data":s.data} for s in Message("hello")] == [{"type":"text", "data":{"text":"hello"}}]
bot = plugin.Bot(nonebot.get_adapter(Adapter), "11111")
def event(qq, role="member"):
    return GroupMessageEvent(time=1,self_id=11111,post_type="message",sub_type="normal",user_id=qq,
        message_type="group",message_id=1,message=Message("/反馈 苹果 Safari 黑屏"),raw_message="text",
        font=0,group_id=88888,sender={"user_id":qq,"role":role})
async def run():
    assert await SUPERUSER(bot,event(10000))
    assert not await SUPERUSER(bot,event(12345,"owner"))
    await plugin.submit(bot,event(12345),True)
    assert plugin.runtime.db.one("SELECT COUNT(*) n FROM jobs")["n"] == 1
    plugin.runtime.service.ban("12345","test","10000")
    for e in [event(12345),PrivateMessageEvent(time=1,self_id=11111,post_type="message",sub_type="friend",
            user_id=12345,message_type="private",message_id=2,message=Message("hello"),raw_message="hello",
            font=0,sender={"user_id":12345})]:
        try:
            await plugin.enforce_blacklist(e)
        except IgnoredException:
            pass
        else:
            raise AssertionError("blacklist not enforced")
    msg = MessageSegment.text("[CQ:at,qq=all]")
    assert msg.type == "text"
    await plugin.runtime.stop()
asyncio.run(run())
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
