# 集成

英文版为准：[English](../integrations.md)

FleetOps 只需要 Alliance Auth 和 django-esi。下面所有内容都是可选的：FleetOps 会检测已安装的应用，缺少其他应用时照常工作。

| 应用或服务 | 带来什么 | 没有它时 |
|---|---|---|
| [fittings](#fittings) | 开舰队页的教义列表，Ping 和 MOTD 里的教义链接和船体 | 只有 `None / Custom` 教义 |
| [django-eveonline-sde](#django-eveonline-sde)（`eve_sde`） | 英文舰船与星系名称、舰船分类、可搭桥质量、三跳地图 | 编成只按舰队位置和船型规则统计；没有地图 |
| [FleetOps SRP、aa-srp、Alliance Auth SRP](#srp-应用) | 为每支舰队创建 SRP 链接 | 没有 SRP 链接；SRP 步骤被跳过 |
| [Discord Webhook](#discord) | 发送 Ping | 手动复制 Ping |
| [Alliance Auth Discord 服务](#discord) | 读取服务器的身份组用于 @ | 手动填写身份组 ID |
| [aa-charlink](#aa-charlink) | 在 CharLink 页面上授权 FleetOps 所需的 ESI 权限 | 飞行员在 FleetOps 页面上授权 |
| [AFAT](#afat) | 与 PAP 登记链接共用 ESI 令牌 | 没有变化 |
| aa-fleetpings、optimer、django-eveuniverse、aadiscordbot | FleetOps 不使用 | 可以与 FleetOps 并存安装 |

## fittings

如果已安装的应用标签为 `fittings`，并提供了教义模型，FleetOps 会在 **开舰队** 页和 PAP 登记链接舰队页里列出它的教义。找不到受支持的教义模型时，
**None / Custom** 仍然可用，FleetOps 继续正常工作。

- FC 看到的是 Fittings 允许其看到的教义：如果某个教义所在的分类限制在 FC 不在的用户组，就会被隐藏。拥有 `fittings.manage` 的用户能看到所有教义。
  后台设置的表单（例如 SRP 赔付表）会列出全部教义。
- 舰队会保存教义的名称和来源，所以教义以后被删除也不影响历史记录。对于 Fittings 教义，开队或预 Ping 时还会保存各套配置（配置名和船体，按教义顺序）。
  它们用于模板变量 `doctrine_ships` 和 `doctrine_fits`；**重新生成并推送 MOTD**（Regenerate & Push MOTD）会重新读取。
- Ping 和 MOTD 里的教义会链接到它的 Fittings 页面（`doctrine_url`）。成员需要 `fittings.access_fittings` 才能打开该页面。
- 安装方式：`pip install "aa-fleetops[fittings]"`，或者自行安装 `fittings`。

## django-eveonline-sde

应用标签是 `eve_sde`。FleetOps 从中读取名称和舰船数据：

- 舰船和星系名称一律以英文保存，不论查看者或 Worker 使用什么语言，并且在所有地方都以英文显示。
- 舰队 **编成** 按舰船功能归类：舰船分类，以及泰坦桥能带的质量（"无法搭桥的舰船组"设置）。规则可以直接输入准确的英文名称。
- 星系名称也让游戏内 MOTD 能把集结地做成可点击的链接。
- 常规设置里的 **无法搭桥的舰船组** 会用 SDE 校验：不是舰船组的 ID 会被拒绝。
- **三跳地图** 标签显示距离 FC 所在星系三跳以内的在队成员，数据来自 SDE 的星门数据。它只是一个舰队成员的邻近视图，不是完整的星图。

没有 SDE 时，编成只能按舰队位置和船型规则统计舰船功能，其余舰船列为"未分类"；舰船组和船型仍然可以用 ID 输入。如果读不到星门数据，地图标签会提示路由数据不可用。
安装方式：`pip install "aa-fleetops[sde]"`。如果你在已有旧舰队的站点上新装了 SDE，请先运行
`python manage.py fleetops_refresh_names --dry-run`，再去掉 `--dry-run` 运行一次，把已保存的名称改成英文。

## 不需要令牌的 ESI 接口

除了用 FC 令牌读写的舰队接口，FleetOps 还为默认 MOTD 读取两个公开的 ESI 接口：角色的血脉（`GET /characters/{id}/`，用来生成正确的游戏内角色链接；缓存 30 天，失败缓存十分钟，此时链接使用默认的角色类型）和按名称查星系 ID
（`POST /universe/ids/`，只在集结地文字是 SDE 不认识的名称时才用；找到的名称缓存 30 天，找不到的缓存一天）。它们都不会阻塞开队：ESI 没有回应时，MOTD 里就是普通文字。不带舰队的 Ping 不做这些请求。

## SRP 应用

FleetOps 可以在每支舰队开队时创建 SRP 链接（"SRP 自动创建"，SRP auto create）。有三种提供方：FleetOps SRP、aa-srp（`aasrp`）和
Alliance Auth 内置的 SRP 应用（`allianceauth.srp`）。常规设置里的 **SRP 提供方**（SRP provider）可选"自动"（FleetOps SRP 启用时优先，然后是 aa-srp，再是
Alliance Auth SRP），也可以指定其中一个。创建链接不会阻塞开队：提供方缺失或出错时，舰队照常开启，SRP 步骤会显示原因。链接可以在 Ping 和 MOTD 模板里用
`srp_url` 引用。详细说明、给其他 SRP 应用使用的提供方接口以及 aa-srp 的字段见 [SRP providers](../srp-providers.md)（英文）；
FleetOps SRP 本身在[配置参考](configuration.md#fleetops-srp)里介绍。

## Discord

- **Webhook** 用来发送 Ping。每个 Ping 频道保存一个 Webhook 地址，它只能写入：任何页面都不会再次显示。见[Ping 频道](configuration.md#ping-频道)。
- **用于 @ 的身份组**：Alliance Auth 的 Discord 服务（`allianceauth.services.modules.discord`）和 `DISCORD_GUILD_ID` 让"从 Discord 同步身份组"
  能读取服务器的身份组。没有它们时，请手动填写身份组 ID。FleetOps 不使用 aadiscordbot。
- 只有当身份组设为"可被任何人提及"，或者 Webhook 有权提及所有身份组时，Discord 才会通知该身份组。

## aa-charlink

安装了 aa-charlink 后，它的页面会提供两个 FleetOps 导入项：

- **FleetOps FC fleet control**：`esi-fleets.read_fleet.v1` 和 `esi-fleets.write_fleet.v1`，面向拥有 `fleetops.start_fleet` 的用户。默认不勾选。
- **FleetOps PAP links**：`esi-location.read_location.v1`、`esi-location.read_ship_type.v1` 和 `esi-location.read_online.v1`，面向拥有
  `fleetops.basic_access` 的用户。默认勾选。

在那里关联的角色，开舰队页和 PAP 登记链接页可以直接使用。CharLink 会检查每个钩子所属的应用标签，所以 `INSTALLED_APPS` 里必须原样写出 `"fleetops"`。
没有 CharLink 时，这个钩子不会被加载。

## AFAT

FleetOps 不依赖 AFAT，也没有 AFAT 导入功能。PAP 登记链接用到的三个位置类权限与 AFAT 点击追踪相同，所以 AFAT 或 CharLink 已经保存的令牌可以直接复用。
两个应用可以并存，各自保留自己的记录。

## 其他舰队应用

aa-fleetpings 和 optimer 不被使用。FleetOps 通过 Discord Webhook 发送自己的 Ping，不会读写 optimer 条目。它们可以与 FleetOps 一起安装。
