# AA FleetOps

[Alliance Auth](https://gitlab.com/allianceauth/allianceauth) 的舰队运营应用。FC 在一个页面里开队、给 Discord 发 Ping、设置 MOTD 并开始追踪。
之后 FleetOps 会跟踪舰队，在舰队结束时发放 PAP，并把结果汇入联盟的统计、FC 积分和 SRP。

[English](README.md) | 简体中文

英文版为准；本文是 [README.md](README.md) 的中文版本。

状态：测试版。当前的预发布版本是 0.3.0b1；每次升级之前请阅读 [CHANGELOG](CHANGELOG.md)（英文）。

## 功能

**开队**

- 从自己的 Alliance Auth 账号里选任意大号或小号作为 FC。FleetOps 会找到该角色所在的 ESI 舰队，并检查该角色是否是舰队 Boss；Boss 可以处在任何舰队位置，
  不是 Boss 的舰队指挥官会被拒绝，并提示当前 Boss 是谁。
- 每支 ESI 舰队只有一条记录。对已经在追踪的舰队再次开队，会直接打开正在运行的记录。新的 Boss 会把上一位 FC 的记录按 Boss 移交结束，并开启新的记录。
- 带 FC 积分权重的舰队类型，来自可选的 `fittings` 应用的教义（也可以自定义文字），自由填写的集结地，以及语音、后勤和加成频道预设。
- 个人开队预设，以及 **预 Ping**：为之后的某个 EVE 时间预告舰队，Discord 时间戳会按每位读者自己的时区显示。预约的舰队之后由预约它的 FC 开启。
- **PAP 仅追踪** 只追踪舰队，不发 Discord Ping、不设 MOTD、不建 SRP 链接。**PAP 登记链接舰队** 完全不需要 ESI 舰队。
- **小组 Ping 与小组舰队。** 小组就是 Alliance Auth 的用户组。管理员在 FleetOps 自己的后台页面里决定：每个 Ping 频道允许哪些小组的组长使用，以及组长可以做什么
  ——只发 Ping（**发送 Ping**，不产生舰队记录、不发 PAP），或者还可以开管理员为该小组允许的舰队类型的带 PAP 的舰队。组长就是 Alliance Auth 里已经记录的组长；
  他们不能修改任何设置，也用不了联盟频道。

**Discord Ping 与 MOTD**

- Ping 通过 Webhook 发到 Discord 频道。FC 选择 Ping 频道和目标：不提及、`@everyone`、`@here` 或 Discord 身份组。每个频道决定允许哪些提及，
  只有选中的提及才能通知到人。超过 Discord 2000 字符限制的 Ping 在发出前就会被拒绝。
- 可编辑的 Ping 和 MOTD 模板，带实时预览（看起来像 Discord 和游戏内窗口）；不论自动化是否成功，复制按钮始终可用；进行中的舰队可以 **重新生成并推送 MOTD**（Regenerate & Push MOTD）。
  内置的 MOTD 里，FC 和集结星系在游戏里可以直接点开（`showinfo` 链接）；发出 Ping 之前会先确认。
- 为 `mumble://` 这类语音链接提供公开的加入页，这样 Discord 和游戏内 MOTD 里的语音链接可以直接点击。

**追踪与舰队管理**

- 每位成员的当前状态和变化事件（加入、离开、重新加入、舰船、星系、职位、联队和中队），以及每个角色的在队时长。
- 只有在连续多次读取失败后才会自动结束舰队，页面会说明舰队为什么结束（手动结束、舰队解散、Boss 移交）。开队的每一步都保留尝试历史。
- **舰队** 标签显示实时的舰队树，以及按舰船功能、舰船分类和星系统计的编成和可搭桥质量。FC 可以在网页上添加、重命名、删除联队和中队，拖放移动成员，邀请
  Alliance Auth 里的角色，应用结构模板。
- 踢出单个成员、星系外的所有人，或者踢蛋（Kick Capsules）。
- 安装了 `eve_sde` 时，显示 FC 周围的 3 跳地图。

**PAP**

- PAP 在舰队结束时发放，给在队时长达到 **PAP 最低在队时长** 或通过 PAP 登记链接登记过的角色，可选择限制每位玩家的角色数，FC 可以为单支舰队调小（FC 主管还可以调大）。小号计入大号。
- 按舰队时长设置 PAP 倍率（例如 90 分钟后 2 倍、180 分钟后 3 倍），由 FC 在结束时选择，或自动设定。
- 补录舰队、补录 PAP 和粘贴的名单需要 FC 主管审批。被拒绝的舰队改正后可以用 **重新提交审批** 送回去；带 FC 积分的特殊角色要等舰队通过审核后才能指派。
- **PAP 登记链接** 让飞行员不必在 ESI 舰队里也能登记自己的角色。
- Back Seat FC 之类的特殊角色，可以选择是否带 FC 积分。

**统计与 FC 积分**

- 按 EVE 月统计个人、军团、联盟和 FC 数据；月度达标线带绿、琥珀、红三种状态；排行榜；不活跃成员；PAP 历史。
- 军团、成员、FC、PAP 历史、FC 奖金和 SRP 页面都能导出 Excel 和 CSV。
- FC 积分（舰队类型权重乘以 PAP 倍率），以及每月的 FC 奖金计算：资格判定、放弃领取和定稿。没有定稿的月份会随着该月舰队的变化自动重新计算；没有设置月度达标线的月份完全不做评估。

**SRP**

- 舰队开队时创建 SRP 链接，由 FleetOps SRP、aa-srp 或 Alliance Auth SRP 提供。
- FleetOps SRP：成员用游戏内 KM 链接或 zKillboard 链接提交损失申请；赔付表和赔付比例、单笔上限、每名玩家和每个军团的月度限额；审核、付款清单、汇总和导出。FleetOps 从不转移 ISK。
- 常驻 SRP 链接（有名称、类别和日期，没有舰队），以及一个列出、关闭和完成所有 SRP 链接的页面。
- 把常驻链接作为提交入口：“我的 SRP”里的卡片、按类别自动归入当天的链接、可选的每日链接和自动关闭（运行在已经安装的 Beat 任务里）。
- 不赔付的损失（舰船组、舰船、空域；保存列表之前什么都不拦）和禁止提交的 SRP 封禁。
- 自动发现损失（打开之前是关闭的）：用成员自己的令牌从 ESI 读取各角色最近的损失，列在“我的 SRP”里并匹配角色当时所在的舰队；仍然可以贴链接。可以要求成员的每个角色都授权。运行在已经安装的 Beat 任务里。
- 成员可以在审核前修改待审核的申请（备注、类别或链接、损失）；审核人按类别、链接、日期和成员筛选队列，并看到攻击者、现在的限额和该成员的驳回历史；付款人筛选待付款申请、按批次付款并导出待付款清单；金额旁显示保险估算（只作参考）。
- SRP 类别（例如舰队、本土防御、单人）：每个类别有自己的单笔上限、月度上限、赔付规则、阵容说明和每日链接；申请保留提交时的类别。
- 任意时间段的报表和导出（月、季度、年或日期范围）：趋势，按类别、成员、审核人和付款人、星域及限额，可在页面查看，也可导出为 CSV 或 Excel 文件。
- 导入 aa-srp 的历史记录（`fleetops_import_aasrp`，带试运行和撤销）以及从 CSV 文件导入赔付表，方便站点带着记录迁移到 FleetOps SRP。

**管理**

- 所有配置都在网页界面里完成，不需要 Django 后台权限。每次修改和每个特权操作都有操作日志。
- 为成员、军团管理、FC、FC 主管、SRP 审核人、SRP 付款人和 SRP 管理员（两个角色）分别设置权限。
- 英文和简体中文页面，跟随每位用户的 Alliance Auth 语言；适配 Alliance Auth 的所有主题（Flatly、Darkly、Materia、Bootstrap、Bootstrap Dark），手机和电脑都能用。

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

# from the wheel of the GitHub Release
pip install --no-deps https://github.com/yilifaer/aa-Fleet-ops/releases/download/v0.3.0b1/aa_fleetops-0.3.0b1-py3-none-any.whl

# or from Git
pip install --no-deps "git+https://github.com/yilifaer/aa-Fleet-ops.git@v0.3.0b1"

# or from PyPI, only once the version is published on PyPI (0.3.0b1 is a pre-release, so name the version)
pip install --no-deps aa-fleetops==0.3.0b1
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

**警告：每日任务会删除数据。** `fleetops_prune_history`（`fleetops.tasks.prune_attendance_history`）会删除早于**数据保留天数**（至少 365 天）的 PAP 历史、成员事件和不带舰队的 Ping 文本（PAP 链接登记不在其中，它们随舰队保留）；设置了**成员联盟 ID** 时，还会删除主角色不在这些联盟里的玩家的 PAP。第一次运行之前请检查这两个设置，并运行 `python manage.py fleetops_prune_history --dry-run` 看会删掉什么。内置的安全检查会在设置明显有误时拒绝"已离开成员"这一部分，但它不知道你的本意，请保留数据库备份。见[数据与隐私](#数据与隐私)。

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

最后运行 `python manage.py check`。FleetOps 会就下面几项给出警告（只是警告，不是错误，不会让站点停止运行）：缺少 beat 条目、`SITE_URL` 为空、`APPS_WITH_PUBLIC_VIEWS` 里没有 `"fleetops"`、开启了 `ATOMIC_REQUESTS`、安装了 aa-charlink 但 `INSTALLED_APPS` 里没有字面写成 `"fleetops"` 的条目（`fleetops.W001` 到 `fleetops.W006`，详见[配置](https://github.com/yilifaer/aa-Fleet-ops/blob/dev/docs/zh-CN/configuration.md)）。不需要某一项的站点可以用 `SILENCED_SYSTEM_CHECKS = ["fleetops.W004"]` 之类的写法屏蔽它。`ATOMIC_REQUESTS` 必须保持关闭（Alliance Auth 的默认值）：FleetOps 为 SRP 额度和舰队结束锁定数据行，这要求每个请求不在同一个事务里运行。

**Docker。** 把 `aa-fleetops @ https://github.com/yilifaer/aa-Fleet-ops/releases/download/v0.3.0b1/aa_fleetops-0.3.0b1-py3-none-any.whl` 这一行（只有该版本发布到 PyPI 之后才能用 `aa-fleetops==0.3.0b1`）加到 Web、Worker 和 Beat 服务共用的镜像的依赖里，重新构建镜像，加上上面的 `local.py` 内容。然后在 Web 容器里打开一个 shell，
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
`esi-location.read_online.v1`。这些与 AFAT 点击追踪以及对应的 aa-charlink 导入项所用的权限相同，所以它们已有的令牌可以复用。FleetOps SRP 可以读取成员各角色最近的损失（`esi-killmails.read_killmails.v1`，用成员自己的令牌，仅在 **自动发现损失** 开启时）。请在你的 EVE SSO 应用里启用这六个权限范围；
没有最后一个时无法授权损失自动发现，其余功能照常。第七个权限范围是可选的：`esi-ui.open_window.v1` 只用于 SRP 付款页的 **在游戏中打开** 按钮，使用付款人自己的游戏内角色的令牌，并且只在付款人添加该角色时才请求。EVE 应用里没有它时，只有这一步授权会失败。

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
| SRP 审核人（只审核） | `basic_access`、`review_srp`、`view_all_fleets` |
| SRP 付款人（只付款） | `basic_access`、`pay_srp`、`view_all_fleets` |

每一项权限和行级规则见 [docs/zh-CN/permissions.md](docs/zh-CN/permissions.md)。

不要给群组授予 Django 为 FleetOps 模型列出的新增、修改、删除、查看权限：它们只在 Django 管理后台里有用，而 FleetOps 的任何设置都不需要那里。

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
2. **升级到 0.3.0b1 之前**，先在 EVE 开发者网站上，给本站 `ESI_SSO_CLIENT_ID` 对应的 EVE 应用加上权限范围 `esi-killmails.read_killmails.v1`。不加的话，损失读取的 SSO 登录会报 invalid scope，成员无法为自己的角色授权（其余功能照常）。
3. 用该版本 GitHub Release 的 wheel 把新版本安装到虚拟环境里（`pip install --no-deps <wheel 地址>`；只有该版本已发布到 PyPI 时才能用 `aa-fleetops==<version>`）。
4. 阅读该版本在 [CHANGELOG](CHANGELOG.md)（英文）里的升级说明。
5. 运行 `python manage.py migrate fleetops` 和 `python manage.py collectstatic --noinput`。
6. 重启 Web 进程、Celery Worker 和 Celery Beat。

**从 0.3.0b1 回退到 0.2.x。** 先回退迁移（0.2.0b3 构建用 `python manage.py migrate fleetops 0023`，0.2.0b2 构建用 `python manage.py migrate fleetops 0021`），再装回旧代码。这两个构建都没有正式发布，请重新安装本站之前用的 wheel 或提交。回退之前先运行 `python manage.py fleetops_srp_rollback_check`：它列出会挡住回退的东西（属于某个类别的赔付规则、有申请的常驻链接、导入的历史），不改任何数据。步骤和回退会丢失的数据见 [CHANGELOG](CHANGELOG.md)（英文）。

## 卸载

卸载 FleetOps 会删除它的数据，所以先备份数据库，并导出想保留的内容（SRP 页面有 CSV 和 Excel 导出）。请按下面的顺序操作；删除数据表时应用必须还装着。

1. 从 `local.py` 里删掉两条 `CELERYBEAT_SCHEDULE["fleetops_..."]`（FleetOps 自己不创建调度记录，但 Celery Beat 会把 `local.py` 里的条目存进数据库，且不会自动删除）。然后在 Django 管理后台的 **Periodic Tasks** 里删除或停用 `fleetops_track_active_fleets` 和 `fleetops_prune_history` 两个周期任务，并重启 Celery Beat，这样就不会再有任务被排给这个应用。在第 4 步之前，`manage.py check` 会就此给出 `fleetops.W001` 和 `fleetops.W002` 警告；请忽略，不要把这两条加回去。
2. 可选：运行 `python manage.py fleetops_srp_rollback_check`。它只读，列出会挡住第 3 步的东西（属于某个类别的赔付规则、有申请的链接、从 aa-srp 导入的申请）；先按 [CHANGELOG](CHANGELOG.md)（英文）的说明处理掉。
3. 运行 `python manage.py migrate fleetops zero`。这会删除 FleetOps 的所有数据表和表里的全部数据。在 MariaDB 和 MySQL 上，迁移中途失败是无法回滚的，所以请保留备份。
4. 从 `INSTALLED_APPS` 里删掉 `"fleetops"`，并从 `local.py` 里删掉 `APPS_WITH_PUBLIC_VIEWS += ["fleetops"]` 这一行。
5. 运行 `pip uninstall aa-fleetops`，然后重启 Web 进程、Celery Worker 和 Celery Beat。

**会留下什么。** Django 会保留该应用的 content type 和权限记录（`app_label` 为 `fleetops`），用户组里这些权限的分配也会保留；没有这个应用它们不起任何作用，可以在 Django 管理后台或 Django shell 里按 `app_label="fleetops"` 筛选后删除。不要运行 `remove_stale_contenttypes --include-stale-apps`，它也会动其他应用。Alliance Auth 自己的数据不受影响：用户、用户组、角色，以及 django-esi 保存的 ESI token（FleetOps 自己不保存任何 token）。缓存（以 `fleetops:` 开头的键）会自行过期。已经发出的 Discord 消息和 EVE SSO 应用上的权限范围都不会改变；不再需要的权限范围请自己到 EVE 应用上去掉。

## 数据与隐私

FleetOps 是你们组织自己的工具，保留什么由你决定。它把下面这些数据存在 Alliance Auth 的数据库里：

| 数据 | 内容 | 保留 |
|---|---|---|
| 舰队与成员 | 每支舰队：FC、类型、时间、配置和 Discord/ESI 的结果。舰队里出现过的每位飞行员：角色、军团、联盟、舰船、星系、职位和在队时长，没有 Alliance Auth 账号的人也一样 | 舰队和成员状态一直保留。成员事件在**数据保留天数**之后删除（见下） |
| PAP | 按角色记录的 PAP，附带所属的主角色和玩家、修正记录 | 在**数据保留天数**之后删除；设置了**成员联盟 ID** 时，已离开成员的也会删除。PAP 链接登记（角色、飞船、星系、在线状态）不在此列：它们保留到所属舰队被删除 |
| SRP | 申请及其 KM 数据（受害者、舰船、星系、攻击者名字）、金额、决定的时间线、付款批次、封禁、发现的损失 | 申请、时间线、付款和封禁是付款记录，保留策略从不删除它们；没人申请的 KM 7 天后删除，已经没用的发现损失也会删除 |
| 审计日志 | 谁在什么时候改了什么（设置、审核、付款、PAP 修正）；不含 token、不含 webhook 地址 | FleetOps 从不删除 |
| 设置 | Discord webhook 地址（和 Alliance Auth 里其他密钥一样，以明文存在数据库中）、SRP 和舰队设置 | 直到你删除 |
| 名称缓存 | 从 ESI 查到的角色、军团、联盟名称和归属 | 直到下次查询；从不删除 |
| SRP 付款人的选择 | 付款人在付款页所做的选择：视图（按玩家或按申请），以及“在游戏中打开”用的游戏内角色（其 ID 和名称） | 直到用户被删除或选择被移除；不受保留期限影响 |
| ESI token | FleetOps 不保存。它读取 django-esi 为用户保存的 token，从不把它写进日志、审计日志、页面或导出文件 | 由 Alliance Auth 管理 |

数据只会发往 Discord（你发出的 Ping）、zKillboard 和 ESI（SRP 用到的 KM ID 和角色 ID，仅在启用 SRP 时），不会发往其他地方。FleetOps 会访问的主机：ESI（通过 django-esi）、`zkillboard.com`（KM 价值，仅 SRP）、`discord.com`（你添加的 webhook），以及 Alliance Auth 自带 Discord 服务的 Discord API（读取身份组，仅在你点按钮时）。你的用户的浏览器会从 `images.evetech.net` 加载图片，并链接到 `zkillboard.com`、`evewho.com` 和 `everef.net`。没有遥测，也没有更新检查。保留期限在**管理**里的 FleetOps 设置页设置，名为**数据保留天数**（365 到 36500）。上面说的每日任务会执行它；`python manage.py fleetops_prune_history --dry-run` 可以先看效果。

**按请求删除数据。** FleetOps 没有按个人导出或删除的按钮。Django 管理后台里 PAP 记录（`AttendanceRecord`）、成员记录（`FleetMemberState`、`FleetMemberEvent`）和 SRP 申请都是只读的，只有 `PapRegistration` 可以在那里删除。某个人的记录请在 Django shell 里删除，例如 `AttendanceRecord.objects.filter(character_id=...).delete()`，`PapRegistration`、`FleetMemberState`、`FleetMemberEvent` 同样按角色 ID 处理；SRP 申请按玩家处理。SRP 封禁会随 Alliance Auth 里的用户一起删除；PAP 和 SRP 记录会作为历史保留，只是不再关联账号。发起或带过舰队、或有 FC 统计的用户，在这些记录还存在时不能被删除（数据库保护这些记录）。审计日志和已付款的 SRP 申请是账目记录：只有在你们的规定和所在国家的法律要求时才删除，并注意这会改变 FleetOps 统计和报表所依据的历史。

## 翻译

FleetOps 跟随 Alliance Auth 的语言菜单，网页提供英文和简体中文。Discord Ping 和游戏内 MOTD 永远是英文，不管站点语言是什么：其中 FleetOps 自己的字样从不翻译，管理员写在模板里的文字原样发出。
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

问题请到 [Discussions](https://github.com/yilifaer/aa-Fleet-ops/discussions) 提问。通过 [Issues](https://github.com/yilifaer/aa-Fleet-ops/issues/new/choose)
报告 Bug、提出功能需求和提交翻译修正。安全问题请按 [SECURITY.md](SECURITY.md) 私下报告。开发环境、测试和编码规则见 [CONTRIBUTING.md](CONTRIBUTING.md)（英文）。

## 许可证

AA FleetOps 是自由软件：你可以依据 GNU 通用公共许可证第 3 版的条款再分发和修改它。见 [LICENSE](LICENSE)。

Copyright (C) 2026 yilifaer and AA FleetOps contributors.
