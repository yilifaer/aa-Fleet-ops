# Translations

FleetOps is available in English and Simplified Chinese. Other languages can be added; this page explains how
the translations work, how to update one, how to add one and which terms the Chinese translation uses.

## How the language is chosen

FleetOps has no language setting of its own. Alliance Auth activates the language of the logged-in user (the
language menu of Alliance Auth), and FleetOps shows its pages in that language: menus, pages, forms, messages,
exports and the texts of the JavaScript. Visitors who are not logged in (the public comms join page) get the
language of their browser when FleetOps has it. A language without a FleetOps catalog shows English text inside
the translated Alliance Auth pages.

Some texts do not follow the viewer:

- **Discord pings and in-game MOTDs** are always English, whatever `LANGUAGE_CODE` of the Alliance Auth settings
  says: FleetOps's own words in them are never translated, and text that administrators write in the templates
  is sent as written. Everybody reads the same text and the preview equals what is sent. Translators do not
  translate anything that only goes into a ping or a MOTD.
- **Messages that code stores for later** (results of start steps, why a fleet ended, ESI errors, automatic SRP
  decisions) are stored in English and translated when they are shown, in the language of whoever looks at them.
  Text written before a message was known to FleetOps, text typed by users and text that came from ESI is shown
  as it was stored. The step texts of fleets recorded by 0.1.0a6 and earlier are converted to the stored forms
  when the site is upgraded, so they are translated like newer ones.
- **Not translated at all**: data that administrators enter (fleet types, presets, templates, role names),
  Django permission names, log lines, the output of management commands, ship and solar system names, and the
  audit action codes.

## Tools

The catalogs are standard Django gettext files in `fleetops/locale/<locale>/LC_MESSAGES/`: `django.po` for
Python and templates, `djangojs.po` for JavaScript, and the compiled `.mo` files next to them. The `.mo` files
are committed, because sites that install from a Git URL do not compile anything.

You need the GNU gettext tools (`sudo apt-get install -y gettext` on Debian and Ubuntu, `brew install gettext`
on macOS) and Django. Run the commands in a checkout of the repository.

## Update a translation

Run `makemessages` inside the `fleetops` folder, without a settings module. With a settings module Django
writes into the `LOCALE_PATHS` of Alliance Auth instead of FleetOps.

```bash
cd fleetops
env -u DJANGO_SETTINGS_MODULE django-admin makemessages -l zh_Hans --no-obsolete --no-wrap --add-location file -i migrations
env -u DJANGO_SETTINGS_MODULE django-admin makemessages -l zh_Hans -d djangojs --no-obsolete --no-wrap --add-location file
```

Then translate every entry that has an empty `msgstr` in `locale/zh_Hans/LC_MESSAGES/django.po` and
`djangojs.po`, and compile:

```bash
env -u DJANGO_SETTINGS_MODULE django-admin compilemessages -l zh_Hans
```

`--no-wrap` and `--add-location file` keep the diffs small. From the repository root,
`python tools/check_translations.py` runs both `makemessages` commands for every language and lists the
entries that are still untranslated or marked fuzzy. It also compares each committed `.mo` with its `.po` and
reports a catalog that was not compiled. The same check runs in CI; it only warns on branches and
pull requests and stops a release.

Rules for the entries:

- Keep every placeholder exactly: `%(name)s`, `%s`, `{name}`. Do not rename or drop one.
- Keep HTML tags, their attributes and the link addresses; translate only the text between them.
- Keep codenames, setting names in code format and the EVE abbreviations (PAP, SRP, FC, MOTD, ESI, ISK, UTC).
- `msgctxt` tells you where a short word is used ("fleet status" or "enabled" for "Active"); translate for that
  meaning.
