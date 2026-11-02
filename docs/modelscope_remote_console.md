# ModelScope Studio 远程实验操作台方案

## 结论

可以把操作台部署到ModelScope Studio，并从云端向T640下发实验命令、读取运行记录和GPU状态；它不局限于服务器本地网页。但Studio容器不能直接访问T640的`127.0.0.1`，必须有一条经过认证的HTTPS链路，或者让T640通过出站队列主动领取命令。

## 能力边界

| 方案 | 云端查看 | 云端启动/停止实验 | 需要T640公网入口 | 工程量 |
|---|---:|---:|---:|---:|
| T640本机操作台 | 否 | 本机/SSH隧道 | 否 | 最小 |
| T640 + Caddy HTTPS | 是 | 是 | 是，或安全隧道 | 小 |
| ModelScope静态Studio | 只能展示静态数据 | 否 | — | 小，但不满足目标 |
| ModelScope Docker Studio反向代理T640 | 是 | 是 | 是，或安全隧道 | 中等，推荐MVP |
| ModelScope + 外部队列 + T640出站Agent | 是 | 是 | 否 | 较大，适合长期无人值守 |

## 推荐MVP架构

```text
浏览器
  │ HTTPS / ModelScope身份
  ▼
私有ModelScope Docker Studio（端口7860）
  │ 服务端代理；凭证只存在Studio Secret
  ▼
T640受认证HTTPS入口（Caddy或安全隧道）
  │ 只转发操作台白名单接口
  ▼
127.0.0.1:8766 Pilot01 Console
  ├─ 实验计划、启动、停止、继续、记录
  └─ /api/gpu-status → 本机nvidia-smi与三个SGLang端点
```

ModelScope只承载界面和代理，不保存正式实验原始记录。正式数据、模型Key、Docker权限和GPU访问全部留在T640。Studio重启不会造成实验记录丢失。

## 为什么选择Docker Studio

现有操作台是自定义HTML和Python HTTP后端，不适合改写成纯静态站，也没有必要套进Gradio组件。Docker Studio可以保留现有页面结构并增加受控反向代理。平台要求容器监听`0.0.0.0:7860`；Docker Studio还要求ModelScope账号完成实名认证并绑定阿里云账号。

Studio默认文件系统会随重启丢失，因此不能把`outputs/experiment_console`放在Studio本地。需要持久化的证据继续保存在T640；如以后引入队列，应使用外部数据库或对象存储。

## 安全约束

1. Studio保持私有；在完成独立身份认证和权限分级前不改成公开。
2. 不从云端开放SSH、Docker socket、任意shell或任意文件路径。
3. 云代理只允许固定路由：健康、配置、状态、GPU状态、计划、启动、停止、继续、详情和记录导出。
4. T640入口使用HTTPS；上游凭证放ModelScope Secret，不写入代码、Git、浏览器JavaScript或日志。
5. 修改类请求保留现有CSRF token、Origin检查，并为云代理增加稳定的服务间认证与请求ID；重复请求必须幂等。
6. GPU监控保持只读；实验启动仍由用户明确点击，不因页面刷新、Studio重启或Agent重连自动开始。
7. Studio不直接保存模型API Key。模型调用始终由T640本地Console读取`config/api.local.env`。

## 两种网络条件

### T640具有稳定公网IP或域名

使用仓库现有`deploy/cloud/` Caddy方案，将443映射到T640；8766继续只监听本机。ModelScope后端代理访问该HTTPS地址。公网入口应单独使用强随机服务凭证，并限制访问频率和请求大小。

### T640没有公网入站能力

不能让Studio直接连接私网地址。选择之一：

- 使用可信的反向隧道，把T640的Caddy入口映射成HTTPS地址；或
- 部署外部命令队列。T640 Agent只建立出站连接，领取经过签名的命令并回传状态。这种方案不开放T640入站端口，但需要持久数据库、任务认领、重放保护和断线恢复。

长期无人值守建议使用出站Agent；当前继续并发测试的MVP建议使用HTTPS反向代理，因为改动更少，仍保留完整审计记录。

## ModelScope部署步骤

1. 国内站默认使用`https://modelscope.cn`，准备该站点对应的`MODELSCOPE_API_KEY`。
2. 创建**私有**Docker Studio；代码仓库默认分支为`master`，服务监听`0.0.0.0:7860`。
3. 配置普通变量：上游T640 HTTPS URL、刷新间隔；配置Secret：服务间凭证。
4. Git推送Studio源码，调用Studio Deploy，检查build日志后检查run日志。
5. 在云端验证：未登录不可访问；打开页面不产生模型调用；GPU状态可见；先生成清单，再用一个开发run验证启动、停止、记录和导出。
6. 确认T640原始记录、模型调用日志和GPU遥测完整后，才进行正式并发实验。

## 当前部署前置缺口

- 当前工作环境没有`MODELSCOPE_API_KEY`，也没有安装`ms`/`modelscope` CLI，因此尚未创建或修改任何Studio。
- T640目前没有运行Caddy或`pilot01-console` systemd服务，也没有确认可供Studio访问的公网HTTPS地址。
- 创建Studio前还需确定ModelScope账号owner/repo；可见性建议保持private。

在补齐ModelScope token和T640可达HTTPS入口之前，可以完成并验证本地融合版，但不能声称云端到T640的控制链路已经跑通。
