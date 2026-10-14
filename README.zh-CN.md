# AA FleetOps

[Alliance Auth](https://gitlab.com/allianceauth/allianceauth) 的舰队运营应用。FC 在一个页面里开队、给 Discord 发 Ping、设置 MOTD 并开始追踪。
之后 FleetOps 会跟踪舰队，在舰队结束时发放 PAP，并把结果汇入联盟的统计、FC 积分和 SRP。

[English](README.md) | 简体中文

英文版为准；本文是 [README.md](README.md) 的中文版本。

状态：测试版。第一个公开的预发布版本是 0.2.0b1；每次升级之前请阅读 [CHANGELOG](CHANGELOG.md)（英文）。

## 功能

**开队**

- 从自己的 Alliance Auth 账号里选任意大号或小号作为 FC。FleetOps 会找到该角色所在的 ESI 舰队，并检查该角色是否是舰队 Boss；Boss 可以处在任何舰队位置，
  不是 Boss 的舰队指挥官会被拒绝，并提示当前 Boss 是谁。
- 每支 ESI 舰队只有一条记录。对已经在追踪的舰队再次开队，会直接打开正在运行的记录。新的 Boss 会把上一位 FC 的记录按 Boss 移交结束，并开启新的记录。
- 带 FC 积分权重的舰队类型，来自可选的 `fittings` 应用的教义（也可以自定义文字），自由填写的集结地，以及语音、后勤和加成频道预设。
- 个人开队预设，以及 **预 Ping**：为之后的某个 EVE 时间预告舰队，Discord 时间戳会按每位读者自己的时区显示。预约的舰队之后由预约它的 FC 开启。
- **PAP 仅追踪** 只追踪舰队，不发 Discord Ping、不设 MOTD、不建 SRP 链接。**PAP 登记链接舰队** 完全不需要 ESI 舰队。

**Discord Ping 与 MOTD**

- Ping 通过 Webhook 发到 Discord 频道。FC 选择 Ping 频道和目标：不提及、`@everyone`、`@here` 或 Discord 身份组。每个频道决定允许哪些提及，
  只有选中的提及才能通知到人。超过 Discord 2000 字符限制的 Ping 在发出前就会被拒绝。
- 可编辑的 Ping 和 MOTD 模板，带实时预览；不论自动化是否成功，复制按钮始终可用；进行中的舰队可以 **重新生成并推送 MOTD**（Regenerate & Push MOTD）。
- 为 `mumble://` 这类语音链接提供公开的加入页，这样 Discord 和游戏内 MOTD 里的语音链接可以直接点击。

**追踪与舰队管理**

- 每位成员的当前状态和变化事件（加入、离开、重新加入、舰船、星系、职位、联队和中队），以及每个角色的在队时长。
- 只有在连续多次读取失败后才会自动结束舰队，页面会说明舰队为什么结束（手动结束、舰队解散、Boss 移交）。开队的每一步都保留尝试历史。
- **舰队** 标签显示实时的舰队树，以及按舰船功能、舰船分类和星系统计的编成和可搭桥质量。FC 可以在网页上添加、重命名、删除联队和中队，拖放移动成员，邀请
  Alliance Auth 里的角色，应用结构模板。
- 踢出单个成员、星系外的所有人，或者踢蛋（Kick Capsules）。
- 安装了 `eve_sde` 时，显示 FC 周围的 3 跳地图。

**PAP**

- PAP 在舰队结束时发放，给在队时长达到 **PAP 最低在队时长** 或通过 PAP 登记链接登记过的角色，可选择限制每位玩家的角色数。小号计入大号。
- 按舰队时长设置 PAP 倍率（例如 90 分钟后 2 倍、180 分钟后 3 倍），由 FC 在结束时选择，或自动设定。
- 补录舰队、补录 PAP 和粘贴的名单需要 FC 主管审批。
- **PAP 登记链接** 让飞行员不必在 ESI 舰队里也能登记自己的角色。
- Back Seat FC 之类的特殊角色，可以选择是否带 FC 积分。

**统计与 FC 积分**

- 按 EVE 月统计个人、军团、联盟和 FC 数据；月度达标线带绿、琥珀、红三种状态；排行榜；不活跃成员；PAP 历史。
- 军团、成员、FC、PAP 历史、FC 奖金和 SRP 页面都能导出 Excel 和 CSV。
- FC 积分（舰队类型权重乘以 PAP 倍率），以及每月的 FC 奖金计算：资格判定、放弃领取和定稿。

**SRP**

- 舰队开队时创建 SRP 链接，由 FleetOps SRP、aa-srp 或 Alliance Auth SRP 提供。
- FleetOps SRP：成员用游戏内 KM 链接或 zKillboard 链接提交损失申请；赔付表和赔付比例、单笔上限、每名玩家和每个军团的月度限额；审核、付款清单、汇总和导出。FleetOps 从不转移 ISK。

**管理**

- 所有配置都在网页界面里完成，不需要 Django 后台权限。每次修改和每个特权操作都有操作日志。
- 为成员、军团管理、FC、FC 主管和 SRP 管理员分别设置权限。
- 英文和简体中文页面，跟随每位用户的 Alliance Auth 语言。

## 环境要求

- Python 3.10 到 3.14
- Alliance Auth 5.x，5.2 及以上
- django-esi 9 或 10
- Redis 和 Celery（与任何 Alliance Auth 站点一样）

django-esi 请与 Alliance Auth 的版本保持匹配：

| Alliance Auth | django-esi |
|---|---|
| 5.2 到 5.4 | 9 |
| 5.5 及之后的 5.x | 10 |

安装 FleetOps 不会升级已有的 django-esi。不要强制升级（例如使用 `--upgrade-strategy eager`）；请把 Alliance Auth 和 django-esi 一起升级。
CI 在 Alliance Auth 5.2 和 5.4（django-esi 9）以及最新的 5.x（django-esi 10）上运行测试。

FleetOps 使用 django-esi 的 OpenAPI 客户端，ESI 兼容日期为 `2025-11-06`。

## 安装

把 FleetOps 安装到 Alliance Auth 站点所用的虚拟环境里。`--no-deps` 能避免 `pip` 改动 Alliance Auth 和 django-esi 的版本。

```bash
source /path/to/venv/bin/activate

# from PyPI (0.2.0b1 is a pre-release, so name the version)
pip install --no-deps aa-fleetops==0.2.0b1

# or from the wheel of a GitHub Release
pip install --no-deps https://github.com/yilifaer/aa-fleetops/releases/download/v0.2.0b1/aa_fleetops-0.2.0b1-py3-none-any.whl

# or from Git
pip install --no-deps "git+https://github.com/yilifaer/aa-fleetops.git@v0.2.0b1"
```

如果站点上还没有依赖，去掉 `--no-deps`。额外依赖 `aa-fleetops[fittings]` 和 `aa-fleetops[sde]` 会安装可选的 `fittings` 与 `django-eveonline-sde`。

在 Alliance Auth 项目的 `local.py` 里加入应用和它的两个定时任务：

```python
INSTALLED_APPS += [
    "fleetops",
]

from celery.schedules import crontab

CELERYBEAT_SCHEDULE["fleetops_track_active_fleets"] = {
    "task": "fleetops.tasks.schedule_active_fleet_tracking",
    "schedule": crontab(minute="*"),
    "apply_offset": True,
}

CELERYBEAT_SCHEDULE["fleetops_prune_history"] = {
    "task": "fleetops.tasks.prune_attendance_history",
    "schedule": crontab(hour=4, minute=15),
    "apply_offset": True,
}
```

每分钟运行的任务只负责判断哪些进行中的舰队到了该读取的时间，读取间隔本身是一个设置。同一个任务还会收尾那些"结束舰队"任务丢失的舰队、取消被中断的开队，并启动 SRP 价值扫描，
所以请保持它一直运行。如果安装了 aa-charlink，`INSTALLED_APPS` 里必须原样写出 `"fleetops"`。

为了让未登录的飞行员能打开语音加入页，再加入下面的内容。`local.py` 里已经定义了 `APPS_WITH_PUBLIC_VIEWS`（新站点里是空列表，也可能已经列着其他有公开页面的应用），
请把这一行写在那个定义的下面，是往列表里追加，而不是替换它（如果你的 `local.py` 里没有这个定义，就写成 `APPS_WITH_PUBLIC_VIEWS = ["fleetops"]`）：

```python
# Lets pilots who are not logged in open the comms join page.
APPS_WITH_PUBLIC_VIEWS += ["fleetops"]
```

Ping 和 MOTD 里的链接由 `SITE_URL` 生成。`local.py` 开头附近已经定义了它，`ALLOWED_HOSTS`、`CSRF_TRUSTED_ORIGINS` 和 `ESI_SSO_CALLBACK_URL` 都是由它派生出来的，
所以不要在这里再加一行，也不要改它：改动会让 EVE SSO 回调失效。只需确认它是站点的 https 地址且末尾没有斜杠，例如 `https://auth.example.com`。

`local.py` 里也已经有 `ESI_USER_CONTACT_EMAIL` 这一行（模板里是空的）。按 django-esi 的要求把联系邮箱填进这一行，不要再添加第二行。然后运行：

```bash
python manage.py migrate fleetops
python manage.py collectstatic --noinput
python manage.py fleetops_seed          # default templates, multipliers, special roles, ship functions, structure template
```

`fleetops_seed --demo` 还会为测试站点创建三个示例舰队类型。按你的部署方式重启 Web 进程、Celery Worker 和 Celery Beat。FleetOps 的日志写入 Alliance Auth 的扩展日志
（`extensions.log`）。

**Docker。** 把 `aa-fleetops==0.2.0b1` 加到 Web、Worker 和 Beat 服务共用的镜像的依赖里，重新构建镜像，加上上面的 `local.py` 内容。然后在 Web 容器里打开一个 shell，
用 Alliance Auth 镜像自带的 `auth` 简写运行命令：

```bash
docker compose exec allianceauth_gunicorn bash
auth migrate fleetops
auth collectstatic --noinput
auth fleetops_seed
```

`auth` 是 shell 别名，只在这个 shell 里可用。不想进 shell 时，可以直接调用脚本，例如
`docker compose exec allianceauth_gunicorn python /home/allianceauth/myauth/manage.py migrate fleetops`。服务名取决于你的 compose 文件。

## EVE SSO 权限范围

FC 需要授权 `esi-fleets.read_fleet.v1` 和 `esi-fleets.write_fleet.v1`。写权限用于 MOTD、踢人和"舰队"标签；FleetOps 始终使用 FC 角色的令牌，而不是点击按钮的人的令牌。
如果 MOTD 更新失败，渲染好的 MOTD 仍可复制。

PAP 登记链接用飞行员自己的令牌读取他在哪里、开什么船、是否在线：`esi-location.read_location.v1`、`esi-location.read_ship_type.v1` 和
`esi-location.read_online.v1`。这些与 AFAT 点击追踪以及对应的 aa-charlink 导入项所用的权限相同，所以它们已有的令牌可以复用。请在你的 EVE SSO 应用里启用这五个权限范围。
FleetOps SRP 不需要任何权限范围。

## 第一步

1. 在 Alliance Auth 里按[权限](docs/zh-CN/permissions.md)创建用户组。FC 至少需要 `fleetops.basic_access`、`fleetops.start_fleet` 和 `fleetops.manage_own_fleet`，
   管理员需要 `fleetops.manage_configuration`。
2. 打开 **后台设置**（`/fleetops/configuration/`）：添加舰队类型、语音预设、后勤和加成频道，以及带 Discord Webhook 的 Ping 频道。详见[配置参考](docs/zh-CN/configuration.md)。
3. 打开 **开舰队** 页面，点击 **授权 / 刷新舰队 ESI**（Authorize / Refresh Fleet ESI）。
4. 在游戏里建一支舰队，让 FC 角色当舰队 Boss，填好表单，点击 **发送 Ping 并开队**（SEND PING & START FLEET）。舰队页会显示每一步的结果。

没有 Ping 频道时，开舰队页会提供可复制的 Ping 文本。

## 权限

| 角色 | 权限 |
|---|---|
| 成员 | `fleetops.basic_access`、`fleetops.request_srp` |
| 军团管理 | 成员权限 + `fleetops.view_corp_stats` |
| FC | `basic_access`、`start_fleet`、`manage_own_fleet`、`view_all_fleets`、`create_manual_fleet`、`manage_attendance` |
| FC 主管 | 除 SRP 之外的全部权限 |
| SRP 管理员 | `basic_access`、`request_srp`、`manage_srp`、`view_all_fleets` |

每一项权限和行级规则见 [docs/zh-CN/permissions.md](docs/zh-CN/permissions.md)。

## 可选集成

| 应用 | 带来什么 |
|---|---|
| fittings | 教义列表，Ping 和 MOTD 里的教义链接和船体 |
| django-eveonline-sde | 英文舰船与星系名称、舰船分类、可搭桥质量、三跳地图 |
| aa-srp、Alliance Auth SRP | 为每支舰队创建 SRP 链接 |
| Alliance Auth Discord 服务 | 读取服务器的身份组用于 @ |
| aa-charlink | 在 CharLink 页面授权 FleetOps 所需的权限 |

没有它们 FleetOps 也能工作。详见 [docs/zh-CN/integrations.md](docs/zh-CN/integrations.md)。

## 升级

1. 备份数据库。
2. 把新版本安装到虚拟环境里（`pip install --no-deps aa-fleetops==<version>`）。
3. 阅读该版本在 [CHANGELOG](CHANGELOG.md)（英文）里的升级说明。
4. 运行 `python manage.py migrate fleetops` 和 `python manage.py collectstatic --noinput`。
5. 重启 Web 进程、Celery Worker 和 Celery Beat。

## 翻译

FleetOps 跟随 Alliance Auth 的语言菜单，目前提供英文和简体中文。Discord Ping 和游戏内 MOTD 使用站点语言书写，而不是点击按钮的 FC 的语言。
想改进翻译或增加语言，请看 [docs/translations.md](docs/translations.md)（英文，含中文术语表）。

## 文档

| 文档 | 面向 |
|---|---|
| [配置参考](docs/zh-CN/configuration.md) | 管理员：每个后台页面和设置，PAP 与统计规则，SRP |
| [消息模板](docs/zh-CN/message-templates.md) | 管理员：Ping 和 MOTD 的变量及内置模板 |
| [权限](docs/zh-CN/permissions.md) | 管理员：权限与推荐用户组 |
| [集成](docs/zh-CN/integrations.md) | 管理员：可选应用 |
| [SRP providers](docs/srp-providers.md)（英文） | 开发者：SRP 链接提供方 |
| [Architecture](docs/architecture.md)（英文） | 开发者：代码结构与流程 |
| [Translations](docs/translations.md)（英文） | 翻译者 |
| [Manual testing](docs/manual-testing.md)（英文） | 维护者：发布检查清单 |
| [Releasing](docs/releasing.md)（英文） | 维护者：发布流程 |

## 帮助与参与

问题请到 [Discussions](https://github.com/yilifaer/aa-fleetops/discussions) 提问。通过 [Issues](https://github.com/yilifaer/aa-fleetops/issues/new/choose)
报告 Bug、提出功能需求和提交翻译修正。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。开发环境、测试和编码规则见 [CONTRIBUTING.md](CONTRIBUTING.md)（英文）。

## 许可证

AA FleetOps 是自由软件：你可以依据 GNU 通用公共许可证第 3 版的条款再分发和修改它。见 [LICENSE](LICENSE)。

Copyright (C) 2026 yilifaer and AA FleetOps contributors.
