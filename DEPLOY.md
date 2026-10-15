# 部署到 Render（免费档）

目标：产出一个评委能打开、能登录、能对话的公网链接。

**你只需要做四件事**：建私有仓库 → 连 Render → 填 4 个环境变量 → 配一个保活。
其余（认证、Dockerfile、健康检查）代码里都做好了。

---

## 零、本地先验一遍

```powershell
# 模拟线上：对外监听 + 强制登录
$env:POLICY_AGENT_HOST = "0.0.0.0"
$env:POLICY_AGENT_USER = "judge"
$env:POLICY_AGENT_PASS = "你的密码"
python run.py
```

浏览器打开 `http://127.0.0.1:8848/`，应当**先看到登录页**，登录后才是对话界面。
换个浏览器窗口（不带 Cookie）直接访问 `/api/meta`，应当返回 401 的 JSON。

确认没问题再往下走。

---

## 一、推到 GitHub（**私有**仓库）

竞赛期间代码公开有被抄的风险，用私有仓库——Render 支持连私有仓库。

```powershell
cd "E:\桌面\C语言\policy-agent"
git init
git add .
git status          # ← 务必看一眼：不该出现 .env、data/cache/
git commit -m "民安 · 社区民生政策服务智能体"
```

然后在 GitHub 上新建一个 **Private** 仓库，按页面提示推送：

```powershell
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

> **推送前自查密钥**：`data/cache/` 已在 `.gitignore` 里，但请再确认
> `git status` 的输出里没有任何 `.env` 或含 `sk-` 的文件。
> 一旦推送成功，密钥即使删掉也留在 git 历史里了。

---

## 二、在 Render 建服务

1. 注册 [render.com](https://render.com)，用 GitHub 账号登录最省事
2. 右上角 **New +** → **Blueprint**
3. 选中刚才那个仓库 —— Render 会自动读到 `render.yaml`，把服务配置好
4. 它会提示你填四个环境变量（因为 `render.yaml` 里标了 `sync: false`）：

| 变量 | 填什么 |
|---|---|
| `DEEPSEEK_API_KEY` | 你的 DeepSeek Key，`sk-` 开头 |
| `POLICY_AGENT_USER` | 给评委的账号，建议短一点好输入，如 `judge` |
| `POLICY_AGENT_PASS` | 给评委的密码，8–12 位、大小写+数字 |
| `POLICY_AGENT_SECRET` | 不用管，Render 会自动生成 |

> ⚠️ **`DEEPSEEK_API_KEY` 一定要填**。代码在找不到它时会回退去读
> `ANTHROPIC_AUTH_TOKEN`——那可能正是你 Claude Code 在用的凭证，
> 等于把它暴露在一个公开链接后面。

5. 点 **Apply**，等构建完成（首次约 3–5 分钟）

构建日志里应当看到：

```
  36 份政策 / 1295 条条款 / 地域 ...
  索引就绪：... 条条款
  监听：0.0.0.0:<端口>
  登录账号：judge
```

**要是看到"36 份政策"里数字是 0**，说明 `.dockerignore` 把 `data/` 排掉了，
政策库没进镜像。文件里已经写了注释警告这一点，别改坏。

---

## 三、配保活（关键，别跳过）

Render 免费档**闲置 15 分钟就休眠**，下次访问要等 30–60 秒冷启动 ——
评委点开看到一个转圈的页面，第一印象就没了。

1. 注册 [uptimerobot.com](https://uptimerobot.com)（免费档够用）
2. **Add New Monitor**
   - Monitor Type：`HTTP(s)`
   - URL：`https://<你的服务名>.onrender.com/healthz`
   - Monitoring Interval：**5 分钟**
3. 保存

这样服务就不会休眠，评委点开就是秒开。

> 额度提醒：免费档 750 实例小时/月，而一个月约 730 小时。
> 挂一个 24×7 的服务基本用满、**没有余量**，别再开第二个免费服务。

---

## 四、验收清单

部署完成后逐条走一遍，全过才算完：

- [ ] 手机**关掉 WiFi 用 4G** 打开链接 —— 确认不是只有你家网络能访问
- [ ] 打开链接先看到**登录页**（不是直接进对话界面）
- [ ] 故意输错密码 → 回到登录页并有错误提示
- [ ] 正确账号密码 → 进入对话界面
- [ ] 问一句"我家老人 82 岁了，能领什么补贴？" → 有流式回答、有工具调用芯片
- [ ] 回答中点一下浏览器刷新，再发一条消息 → **不报错、能正常回答**
      （这条验证会话收尾修复，最容易出问题的地方）
- [ ] 侧边栏能看到政策库统计和入库政策列表
- [ ] 直接访问 `/healthz` → 显示 `{"ok": true, ...}`
- [ ] 访问 `/logout` → 回到登录页，且此时再打开链接仍需重新登录

---

## 五、故障排查

| 现象 | 原因 |
|---|---|
| 页面能开，一发消息就"请求失败" | `DEEPSEEK_API_KEY` 没填或填错。看 Render 日志有没有 `服务端未就绪` |
| 打开是空白 / 一直转圈 | 冷启动中，等 30–60 秒。若长期如此说明保活没配对 |
| 登录页正常，但登录后立刻弹回登录页 | 检查是不是把服务跑在 HTTP 上了。HTTPS 下才有这个问题的话，看 `_is_https()` |
| 侧边栏"政策库"是空的 | 镜像里没带上 `data/policies/`，见第二节末尾 |
| 提示"今日演示额度已用完" | 撞到了每日 1000 轮的防刷上限。改 `POLICY_AGENT_RATE_PER_DAY` 即可 |
| Render 反复重启 | 健康检查路径没指到 `/healthz` |

---

## 六、以后想换到学生机

Dockerfile 是平台通用的，换机器**不用改任何代码**：

```bash
docker build -t minan .
docker run -d --restart=always -p 80:8848 \
  -e DEEPSEEK_API_KEY=sk-xxx \
  -e POLICY_AGENT_USER=judge \
  -e POLICY_AGENT_PASS=你的密码 \
  -e POLICY_AGENT_SECRET=$(openssl rand -hex 32) \
  -e POLICY_AGENT_HOST=0.0.0.0 \
  -e POLICY_AGENT_PORT=8848 \
  --name minan minan
```

学生机（阿里云/腾讯云香港节点约 ¥10–30/月）的好处：不休眠、无冷启动、
不用跟 750 小时额度赛跑，国内访问也更快。香港节点免备案。

---

## 交付物

竞赛要求提交的三项，从 Render 控制台抄下来即可：

- **智能体链接**：`https://<你的服务名>.onrender.com`
- **账号**：你填的 `POLICY_AGENT_USER`
- **密码**：你填的 `POLICY_AGENT_PASS`
