"""查看某份政策的结构化结果。用法：python tests/inspect_policy.py 关键词"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

kw = sys.argv[1] if len(sys.argv) > 1 else ""

for f in sorted(config.STRUCT_DIR.glob("*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    title = d.get("title", "")
    if kw and kw not in title:
        continue
    print("=" * 78)
    print(f"{title}")
    print(f"  policy_id : {d.get('policy_id')}")
    print(f"  类别/地域  : {d.get('category')} / {d.get('region')}")
    print(f"  适用对象   : {d.get('target_group')}")
    print(f"  待遇       : {json.dumps(d.get('benefit'), ensure_ascii=False)[:300]}")
    print(f"  材料       : {d.get('materials')}")
    print(f"  流程       : {d.get('process')}")
    print(f"  受理/时限  : {d.get('handler')} / {d.get('duration')}")
    print(f"  表格       : {d.get('form_name')} {d.get('form_fields')}")
    print(f"  资格规则   : {json.dumps(d.get('eligibility_rules'), ensure_ascii=False)[:400]}")
