"""抽查 LLM 结构化抽取的地域字段是否准确。会真实调用模型。"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm import LLM
from ingest.structure import structure_policy

# 抽 5 份地域特征明显的政策验证
SAMPLES = [
    "https://www.shanghai.gov.cn/202411bmgfxwj/20240612/5df35664fcef4e1c939c938327b78503.html",
    "https://www.gov.cn/zhengce/zhengceku/2021-07/29/content_5628296.htm",
    "https://scnj.gov.cn/ztzl/jczwgkzl/gkly/shjz/zhyw/zcfgwj/13994304.html",
    "https://www.beijing.gov.cn/zhengce/zhengcefagui/202602/t20260206_4495163.html",
    "https://ahqy.gov.cn/OpennessContent/show/1686180.html",
]

import hashlib

llm = LLM()
print(f"{'政策标题':<44} {'region':<18} {'类别':<8} 规则数")
print("-" * 84)

for url in SAMPLES:
    pid = "p" + hashlib.sha1(url.encode()).hexdigest()[:8]
    f = Path("data/policies") / f"{pid}.json"
    if not f.exists():
        print(f"(缺文件 {pid})")
        continue
    p = json.loads(f.read_text(encoding="utf-8"))
    r = structure_policy(p, llm)
    print(f"{p['title'][:42]:<44} {str(r.get('region'))[:16]:<18} "
          f"{str(r.get('category')):<8} {len(r.get('eligibility_rules') or [])}")
