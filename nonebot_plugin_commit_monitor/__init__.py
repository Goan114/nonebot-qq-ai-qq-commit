from nonebot import require
from nonebot.plugin import PluginMetadata

require("nonebot_plugin_feedback_manager")
from nonebot_plugin_feedback_manager import runtime

__plugin_meta__ = PluginMetadata(
    name="GitHub Commit 监视器",
    description="多仓库轮询/Webhook、通俗摘要和反馈修复联动",
    usage="配置 FEEDBACK_REPOS，Webhook 地址 /feedback/github/webhook",
    type="application",
    supported_adapters={"~onebot.v11"},
)
runtime.monitor_enabled = True
