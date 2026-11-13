# 消息模板

英文版为准：[English](../message-templates.md)

FleetOps 为每支舰队生成两段文字：发到 Discord 的 **Ping**，以及写进游戏内舰队公告的 **MOTD**。两者都来自模板，由管理员在
**后台设置** > **消息模板** 中编辑（需要权限 `fleetops.manage_configuration`）。第三种类型 **无舰队 Ping**（Ping without fleet）是不开舰队、
直接发出的 Ping 所用的文字。

模板使用 Django 模板语言。所有模板拿到的变量相同；除 `is_pre_ping` 外，所有值都是纯文本。时间一律是 EVE 时间（UTC）。

## 用哪个模板

- 每个模板有类型（Ping、MOTD 或无舰队 Ping）、名称、内容，以及"默认"（Default）和"启用"（Active）两个开关。
- 在 **开舰队** 页面，FC 可以为每种类型选择任意已启用的模板。没有选择时，使用该类型的默认模板；如果没有默认模板，则使用第一个已启用的模板。
- 无舰队 Ping 同样使用它自己类型的模板。
- 每种类型只有一个默认模板，并且默认模板必须是启用的。把某个模板存为默认时，之前那个模板的默认标记会被去掉。
- 未启用的模板永远不会被使用。有语法错误的模板无法保存。
- 模板在渲染时出错也不会阻止开队：FleetOps 改用内置文本，并在页面上报告问题。

## 变量

| 变量 | 值 |
|---|---|
| `ping_mention` | 选中的提及：`@everyone`、`@here`、身份组提及（`<@&id>`），选"不 Ping"时为空。只有这个提及会通知到人。 |
| `ping_target` | 与 `ping_mention` 相同。 |
| `ping_target_label` | 这条 Ping 发给谁，用文字表示（始终是英文）：`all`、`online members`、身份组名称（`Sig A, Sig B`）或 `this channel`。 |
| `ping_target_name` | 与 `ping_target_label` 相同。 |
| `ping_channel` | Ping 频道的名称；只复制不发送时为空。 |
| `sender` | 发送消息的用户的大号；没有则用 FC 的大号，再没有则用 FC 角色。 |
| `fc` | FC（舰队 Boss）角色的名字。 |
| `fc_main` | FC 的大号。 |
| `fc_character_id` | FC 角色的 ID，没有则为空。 |
| `fc_link` | FC 的游戏内链接，点开是该角色的资料窗口（`<a href="showinfo:…">Name</a>`）；没有时只是名字。用于 MOTD；放进 Discord Ping 里，标签会按文字显示。 |
| `fleet_type` | 舰队类型名称。 |
| `group` | 小组舰队或小组 Ping 所属小组的名称；联盟舰队为空。 |
| `formup` | FC 填写的集结地。 |
| `formup_system_id` | 集结地文字恰好是某个星系的名字时，该星系的 ID，否则为空。 |
| `formup_link` | 集结地文字恰好是某个星系的名字时，指向该星系的游戏内链接（`<loc><a href="showinfo:5//30000142">Jita</a></loc>`），否则就是原文字。用于 MOTD。 |
| `doctrine` | 建制名称，或 `None / Custom`（始终是英文）。要判断有没有建制，请用 `doctrine_name`，没有建制时它为空。 |
| `doctrine_name` | 建制名称，没有则为空。 |
| `doctrine_url` | 建制在 Fittings 中的页面链接；其他来源的建制为空。 |
| `doctrine_ships` | Fittings 建制中的船体，每种只出现一次，按建制顺序用 ` > ` 连接（例如 `Moa > Osprey`）；或按**建制设置**中为该建制保存的顺序，用符号 `>`、`=`、`>>>` 连接（例如 `Minokawa = Lif > Raven >>> Basilisk`）。 |
| `doctrine_fits` | Fittings 建制中每套配置的名称（每套都列出），用 ` > ` 连接。 |
| `comms` | 语音预设名称。 |
| `comms_channel` | 语音预设的频道名。 |
| `comms_link` | 打开语音的链接：语音客户端链接给出加入页，`http` 和 `https` 链接直接使用。 |
| `comms_voice_url` | 预设里保存的语音链接，不含用户名和密码。 |
| `srp_url` | 舰队 SRP 链接的完整地址（FleetOps 舰队 SRP 页，或 aa-srp、Alliance Auth SRP 的链接）；舰队还没有时为空，开队预览里也为空。SRP 步骤在 Ping 和 MOTD 之前执行，所以两者里都可以用。 |
| `logi_channel` | 后勤频道的名称。 |
| `logi_channel_link` | 后勤频道的游戏内链接（`<a href="joinChannel:…">Name</a>`）；没有游戏内链接时只是名称。 |
| `boost_channel` | 加成频道的名称。 |
| `boost_channel_link` | 加成频道的游戏内链接；没有时只是名称。 |
| `fleet_time` | 预 Ping 的舰队为 `YYYY-MM-DD HH:MM EVE`，否则为 `Now`（始终是英文）。要判断是不是预 Ping，请用 `is_pre_ping`。 |
| `fleet_time_eve` | 预 Ping 的舰队为 `YYYY-MM-DD HH:MM EVE`，否则为空。 |
| `fleet_time_unix` | 舰队时间的 Unix 时间戳，或为空。 |
| `fleet_time_discord` | Discord 相对时间戳 `<t:…:R>`，按每位读者自己的时区显示；或为空。 |
| `fleet_time_local` | Discord 时间戳 `<t:…:t>`，按每位读者自己的时区显示当天的时刻；或为空。 |
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
- MOTD：游戏读的是类 HTML 文本，所以渲染模板时，来自人或 FleetOps 的每个值都会做 HTML 转义（`<`、`>`、`&` 变成 `&lt;`、`&gt;`、`&amp;`；否则游戏会吃掉 `doctrine_ships` 里的 `>` 和 `>>>` 符号）。本身就是游戏标记的变量（`fc_link`、`formup_link`、`logi_channel_link`、`boost_channel_link`）保留标签，只转义可见文字。模板里自己已经转义的值（`{{ doctrine_ships|force_escape }}`、`|escape`）不会被转义两次。Discord Ping 不转义。游戏能识别一小部分类 HTML 标签（`<b>`、`<br>`、`<font color="#bfffffff">`、`<a href="…">`）。值里的撇号在标签之间的文字中按原样显示；在标签内部仍保持转义，所以自定义模板里单引号属性（`<font color='{{ formup }}'>`）取不到值里的撇号，请用双引号。
  `{{ additional_message|linebreaksbr }}` 会保留 FC 输入的换行。`<script` 会被转义。MOTD 超过 1000 个字符只会给出警告，因为游戏可能会截断更长的 MOTD。