- Common words such as "Name", "Yes", "No", "Corporation", "Alliance", "Dashboard" and "Doctrine" have the
  context `FleetOps` (`pgettext("FleetOps", "Name")` in Python, `{% translate "Name" context "FleetOps" %}` in
  templates). Django merges the catalogs of all installed apps, and when two apps translate the same text
  without a context, the app listed first in `INSTALLED_APPS` wins. FleetOps is listed after Alliance Auth's
  own apps, so without the context their wording would replace the FleetOps wording. Give every new
  short, common word the context too; `tests/test_i18n_catalogs.py` fails when an installed app translates a
  FleetOps message without a context differently.
- Plural entries have one `msgstr[n]` per plural form of the language.
- Remove `fuzzy` flags only after you checked the translation.
- The header holds the project, the language and the plural rule. It has no personal names or e-mail addresses.

`tests/test_i18n_catalogs.py` checks all of this: no empty or fuzzy entry, equal placeholders and HTML tags in
the original and the translation, and a compiled `.mo` that equals the `.po`. A forgotten `compilemessages`
fails the suite.

## Add a language

1. Look up the locale folder name of the language in the table below. It is the Alliance Auth language code with
   an underscore and the usual capitalisation (`ko-kr` becomes `ko_KR`).
2. Run both `makemessages` commands above with `-l <locale>`. Django creates the folder
   `fleetops/locale/<locale>/LC_MESSAGES/`.
3. Fix the header: `Language: <locale>` and the `Plural-Forms` line of the language (see the table).
4. Translate every entry in `django.po` and `djangojs.po`, and compile with `compilemessages -l <locale>`.
   The suite fails on empty and fuzzy entries, so a language is merged when it is complete.
5. Run the tests, add the language to the table below and to the README, and open a pull request. If you cannot
   use Git, send the `.po` files in a translation issue.

| Alliance Auth code | Locale folder | Language | Plural-Forms |
|---|---|---|---|
| `en` | (source language) | English | |
| `cs-cz` | `cs_CZ` | Czech (not enabled in Alliance Auth yet) | `nplurals=3; plural=(n==1) ? 0 : (n>=2 && n<=4) ? 1 : 2;` |
| `de` | `de` | German | `nplurals=2; plural=(n != 1);` |
| `es` | `es` | Spanish | `nplurals=2; plural=(n != 1);` |
| `fr-fr` | `fr_FR` | French | `nplurals=2; plural=(n > 1);` |
| `it-it` | `it_IT` | Italian | `nplurals=2; plural=(n != 1);` |
| `ja` | `ja` | Japanese | `nplurals=1; plural=0;` |
| `ko-kr` | `ko_KR` | Korean | `nplurals=1; plural=0;` |
| `nl-nl` | `nl_NL` | Dutch | `nplurals=2; plural=(n != 1);` |
| `pl-pl` | `pl_PL` | Polish | `nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 \|\| n%100>=20) ? 1 : 2);` |
| `ru` | `ru` | Russian | `nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 \|\| n%100>=20) ? 1 : 2);` |
| `uk` | `uk` | Ukrainian | `nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 \|\| n%100>=20) ? 1 : 2);` |
| `zh-hans` | `zh_Hans` | Simplified Chinese | `nplurals=1; plural=0;` |

The Plural-Forms values are the common ones; check Django's own catalog of the language
(`django/conf/locale/<locale>/LC_MESSAGES/django.po`) if in doubt. Traditional Chinese browsers get the
Simplified Chinese translation on public pages, because Django falls back to the nearest language it has.

## Try a translation

1. Compile the catalogs (`compilemessages`).
2. Restart the Alliance Auth web process, so that it reads the new `.mo` files. (A development server reloads
   them by itself when a file changes.)
3. Choose the language in the Alliance Auth language menu and open the FleetOps pages. The browser keeps the
   JavaScript catalog for a day, under an address that carries the language and the FleetOps version. After you
   change a JavaScript text, reload the page with the cache bypassed (Ctrl+F5) to see it at once.
