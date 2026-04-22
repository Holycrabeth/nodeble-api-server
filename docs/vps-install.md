# 在 VPS 上安装 NODEBLE API Server

**目标读者**:准备把 api-server 部署到自己 Linux VPS 上的客户。
**时间预估**:10-15 分钟(取决于 VPS 网速)。
**前提**:
- 一台 **Ubuntu 24.04+** 或 **Debian 13+** VPS(其他现代 Linux 也行,只要带 Python 3.12 + systemd)
- SSH 登录权限(普通 user,**不是** root)
- VPS 安全组 / 防火墙能放行 **TCP 8765**

> 这个文档只管 api-server 一个组件 — 它是给 NODEBLE 桌面 app 后端用的。
> 策略模块(IC / Wheel / PMCC …)有各自的 deploy 流程,不在本文档范围。

---

## 步骤 1:连 VPS,确认 Python

SSH 登进 VPS:

```bash
ssh your-user@your-vps.example.com
```

确认 Python 版本 ≥ 3.12:

```bash
python3 --version
```

若低于 3.12(Ubuntu 22.04 默认 3.10),先装新版:

```bash
sudo add-apt-repository ppa:deadsnakes/ppa
sudo apt update
sudo apt install -y python3.12 python3.12-venv
# 临时让 python3 指向 3.12(方便 install.sh 的 prereq 检查):
alias python3=python3.12
```

---

## 步骤 2:clone repo

```bash
cd ~
git clone https://github.com/Holycrabeth/nodeble-api-server.git
cd nodeble-api-server
```

(若用 deploy key 或 SSH:`git clone git@github.com:Holycrabeth/nodeble-api-server.git`)

---

## 步骤 3:运行安装脚本

```bash
./install.sh
```

脚本会在几分钟内:

1. 做前置检查(Python / systemd / 端口 8765 空闲 / git / openssl)
2. 建 Python 虚拟环境,pip install
3. 生成自签 TLS 证书 + API token(label=`desktop`)
4. 装 systemd `--user` service unit
5. 询问是否开 linger(**需要 sudo 一次**,建议开 — 不开的话 SSH 退出 service 就停)
6. 启动 service,health check `/health`

安装成功后,最后会打印一个高亮输出的 **3 件套**:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  ✅ NODEBLE API Server 安装完成
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

把下面 3 条填进 NODEBLE 桌面 app 的首次启动向导:

  服务器地址:
    https://10.0.0.5:8765

  访问令牌:
    a3aae14a-3400-4ca6-9cec-1835167afeaf

  证书指纹:
    68:D0:52:B8:06:DD:00:10:AB:80:9E:8A:DF:99:72:EB:01:FF:FA:68:A2:3A:27:3B:74:BA:07:0F:7E:85:70:7B
```

---

## 步骤 4:防火墙放行 8765

脚本**不会**自动开防火墙(这通常需要 sudo,每家 VPS 方式又不一样)。手动开:

```bash
# Ubuntu ufw(最常见)
sudo ufw allow 8765/tcp
sudo ufw status

# 或者直接 iptables
sudo iptables -I INPUT -p tcp --dport 8765 -j ACCEPT

# AWS / Azure / GCP 等云厂商:去控制台的 Security Group /
# 网络安全组 里加 8765/tcp inbound 规则
```

从另一台机器 ping 试试:

```bash
curl -sk https://<your-vps-ip>:8765/health
# 期望输出 {"status":"ok"}
```

---

## 步骤 5:把 3 件套填进 Mac app

1. 打开 NODEBLE 桌面 app(首次启动会出向导)
2. **服务器地址** → 填 `https://<vps-ip-或-域名>:8765`
   ⚠️ 如果脚本输出的 IP 是内网 IP(如 `10.x.x.x` / `192.168.x.x`),
      请替换成 VPS 的**公网 IP 或域名**
3. **访问令牌** → 粘贴脚本输出的 token
4. **证书指纹** → 向导显示一个指纹,跟脚本打印的逐字核对,一致再点接受

