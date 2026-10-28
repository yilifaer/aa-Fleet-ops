# 权限

英文版为准：[English](../permissions.md)

FleetOps 使用 Django 应用 `fleetops` 的权限。请通过 Alliance Auth 的用户组、状态（State）或单个用户来分配。日常管理不需要
staff 或 superuser 账号。

无论页面是什么语言，权限名称在 Alliance Auth 后台里都显示英文，所以本文以权限代号（codename）为准。`fleetops.manage_attendance`
的代号和名称里保留了 "attendance" 一词，它管理的就是 PAP。

## 权限一览

| 代号 | 后台显示的名称 | 作用 |
|---|---|---|
| `fleetops.basic_access` | Can access FleetOps | 打开 FleetOps，查看个人总览、统计和历史，以及自己参与过的舰队（包括自己正在被追踪、或通过 PAP 登记链接登记过的进行中舰队）。通过 PAP 登记链接登记角色。 |
| `fleetops.start_fleet` | Can start fleets | 开启由 ESI 追踪的舰队：发送 Ping、设置 MOTD、创建 SRP 链接并开始追踪。发送预 Ping 并预约舰队，查看所有预约舰队，开启自己预约的舰队。开启 PAP 登记链接舰队。保存和使用个人开队预设。 |
| `fleetops.manage_own_fleet` | Can manage own fleets | 结束、编辑、重试和控制自己担任 FC、或通过特殊角色获得 FC 积分的舰队：重新生成并推送 MOTD、踢人、舰队页的"舰队"标签（联队、中队、移动成员、邀请、结构模板）、PAP 登记链接、把舰队排除出统计、处理待定的 PAP 倍率选择，以及编辑或取消自己预约的舰队。 |
| `fleetops.view_all_fleets` | Can view all fleet records | 读取全部舰队档案，打开任何舰队记录，包括预约舰队、进行中舰队的成员列表及其"舰队"标签（只读）。不带任何编辑或控制权。 |
| `fleetops.create_manual_fleet` | Can create manual fleet records | 添加不经 ESI 追踪的补录（历史）舰队记录。 |
| `fleetops.manage_fleets` | Can manage all fleets | 管理所有舰队，不论 FC 是谁，包括编辑或取消任何预约舰队，以及任何进行中舰队的"舰队"标签。不能开启别的 FC 预约的舰队。同时它也是 PAP 规则里的"FC 主管"权限：通过或拒绝补录舰队、补录 PAP、粘贴名单、PAP 登记链接舰队和被修改过时间的舰队（自己提交的记录直接生效），在时长规则之外设置 PAP 倍率（必须写明理由），给一支舰队设置超过设置值的"每位玩家的 PAP 角色数"或不限，舰队结束之后修改这个值（必须写明理由），强制把舰队计入统计或撤销排除，删除已通过的补录 PAP。 |
| `fleetops.view_corp_stats` | Can view own corporation statistics | 查看自己所在军团的统计、成员、不活跃成员、成员页、PAP 历史及其导出。 |
| `fleetops.view_all_stats` | Can view all FleetOps statistics | 查看全联盟统计：所有军团、全部成员和不活跃成员、每个成员页、FC 统计、PAP 历史、排行榜和所有统计导出。只看统计；打开舰队记录需要 `view_all_fleets` 或 `manage_fleets`。 |
| `fleetops.manage_attendance` | Can manage attendance | 在自己可管理的舰队上补录 PAP（按名字、ID 或粘贴名单），在 FC 主管通过之前删除自己的记录，重新计算 PAP，在时长规则范围内修改已结束舰队的 PAP 倍率。与 `manage_fleets` 同时拥有时对所有舰队生效。 |
| `fleetops.manage_incentives` | Can manage FC incentives | 审核、重新计算、放弃领取、定稿和解锁每月的 FC 奖金周期。只有在"常规设置"里勾选了"启用 FC 奖金"（Incentive enabled，默认关闭）时这些页面才存在。 |
| `fleetops.manage_configuration` | Can manage FleetOps configuration | 使用 FleetOps 后台设置：常规设置、舰队类型、语音预设、后勤和加成频道、Ping 频道和 Ping 身份组（含"从 Discord 同步身份组"）、PAP 倍率、特殊角色、月度达标线、舰船功能及规则、结构模板、消息模板，以及 SRP 设置、赔付表和军团限额。在自己能打开的舰队里，从编成页快速归类舰船。 |
| `fleetops.view_audit_log` | Can view FleetOps audit log | 打开"操作日志"页。 |
| `fleetops.request_srp` | Can request SRP | 在 FleetOps SRP 中提交舰船损失申请（"我的 SRP"和舰队的 SRP 链接），查看自己的申请和本月额度。需要先启用 FleetOps SRP。加入 SRP 功能的那次迁移已经把它授予所有已拥有 `basic_access` 的用户组、状态和用户；之后新建的用户组需要自己加上。 |
| `fleetops.manage_srp` | Can review and pay SRP claims | 审核并支付申请：通过、修改金额、拒绝、撤销、标记已付款、重新打开、重新归类、重新计算、重新检查在队情况、添加内部备注。打开和关闭舰队的 SRP 链接。查看所有 SRP 汇总和申请。因为它决定付款，所以不会默认授予任何人。 |

