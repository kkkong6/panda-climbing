"""检查 Git 候选文件，不输出疑似密钥值。"""
from pathlib import Path
import subprocess
import re
import ast
from html.parser import HTMLParser

ROOT=Path(__file__).resolve().parent.parent
names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
files=[ROOT/n for n in set(names) if n and (ROOT/n).is_file()]
relative_files={p.relative_to(ROOT).as_posix() for p in files}
tracked_ignored=subprocess.check_output(['git','ls-files','--cached','--ignored','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
assert not any(tracked_ignored),'有已跟踪文件违反忽略规则，请先检查暂存区'
patterns=[re.compile(r'\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'),re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]
issues=[]
for p in files:
    if p.stat().st_size>=100*1024*1024:issues.append((str(p.relative_to(ROOT)),'文件超过100MB'))
    if p.suffix in {'.py','.yaml','.yml','.json','.sh','.md','.txt','.toml','.h','.cc','.cpp','.html'}:
        text=p.read_text(errors='replace')
        for pattern in patterns:
            for match in pattern.finditer(text):issues.append((str(p.relative_to(ROOT)),f'疑似密钥 行{text.count(chr(10),0,match.start())+1}'))
    if p.suffix=='.py' and p.relative_to(ROOT).parts[0] in {'scripts','tests','server'}:
        ast.parse(p.read_text(),filename=str(p))
for item in issues:print(*item)
assert not issues,'公开文件检查未通过'
for filename in ['README.md','06_参赛产品方案.md']:
    text=(ROOT/filename).read_text()
    for link in re.findall(r'\]\(([^)]+)\)',text):
        if link.startswith(('http:','https:','#')):continue
        assert link in relative_files,f'{filename} 引用了未纳入上传的文件：{link}'
for forbidden in ['服务端/','设备固件备份/','攀搭报告_上传这个文件夹/','.github-preparation/','.site/']:
    assert not any(n.startswith(forbidden) for n in relative_files),f'发现不应上传的目录：{forbidden}'
assert all(n=='app/攀搭App.html' for n in relative_files if n.startswith('app/')),'App 目录仍包含历史文件或个人工具'
assert not any('备份' in n for n in relative_files),'发现备份文件'
class Links(HTMLParser):
    def handle_starttag(self,tag,attrs):
        for k,v in attrs:
            if k in {'href','src'} and v and not v.startswith(('https:','http:','data:','#')):
                assert (ROOT/'.site'/v).is_file(),f'站点链接不存在：{v}'
Links().feed((ROOT/'.site/index.html').read_text())
print(f'候选 {len(files)} 个文件，{sum(p.stat().st_size for p in files)/1e6:.1f} MB；无超限文件和已知密钥格式；Python语法及首页链接检查通过。')
from collections import Counter
groups=Counter(p.relative_to(ROOT).parts[0] for p in files)
print('候选目录分布：',dict(sorted(groups.items())))
