"""隔离运行已有存储回归检查：本地临时 DB，不请求网络。"""
from pathlib import Path
import os
import sys
from unittest.mock import patch
from loguru import logger

ROOT=Path(__file__).resolve().parent.parent
SERVER=ROOT/'server'
sys.path.insert(0,str(SERVER))
os.chdir(SERVER)
# 隔离日志初始化，避免测试读取真实配置或创建服务端日志。
with patch('config.logger.setup_logging',return_value=logger):
    for name in ['test_route_store.py','test_attempt_store.py','test_fix_store.py']:
        source=(ROOT/'tests/legacy'/name).read_text()
        source=source.replace('SERVER = os.path.expanduser("~/panda-server/xiaozhi-esp32-server-main/main/xiaozhi-server")',f'SERVER = {str(SERVER)!r}')
        ns={'__name__':'__main__','__file__':str(ROOT/'tests/legacy'/name)}
        exec(compile(source,name,'exec'),ns)
        if ns.get('fails',0):raise SystemExit(f'{name}: {ns["fails"]} 项失败')
print('3 组存储检查通过；数据位于临时目录。')
