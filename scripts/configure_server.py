"""以交互方式创建仅保存在本地的服务端配置。"""
from pathlib import Path
import argparse
import getpass
import ipaddress
import json

ROOT=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser()
parser.add_argument('--host',required=True,help='设备可访问的电脑局域网 IP')
args=parser.parse_args()
ip=ipaddress.ip_address(args.host)
host=f'[{ip}]' if ip.version==6 else str(ip)
target=ROOT/'server/data/.config.yaml'
if target.exists():raise SystemExit('配置已存在，请手动编辑；不会覆盖。')
key=getpass.getpass('Step API Key（输入时不显示）: ').strip()
if not key:raise SystemExit('Key 不能为空')
text=(ROOT/'server-config.example.yaml').read_text()
text=text.replace('api_key: 填你的StepAPIKey','api_key: '+json.dumps(key))
text=text.replace('ws://YOUR_LAN_IP:8000/xiaozhi/v1/',f'ws://{host}:8000/xiaozhi/v1/')
target.parent.mkdir(parents=True,exist_ok=True)
target.write_text(text);target.chmod(0o600)
print('已生成 server/data/.config.yaml（Git 已忽略该路径）。')
