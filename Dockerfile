# 民安 · 社区民生政策服务智能体
#
# 零第三方依赖，所以不需要 pip install，镜像里只有一个 Python 和本项目代码。
# 换任何支持 Docker 的平台都直接能用，不锁定某一家。
FROM python:3.13-slim

# PYTHONUNBUFFERED 是刚需：不设的话容器日志里看不到 boot() 的输出，
# 冷启动出问题只能干瞪眼。PYTHONUTF8 保证中文路径与输出不乱码。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    POLICY_AGENT_HOST=0.0.0.0 \
    POLICY_AGENT_PORT=8848

# tzdata 显式装一次。core/store.py 用 date.today() 判政策是否已废止，
# 容器默认 UTC 会比北京时间早 8 小时翻成"已过期"——这个演示里
# 政策时效正是重点，被评委抓到会很尴尬。基础镜像未必带 tzdata，
# 不显式装的话 TZ 会被静默忽略，正好落到那个 bug 上。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY config.py run.py ./
COPY web/ web/
COPY core/ core/
COPY ingest/ ingest/
# 政策库是演示的命根子，必须进镜像
COPY data/policies/ data/policies/
COPY data/structured/ data/structured/

EXPOSE 8848

# 用 -m web.server 而不是 run.py：后者会调 webbrowser.open()，
# 且强制要求本地有 API Key 才继续，语义都不对。
CMD ["python", "-m", "web.server"]
