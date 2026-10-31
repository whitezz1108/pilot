# 云端部署：公网 IP、HTTPS 与账号登录

## 部署结果应是什么样

浏览器访问 `https://服务器公网IP`，输入实验台账号密码后进入现有实验台；`/records` 查看、上传和导出实验记录。模型 API Key 只保存在服务器的 `config/api.local.env`，网页不展示 Key。

2026-10-18 更新：默认入口为 experiment_v3，支持手动选择A/V/E/D以及旧40、新40、全部80份材料；`/materials` 在线预览合同、memo与引用依据。详见 `docs/factorial_console_v3.md`。

2026-10-19 更新：历史记录使用可重建SQLite摘要索引、服务端分页、进度增量更新和分轮ZIP导出。ZIP上传上限512MiB，网关与后端共同限制；升级脚本保留原TLS与认证设置。升级前等待实验结束，升级后重启服务；旧原始记录不重写。首次索引可提前运行`scripts/rebuild_records_index.py`，详见[存储与迁移说明](records_storage_v2.md)。

使用免费公网 IP 证书，不必购买域名。Caddy 2.11.7 使用 Let's Encrypt 的 `shortlived` 配置申请并自动续期证书。80/443 端口须从公网可达，公网 IP 须保持不变，服务器须能连接证书服务。证书申请成功前不能视为公网部署完成；不使用明文 HTTP 传输账号密码。

公网 IP 经 NAT 映射到内网 IP 时，客户端访问 IP 可能不发送 SNI；网关通过 `default_sni 公网IP` 选择已经签发的 IP 证书，不能省略此设置。

## 部署结构

```text
浏览器 → HTTPS 公网IP:443 → Caddy 账号验证 → 127.0.0.1:8766 实验台
                                             ↓
                               原有 Python Manager → Compliance
                                             ↓
                               原有 API、合同搜索、核验和日志
```

仅增加网关和系统服务，实验 Python 源码、prompt、材料、策略和预算按字节复制，不改研究规则。网关验证浏览器 Origin，再将 Host/Origin 改写为实验台既有本机地址；既有操作 token 检查继续生效。未登录不能读取页面、API、历史结果或上传记录。

这是一个共享实验空间：两人看到同一批次和历史记录。现有调度器同时仅运行一个批次；不同用户不能同时创建互相独立的批次。建议先用 16 路并发测完成率。

## 本地准备

```powershell
.venv/Scripts/python.exe scripts/package_cloud.py --output outputs/cloud_deployment_20261018/pilot01.tar.gz
```

部署包含源码、配置、prompt、clean80 冻结材料与实验台历史记录，不含虚拟环境、本地 API 凭证、SSH 私钥。脚本核查冻结材料并拒绝在本地批次运行中复制；凭证通过另一条 SSH 通道传输。

准备 Linux x86_64 Python 3.12 运行时、Caddy 2.11.7 及固定版本 wheels。下载后核对官方发布 SHA256，将归档和 `verified.json` 放到服务器的 `~/pilot01-runtime/`。将项目解压到 `~/pilot01/`。保留原始文件字节和部署包 manifest。

在服务器上准备 `~/pilot01/config/api.local.env`（600 权限）和 `~/.pilot01-access.json`（600 权限，用户名到强随机密码的 JSON）。账号可使用 `owner`、`researcher`，均可操作共享实验；API 文件不能放在可公开下载的位置。

```bash
cd ~/pilot01
bash deploy/cloud/install.sh 服务器公网IP
```

安装脚本检查原始材料和模型配置，只做离线检查，不启动付费请求。读取历史记录不会调用模型。系统服务随系统启动，退出 SSH 或关闭网页不影响后台运行。

## 云平台需要开放什么

- TCP 22：SSH 管理，可按实际管理员来源限制。
- TCP 80：证书验证和 HTTPS 跳转。
- TCP 443：网页 HTTPS。
- 不向公网开放 8766、2019。

云防火墙和系统防火墙是两层；端口监听不代表云平台放行。内地公网网站的备案及平台接入要求按云平台规定核实，不把改用 IP 或其他端口当作绕过规则的方法。

## 验收

1. 公网 HTTPS 证书有效；HTTP 跳转 HTTPS。
2. 无账号或错误密码访问页面与所有 API 返回 401；不能取得 token 或结果。
3. 正确账号可打开实验台、历史记录，能生成清单，未点击开始不产生模型调用。
4. 正确 token 的跨站 POST 返回 403；缺失或错误 token 同样返回 403。
5. 80份材料、新旧范围选择、16次工具预算和32768输出上限核对一致；部署机器与本地同一版本的profile指纹相同。升级到新版时指纹变化，旧结果按历史版本保留。
6. 第一次小规模真实实验由用户明确启动；未进行真实调用前不声称 API 额度或端到端实验完成率已验证。

## 常用维护

```bash
sudo systemctl status pilot01-console pilot01-caddy --no-pager
sudo journalctl -u pilot01-console -n 50 --no-pager
sudo journalctl -u pilot01-caddy -n 50 --no-pager
```

历史记录保存在 `~/pilot01/outputs/experiment_console/`。服务器重启后，未完成的旧批次显示中断，由用户手动决定继续，不自动补发可能已收费的模型请求。升级源码或配置必须等批次结束，备份后创建新批次，不混入历史实验。

## 备份与结束试用

等待当前批次结束后运行：

```bash
~/pilot01/.venv/bin/python ~/pilot01/deploy/cloud/backup.py --destination ~/pilot01-backups
```

备份含原始日志、输入材料、源码、prompt 和非秘密配置，排除 API Key。再用 SCP 下载整个备份目录到自己的电脑；服务器上的副本不构成异地备份。API Key 单独保管。释放服务器会删除磁盘上的数据，先核对备份可读取再释放。

## 官方参考

- [Let's Encrypt：公网 IP 和六天证书](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)
- [Caddy ACME profile](https://caddyserver.com/docs/caddyfile/directives/tls)
- [Caddy 账号认证](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