- **Retry Ping** 会先按舰队当前的数据和当前的模板重新生成 Ping 再发出，所以旧版本保存的舰队不会再把旧的存储文本发一遍。**Retry MOTD** 发送存储的 MOTD；**Regenerate & Push MOTD** 会重新生成。已结束的舰队没有这两个重试按钮。
- 人工填写的文字在消息里一律是纯文本：建制名称、集结地点、小组和附加信息在放进模板之前，会先去掉游戏的富文本（`<font>`、`<b>`；`<a href="…">名字</a>` 只保留名字；被截断的标签直接丢弃），MOTD 和 Ping 都一样，所以一个字段不会吞掉后面的行。附加信息的换行会保留。以前保存的舰队同样适用：库里的数据不会被改动；不改建制名称直接保存赔付规则或舰队，也保留原名称（以及建制键）。只去掉游戏自己的标签，`<ABCD>` 这类联盟代号、Discord 时间戳 `<t:…>`、`x<y` 保持原样。
- 语音和建制的链接由 `SITE_URL` 生成。没有它，链接只是站内路径，在 Alliance Auth 之外打不开，预览会对此给出警告。

## 内置模板

内置的 **Default Ping**：

```django
{% if ping_mention %}{{ ping_mention }}
{% endif %}{% if additional_message %}{{ additional_message }}

{% endif %}**FC Name:** {{ fc }}
**Formup Location:** {{ formup }}
**PAP Type:** {{ fleet_type }}
{% if group %}**Group:** {{ group }}
{% endif %}{% if comms %}**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}
{% endif %}{% if doctrine_name %}**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}
{% endif %}{% if srp_url %}**SRP:** [Request SRP]({{ srp_url }})
{% endif %}{% if is_pre_ping %}**Fleet Time:** {{ fleet_time_eve }} · {{ fleet_time_local }} ({{ fleet_time_discord }})
{% endif %}
-# This was a FleetOps broadcast from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE
```

内置的 **Default MOTD**（一行）：

```django
<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc_link }}<br>Form Up: {{ formup_link }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>
```

内置的 **Default Ping without fleet**：

