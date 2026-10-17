"""只构建公开展示所需文件，GitHub Pages 不发布服务端和配置。"""
from pathlib import Path
import shutil

ROOT=Path(__file__).resolve().parent.parent
OUT=ROOT/'.site'
OUT.mkdir(exist_ok=True)
files={'index.html':'index.html','app/攀搭App.html':'app.html','06_攀搭产品方案_PDF精排版.html':'report.html','06_攀搭产品方案_PDF精排版.pdf':'report.pdf','产品展示.png':'product.png'}
for src,dst in files.items():shutil.copyfile(ROOT/src,OUT/dst)
(OUT/'.nojekyll').touch()
report=(OUT/'report.html').read_text()
report=report.replace('<button onclick="window.print()">打印 / 保存为 PDF</button>','<a href="report.pdf" download>下载 PDF</a><button onclick="window.print()">打印</button>')
(OUT/'report.html').write_text(report)
assert (OUT/'report.pdf').read_bytes().startswith(b'%PDF-')
print('公开站点已生成 .site/：首页、App、报告、PDF、产品图。')
