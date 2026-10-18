"""
攀搭 · 训练记忆

大模型本身没有记忆：每次对话前，从攀搭数据库（data/panda.db）整理一份训练档案，
填进系统提示词的 <memory> 里。档案里的数字全部由规则算出，不靠大模型总结。
训练数据由按键 / 登记线路实时写入数据库，所以这里不需要额外保存对话（save_memory 什么都不做）。
"""
from ..base import MemoryProviderBase, logger

TAG = __name__

GUIDE = (
    "下面是这位攀岩者的训练档案（系统实时整理，数字准确）。"
    "用户问进步、对比、某条线以前怎么样、今天练得如何时，用这里的数据回答，可以做前后对比"
    "（比如“上次在这条线试了4次没拿下”）；档案里没有的不要编。"
    "说次数、日期时严格照档案说，比如档案写“第2次完攀”就不能说成“一次就过”。"
    "不要主动念档案，只在相关时自然地提一句。\n"
)


class MemoryProvider(MemoryProviderBase):
    def __init__(self, config, summary_memory=None):
        super().__init__(config)

    async def save_memory(self, msgs, session_id=None):
        return None

    # 记忆模块是所有连接共用的一个实例，role_id 会被后连上的设备覆盖，
    # 所以 connection.chat 直接调用 archive_for(当前设备号)，不走 query_memory
    panda_archive = True

    def archive_for(self, device_id) -> str:
        if not device_id:
            return ""
        try:
            from panda import store
            text = store.archive(device_id)
        except Exception as e:
            logger.bind(tag=TAG).warning(f"训练档案读取失败：{e}")
            return ""
        return GUIDE + text if text else ""

    async def query_memory(self, query: str) -> str:
        return self.archive_for(self.role_id)