## 推荐的用户组

下面五个用户组能满足大多数联盟。在 Alliance Auth 里建好用户组，然后加上对应权限。

### 成员

- `fleetops.basic_access`
- `fleetops.request_srp`

成员能看到自己的 PAP、历史、月度最低要求和排名，能看到全联盟排行榜和自己军团的排行榜（"排行榜可见范围"的默认值），
能看到"进行中舰队里的 PAP"，以及自己参与过的舰队。成员可以通过 PAP 登记链接登记自己的角色，并在 SRP 启用后提交损失申请。
舰队结束之前，成员只能看到自己的角色。

### 军团管理

在成员权限之上加 `fleetops.view_corp_stats`。

军团管理可以看到本军团的月度统计与达标线对比、成员列表、不活跃名单、每位成员的页面、本军团的 PAP 历史，并把成员列表和
PAP 历史导出为 Excel 或 CSV。其他军团的成员、成员页和导出仍然看不到：需要 `view_all_stats` 的页面返回 403，
其他军团成员的页面及其导出返回 404。

### FC

- `fleetops.basic_access`
- `fleetops.start_fleet`
- `fleetops.manage_own_fleet`
- `fleetops.view_all_fleets`
- `fleetops.create_manual_fleet`
- `fleetops.manage_attendance`

FC 可以开队（完整模式或 **PAP 仅追踪**），发送和重试 Ping 与 MOTD，创建 SRP 链接，发送预 Ping，并开启、编辑或取消自己预约的舰队。
FC 追踪并结束自己的舰队，结束时选择 PAP 倍率，踢人，在"舰队"标签里管理舰队，创建和撤销 PAP 登记链接，开启 PAP 登记链接舰队，
分配特殊角色（舰队结束后也可以），补录 PAP（经 FC 主管通过后生效），创建补录舰队记录，并编辑自己舰队的时间（新时间需要审核）。
FC 可以只读地查看所有历史舰队和所有进行中舰队的"舰队"标签。

`view_all_fleets` 从不允许修改别的 FC 的舰队。

### FC 主管

除 SRP 之外的全部 FleetOps 权限：

- `fleetops.basic_access`
- `fleetops.start_fleet`
- `fleetops.manage_own_fleet`
- `fleetops.view_all_fleets`
- `fleetops.create_manual_fleet`
- `fleetops.manage_fleets`
- `fleetops.view_corp_stats`
- `fleetops.view_all_stats`
- `fleetops.manage_attendance`
- `fleetops.manage_incentives`
- `fleetops.manage_configuration`
- `fleetops.view_audit_log`

FC 主管管理所有舰队，审批补录记录，覆盖 PAP 倍率和每位玩家的 PAP 角色数，查看全部统计和操作日志，运行 FC 奖金（需先勾选"启用 FC 奖金"），并管理 FleetOps 的设置。
如果 FC 主管同时负责 SRP，再加上 `fleetops.request_srp` 和 `fleetops.manage_srp`。

