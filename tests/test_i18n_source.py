# SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Source guard: text that people read goes through the translation functions.

The guard reads every module of the package (not the migrations and the management commands, whose
text stays English) and reports the patterns that leave a sentence out of the catalogs:

* a flash message, a form label, a help text or a validation error written as a plain string;
* a translated string that is built with an f-string, ``%`` or ``.format()`` inside the call, which no
  catalog entry can match;
* a translation function imported under a name other than ``_``, which ``makemessages`` does not know.
"""

import ast
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

import fleetops

PACKAGE_DIR = Path(fleetops.__file__).resolve().parent

MESSAGE_LEVELS = {"debug", "info", "success", "warning", "error"}
LABEL_KEYWORDS = {"label", "help_text", "verbose_name", "verbose_name_plural", "empty_label", "short_description"}
# The positions of the message arguments of each translation function (the context of pgettext comes first).
MESSAGE_ARGUMENTS = {
    "_": (0,),
    "gettext": (0,),
    "gettext_lazy": (0,),
    "gettext_noop": (0,),
    "ngettext": (0, 1),
    "ngettext_lazy": (0, 1),
    "pgettext": (1,),
    "pgettext_lazy": (1,),
    "npgettext": (1, 2),
    "npgettext_lazy": (1, 2),
}

# Text that is not for the person in front of the page, with the reason. Keys are "path:function".
ALLOWED = {}


def _name(node):
    """The called name of ``node``: ``_`` for ``_(...)`` and ``gettext`` for ``translation.gettext(...)``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_text(node):
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _is_words(node):
    """A literal or f-string that holds text people read: not empty and not just markup or an identifier."""
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip() != ""