---

## 日常管理

```bash
# 看服务状态
systemctl --user status nodeble-api-server

# 重启
systemctl --user restart nodeble-api-server

# 实时日志
journalctl --user -u nodeble-api-server -f

# 最近 50 行日志
journalctl --user -u nodeble-api-server -n 50 --no-pager

# 升级(拉新版本)
cd ~/nodeble-api-server
git pull
.venv/bin/pip install -e .
systemctl --user restart nodeble-api-server
```

---

## 常见问题

**Q:SSH 退出后服务断了?**
A:linger 没开。重跑 `sudo loginctl enable-linger $USER`,或重新跑 `./install.sh`
   在 linger 那步选 `y`。

**Q:Mac app 报 "certificate fingerprint mismatch"?**
A:api-server 的证书换过(脚本里 cert 已存在就**不会**重新生成,所以这通常
   意味着有人手动删了 `~/.nodeble-api/certs/` 然后重跑了脚本)。
   让 Mac app 清掉旧的 pinned fingerprint,重新走首次启动向导,
   用**当前**指纹(服务器上跑 `cat ~/.nodeble-api/certs/fingerprint.txt`)。

**Q:端口 8765 被占了?**
A:`ss -tlnp | grep :8765` 看谁在用。如果是旧的 api-server 进程没正常退出,
   `systemctl --user stop nodeble-api-server` 然后 `pkill -f nodeble_api_server`
   再重跑 install.sh。

**Q:想换 token / 加一个 Mac 的 token?**
A:
```bash
cd ~/nodeble-api-server
.venv/bin/python -m nodeble_api_server generate-token my-second-mac   # 加一个
.venv/bin/python -m nodeble_api_server revoke-token desktop           # 撤销旧的
systemctl --user restart nodeble-api-server                           # 重启生效
```

**Q:想完全卸载?**
A:
```bash
systemctl --user stop nodeble-api-server
systemctl --user disable nodeble-api-server
rm ~/.config/systemd/user/nodeble-api-server.service
rm -rf ~/.nodeble-api          # ⚠️ 删数据目录(audit / 快照都没了)
rm -rf ~/nodeble-api-server    # 删代码
sudo loginctl disable-linger $USER   # 可选
```

---

## 反模式提醒

- ❌ **不要** `sudo ./install.sh` — 脚本显式拒绝 root 运行。sudo 会把
  `~/.nodeble-api/` 变成 root 所有,之后非 root 的 Python 进程读不了
- ❌ **不要** 禁用系统 Gatekeeper / 防火墙来"方便调试" — 8765 只放给可信
  网段(客户自己的 Mac / 办公网),公网暴露的唯一防线就是 token + 证书指纹
- ❌ **不要** 把 `api.yaml` 里的 token 发到聊天软件 / 邮件里 — 这就是登录凭据

---

## 安装脚本本身做了什么?

透明起见,`install.sh` 的每一步:

| 步骤 | 做什么 | 是否需要 sudo |
|---|---|---|
| 前置检查 | 查 Python / systemd / git / curl / openssl / 8765 端口 / 已装状态 | 否 |
| venv | `python3 -m venv .venv` + `pip install -e .` | 否 |
| 数据目录 | `mkdir -p ~/.nodeble-api/{config,certs,audit,history}` + chmod 700 | 否 |
| 证书 | 调 CLI,自签 cert + 写 fingerprint.txt + 更新 api.yaml | 否 |
| Token | 调 CLI 生成 UUID4,写 api.yaml(label=desktop,已存在就保留) | 否 |
| systemd unit | 写 `~/.config/systemd/user/nodeble-api-server.service` | 否 |
| linger | `loginctl enable-linger $USER`(仅此一步) | **需要**(会 prompt) |
| 启动 + 健康检查 | systemctl --user enable + start + 循环 curl /health | 否 |
| 输出 3 件套 | 读 api.yaml 和 fingerprint.txt,打印 | 否 |
