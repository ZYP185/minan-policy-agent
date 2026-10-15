"""HTML 解析层测试：先验合成样本，再可选用真实网页验证。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ingest.parse import decode, extract_body, extract_meta, parse_policy

# 模拟某市政府网站的典型结构：导航栏 + 正文 + 侧边栏 + 页脚
FIXTURE = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="ArticleTitle" content="上海市高龄老年人津贴发放管理办法">
<meta name="PubDate" content="2024-03-15 09:30:00">
<title>上海市高龄老年人津贴发放管理办法_上海市民政局</title>
</head>
<body>
<div class="header">
  <a href="/">首页</a> <a href="/zwgk">政务公开</a> <a href="/bmfw">便民服务</a>
  <a href="/hdjl">互动交流</a> <a href="/zjsh">走进上海</a>
</div>
<div class="left-nav">
  <a href="/a">机构职能</a><a href="/b">政策法规</a><a href="/c">规划计划</a>
  <a href="/d">财政信息</a><a href="/e">人事信息</a>
</div>
<div id="UCAP-CONTENT">
  <p>沪民规〔2024〕3号</p>
  <p>各区民政局、财政局：</p>
  <p>为进一步完善本市老年人福利保障体系，经市政府同意，现将《上海市高龄老年人津贴发放管理办法》印发给你们，请遵照执行。</p>
  <p>第一条 为规范本市高龄老年人津贴发放管理工作，根据《中华人民共和国老年人权益保障法》，制定本办法。</p>
  <p>第二条 本办法适用于具有本市户籍且年满80周岁的老年人。</p>
  <p>第三条 高龄老年人津贴标准为：80至89周岁每人每月180元；90至99周岁每人每月350元；100周岁及以上每人每月600元。</p>
  <p>第四条 申请高龄津贴应当提交下列材料：（一）居民身份证原件及复印件；（二）居民户口簿；（三）本人社会保障卡或银行卡。</p>
  <p>第五条 申请人应当向户籍所在地的街道办事处或者乡镇人民政府提出申请，填写《高龄老年人津贴申请审批表》。</p>
  <p>第六条 街道办事处应当自受理之日起15个工作日内完成审核，报区民政局审批。</p>
  <p>第七条 本办法自2024年5月1日起施行，有效期5年。</p>
  <p>上海市民政局</p>
  <p>2024年3月15日</p>
</div>
<div class="right-bar">
  <a href="/x">相关链接一</a><a href="/y">相关链接二</a>
</div>
<div class="footer">
  <p>版权所有：上海市民政局</p>
  <p>ICP备案号：沪ICP备00000000号</p>
  <p>打印 关闭 分享</p>
</div>
</body></html>
"""

NAV_ONLY = """<html><head><title>某市政府门户</title></head><body>
<div class="nav"><a href="/1">首页</a><a href="/2">政务服务</a><a href="/3">政民互动</a></div>
<div class="links"><a href="/4">链接甲</a><a href="/5">链接乙</a><a href="/6">链接丙</a></div>
</body></html>"""


def test_decode():
    raw = "中文测试".encode("gb18030")
    assert decode(raw, "text/html; charset=gb2312") == "中文测试"
    assert decode("中文测试".encode("utf-8"), "text/html; charset=utf-8") == "中文测试"
    # 无声明时按 utf-8 兜底
    assert decode("中文测试".encode("utf-8"), "") == "中文测试"
    print("[OK] decode（含 gb2312→gb18030 回退）")


def test_extract_body():
    body, how = extract_body(FIXTURE)
    assert how == "UCAP", f"容器识别错误：{how}"
    assert "高龄老年人津贴标准" in body
    assert "80至89周岁每人每月180元" in body
    # 导航和页脚必须被清掉
    assert "ICP备" not in body, "页脚噪声未清除"
    assert "相关链接一" not in body, "侧栏未剔除"
    assert "打印" not in body
    lines = [ln for ln in body.split("\n") if ln.strip()]
    print(f"[OK] extract_body（容器={how}，{len(lines)} 行）")
    for ln in lines[:3]:
        print("      |", ln[:52])


def test_meta():
    body, _ = extract_body(FIXTURE)
    meta = extract_meta(FIXTURE, body, "https://mzj.sh.gov.cn/example.html")
    assert meta["title"] == "上海市高龄老年人津贴发放管理办法", meta["title"]
    assert meta["doc_number"] == "沪民规〔2024〕3号", meta["doc_number"]
    assert meta["publish_date"] == "2024-03-15", meta["publish_date"]
    assert meta["effective_date"] == "2024-05-01", meta["effective_date"]
    print("[OK] extract_meta")
    for k in ("title", "doc_number", "publish_date", "effective_date", "issuer", "region"):
        print(f"      {k:16}= {meta.get(k)!r}")


def test_fallback():
    """没有已知容器时，密度兜底不应崩。"""
    body, how = extract_body(NAV_ONLY)
    assert "首页" not in body or how in ("density", "body"), how
    print(f"[OK] 兜底路径未崩溃（how={how}, 长度={len(body)}）")


def test_clause_split():
    from core.store import split_clauses
    body, _ = extract_body(FIXTURE)
    clauses = split_clauses(body)
    nos = [n for n, _ in clauses]
    assert any("第一条" in n for n in nos), nos
    assert len(clauses) >= 6, f"只切出 {len(clauses)} 条：{nos}"
    print(f"[OK] split_clauses 切出 {len(clauses)} 条：{nos}")


if __name__ == "__main__":
    test_decode()
    test_extract_body()
    test_meta()
    test_fallback()
    test_clause_split()
    print("\n解析层全部通过")