def _is_built(node):
    """A string made at run time inside the call: an f-string, ``"..." % x`` or ``"...".format(...)``."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return True
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format"


def _function_names(tree):
    """``{node: name of the function it sits in}`` for every node of ``tree``."""
    owners = {}

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            child_owner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            owners[child] = child_owner
            visit(child, child_owner)

    visit(tree, "")
    return owners


def violations(source, filename="module.py"):
    """The problems of one module as ``"file:line: what"`` strings."""
    tree = ast.parse(source)
    owners = _function_names(tree)
    found = []

    def report(node, what):
        key = f"{filename}:{owners.get(node, '')}"
        if key not in ALLOWED:
            found.append(f"{filename}:{node.lineno}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "django.utils.translation":
            for alias in node.names:
                if alias.asname not in (None, "_"):
                    report(node, f"{alias.name} is imported as {alias.asname}; makemessages only knows `_`")
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        if (
            name in MESSAGE_LEVELS
            and isinstance(node.func, ast.Attribute)
            and _name(node.func.value) == "messages"
            and len(node.args) >= 2
            and _is_text(node.args[1])
        ):
            report(node, f"messages.{name}() with a plain string; wrap it in _()")
        if name == "add_error" and len(node.args) >= 2 and _is_text(node.args[1]):
            report(node, "add_error() with a plain string; wrap it in _()")
        if name == "message_user" and len(node.args) >= 2 and _is_text(node.args[1]):
            report(node, "message_user() with a plain string; wrap it in _()")
        if name == "ValidationError":
            for argument in node.args[:1]:
                if _is_words(argument):
                    report(node, "ValidationError() with a plain string; wrap it in _()")
                if isinstance(argument, ast.Dict):
                    for value in argument.values:
                        if _is_words(value):
                            report(node, "ValidationError() with a plain string; wrap it in _()")
        for keyword in node.keywords:
            if keyword.arg in LABEL_KEYWORDS and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str) and keyword.value.value.strip():
                    report(node, f"{keyword.arg}= is a plain string; wrap it in _()")
        if name in MESSAGE_ARGUMENTS:
            for index in MESSAGE_ARGUMENTS[name]:
                if index < len(node.args) and _is_built(node.args[index]):
                    report(node, f"{name}() is given a built string; use a fixed message with %(name)s")
    return found


def package_files():
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        parts = path.relative_to(PACKAGE_DIR).parts
        if "migrations" in parts or "management" in parts:
            continue
        yield path


class SourceGuardTests(SimpleTestCase):
    def test_every_module_keeps_its_text_in_the_catalog(self):
        problems = []
        for path in package_files():
            relative = path.relative_to(PACKAGE_DIR).as_posix()
            problems.extend(violations(path.read_text(encoding="utf-8"), relative))
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_the_allowed_entries_still_exist(self):
        # An entry for code that is gone would hide nothing but would stay in the file for ever.
        existing = set()
        for path in package_files():
            relative = path.relative_to(PACKAGE_DIR).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            existing.update(
                f"{relative}:{node.name}"
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        self.assertEqual(sorted(set(ALLOWED) - existing), [])


class GuardRulesTests(SimpleTestCase):
    """The guard finds what it is meant to find, and leaves translated text alone."""

    def check(self, source):
        return violations(source, "example.py")

    def test_a_plain_flash_message_is_reported(self):
        self.assertEqual(len(self.check('messages.success(request, "Saved.")')), 1)
        self.assertEqual(len(self.check('messages.error(request, f"Failed: {error}")')), 1)

    def test_a_translated_flash_message_is_fine(self):
        self.assertEqual(self.check('messages.success(request, _("Saved."))'), [])
        self.assertEqual(self.check('messages.error(request, _("Failed: %(error)s") % {"error": error})'), [])
        self.assertEqual(self.check("messages.error(request, text)"), [])

    def test_form_and_admin_messages_are_reported(self):
        self.assertEqual(len(self.check('self.add_error("name", "Taken.")')), 1)
        self.assertEqual(len(self.check('self.add_error(None, f"Taken: {name}")')), 1)
        self.assertEqual(self.check('self.add_error("name", _("Taken."))'), [])
        self.assertEqual(self.check("self.add_error(None, message)"), [])
        self.assertEqual(len(self.check('self.message_user(request, "Done.")')), 1)
        self.assertEqual(self.check('self.message_user(request, _("Done."))'), [])

    def test_labels_help_texts_and_verbose_names_are_reported(self):
        for keyword in ("label", "help_text", "verbose_name", "verbose_name_plural", "empty_label", "short_description"):
            self.assertEqual(len(self.check(f'Field({keyword}="Name")')), 1, keyword)
            self.assertEqual(self.check(f'Field({keyword}=_("Name"))'), [], keyword)

    def test_an_empty_label_is_not_text(self):
        self.assertEqual(self.check('Field(label="")'), [])
        self.assertEqual(self.check("Field(label=None)"), [])

    def test_a_validation_error_with_a_plain_string_is_reported(self):
        self.assertEqual(len(self.check('raise ValidationError("Not valid.")')), 1)
        self.assertEqual(len(self.check('raise forms.ValidationError(f"Not valid: {value}")')), 1)
        self.assertEqual(len(self.check('raise ValidationError({"name": "Not valid."})')), 1)
        self.assertEqual(self.check('raise ValidationError(_("Not valid."), code="invalid")'), [])
        self.assertEqual(self.check("raise ValidationError(message)"), [])

    def test_a_translated_string_that_is_built_is_reported(self):
        self.assertEqual(len(self.check('_(f"Hello {name}")')), 1)
        self.assertEqual(len(self.check('_("Hello %s" % name)')), 1)
        self.assertEqual(len(self.check('gettext("Hello {}".format(name))')), 1)
        self.assertEqual(len(self.check('ngettext(f"{n} pilot", "pilots", n)')), 1)
        self.assertEqual(len(self.check('pgettext("ctx", f"Hello {name}")')), 1)
        self.assertEqual(self.check('_("Hello %(name)s") % {"name": name}'), [])
        self.assertEqual(self.check('ngettext("%(count)s pilot", "%(count)s pilots", n) % {"count": n}'), [])

    def test_an_alias_other_than_underscore_is_reported(self):
        self.assertEqual(len(self.check("from django.utils.translation import gettext_lazy as lazy")), 1)
        self.assertEqual(self.check("from django.utils.translation import gettext_lazy as _"), [])
        self.assertEqual(self.check("from django.utils.translation import gettext, gettext_lazy"), [])

    def test_an_allowed_function_is_left_alone(self):
        source = 'def shown_elsewhere():\n    messages.error(request, "Plain")\n'
        with mock.patch.dict(ALLOWED, {"example.py:shown_elsewhere": "reason"}):
            self.assertEqual(violations(source, "example.py"), [])
        self.assertEqual(len(violations(source, "example.py")), 1)


class ModelLabelTests(SimpleTestCase):
    """Every field, model and choice of the models can be translated."""

    @staticmethod
    def models():
        from django.apps import apps

        return list(apps.get_app_config("fleetops").get_models())

    def test_every_field_has_a_translatable_name(self):
        from django.utils.functional import Promise

        plain = [
            f"{model.__name__}.{field.name}"
            for model in self.models()
            for field in model._meta.get_fields()
            if getattr(field, "concrete", False)
            and not isinstance(field.verbose_name, Promise)
            and not (field.primary_key and field.auto_created)
        ]
        self.assertEqual(plain, [])

    def test_every_model_has_a_translatable_name(self):
        from django.utils.functional import Promise

        plain = [
            model.__name__
            for model in self.models()
            if not (
                isinstance(model._meta.verbose_name, Promise) and isinstance(model._meta.verbose_name_plural, Promise)
            )
        ]
        self.assertEqual(plain, [])

    def test_help_texts_and_choices_can_be_translated(self):
        from django.utils.functional import Promise

        plain = []
        for model in self.models():
            for field in model._meta.get_fields():
                if not getattr(field, "concrete", False):
                    continue
                if field.help_text and not isinstance(field.help_text, Promise):
                    plain.append(f"{model.__name__}.{field.name} help text")
                for value, label in field.flatchoices:
                    if not isinstance(label, Promise):
                        plain.append(f"{model.__name__}.{field.name} choice {value!r}")
        self.assertEqual(plain, [])


class FormLabelTests(SimpleTestCase):
    """Every visible field of the FleetOps forms has a label that can be translated."""

    def test_every_visible_field_has_a_translatable_label(self):
        import importlib
        import inspect

        from django import forms
        from django.utils.functional import Promise

        plain = []
        for module_name in (
            "forms",
            "forms_config",
            "forms_fleet",
            "forms_links",
            "forms_pap",
            "forms_presets",
            "forms_records",
            "forms_srp",
        ):
            module = importlib.import_module(f"fleetops.{module_name}")
            for name, form_class in inspect.getmembers(module, inspect.isclass):
                if not issubclass(form_class, forms.BaseForm) or form_class.__module__ != module.__name__:
                    continue
                for field_name, field in form_class.base_fields.items():
                    if field.widget.is_hidden:
                        continue
                    if not isinstance(field.label, Promise):
                        plain.append(f"{module_name}.{name}.{field_name}")
        self.assertEqual(plain, [])
