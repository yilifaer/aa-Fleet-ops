// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Fleet tab: renaming, drag and drop moves, member menus and the selection bar.
//
// Every change is a POST of a form[data-fleet-action]; the shared action handler of fleet_live.js
// (window.FleetOpsFleet) sends it and shows the answer. Moves go through the hidden
// #fleet-move-form, filled here from a drop, a member menu entry or the Move to… dialog.
(() => {
  const root = document.querySelector('[data-fleet-root]');
  if (!root || root.dataset.canManage !== '1') {
    return;
  }

  const COMMANDER_ROLES = ['fleet_commander', 'wing_commander', 'squad_commander'];
  const SQUAD_ROLES = ['squad_commander', 'squad_member'];
  // A new wing or squad is looked for in the redrawn tree this long after "+" was clicked.
  const ADD_WAIT_MS = 30000;

  const fleet = () => window.FleetOpsFleet || null;
  const tree = () => document.getElementById('fleet-tree');

  function flash(message, level) {
    const api = fleet();
    if (api && typeof api.flash === 'function') {
      api.flash(message, level);
      return;
    }
    const box = document.getElementById('fleet-flash');
    if (!box) {
      return;
    }
    const alert = document.createElement('div');
    alert.className = `alert alert-${level === 'error' ? 'danger' : level || 'info'} py-2`;
    alert.textContent = message;
    box.replaceChildren(alert);
  }

  function setBusy(key, on) {
    const api = fleet();
    if (api && typeof api.setBusy === 'function') {
      api.setBusy(key, on);
    }
  }

  // Ids are digits; anything else never reaches a selector.
  const isId = (value) => /^\d+$/.test(String(value || ''));

  function submit(form) {
    const button = form.querySelector('button[type="submit"]');
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit(button || undefined);
      return;
    }
    const event = new Event('submit', { bubbles: true, cancelable: true });
    if (form.dispatchEvent(event)) {
      form.submit();
    }
  }

  // ---------------------------------------------------------------- moves

  function submitMove(ids, role, wingId, squadId) {
    const form = document.getElementById('fleet-move-form');
    const characterIds = ids.filter(isId);
    if (!form || !characterIds.length || !role) {
      return;
    }
    if (COMMANDER_ROLES.includes(role) && characterIds.length !== 1) {
      flash(gettext('Only one member can take a commander position. Choose a single member.'), 'warning');
      return;
    }
    form.querySelectorAll('input[name="character_ids"]').forEach((input) => input.remove());
    characterIds.forEach((id) => {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'character_ids';
      input.value = id;
      form.appendChild(input);
    });
    form.querySelector('input[name="role"]').value = role;
    form.querySelector('input[name="wing_id"]').value = role === 'fleet_commander' ? '' : wingId || '';
    form.querySelector('input[name="squad_id"]').value = SQUAD_ROLES.includes(role) ? squadId || '' : '';
    submit(form);
    if (characterIds.length > 1) {
      // The selection has been used; the job's progress shows above the tree.
      clearSelection();
    }
  }

  function memberRow(id) {
    const fleetTree = tree();
    return isId(id) && fleetTree ? fleetTree.querySelector(`li.fleet-member[data-character-id="${id}"]`) : null;
  }

  function memberName(row) {
    const name = row && row.querySelector('.fleet-member-name');
    return name ? name.textContent.trim() : '';
  }

  // Whether the member in ``row`` already holds this position.
  function isAt(row, role, wingId, squadId) {
    if (!row || row.dataset.role !== role) {
      return false;
    }
    if (role === 'fleet_commander') {
      return true;
    }
    if (role === 'wing_commander') {
      return row.dataset.wingId === String(wingId);
    }
    return row.dataset.squadId === String(squadId);
  }

  // The member holding a commander position, from the tree.
  function holder(role, wingId, squadId) {
    const fleetTree = tree();
    if (!fleetTree) {
      return null;
    }
    let selector = `li.fleet-member[data-role="${role}"]`;
    if (role === 'wing_commander') {
      selector += `[data-wing-id="${wingId}"]`;
    } else if (role === 'squad_commander') {
      selector += `[data-squad-id="${squadId}"]`;
    }
    return fleetTree.querySelector(selector);
  }

  function selectedIds() {
    const boxes = root.querySelectorAll('input.fleet-select:checked');
    return Array.from(new Set(Array.from(boxes, (box) => box.value).filter(isId)));
  }

  // ---------------------------------------------------------------- Move to… dialog

  const moveModal = document.getElementById('fleet-move-modal');
  let moveIds = [];

  function showModal(element) {
    const bootstrap = window.bootstrap;
    if (bootstrap && bootstrap.Modal) {
      bootstrap.Modal.getOrCreateInstance(element).show();
      return;
    }
    element.style.display = 'block';
    element.classList.add('show');
    element.removeAttribute('aria-hidden');
    element.setAttribute('aria-modal', 'true');
  }

  function hideModal(element) {
    const bootstrap = window.bootstrap;
    if (bootstrap && bootstrap.Modal) {
      bootstrap.Modal.getOrCreateInstance(element).hide();
      return;
    }
    element.classList.remove('show');
    element.style.display = 'none';
    element.setAttribute('aria-hidden', 'true');
    element.removeAttribute('aria-modal');
  }

  function nodeName(node) {
    const name = node.querySelector(':scope > .fleet-node-head .fleet-node-name');
    return name ? name.textContent.trim() : '';
  }

  function targetButton(label, role, wingId, squadId, rows) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm btn-outline-primary';
    button.dataset.moveRole = role;
    button.dataset.moveWingId = wingId || '';
    button.dataset.moveSquadId = squadId || '';
    button.textContent = label;
    const single = rows.length === 1 ? rows[0] : null;
    if (single && isAt(single, role, wingId, squadId)) {
      button.disabled = true;
      button.textContent = interpolate(gettext('%(label)s (now)'), { label }, true);
    } else if (COMMANDER_ROLES.includes(role)) {
      const current = holder(role, wingId, squadId);
      if (current && (!single || current !== single)) {
        button.disabled = true;
        button.title = interpolate(gettext('Taken by %(name)s'), { name: memberName(current) || gettext('another member') }, true);
      }
    }
    return button;
  }

  function targetLine(label, buttons, indent) {
    const line = document.createElement('div');
    line.className = `d-flex flex-wrap align-items-center gap-2 py-1${indent ? ' ps-4' : ''}`;
    const text = document.createElement('span');
    text.className = 'me-auto';
    text.textContent = label;
    line.append(text, ...buttons);
    return line;
  }

  function buildTargets(container, ids) {
    const rows = ids.map(memberRow).filter(Boolean);
    const single = ids.length === 1;
    const lines = [];
    if (single) {
      lines.push(targetLine(gettext('Fleet'), [targetButton(gettext('Fleet commander'), 'fleet_commander', '', '', rows)], false));
    }
    const fleetTree = tree();
    const wings = fleetTree ? fleetTree.querySelectorAll('li.fleet-node[data-node="wing"]') : [];
    wings.forEach((wing) => {
      const wingId = wing.dataset.wingId;
      if (!isId(wingId)) {
        return;
      }
      const wingButtons = single ? [targetButton(gettext('Wing commander'), 'wing_commander', wingId, '', rows)] : [];
      const wingLine = targetLine(nodeName(wing) || interpolate(gettext('Wing %(id)s'), { id: wingId }, true), wingButtons, false);
      wingLine.classList.add('fw-semibold', 'border-top');
      lines.push(wingLine);
      wing.querySelectorAll('li.fleet-node[data-node="squad"]').forEach((squad) => {
        const squadId = squad.dataset.squadId;
        if (!isId(squadId)) {
          return;
        }
        const buttons = [targetButton(gettext('Member'), 'squad_member', wingId, squadId, rows)];
        if (single) {
          buttons.push(targetButton(gettext('Commander'), 'squad_commander', wingId, squadId, rows));
        }
        lines.push(targetLine(nodeName(squad) || interpolate(gettext('Squad %(id)s'), { id: squadId }, true), buttons, true));
      });
    });
    if (!wings.length) {
      const empty = document.createElement('p');
      empty.className = 'text-muted mb-0';
      empty.textContent = gettext('The wings and squads are not known yet. Try again after the next refresh.');
      lines.push(empty);
    }
    container.replaceChildren(...lines);
  }

  function openMoveDialog(ids) {
    if (!moveModal || !ids.length) {
      return;
    }
    moveIds = ids;
    const title = document.getElementById('fleet-move-title');
    const name = ids.length === 1 ? memberName(memberRow(ids[0])) : '';
    title.textContent = ids.length === 1
      ? interpolate(gettext('Move %(name)s to…'), { name: name || gettext('member') }, true)
      : interpolate(ngettext('Move %(count)s member to…', 'Move %(count)s members to…', ids.length), { count: ids.length }, true);
    const hint = document.getElementById('fleet-move-hint');
    if (hint) {
      hint.classList.toggle('d-none', ids.length === 1);
    }
    buildTargets(document.getElementById('fleet-move-targets'), ids);
    showModal(moveModal);
  }

  if (moveModal) {
    moveModal.addEventListener('click', (event) => {
      const target = event.target.closest('button[data-move-role]');
      if (target && !target.disabled) {
        hideModal(moveModal);
        submitMove(moveIds, target.dataset.moveRole, target.dataset.moveWingId, target.dataset.moveSquadId);
        return;
      }
      // Without Bootstrap's script the close buttons and the backdrop click still close it.
      if (!(window.bootstrap && window.bootstrap.Modal)) {
        if (event.target === moveModal || event.target.closest('[data-bs-dismiss="modal"]')) {
          hideModal(moveModal);
        }
      }
    });
    moveModal.addEventListener('shown.bs.modal', () => {
      const first = moveModal.querySelector('button[data-move-role]:not([disabled])');
      if (first) {
        first.focus();
      }
    });
  }

  // ---------------------------------------------------------------- selection bar

  function updateBulkBar() {
    const bar = document.getElementById('fleet-bulk-bar');
    if (!bar) {
      return;
    }
    const count = selectedIds().length;
    bar.classList.toggle('d-none', count === 0);
    bar.classList.toggle('d-flex', count > 0);
    const label = document.getElementById('fleet-bulk-count');
    if (label) {
      label.textContent = interpolate(gettext('%(count)s selected'), { count }, true);
    }
  }

  function clearSelection() {
    root.querySelectorAll('input.fleet-select:checked').forEach((box) => {
      box.checked = false;
      box.dispatchEvent(new Event('change', { bubbles: true }));
    });
    updateBulkBar();
  }

  root.addEventListener('change', (event) => {
    if (event.target.matches && event.target.matches('input.fleet-select')) {
      updateBulkBar();
    }
  });

  // ---------------------------------------------------------------- clicks

  root.addEventListener('click', (event) => {
    const opener = event.target.closest('[data-fleet-rename-open]');
    if (opener && root.contains(opener)) {
      event.preventDefault();
      openEditor(opener.closest('.fleet-node-head'));
      return;
    }
    const quick = event.target.closest('[data-fleet-move]');
    if (quick && root.contains(quick)) {
      event.preventDefault();
      const row = quick.closest('li.fleet-member');
      if (row) {
        submitMove([row.dataset.characterId], quick.dataset.role, quick.dataset.wingId, quick.dataset.squadId);
      }
      return;
    }
    const mover = event.target.closest('[data-fleet-move-open]');
    if (mover && root.contains(mover)) {
      event.preventDefault();
      if (mover.disabled) {
        return;
      }
      const row = mover.closest('li.fleet-member');
      openMoveDialog(row ? [row.dataset.characterId].filter(isId) : selectedIds());
      return;
    }
    if (event.target.closest('#fleet-bulk-clear')) {
      event.preventDefault();
      clearSelection();
    }
  });

  // ---------------------------------------------------------------- drag and drop

  let drag = null;

  function dropRole(target, ids) {
    const role = target.dataset.dropRole;
    if (!role || (COMMANDER_ROLES.includes(role) && ids.length !== 1)) {
      return '';
    }
    return role;
  }

  function dropTarget(event) {
    if (!drag || !event.target.closest) {
      return null;
    }
    const target = event.target.closest('[data-drop-role]');
    if (!target || !root.contains(target) || !dropRole(target, drag.ids)) {
      return null;
    }
    return target;
  }

  // Marks the places the dragged members can go. An empty squad has no rows to drop on, so its
  // member list gets some height while the drag lasts.
  function markDropTargets(ids) {
    root.querySelectorAll('[data-drop-role]').forEach((target) => {
      if (!dropRole(target, ids)) {
        return;
      }
      target.classList.add('fleet-drop-target');
      if (target.tagName === 'UL' && !target.children.length) {
        target.style.minHeight = '1.75rem';
        target.style.outline = '1px dashed var(--bs-secondary-color, #6c757d)';
        target.dataset.fleetDropSized = '1';
      }
    });
  }

  function clearDropMarks() {
    root.querySelectorAll('.fleet-drop-over, .fleet-drop-target').forEach((element) => {
      element.classList.remove('fleet-drop-over', 'fleet-drop-target');
    });
    root.querySelectorAll('[data-fleet-drop-sized]').forEach((element) => {
      element.style.minHeight = '';
      element.style.outline = '';
      delete element.dataset.fleetDropSized;
    });
  }

  function endDrag() {
    if (!drag) {
      return;
    }
    drag.rows.forEach((row) => row.classList.remove('opacity-50'));
    drag = null;
    clearDropMarks();
    setBusy('drag', false);
  }

  root.addEventListener('dragstart', (event) => {
    const row = event.target.closest && event.target.closest('li.fleet-member[draggable="true"]');
    if (!row || !root.contains(row) || !isId(row.dataset.characterId)) {
      return;
    }
    const box = row.querySelector('input.fleet-select');
    // A selected member takes the whole selection along.
    const ids = box && box.checked ? selectedIds() : [row.dataset.characterId];
    drag = { ids, rows: ids.map(memberRow).filter(Boolean) };
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', ids.join(','));
    }
    setBusy('drag', true);
    // After the browser took the drag image, so the image shows the row as it was.
    const current = drag;
    setTimeout(() => {
      if (drag === current) {
        current.rows.forEach((member) => member.classList.add('opacity-50'));
        markDropTargets(ids);
      }
    }, 0);
  });

  root.addEventListener('dragover', (event) => {
    const target = dropTarget(event);
    if (!target) {
      return;
    }
    event.preventDefault();
    if (event.dataTransfer) {
      event.dataTransfer.dropEffect = 'move';
    }
    root.querySelectorAll('.fleet-drop-over').forEach((element) => {
      if (element !== target) {
        element.classList.remove('fleet-drop-over');
      }
    });
    target.classList.add('fleet-drop-over');
  });

  root.addEventListener('dragleave', (event) => {
    const target = event.target.closest && event.target.closest('[data-drop-role]');
    if (target && !target.contains(event.relatedTarget)) {
      target.classList.remove('fleet-drop-over');
    }
  });

  root.addEventListener('drop', (event) => {
    const target = dropTarget(event);
    if (!target) {
      return;
    }
    event.preventDefault();
    const { ids, rows } = drag;
    const role = dropRole(target, ids);
    const { wingId, squadId } = target.dataset;
    endDrag();
    if (rows.length === 1 && ids.length === 1 && isAt(rows[0], role, wingId, squadId)) {
      return;
    }
    submitMove(ids, role, wingId, squadId);
  });

  root.addEventListener('dragend', endDrag);

  // ---------------------------------------------------------------- renaming

  function renameParts(head, form) {
    const name = head.querySelector('.fleet-node-name');
    const siblings = form.parentElement ? Array.from(form.parentElement.children) : [];
    return [name, ...siblings.filter((element) => element !== form)].filter(Boolean);
  }

  function showEditor(head, form, on) {
    renameParts(head, form).forEach((element) => {
      if (on && !element.classList.contains('d-none')) {
        element.classList.add('d-none');
        element.dataset.fleetRenameHidden = '1';
      } else if (!on && element.dataset.fleetRenameHidden) {
        element.classList.remove('d-none');
        delete element.dataset.fleetRenameHidden;
      }
    });
    form.classList.toggle('d-none', !on);
    form.classList.toggle('d-inline-flex', on);
    head.classList.toggle('fleet-renaming', on);
  }

  function closeEditor(form, restore) {
    const head = form.closest('.fleet-node-head');
    const input = form.querySelector('input[name="name"]');
    if (restore && input && input.dataset.original !== undefined) {
      input.value = input.dataset.original;
    }
    if (head) {
      showEditor(head, form, false);
    }
    if (!root.querySelector('.fleet-renaming')) {
      setBusy('rename', false);
    }
  }

  function openEditor(head) {
    const form = head && head.querySelector('form[data-fleet-rename]');
    if (!form) {
      return;
    }
    root.querySelectorAll('form[data-fleet-rename].d-inline-flex').forEach((open) => {
      if (open !== form) {
        closeEditor(open, true);
      }
    });
    const input = form.querySelector('input[name="name"]');
    if (input.dataset.original === undefined) {
      input.dataset.original = input.value;
    }
    showEditor(head, form, true);
    setBusy('rename', true);
    input.focus();
    input.select();
  }

  root.addEventListener('keydown', (event) => {
    const form = event.target.closest && event.target.closest('form[data-fleet-rename]');
    if (form && event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeEditor(form, true);
    }
  });

  // Clicking elsewhere cancels the rename; the Save button keeps the focus in the form.
  root.addEventListener('mousedown', (event) => {
    const button = event.target.closest('form[data-fleet-rename] button');
    if (button) {
      event.preventDefault();
    }
  });

  root.addEventListener('focusout', (event) => {
    const form = event.target.closest && event.target.closest('form[data-fleet-rename]');
    if (!form || form.contains(event.relatedTarget)) {
      return;
    }
    setTimeout(() => {
      if (form.isConnected && !form.contains(document.activeElement) && form.classList.contains('d-inline-flex')) {
        closeEditor(form, true);
      }
    }, 150);
  });

  // ---------------------------------------------------------------- after "+" and renames

  let pendingAdd = null;

  function nodeSelector(kind, wingId) {
    return kind === 'wing'
      ? 'li.fleet-node[data-node="wing"]'
      : `li.fleet-node[data-node="squad"][data-wing-id="${wingId}"]`;
  }

  function nodeId(kind, node) {
    return kind === 'wing' ? node.dataset.wingId : node.dataset.squadId;
  }

  function nodeIds(kind, wingId) {
    const fleetTree = tree();
    const nodes = fleetTree ? fleetTree.querySelectorAll(nodeSelector(kind, wingId)) : [];
    return new Set(Array.from(nodes, (node) => nodeId(kind, node)));
  }

  // Opens the name editor on the wing or squad just created, once the redrawn tree shows it.
  function openNewNode() {
    if (!pendingAdd) {
      return;
    }
    if (Date.now() > pendingAdd.until) {
      pendingAdd = null;
      return;
    }
    const fleetTree = tree();
    if (!fleetTree) {
      return;
    }
    const { kind, wingId, known, id } = pendingAdd;
    const nodes = Array.from(fleetTree.querySelectorAll(nodeSelector(kind, wingId)));
    const fresh = nodes.filter((node) => (id ? nodeId(kind, node) === id : !known.has(nodeId(kind, node))));
    if (fresh.length !== 1) {
      return;
    }
    pendingAdd = null;
    openEditor(fresh[0].querySelector(':scope > .fleet-node-head'));
  }

  root.addEventListener('submit', (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || !root.contains(form)) {
      return;
    }
    if (form.matches('[data-fleet-add]')) {
      const kind = form.dataset.fleetAdd;
      const wingId = isId(form.dataset.wingId) ? form.dataset.wingId : '';
      if (kind === 'wing' || (kind === 'squad' && wingId)) {
        pendingAdd = { kind, wingId, known: nodeIds(kind, wingId), id: '', until: Date.now() + ADD_WAIT_MS };
      }
    } else if (form.matches('form[data-fleet-rename]')) {
      const head = form.closest('.fleet-node-head');
      const input = form.querySelector('input[name="name"]');
      const name = head && head.querySelector('.fleet-node-name');
      if (name && input && input.value.trim()) {
        // Shown until the answer redraws the tree.
        name.textContent = input.value.trim();
        name.classList.add('is-pending');
      }
      if (input) {
        delete input.dataset.original;
      }
      closeEditor(form, false);
    }
  });

  // fleet_live.js reports each answer of an action; a new wing or squad is then known by its id.
  document.addEventListener('fleetops:answer', (event) => {
    const detail = event.detail || {};
    const form = detail.form;
    if (!pendingAdd || !form || !form.matches || !form.matches('[data-fleet-add]')) {
      return;
    }
    const answer = detail.answer || {};
    const data = answer.data || {};
    const id = pendingAdd.kind === 'wing' ? data.wing_id : data.squad_id;
    if (!answer.ok || !id) {
      pendingAdd = answer.ok ? pendingAdd : null;
      return;
    }
    pendingAdd.id = String(id);
    openNewNode();
  });

  function afterRender() {
    updateBulkBar();
    openNewNode();
  }

  const api = fleet();
  if (api && typeof api.onRender === 'function') {
    api.onRender(afterRender);
  }
  // The tree is redrawn by replacing its content.
  const fleetTree = tree();
  if (fleetTree && 'MutationObserver' in window) {
    new MutationObserver(afterRender).observe(fleetTree, { childList: true });
  }
  updateBulkBar();
})();