### SRP 管理员

- `fleetops.basic_access`
- `fleetops.request_srp`
- `fleetops.manage_srp`
- `fleetops.view_all_fleets`（只用于从申请跳转到舰队）

SRP 管理员按规则要求填写理由，审核、通过、修改金额、拒绝、撤销和支付申请，打开和关闭舰队的 SRP 链接，查看每个舰队的申请和所有
SRP 汇总并下载 SRP 文件，还可以添加只有 SRP 管理员能看到的内部备注。管理者自己的申请由另一位 SRP 管理员处理，除非勾选了
"SRP 管理员可以处理自己的申请"。

### 组长

组长是 Alliance Auth 里被记录为某个用户组组长的用户（该用户组的组长，以及它的组长组里的成员；如果这个小组的记录打开了 **这个用户组的所有成员都算作它的组长**，则是该组的所有成员）。只有权限 `auth.group_management` 并不会让谁成为组长。组长除了 `fleetops.basic_access` 之外不需要任何 FleetOps 权限：
**后台设置** > **小组** 里这个用户组的记录就是授权（见[小组](configuration.md#小组)）。组长能做什么，取决于这个小组的 **组长可以**（Leaders may）设置：

| 小组的设置 | 组长可以 |
|---|---|
| **仅发送 Ping** | 打开 **发送 Ping**，向分配给该小组的频道发 Ping。不产生舰队记录，不发 PAP。 |
| **发送 Ping 和开带 PAP 的舰队** | 还可以在开舰队页开 **小组舰队**（小组固定，或在 **舰队归属** 里选），只能用该小组允许的舰队类型，只能发到该小组的频道，只能立即开始（没有预 Ping）。他们管理自己开的小组舰队，以只读方式打开本小组的其他舰队；小组开了 SRP 开关时，SRP 菜单里有 **我的舰队**。 |

**每个小组两个用户组。** 如果联盟把“示例小队成员”和“示例小队组长”建成两个 Alliance Auth 用户组，并且不记录组长，那么组长就是第二个组的成员：

1. 在 Alliance Auth 里把小队组长放进“示例小队组长”，并给他们 `fleetops.basic_access`（例如通过状态或他们已有的组）。
2. 在 **后台设置** > **小组** 为“示例小队组长”添加一条记录，打开 **这个用户组的所有成员都算作它的组长**。
3. 在 **后台设置** > **Ping 频道** 里，把“示例小队组长”勾选到这个小队的频道上。

这些成员就拥有和 Alliance Auth 里记录的组长完全一样的权限，按这条记录的模式，只能用这个小组的频道。“示例小队成员”里的人没有这些权限。把人移出“示例小队组长”，下一次请求权限就结束；未启用的账号或没有 `basic_access` 的账号永远没有。详见[小组](configuration.md#每个小组两个用户组)。

组长不能做的事：修改任何设置、发预 Ping、开 PAP 登记链接舰队、使用联盟频道（没有小组的频道）或别的小组的频道、修改小组舰队的舰队类型、放宽 PAP 角色数上限。同时拥有正常 FC 权限的组长保留这些权限，并在联盟舰队和小组舰队之间选择。

## 行级规则

- 舰队由它的 FC、通过特殊角色获得 FC 积分且拥有 `fleetops.manage_own_fleet` 的成员、以及拥有 `fleetops.manage_fleets` 的人管理。
- 单独拥有 `manage_attendance` 不能改别的 FC 的舰队。它只对用户有权管理的舰队生效；与 `manage_fleets` 同时拥有时对所有舰队生效，
  并且提交的记录直接通过。
- 特殊角色的"带 FC 积分"（Gives FC credit）开关单独决定持有者能否获得 FC 舰队和积分；持有者不需要任何 FC 权限。
  管理舰队仍然需要 `manage_own_fleet`。特殊角色只能给舰队里有记录的角色；待审核或被拒绝的补录 PAP 不会让角色符合资格，
  所以无法用未经审核的补录 PAP 来发放 FC 积分。
- 小组舰队由开队的组长和 `manage_fleets` 管理。FC 主管（`manage_fleets`）可以向所有启用的 Ping 频道发 Ping。普通 FC 可以用所有没有小组的频道；属于小组的频道，只有勾选了 **联盟 FC 也可以使用此频道** 才能用。
- 用户所属军团指的是其大号所在的军团。没有大号的用户打开军团页面会得到 404。别的军团的页面，不管这个军团存不存在，都得到同一个 404，并且页面会写明原因（例如“你只能打开自己军团的统计。”）。
  所有不存在或不可见的 FleetOps 页面都显示同一个中性的 FleetOps 404 页；缺少权限的页面显示带原因和权限名的 FleetOps 页面（403）。
- 带 FC 积分的特殊角色要等 FC 主管通过舰队审批后才能指派。被拒绝的舰队改动后仍是被拒绝状态；它的 FC（或管理它的人）用 **重新提交审批** 送回去，可以附一条审批人能看到的备注。
- 拒绝（舰队、PAP 记录、SRP 申请）、撤销付款、撤销通过等不可逆的操作，都会先弹出确认。

## FC 奖金开关

新站点默认关闭 FC 奖金。单独拥有 `fleetops.manage_incentives` 不会显示 FC 奖金页面；请先在 **后台设置** > **常规设置**
里勾选"启用 FC 奖金"（Incentive enabled）。开关关闭时，菜单入口隐藏，奖金周期不能被审核、重新计算、放弃领取、定稿、解锁或导出。
FC 统计不受影响。

## 统计

| 操作 | 谁可以 |
|---|---|
| 我的统计、我的历史、月度最低要求和排名卡片 | `fleetops.basic_access` |
| 排行榜 | `fleetops.basic_access` 并取决于"排行榜可见范围"（Leaderboard visibility）：选"所有有 FleetOps 权限的人"（默认）时，每个用户都能看全联盟排行榜和自己军团的排行榜，`view_all_stats` 还能看任何军团的；选"仅统计管理者"时，`view_corp_stats` 看自己军团的排行榜，`view_all_stats` 看全联盟和任何军团的。没有权限时页面返回 403，菜单入口隐藏。"我的统计"上的排名卡片遵循同一设置。 |
| 本军团：统计、成员、不活跃、历史 | `fleetops.view_corp_stats` |
| 单个成员页 | 成员本人（会跳转到"我的统计"）；`view_corp_stats` 可看大号在本军团的成员；`view_all_stats` 可看所有人。不在统计范围内的用户（没有大号、账号停用、已离开成员联盟）没有成员页（404）。页面上的舰队链接需要 `view_all_fleets` 或 `manage_fleets`。 |
| 联盟：所有军团、任意军团页及其成员列表、成员、不活跃、FC 统计、PAP 历史 | `fleetops.view_all_stats` |
| 月度达标线、排行榜名次数、排行榜可见范围、成员联盟 ID | `fleetops.manage_configuration`；每次修改都会写入操作日志 |

导出地址为 `/fleetops/statistics/export/<dataset>/?format=xlsx` 或 `format=csv`：

| 数据集 | 谁可以 |
|---|---|
| `corporations`、`fcs` | `view_all_stats` |
| `members` | `view_all_stats` 可导出全联盟或任意军团（`corporation=<id>`）；`view_corp_stats` 只能带上本军团的 ID，其他军团返回 404 |
| `member-fleets` | 能打开该成员页的人（`user=<id>`），否则 404 |
| `attendance`（PAP 历史） | `scope=me`：`basic_access`；`scope=corporation`：`view_corp_stats`；`scope=alliance`：`view_all_stats` |
| `incentives` | `manage_incentives`，且已勾选"启用 FC 奖金" |

未知的数据集或格式返回 404，只接受 GET，响应为私有且不缓存。每次下载都会记为操作日志 `statistics.export`，包含数据集、范围、月份、格式和筛选条件。

## SRP

| 操作 | 谁可以 |
|---|---|
| 我的 SRP、舰队的 SRP 链接页、检查并提交损失、撤回并重新提交申请、查看自己的申请页 | `fleetops.request_srp`。申请的角色必须属于用户自己的 Alliance Auth 账号。 |
| 舰队页上的 SRP 卡片 | 链接及复制按钮：舰队的管理者、FC 和 SRP 管理员。"为本舰队申请 SRP"按钮：`request_srp`，且链接处于开启状态。 |
| 创建舰队的 SRP 链接（"创建 SRP 链接"、"重试 SRP"） | 该舰队上的 `fleetops.manage_own_fleet`，或 `fleetops.manage_fleets` |
| 开启或关闭舰队的 SRP 链接 | `fleetops.manage_srp` |
| 审核队列、付款页、对申请的各种决定、批量通过、"重新计算" | `fleetops.manage_srp`；不能处理管理者自己的申请，除非勾选了"SRP 管理员可以处理自己的申请"（返回 403；批量操作会跳过并给出提示）。重新检查在队情况和添加内部备注只需要 `manage_srp`。 |
| 任何申请页 | 申请人、`manage_srp`、该申请所在舰队的 FC、`view_all_stats`（只读）、以及 `view_corp_stats`（本军团的申请）。其他人得到 404。内部备注和经手人的名字只有 `manage_srp` 能看到，其他人看到的是"申请人"、"审核人"或"系统"。本月已用额度显示给申请人、`manage_srp`、`view_all_stats` 以及申请所属军团的 `view_corp_stats`，不显示给舰队的 FC。 |
| SRP 汇总（联盟、军团、FC） | `manage_srp` 或 `view_all_stats`（只读） |
| SRP 军团（本军团的合计、成员、申请） | 本军团的 `view_corp_stats`（没有大号的用户得到 404）；`manage_srp` 或 `view_all_stats` 可看任意军团 |
| SRP 我的舰队（自己带过的舰队的申请和合计） | 拥有任一 FC 权限（`start_fleet`、`manage_own_fleet`、`manage_fleets`、`create_manual_fleet`）的用户，以及带过有 SRP 链接舰队的人，可看自己的页面；`manage_srp` 和 `view_all_stats` 还能看任意 FC 的页面 |
| SRP 设置、SRP 赔付表、SRP 军团限额 | `fleetops.manage_configuration`；每次修改都会记录新旧值 |

SRP 文件地址为 `/fleetops/statistics/export/<dataset>/`：

| 数据集 | 谁可以 |
|---|---|
| `srp-claims`、`srp-members` | `manage_srp` 或 `view_all_stats` 可导出全联盟或任意军团（`corporation=<id>`）；`view_corp_stats` 只能带上本军团的 ID（其他军团返回 404，没带军团返回 403） |
| `srp-corporations`、`srp-fcs` | `manage_srp` 或 `view_all_stats` |

`srp-claims` 里的"处理人"（Decided by）和"付款人"（Paid by）两列只对 `manage_srp` 填写。FC 和普通成员没有 SRP 文件。每次下载都记为 `srp.export`。
其他 SRP 操作都会记入操作日志，并显示在申请自己的时间线上：`srp.link_create`、`srp.link_open`、`srp.link_close`、`srp.claim_submit`、
`srp.claim_auto_reject`、`srp.claim_withdraw`、`srp.claim_resubmit`、`srp.claim_approve`、`srp.claim_amount_change`、`srp.claim_reject`、
`srp.claim_revoke`、`srp.claim_paid`、`srp.claim_unpaid`、`srp.claim_reopen`、`srp.claim_reclassify`、`srp.claim_recalculate`、
`srp.claim_recheck`、`srp.claim_note`。记录里只有 ID、状态和金额，不会出现令牌或 Webhook 地址；`srp.claim_note` 只记录备注的长度。

## 预约舰队（预 Ping）

| 操作 | 谁可以 |
|---|---|
| 发送预 Ping 并预约舰队 | `fleetops.start_fleet` |
| 查看预约舰队（总览里的列表、预约舰队页、舰队档案） | 舰队的 FC，以及拥有 `start_fleet`、`view_all_fleets` 或 `manage_fleets` 的用户。其他成员在 FleetOps 里看不到预 Ping，因为预 Ping 可能发到了限制访问的频道。 |
| 开启预约舰队 | 只有预约它的 FC（拥有 `start_fleet`），并且要用自己名下经 ESI 确认为舰队 Boss 的角色。其他 FC 和舰队管理者不能代开。 |
| 编辑预约舰队（可同时发出更新后的预 Ping） | 该舰队的 FC（拥有 `manage_own_fleet`），或 `manage_fleets` |
| 取消预约舰队（可同时发出不 @ 任何人的取消通知） | 该舰队的 FC（拥有 `manage_own_fleet`），或 `manage_fleets` |

预约、编辑、取消和开启都会记入操作日志：`fleet.schedule`、`fleet.schedule_edit`、`fleet.schedule_cancel`，以及每次开队一条的 `fleet.start`
（从预约开启时带有 `from_schedule`）。

## 进行中的舰队

- **重新生成并推送 MOTD**（Regenerate & Push MOTD）需要用户担任 FC 的舰队上的 `manage_own_fleet`，或 `manage_fleets`。记为 `fleet.motd_regenerate`。
- 舰队处于开启中、进行中或结束中时，成员列表、三跳地图、事件和踢人步骤的详情只显示给舰队的管理者以及拥有 `view_all_fleets` 或
  `manage_fleets` 的用户。其他能打开该舰队的成员只能看到自己的 PAP 和特殊角色，其余内容在舰队结束后才可见。
- 开队时如果 ESI 确认了新的舰队 Boss，同一个 ESI 舰队上上一位 FC 的记录会以"舰队 Boss 已移交"结束，不论新 Boss 对那条记录有什么权限。
  两条记录各自保留自己的 PAP 和 FC 积分。移交记为旧记录上的 `fleet.boss_handover`。

## 舰队标签

| 操作 | 谁可以 |
|---|---|
| 查看进行中或结束中舰队的"舰队"标签（舰队树和编成） | 舰队的管理者（FC、拥有 `manage_own_fleet` 且特殊角色带 FC 积分的成员，以及 `manage_fleets`），以及只读的 `view_all_fleets`。其他能打开舰队页的成员在舰队进行中看不到这个标签。 |
| 查看已结束舰队的编成 | 所有能打开舰队页的人 |
| 从 ESI 刷新舰队，添加、重命名、删除联队和中队，移动成员，设置中队、联队和舰队指挥官，从舰队树踢人，邀请角色，应用结构模板 | 自己及被授予 FC 积分的舰队上的 `manage_own_fleet`，或 `manage_fleets`；仅限进行中的 ESI 舰队。ESI 调用始终使用 FC 角色的令牌，该令牌需要 `esi-fleets.write_fleet.v1`。 |
| 邀请角色 | 任何已由活跃用户登记在 Alliance Auth 中的角色，不论该用户是否有 FleetOps 权限。不在 Alliance Auth 中的角色以及停用用户的角色会被拒绝。 |
| 在编成页快速归类舰船 | `manage_configuration`，且对该舰队有访问权 |
| 舰船功能、规则、结构模板、"无法搭桥的舰船组"、"舰队实时刷新" | `manage_configuration` |

FC 把舰队 Boss 交给了别人、舰队读不出来、FC 角色没有带写权限的令牌、或舰队上有后台任务在运行时，修改会被拒绝并给出原因。实时 ESI 刷新只为舰队的管理者运行，
绝不会因只读查看者而触发。

每一次修改都会连同结果写入操作日志：`fleet.wing_create`、`fleet.wing_rename`、`fleet.wing_delete`、`fleet.squad_create`、`fleet.squad_rename`、
`fleet.squad_delete`、`fleet.member_move`、`fleet.invite`，后台任务则是 `fleet.move_job`、`fleet.invite_job` 和 `fleet.template_job`。
快速归类记为 `configuration.create` 或 `configuration.update`。

## PAP 规则

| 操作 | 谁可以 |
|---|---|
| 通过或拒绝补录舰队、补录 PAP、粘贴名单、PAP 登记链接舰队和被 FC 改过时间的舰队（"审批"页和舰队页） | `fleetops.manage_fleets`。拥有 `manage_fleets` 的用户提交的记录和时间修改直接通过；其他人的要等待审批，批准前不计入。 |
| 删除补录或导入的 PAP 行 | 待审核或被拒绝时由作者删除（需要该舰队上的 `manage_attendance`）；任何行都可由 `manage_fleets` 删除 |
| 结束舰队时选择 PAP 倍率 | 有权结束该舰队的人。服务器只允许舰队时长已达到的倍率。 |
| 在时长规则范围内修改已结束舰队的倍率 | 该舰队上的 `manage_attendance`。舰队的 FC（`manage_own_fleet`）始终可以处理待定的选择。 |
| 设置超出时长规则的倍率 | `manage_fleets`，必须写至少 5 个字符的理由（记为 `attendance.multiplier`，来源为 "override"） |
| 在"开舰队"页、PAP 登记链接舰队页和预约舰队编辑页为一支舰队选择 **每位玩家的 PAP 角色数** | FC 和小组组长：设置里有数时不能超过它（设置为空时可填任何值）；`manage_fleets`：任何值，包括不限 |
| 修改进行中舰队的 **每位玩家的 PAP 角色数**（PAP 预览上方的控件） | 能管理该舰队的人，规则相同：不超过设置值，有 `manage_fleets` 则可填任何值（记为 `pap.limit`） |
| 修改已结束舰队的 **每位玩家的 PAP 角色数**（会重新计算 PAP） | 只有 `manage_fleets`，必须写至少 5 个字符的理由（记为 `pap.limit`）。FC 和小组组长在舰队结束后不能再改。 |
| 重新计算已结束舰队的 PAP | 该舰队上的 `manage_attendance` |
| 把舰队排除出统计（"不计入"，必须写理由） | 管理该舰队的人 |
| 强制计入（"始终计入"）或恢复自动规则 | `manage_fleets` |
| 创建或撤销 PAP 登记链接 | 自己及被授予 FC 积分的舰队上的 `manage_own_fleet`，或 `manage_fleets`；仅限进行中或已结束的舰队 |
| 通过 PAP 登记链接登记角色 | `basic_access`，登记的是用户自己的角色，使用用户自己的 ESI 令牌 |
| 开启 PAP 登记链接舰队 | `start_fleet`；除非开队的人有 `manage_fleets`，否则舰队要等待审批 |
| 踢出成员、踢出星系外的人、一键踢蛋 | 自己及被授予 FC 积分的舰队上的 `manage_own_fleet`，或 `manage_fleets`。FC 角色需要带 `esi-fleets.write_fleet.v1` 的令牌；FC 和舰队 Boss 永远不会被踢。 |
| 保存、载入、重命名、删除开队预设 | `start_fleet`；每个用户只看到自己的预设。预设不记入操作日志。 |
| 配置 PAP 倍率和特殊角色 | `manage_configuration` |

审批、倍率修改、统计计入、PAP 登记链接、登记、重新计算和踢人都会记入操作日志，对应 `approval.*`、`attendance.multiplier`、`fleet.stats_mode`、
`pap_link.create`、`pap_link.revoke`、`pap.register`、`pap.recalculate`、`fleet.kick_member`、`fleet.kick_outside_system` 和 `fleet.kick_capsules`。

## 配置

- Ping 身份组页面上的"从 Discord 同步身份组"需要 `fleetops.manage_configuration`、Alliance Auth 的 Discord 服务以及 `DISCORD_GUILD_ID`。同步来的身份组默认是停用的，
  由管理员决定 FC 可以 @ 哪些。每次同步记为 `configuration.discord_role_sync`。
- Ping 频道的 Webhook 地址只能写入：拥有 `manage_configuration` 的用户可以替换地址，但没有任何页面会显示已保存的地址。在 Django 后台里，只有查看权限的用户看到的是 "已保存（已隐藏）"（Stored (hidden)）。
- 语音加入页（`/fleetops/comms/<token>/join/`）是 FleetOps 中唯一不做权限检查的页面。如果 `APPS_WITH_PUBLIC_VIEWS` 里有 `"fleetops"`，不登录也能打开；
  否则像其他 Alliance Auth 页面一样，需要登录并设置了大号。
