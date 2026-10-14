# Message templates

FleetOps builds two texts for every fleet: the **Ping** that goes to Discord and the **MOTD** that is written
into the fleet's message of the day in game. Both come from templates that an administrator edits under
**Administration** > **Message Templates** (permission `fleetops.manage_configuration`). A Simplified Chinese
version of this page is in [zh-CN/message-templates.md](zh-CN/message-templates.md).

Templates use the Django template language. Ping and MOTD get the same variables; every value is plain text
except `is_pre_ping`. Times are EVE time (UTC).

## Which template is used

- Each template has a type (Ping or MOTD), a name, a content, and the switches **Default** and **Active**.
- On **Start Fleet** the FC can pick any active template of each type. Without a choice, the default template of
  the type is used. If no default exists, the first active template is used.
- There is one default per type, and a default must be active. Saving a template as the default removes the flag
  from the previous one.
- An inactive template is never used. A template with a syntax error cannot be saved.
- A template that fails while it is rendered never blocks a start: FleetOps uses the built-in text instead and
  reports the problem on the page.

## Variables

| Variable | Value |
|---|---|
| `ping_mention` | The selected mention: `@everyone`, `@here`, the role mentions (`<@&id>`), or empty for No ping. Only this mention notifies anybody. |
| `ping_target` | The same as `ping_mention`. |
| `ping_target_label` | Who the ping is for, in words: `all`, `online members`, the role names (`Sig A, Sig B`) or `this channel`. |
| `ping_target_name` | The same as `ping_target_label`. |
| `ping_channel` | Name of the ping channel, or empty when the ping is only copied. |
| `sender` | Main character of the user who sends the message, else the main of the FC, else the FC character. |
| `fc` | Name of the FC (fleet boss) character. |
| `fc_main` | Main character of the FC. |
| `fleet_type` | Fleet type name. |
| `formup` | Form-up text the FC entered. |
| `doctrine` | Doctrine name, or `None / Custom` (in the site language). To test for a doctrine, use `doctrine_name`, which is empty when there is none. |
| `doctrine_name` | Doctrine name, or empty. |
| `doctrine_url` | Link to the doctrine's Fittings page, or empty for other doctrines. |
| `doctrine_ships` | Hull of every fit of a Fittings doctrine in doctrine order, joined with ` > ` (for example `Moa > Moa > Osprey`). |
| `doctrine_fits` | Fit names of a Fittings doctrine, joined with ` > `. |
| `comms` | Comms preset name. |
| `comms_channel` | Channel name of the comms preset. |
| `comms_link` | Link that opens the comms: the join page for voice client links, the link itself for `http` and `https` links. |
| `comms_voice_url` | The voice link of the preset as stored, without a user name or password. |
| `srp_url` | Full address of the fleet's SRP link (FleetOps SRP page of the fleet, or the link of aa-srp or Alliance Auth SRP); empty while the fleet has none, also in the Start Fleet preview. The SRP step runs before the ping and the MOTD, so the address is available in both. |
| `logi_channel` | Name of the Logi channel. |
| `logi_channel_link` | The Logi channel as an in-game link (`<a href="joinChannel:…">Name</a>`), or the plain name when the channel has no in-game link. |
| `boost_channel` | Name of the Boost channel. |
| `boost_channel_link` | The Boost channel as an in-game link, or the plain name. |
| `fleet_time` | `YYYY-MM-DD HH:MM EVE` for a pre-pinged fleet, otherwise `Now` (in the site language). To test for a pre ping, use `is_pre_ping`. |
| `fleet_time_eve` | `YYYY-MM-DD HH:MM EVE` for a pre-pinged fleet, otherwise empty. |
| `fleet_time_unix` | Unix time of the fleet time, or empty. |
| `fleet_time_discord` | Discord relative timestamp `<t:…:R>`, shown in each reader's own time zone, or empty. |
| `is_pre_ping` | True for a pre ping. Use it with `{% if %}`. |
| `sent_at` | Time the message was rendered, `YYYY-MM-DD HH:MM` in EVE time. |
| `additional_message` | The additional information the FC typed. |
| `operation_id` | The ID of the FleetOps fleet record. |

