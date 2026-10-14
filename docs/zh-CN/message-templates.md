# 消息模板

英文版为准：[English](../message-templates.md)

FleetOps 为每支舰队生成两段文字：发到 Discord 的 **Ping**，以及写进游戏内舰队公告的 **MOTD**。两者都来自模板，由管理员在
**后台设置** > **消息模板** 中编辑（需要权限 `fleetops.manage_configuration`）。

模板使用 Django 模板语言。Ping 和 MOTD 拿到的变量相同；除 `is_pre_ping` 外，所有值都是纯文本。时间一律是 EVE 时间（UTC）。

## 用哪个模板

- 每个模板有类型（Ping 或 MOTD）、名称、内容，以及"默认"（Default）和"启用"（Active）两个开关。
- 在 **开舰队** 页面，FC 可以为每种类型选择任意已启用的模板。没有选择时，使用该类型的默认模板；如果没有默认模板，则使用第一个已启用的模板。
- 每种类型只有一个默认模板，并且默认模板必须是启用的。把某个模板存为默认时，之前那个模板的默认标记会被去掉。
- 未启用的模板永远不会被使用。有语法错误的模板无法保存。
- 模板在渲染时出错也不会阻止开队：FleetOps 改用内置文本，并在页面上报告问题。

## 变量

| 变量 | 值 |
|---|---|
| `ping_mention` | 选中的提及：`@everyone`、`@here`、身份组提及（`<@&id>`），选"不 Ping"时为空。只有这个提及会通知到人。 |
| `ping_target` | 与 `ping_mention` 相同。 |
| `ping_target_label` | 这条 Ping 发给谁，用文字表示：`all`、`online members`、身份组名称（`Sig A, Sig B`）或 `this channel`。 |
| `ping_target_name` | 与 `ping_target_label` 相同。 |
| `ping_channel` | Ping 频道的名称；只复制不发送时为空。 |
| `sender` | 发送消息的用户的大号；没有则用 FC 的大号，再没有则用 FC 角色。 |
| `fc` | FC（舰队 Boss）角色的名字。 |
| `fc_main` | FC 的大号。 |
| `fleet_type` | 舰队类型名称。 |
| `formup` | FC 填写的集结地。 |
| `doctrine` | 教义名称，或 `None / Custom`（按站点语言显示）。要判断有没有教义，请用 `doctrine_name`，没有教义时它为空。 |
| `doctrine_name` | 教义名称，没有则为空。 |
| `doctrine_url` | 教义在 Fittings 中的页面链接；其他来源的教义为空。 |
| `doctrine_ships` | Fittings 教义中每套配置的船体，按教义顺序用 ` > ` 连接（例如 `Moa > Moa > Osprey`）。 |
| `doctrine_fits` | Fittings 教义中各套配置的名称，用 ` > ` 连接。 |
| `comms` | 语音预设名称。 |
| `comms_channel` | 语音预设的频道名。 |
| `comms_link` | 打开语音的链接：语音客户端链接给出加入页，`http` 和 `https` 链接直接使用。 |
| `comms_voice_url` | 预设里保存的语音链接，不含用户名和密码。 |
| `srp_url` | 舰队 SRP 链接的完整地址（FleetOps 舰队 SRP 页，或 aa-srp、Alliance Auth SRP 的链接）；舰队还没有时为空，开队预览里也为空。SRP 步骤在 Ping 和 MOTD 之前执行，所以两者里都可以用。 |
| `logi_channel` | 后勤频道的名称。 |
| `logi_channel_link` | 后勤频道的游戏内链接（`<a href="joinChannel:…">Name</a>`）；没有游戏内链接时只是名称。 |
| `boost_channel` | 加成频道的名称。 |
| `boost_channel_link` | 加成频道的游戏内链接；没有时只是名称。 |
| `fleet_time` | 预 Ping 的舰队为 `YYYY-MM-DD HH:MM EVE`，否则为 `Now`（按站点语言显示）。要判断是不是预 Ping，请用 `is_pre_ping`。 |
| `fleet_time_eve` | 预 Ping 的舰队为 `YYYY-MM-DD HH:MM EVE`，否则为空。 |
| `fleet_time_unix` | 舰队时间的 Unix 时间戳，或为空。 |
| `fleet_time_discord` | Discord 相对时间戳 `<t:…:R>`，按每位读者自己的时区显示；或为空。 |
| `is_pre_ping` | 预 Ping 时为真。配合 `{% if %}` 使用。 |
| `sent_at` | 消息渲染的时间，EVE 时间，格式 `YYYY-MM-DD HH:MM`。 |
| `additional_message` | FC 填写的补充说明。 |
| `operation_id` | FleetOps 舰队记录的 ID。 |

## 提及

只有 FleetOps 自己加的提及能通知到人。模板里看到的 `ping_mention` 只是一个占位符；渲染出的 Ping 里其他所有 `@everyone` 和 `@here`
都会被零宽空格打断，同时 Discord 会被告知只允许哪一种提及。模板里手写的 `@everyone` 不会 Ping 到任何人。

如果选了提及，但模板里没有 `{{ ping_mention }}`（或 `{{ ping_target }}`），预览会警告没有人会被通知。

## 格式与限制

- Ping：Discord Markdown（`**粗体**`、`[文字](链接)`、`-# 小字`）。Discord 最多接受 2000 个字符（按 Discord 的算法计数）。超过限制的 Ping
  在发出之前就会被拒绝；只复制的 Ping 则只给出警告。**开舰队** 页面上的计数器显示当前长度。链接预览已被关闭。
- MOTD：模板输出不做 HTML 转义。游戏能识别一小部分类 HTML 标签（`<b>`、`<br>`、`<font color="#bfffffff">`、`<a href="…">`）。
  `{{ additional_message|linebreaksbr }}` 会保留 FC 输入的换行。`<script` 会被转义。MOTD 超过 1000 个字符只会给出警告，因为游戏可能会截断更长的 MOTD。
- 语音和教义的链接由 `SITE_URL` 生成。没有它，链接只是站内路径，在 Alliance Auth 之外打不开，预览会对此给出警告。

## 内置模板

内置的 **Default Ping**：

```django
{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE
```

内置的 **Default MOTD**（一行）：

```django
<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>
```

`python manage.py fleetops_seed` 会在它们还不存在时创建 "Default Ping" 和 "Default MOTD"，不会覆盖你改过的模板。自定义模板如果要带上 SRP 链接，
需要自己加上 `{{ srp_url }}`。

预约舰队被取消时，FleetOps 可以在发预 Ping 的同一个频道发一条通知。通知来自固定文本（`**Cancelled:** <舰队类型> at <舰队时间> (<集结地>)`），
不会 @ 任何人；它不是可以编辑的模板。

## 语言

Ping 和 MOTD 使用站点语言（Alliance Auth 设置里的 `LANGUAGE_CODE`）书写，而不是点击按钮的 FC 的语言，所以每位读者看到的文字都一样，
预览也与实际发出的内容一致。模板是你自己写的文字，不会被翻译。只有 FleetOps 自己加的词会跟随站点语言：`ping_target_label` 里的目标词
（`all`、`online members`、`this channel`）、`fleet_time` 里的 `Now`、`doctrine` 里的 `None / Custom`，以及取消通知。站点语言为中文时，后两个词显示为
`现在` 和 `无 / 自定义`，所以 `{% if fleet_time == "Now" %}` 这样的条件在那里永远不成立。条件请用 `{% if is_pre_ping %}` 和 `{% if doctrine_name %}`，不要和这些词比较。
