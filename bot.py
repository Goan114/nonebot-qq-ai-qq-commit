import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_plugin("nonebot_plugin_feedback_manager")
nonebot.load_plugin("nonebot_plugin_commit_monitor")

if __name__ == "__main__":
    nonebot.run()