## Mentions

Only the mention FleetOps adds can notify anybody. The template sees `ping_mention` as a placeholder, every
other `@everyone` or `@here` in the rendered ping is broken with a zero-width space, and Discord is told which
mention is allowed. A template that writes `@everyone` by hand does not ping anybody.

If a template does not contain `{{ ping_mention }}` (or `{{ ping_target }}`) while a mention is selected, the
preview warns that nobody will be notified.

## Formatting and limits

- Ping: Discord Markdown (`**bold**`, `[text](link)`, `-# small text`). Discord accepts at most 2000
  characters (counted the way Discord counts them). A ping over the limit is refused before anything is posted
  to Discord; a ping that is only copied gets a warning. The counter on **Start Fleet** shows the length.
  Link previews are suppressed.
- MOTD: template output is not HTML-escaped. The game reads a small set of HTML-like tags (`<b>`, `<br>`,
  `<font color="#bfffffff">`, `<a href="…">`). `{{ additional_message|linebreaksbr }}` keeps the line breaks the
  FC typed. `<script` is escaped. A MOTD longer than 1000 characters only gets a warning, because the game may
  cut a longer one.
- Links to comms and doctrines are built from `SITE_URL`. Without it they are site paths that do not open
  outside Alliance Auth, and the preview warns about it.

## Built-in templates

The built-in **Default Ping**:

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

The built-in **Default MOTD** (one line):

```django
<font color="#bfffffff"><b>{{ fleet_type }}</b><br>FC: {{ fc }}<br>Form Up: {{ formup }}{% if doctrine_name %}<br>Doctrine: {% if doctrine_url %}<a href="{{ doctrine_url }}">{{ doctrine_name }}</a>{% else %}{{ doctrine_name }}{% endif %}{% if doctrine_ships %} ({{ doctrine_ships }}){% endif %}{% endif %}{% if comms %}<br>Comms: {% if comms_link %}<a href="{{ comms_link }}">{{ comms }}</a>{% else %}{{ comms }}{% endif %}{% endif %}{% if logi_channel %}<br>Logi: {{ logi_channel_link }}{% endif %}{% if boost_channel %}<br>Boost: {{ boost_channel_link }}{% endif %}{% if srp_url %}<br>SRP: <a href="{{ srp_url }}">Request SRP</a>{% endif %}{% if additional_message %}<br><br>{{ additional_message|linebreaksbr }}{% endif %}</font>
```

`python manage.py fleetops_seed` creates both as "Default Ping" and "Default MOTD" if they do not exist yet, and
never overwrites a template you edited. Custom templates need `{{ srp_url }}` added by hand if they should carry
the SRP link.

When a scheduled fleet is cancelled, FleetOps can post a notice in the same channel as the pre ping. The notice
comes from a fixed text (`**Cancelled:** <fleet type> at <fleet time> (<form-up>)`) and mentions nobody; it is
not a template you edit.

## Language

Pings and MOTDs are written in the site language (`LANGUAGE_CODE` of the Alliance Auth settings), never in
the language of the FC who clicked, so every reader gets the same text and the preview equals what is sent. The
templates are text you write and are not translated. Only the words FleetOps adds itself follow the site
language: the target words in `ping_target_label` (`all`, `online members`, `this channel`), the words `Now`
(`fleet_time`) and `None / Custom` (`doctrine`), and the cancellation notice. On a site that runs in Chinese
these words are Chinese, so a condition such as `{% if fleet_time == "Now" %}` is never true there. Use
`{% if is_pre_ping %}` and `{% if doctrine_name %}` in conditions instead of comparing with these words.