```django
{% if ping_mention %}{{ ping_mention }}
{% endif %}{{ additional_message }}
{% if group %}
**Group:** {{ group }}{% endif %}{% if formup %}
**Formup Location:** {{ formup }}{% endif %}{% if comms %}
**Comms:** {% if comms_link %}[{{ comms }}]({{ comms_link }}){% else %}{{ comms }}{% endif %}{% endif %}{% if doctrine_name %}
**Doctrine:** {% if doctrine_url %}[{{ doctrine_name }}]({{ doctrine_url }}){% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if is_pre_ping %}
**Time:** {{ fleet_time_eve }} · {{ fleet_time_local }} ({{ fleet_time_discord }}){% endif %}

-# FleetOps ping from {{ sender }} to {{ ping_target_label }} at {{ sent_at }} EVE
```

无舰队 Ping 的变量与舰队的相同。`fc`、`fc_main` 和 `sender` 是发出这条 Ping 的人的大号；只描述舰队的变量（`fleet_type`、`operation_id`、`srp_url`、
后勤和加成频道、`doctrine_fits`）为空；Ping 里写了时间时 `is_pre_ping` 为真。

从 0.3.0b5 起，内置的 **Default Ping** 带有 SRP 一行（`**SRP:** [Request SRP](地址)`）：它单独占一行，只在舰队有 SRP 链接时才出现；没有链接时既没有这一行，也不会出现空的 "SRP:"。地址会被处理成不会截断 Discord 链接的写法（`(`、`)`、`<`、`>`、空格和引号变成百分号编码），这一行和 Ping 的其余部分一样计入 2000 字符的限制。**已保存的内容一律不变**：数据库里已有的 Default Ping 保持原文（新安装也一样，迁移创建的是 0.3.0b4 的文本），你改过的模板也不变。只有 `fleetops_seed` 新建的模板，以及已保存的 Ping 无法渲染时使用的内置文本才带这一行。想让已有模板带上它，请在合适的位置在 `{% if srp_url %}` … `{% endif %}` 里加上 `**SRP:** [Request SRP]({{ srp_url }})`（或直接写 `{{ srp_url }}`）。开队预览里没有这一行，因为链接是开队时才创建的，在预览之后（MOTD 里的 SRP 一行也是这样）；实际发出的 Ping 有这一行。无舰队的 Default Ping 没有舰队，也就没有 SRP 链接。

`python manage.py fleetops_seed` 会在它们还不存在时创建 "Default Ping"、"Default MOTD" 和 "Default Ping without fleet"，不会覆盖你改过的模板。
升级时，只有仍与上一版默认内容完全一致的 Ping 或 MOTD 模板才会被替换（不论叫什么名字）；降级会把它们改回上一版的默认内容，升级之后你改过的模板会保留。
你改过的和你自己写的模板不会被碰：自己写的 MOTD 如果要让 FC 和集结地可以点击，需要自己加上 `{{ fc_link }}` 和 `{{ formup_link }}`（`{{ fc }}` 和 `{{ formup }}` 只是纯文字，模板编辑页也会提示），要带上 SRP 链接则加 `{{ srp_url }}`。

预约舰队被取消时，FleetOps 可以在发预 Ping 的同一个频道发一条通知。通知来自固定的英文文本（`**Cancelled:** <舰队类型> at <舰队时间> (<集结地>)`），
不会 @ 任何人；它不是可以编辑的模板。

## 语言

Ping 和 MOTD 始终是英文，不论 Alliance Auth 设置里的 `LANGUAGE_CODE` 是什么，也不论点击按钮的 FC 用什么语言，所以每位读者看到的文字都一样，
预览也与实际发出的内容一致。模板按你写的原样发出：请用你的飞行员读得懂的语言来写。FleetOps 自己加的词都是英文，并且永远不会被翻译：
`ping_target_label` 里的目标词（`all`、`online members`、`this channel`，以及没有名字的身份组显示的 `role <id>`）、`fleet_time` 里的 `Now`、
`doctrine` 里的 `None / Custom`、取消通知，以及内置模板里的字段名和页脚。模板里的翻译标签（`{% trans %}`）也按英文渲染。这些词永远不变，所以
`{% if fleet_time == "Now" %}` 这样的条件在任何站点都成立，但仍然推荐用 `{% if is_pre_ping %}` 和 `{% if doctrine_name %}`。升级前存下的 Ping
在这支舰队的消息重新渲染之前保持原来的文字。

FleetOps 页面跟随每位用户在 Alliance Auth 里选择的语言，预览旁边的警告也一样。
