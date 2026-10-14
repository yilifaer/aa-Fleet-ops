// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// The kick list window of the fleet page: it lists the members a kick would remove and lets the FC untick
// the ones to spare. The page starts it from the buttons of the Fleet Control card (window.FleetOpsKick).
// Names come from ESI, so they are only ever set as text.
(() => {
  'use strict';

  const modal = document.getElementById('kickListModal');
  const form = document.getElementById('kick-list-form');
  if (!modal || !form) {
    return;
  }
  const systemsBox = document.getElementById('kick-list-systems');
  const membersBox = document.getElementById('kick-list-members');
  const status = document.getElementById('kick-list-status');
  const summary = document.getElementById('kick-list-summary');
  const submit = document.getElementById('kick-list-submit');
  const title = document.getElementById('kick-list-title');
  let mode = 'system';
  let systemName = '';

  function element(tag, attrs, text) {
    const node = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  function updateSummary() {
    const count = membersBox.querySelectorAll('input[name="character_ids"]:checked').length;
    summary.textContent = mode === 'capsules'
      ? interpolate(ngettext('Kick %(count)s capsule pilot', 'Kick %(count)s capsule pilots', count), { count }, true)
      : interpolate(
        ngettext('Kick %(count)s member outside %(system)s', 'Kick %(count)s members outside %(system)s', count),
        { count, system: systemName },
        true,
      );
    submit.textContent = interpolate(gettext('Kick %(count)s'), { count }, true);
    submit.disabled = count === 0;
  }

  function render(data) {
    systemsBox.replaceChildren();
    membersBox.replaceChildren();
    if (mode === 'system') {
      systemsBox.append(element('div', { class: 'fw-bold mb-1' }, gettext('Members in this system stay in the fleet')));
      data.systems.forEach((system) => {
        const id = `kick-system-${system.id}`;
        const input = element('input', { class: 'form-check-input', type: 'radio', name: 'system_id', value: system.id, id });
        if (system.id === data.selected_system_id) {
          input.checked = true;
          systemName = system.name;
        }
        input.addEventListener('change', () => load(system.id));
        const label = system.id === data.fc_system_id
          ? interpolate(gettext("%(name)s (%(count)s) — FC's system, the default"), { name: system.name, count: system.count }, true)
          : `${system.name} (${system.count})`;
        const row = element('div', { class: 'form-check' });
        row.append(input, element('label', { class: 'form-check-label', for: id }, label));
        systemsBox.append(row);
      });
    }
    if (data.members.length) {
      status.textContent = gettext('Untick the members to spare.');
    } else {
      status.textContent = mode === 'capsules' ? gettext('No capsule pilots to kick.') : gettext('Nobody is outside this system.');
    }
    data.members.forEach((member) => {
      const id = `kick-target-${member.character_id}`;
      const input = element('input', { class: 'form-check-input', type: 'checkbox', name: 'character_ids', value: member.character_id, id });
      input.checked = true;
      input.addEventListener('change', updateSummary);
      const details = [member.corporation, member.ship, member.system].filter(Boolean).join(' · ');
      const row = element('div', { class: 'form-check' });
      row.append(input, element('label', { class: 'form-check-label', for: id }, details ? `${member.name} — ${details}` : member.name));
      membersBox.append(row);
    });
    updateSummary();
  }

  function load(systemId) {
    status.textContent = gettext('Reading the fleet…');
    submit.disabled = true;
    const params = new URLSearchParams({ mode });
    if (systemId) {
      params.set('system_id', systemId);
    }
    fetch(`${modal.dataset.previewUrl}?${params}`, { credentials: 'same-origin', headers: { 'X-Requested-With': 'XMLHttpRequest' } })
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then(render)
      .catch(() => {
        membersBox.replaceChildren();
        status.textContent = gettext('The fleet could not be read. Close this window and try again.');
      });
  }

  function open(newMode) {
    mode = newMode;
    form.action = mode === 'capsules' ? modal.dataset.capsulesUrl : modal.dataset.systemUrl;
    title.textContent = mode === 'capsules' ? gettext('Kick capsule pilots') : gettext('Kick members outside a system');
    systemsBox.replaceChildren();
    membersBox.replaceChildren();
    summary.textContent = '';
    window.bootstrap.Modal.getOrCreateInstance(modal).show();
    load(null);
  }

  // Without the list window the names are confirmed in a plain dialog.
  function confirmText(names) {
    return interpolate(gettext('Kick these capsule pilots from the fleet? %(names)s'), { names }, true);
  }

  form.addEventListener('submit', () => {
    submit.disabled = true;
    submit.textContent = gettext('Kicking…');
  });

  window.FleetOpsKick = { open, confirmText };
})();