4. Walk through Start Fleet, a fleet page with the End Fleet dialog, the PAP pages, the statistics, the
   exports, the administration and SRP. Texts that are still English are either missing from the catalog (run
   `makemessages` again) or are one of the texts listed under "Not translated at all" above.

## Change wording on one site

A site can change any text without touching FleetOps. Create a folder for your own catalogs, add it to
`LOCALE_PATHS` in `local.py`, and put the messages you want to change in
`<folder>/<locale>/LC_MESSAGES/django.po`, compiled to `django.mo`. Django prefers the catalogs in
`LOCALE_PATHS` over the catalogs of installed apps, so only the messages you list change, and they are not
overwritten when FleetOps is upgraded.

```python
LOCALE_PATHS = (os.path.join(BASE_DIR, "my_locale"),) + tuple(LOCALE_PATHS)
```

## Chinese style and glossary

The Simplified Chinese translation follows these rules, and so do the Chinese documents.

- Simplified Chinese. Put one space between Chinese characters and Latin letters or digits ("已累计 15 分钟").
- Use Chinese punctuation in sentences. Keep placeholders, HTML tags, codenames, setting names and EVE
  abbreviations unchanged.
- Use the terms of the table. They are the words EVE players use, not the words of Alliance Auth's own
  catalog (which is not consistent: it calls a corporation 公司 in one place and 军团 in another).

| English | Chinese | English | Chinese |
|---|---|---|---|
| Fleet | 舰队 | Corporation / Alliance | 军团 / 联盟 |
| FC / Fleet boss | FC / Boss | Main / Alt | 大号 / 小号 |
| Fleet commander (position) | 舰队指挥官 | Character / Member | 角色 / 成员 |
| Wing / Squad | 联队 / 中队 | Special role | 特殊角色 |
| Wing commander / Squad commander / Squad member | 联队指挥官 / 中队指挥官 / 中队队员 | FC points | FC 积分 |
| Start Fleet / End Fleet | 开舰队 / 结束舰队 | FC incentive | FC 奖金 |
| Fleet type | 舰队类型 | Waive / Finalize / Unlock / Recalculate | 放弃领取 / 定稿 / 解锁 / 重新计算 |
| Doctrine | 教义 | Statistics / Target | 统计 / 达标线 |
| Form-up | 集结地 | Leaderboard / Inactive | 排行榜 / 不活跃 |
| Comms | 语音 | Export | 导出 |
| Logi channel / Boost channel | 后勤频道 / 加成频道 | Kick / Kick capsules | 踢出 / 一键踢蛋 |
| Ping / Pre-ping | Ping / 预 Ping | Solar system | 星系 |
| MOTD | MOTD | Ship / Ship type | 舰船 / 船型 |
| PAP / PAP multiplier | PAP / PAP 倍率 | Tracking | 追踪 |
| PAP link | PAP 登记链接 | Audit log | 操作日志 |
| Minimum time in fleet | 最低在队时长 | Administration / Settings | 后台设置 / 设置 |
| Manual fleet / Manual PAP | 补录舰队 / 补录 PAP | EVE time | EVE 时间 |
| Pending review / Approve / Reject | 待审核 / 通过 / 拒绝 | Dashboard | 总览 |
| SRP / Claim / Paid | SRP / 补损申请 / 已付款 | Killmail | KM |
| Group / Group leader | 小组 / 组长 | Send Ping | 发送 Ping |
| Group fleet / Alliance fleet | 小组舰队 / 联盟舰队 | Resubmit for approval | 重新提交审批 |
| Command deck | 指挥台 | PAP multiplier "%(multiplier)sx" | %(multiplier)s 倍 |
| Ended manually | 手动结束 | Fleet boss handed over | 因 Boss 移交而结束 |
| Fleet no longer exists or the FC left it | 舰队已解散或 FC 已离队 | Webhook / Template | Webhook / 模板 |

Ship names and solar system names stay in English. Lists are separated with "、", and relative times have no space
before 后 and 前 ("3 小时后").
