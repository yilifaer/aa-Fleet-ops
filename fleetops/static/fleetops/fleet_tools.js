// Fleet tab: invites, structure templates and quick ship classification.
//
// The invite, template and classify forms are form[data-fleet-action]: the shared action handler
// of fleet_live.js (window.FleetOpsFleet) sends them and shows the answer, or the browser posts
// them when that script is missing. This script fills them in: the character search ("My
// characters" while it is empty), the squads to invite into and the summary of what a template
// changes, both read from the fleet tree as it is shown now. All text goes in with textContent.
(() => {
  const root = document.querySelector('[data-fleet-root]');
  if (!root) {
    return;
  }

  const MIN_QUERY_LENGTH = 3;
  const SEARCH_DELAY_MS = 300;
  const MAX_INVITES = 50;
  const SEARCH_LIMIT = 20;

  const fleet = () => window.FleetOpsFleet || null;
  const isId = (value) => /^\d+$/.test(String(value || ''));

  function setBusy(key, on) {
    const api = fleet();
    if (api && typeof api.setBusy === 'function') {
      api.setBusy(key, on);
    }
  }

  // Alliance Auth marks every form of the page "is-submitting" when it is sent and refuses to send
  // it again. These forms are sent by script and stay on the page, so each send clears the mark.
  function sendable(form) {
    form.classList.remove('is-submitting');
  }

  function submit(form) {
    sendable(form);
    if (typeof form.requestSubmit === 'function') {
      form.requestSubmit();
      return;
    }
    const event = new Event('submit', { bubbles: true, cancelable: true });
    if (form.dispatchEvent(event)) {
      form.submit();
    }
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = text;
    }
    return node;
  }

  const plural = (count, noun, nouns) => `${count} ${count === 1 ? noun : nouns || `${noun}s`}`;

  // ---------------------------------------------------------------- modals

  // Bootstrap's data API opens and closes the modals; a theme without Bootstrap's scripts gets
  // the same through these fallbacks.
  function showModal(modal) {
    const bootstrap = window.bootstrap;
    if (bootstrap && bootstrap.Modal) {
      bootstrap.Modal.getOrCreateInstance(modal).show();
      return;
    }
    modal.dispatchEvent(new Event('show.bs.modal'));
    modal.style.display = 'block';
    modal.classList.add('show');
    modal.removeAttribute('aria-hidden');
    modal.setAttribute('aria-modal', 'true');
    modal.dispatchEvent(new Event('shown.bs.modal'));
  }

  function hideModal(modal) {
    const bootstrap = window.bootstrap;
    if (bootstrap && bootstrap.Modal) {
      bootstrap.Modal.getOrCreateInstance(modal).hide();
      return;
    }
    modal.classList.remove('show');
    modal.style.display = 'none';
    modal.setAttribute('aria-hidden', 'true');
    modal.removeAttribute('aria-modal');
    modal.dispatchEvent(new Event('hidden.bs.modal'));
  }

  function withoutBootstrap(modal, opener) {
    if (opener) {
      opener.addEventListener('click', (event) => {
        if (!(window.bootstrap && window.bootstrap.Modal) && !opener.disabled) {
          event.preventDefault();
          showModal(modal);
        }
      });
    }
    modal.addEventListener('click', (event) => {
      if (!(window.bootstrap && window.bootstrap.Modal) && event.target.closest('[data-bs-dismiss="modal"]')) {
        hideModal(modal);
      }
    });
  }

  function trackOpen(modal, key) {
    modal.addEventListener('show.bs.modal', () => setBusy(key, true));
    modal.addEventListener('hidden.bs.modal', () => setBusy(key, false));
  }

  // ---------------------------------------------------------------- the fleet tree as shown

  function nodeName(node) {
    const name = node.querySelector(':scope > .fleet-node-head .fleet-node-name');
    return name ? name.textContent.trim() : '';
  }

  // [{id, name, members, squads: [{id, name, members}]}] in tree order.
  function shownWings() {
    const tree = document.getElementById('fleet-tree');
    if (!tree) {
      return [];
    }
    return Array.from(tree.querySelectorAll('li.fleet-node[data-node="wing"]'), (wing) => ({
      id: wing.dataset.wingId || '',
      name: nodeName(wing),
      members: wing.querySelectorAll('li.fleet-member').length,
      squads: Array.from(wing.querySelectorAll('li.fleet-node[data-node="squad"]'), (squad) => ({
        id: squad.dataset.squadId || '',
        name: nodeName(squad),
        members: squad.querySelectorAll('li.fleet-member').length,
      })),
    }));
  }

  // ---------------------------------------------------------------- quick classify

  root.addEventListener('change', (event) => {
    const select = event.target.closest('form.fleet-classify select[name="function_id"]');
    if (select && select.value) {
      submit(select.form);
    }
  });

  if (root.dataset.canManage !== '1') {
    return;
  }

  // ---------------------------------------------------------------- invites

  function setupInvites(modal) {
    const form = document.getElementById('fleet-invite-form');
    const search = document.getElementById('fleet-invite-search');
    const hint = document.getElementById('fleet-invite-hint');
    const results = document.getElementById('fleet-invite-results');
    const chosen = document.getElementById('fleet-invite-selected');
    const squads = document.getElementById('fleet-invite-squad');
    const wingInput = document.getElementById('fleet-invite-wing');
    const button = document.getElementById('fleet-invite-submit');
    if (!form || !search || !results || !chosen || !squads || !button) {
      return;
    }
    const hintText = hint ? hint.textContent : '';
    const selected = new Map();
    let timer = null;
    let controller = null;
    let shownQuery = null;
    let sent = false;

    function setHint(text, warn) {
      if (!hint) {
        return;
      }
      hint.textContent = text || hintText;
      hint.classList.toggle('text-danger', Boolean(warn));
    }

    function status(text, warn) {
      results.replaceChildren(element('div', `list-group-item small ${warn ? 'text-danger' : 'text-muted'}`, text));
    }

    function updateButton() {
      button.disabled = selected.size === 0;
      button.textContent = selected.size ? `Invite ${selected.size}` : 'Invite';
    }

    function renderChosen() {
      const items = Array.from(selected, ([id, name]) => {
        const chip = element('span', 'badge text-bg-primary me-1 mb-1 fleet-invite-chip');
        chip.dataset.characterId = id;
        chip.append(element('span', '', name));
        const remove = element('button', 'btn-close btn-close-white ms-1 align-middle');
        remove.type = 'button';
        remove.style.fontSize = '0.6em';
        remove.dataset.remove = id;
        remove.setAttribute('aria-label', `Remove ${name}`);
        chip.append(remove);
        const input = element('input');
        input.type = 'hidden';
        input.name = 'character_ids';
        input.value = id;
        return [chip, input];
      }).flat();
      if (items.length) {
        chosen.replaceChildren(element('div', 'small text-muted mb-1', `Selected (${selected.size})`), ...items);
      } else {
        chosen.replaceChildren();
      }
      updateButton();
    }

    function candidateRow(candidate) {
      const id = String(candidate.character_id);
      const row = element('label', 'list-group-item d-flex gap-2 align-items-start');
      const box = element('input', 'form-check-input mt-1 flex-shrink-0');
      box.type = 'checkbox';
      box.value = id;
      box.dataset.name = candidate.name;
      box.checked = selected.has(id);
      box.disabled = Boolean(candidate.in_fleet);
      const text = element('div', 'flex-grow-1');
      const title = element('div');
      title.append(element('strong', '', candidate.name));
      if (candidate.in_fleet) {
        title.append(' ', element('span', 'badge text-bg-secondary', 'In fleet'));
      } else if (candidate.invited) {
        title.append(' ', element('span', 'badge text-bg-info', 'Invitation pending'));
      }
      if (candidate.is_mine) {
        title.append(' ', element('span', 'badge text-bg-light border', 'Yours'));
      }
      const details = [candidate.corporation, candidate.alliance].filter(Boolean);
      if (candidate.main_name && candidate.main_name !== candidate.name) {
        details.push(`main: ${candidate.main_name}`);
      }
      text.append(title, element('div', 'small text-muted', details.join(' · ')));
      row.append(box, text);
      return row;
    }

    function render(candidates, query) {
      const rows = candidates.filter((candidate) => isId(candidate.character_id)).map(candidateRow);
      const note = (text) => element('div', 'list-group-item small text-muted', text);
      let heading = rows.length ? 'My characters' : 'You have no characters in Alliance Auth.';
      if (query) {
        heading = rows.length ? `Characters matching “${query}”` : `No character matches “${query}”.`;
      }
      results.replaceChildren(note(heading), ...rows);
      if (query && rows.length >= SEARCH_LIMIT) {
        results.append(note(`Showing the first ${SEARCH_LIMIT}. Type more of the name to narrow the search.`));
      }
    }

    function load(value) {
      const query = String(value || '').trim().replace(/\s+/g, ' ');
      if (query && query.length < MIN_QUERY_LENGTH) {
        if (controller) {
          controller.abort();
        }
        shownQuery = null;
        status(`Type at least ${MIN_QUERY_LENGTH} letters, or clear the search to list your own characters.`);
        return;
      }
      if (query === shownQuery) {
        return;
      }
      if (controller) {
        controller.abort();
      }
      controller = new AbortController();
      const url = new URL(root.dataset.candidatesUrl, window.location.href);
      if (query) {
        url.searchParams.set('q', query);
      }
      status('Searching…');
      fetch(url.toString(), {
        headers: { Accept: 'application/json' },
        credentials: 'same-origin',
        signal: controller.signal,
      })
        .then((response) => {
          const type = response.headers.get('Content-Type') || '';
          if (!response.ok || !type.includes('application/json')) {
            throw new Error('search failed');
          }
          return response.json();
        })
        .then((data) => {
          shownQuery = query;
          render(Array.isArray(data.candidates) ? data.candidates : [], query);
        })
        .catch((error) => {
          if (error && error.name === 'AbortError') {
            return;
          }
          shownQuery = null;
          status('The search failed. Reload the page if this keeps happening.', true);
        });
    }

    function syncWing() {
      const option = squads.selectedOptions[0];
      if (wingInput) {
        wingInput.value = option && option.value ? option.dataset.wingId || '' : '';
      }
    }

    // The squads of the tree as it is shown now; the chosen one stays chosen while it exists.
    function fillSquads() {
      const current = squads.value;
      const any = element('option', '', 'Any squad with free positions');
      any.value = '';
      const groups = [];
      shownWings().forEach((wing) => {
        const usable = wing.squads.filter((squad) => isId(squad.id));
        if (!isId(wing.id) || !usable.length) {
          return;
        }
        const group = element('optgroup');
        group.label = wing.name || 'Wing';
        usable.forEach((squad) => {
          const option = element('option', '', `${squad.name || 'Squad'} (${squad.members})`);
          option.value = squad.id;
          option.dataset.wingId = wing.id;
          group.append(option);
        });
        groups.push(group);
      });
      if (!groups.length) {
        // Keep what the page brought along while the tree shows no squads.
        syncWing();
        return;
      }
      squads.replaceChildren(any, ...groups);
      squads.value = Array.from(squads.options).some((option) => option.value === current) ? current : '';
      syncWing();
    }

    function reset() {
      selected.clear();
      renderChosen();
      search.value = '';
      shownQuery = null;
      results.replaceChildren();
      setHint('');
    }

    modal.addEventListener('show.bs.modal', () => {
      if (sent) {
        reset();
        sent = false;
      }
      fillSquads();
      updateButton();
      load(search.value);
    });
    modal.addEventListener('shown.bs.modal', () => search.focus());

    search.addEventListener('input', () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => load(search.value), SEARCH_DELAY_MS);
    });
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        // Enter searches; it never sends the invitations.
        event.preventDefault();
        window.clearTimeout(timer);
        load(search.value);
      }
    });

    results.addEventListener('change', (event) => {
      const box = event.target.closest('input[type="checkbox"]');
      if (!box || !isId(box.value)) {
        return;
      }
      if (box.checked && !selected.has(box.value) && selected.size >= MAX_INVITES) {
        box.checked = false;
        setHint(`Invite at most ${MAX_INVITES} characters at a time.`, true);
        return;
      }
      setHint('');
      if (box.checked) {
        selected.set(box.value, box.dataset.name || `Character ${box.value}`);
      } else {
        selected.delete(box.value);
      }
      renderChosen();
    });

    chosen.addEventListener('click', (event) => {
      const remove = event.target.closest('[data-remove]');
      if (!remove) {
        return;
      }
      selected.delete(remove.dataset.remove);
      const box = results.querySelector(`input[type="checkbox"][value="${remove.dataset.remove}"]`);
      if (box) {
        box.checked = false;
      }
      renderChosen();
    });

    squads.addEventListener('change', syncWing);

    form.addEventListener('submit', (event) => {
      sendable(form);
      if (!selected.size) {
        event.preventDefault();
        event.stopImmediatePropagation();
        setHint('Choose the characters to invite.', true);
        return;
      }
      syncWing();
      // The answer shows above the fleet tree; the next opening starts with an empty list.
      sent = true;
      hideModal(modal);
    });

    withoutBootstrap(modal, document.getElementById('fleet-invite-open'));
    trackOpen(modal, 'fleet-invite');
  }

  // ---------------------------------------------------------------- structure templates

  // The layout grammar of the configuration: "Wing: Squad A, Squad B" per line, # comments.
  function parseLayout(text) {
    const clean = (value) => value.replace(/\s+/g, ' ').trim();
    return String(text || '')
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#'))
      .map((line) => {
        const colon = line.indexOf(':');
        const name = clean(colon < 0 ? line : line.slice(0, colon));
        const squads = colon < 0 ? [] : line.slice(colon + 1).split(',').map(clean).filter(Boolean);
        return { name, squads };
      });
  }

  // What applying ``layout`` to the shown wings does, counted like the template job does it.
  function planTemplate(wings, layout, deleteExtra) {
    const plan = {
      renameWings: 0,
      createWings: 0,
      renameSquads: 0,
      createSquads: 0,
      deleteWings: 0,
      deleteSquads: 0,
      keepWings: 0,
      keepSquads: 0,
      extraWings: 0,
      extraSquads: 0,
    };
    layout.forEach((target, index) => {
      const wing = wings[index];
      if (!wing) {
        plan.createWings += 1;
        plan.createSquads += target.squads.length;
        return;
      }
      if (wing.name !== target.name) {
        plan.renameWings += 1;
      }
      target.squads.forEach((name, squadIndex) => {
        const squad = wing.squads[squadIndex];
        if (!squad) {
          plan.createSquads += 1;
        } else if (squad.name !== name) {
          plan.renameSquads += 1;
        }
      });
      wing.squads.slice(target.squads.length).forEach((squad) => {
        plan.extraSquads += 1;
        if (deleteExtra) {
          plan[squad.members ? 'keepSquads' : 'deleteSquads'] += 1;
        }
      });
    });
    wings.slice(layout.length).forEach((wing) => {
      plan.extraWings += 1;
      if (deleteExtra) {
        plan[wing.members ? 'keepWings' : 'deleteWings'] += 1;
      }
    });
    return plan;
  }

  function both(count, first, second) {
    const parts = [];
    if (count[0]) {
      parts.push(plural(count[0], first));
    }
    if (count[1]) {
      parts.push(plural(count[1], second));
    }
    return parts.join(' and ');
  }

  function summaryLines(plan, deleteExtra) {
    const lines = [];
    const renames = both([plan.renameWings, plan.renameSquads], 'wing', 'squad');
    const creates = both([plan.createWings, plan.createSquads], 'wing', 'squad');
    if (renames) {
      lines.push(`Renames ${renames}.`);
    }
    if (creates) {
      lines.push(`Creates ${creates}.`);
    }
    if (deleteExtra) {
      const deletes = both([plan.deleteWings, plan.deleteSquads], 'empty wing', 'empty squad');
      const keeps = both([plan.keepWings, plan.keepSquads], 'wing', 'squad');
      if (deletes) {
        lines.push(`Deletes ${deletes}.`);
      }
      if (keeps) {
        lines.push(`Keeps ${keeps} with members.`);
      }
    } else {
      const extras = both([plan.extraWings, plan.extraSquads], 'wing', 'squad');
      if (extras) {
        lines.push(`Leaves ${extras} beyond the template as they are.`);
      }
    }
    return lines;
  }

  function setupTemplates(modal) {
    const form = document.getElementById('fleet-template-form');
    const select = document.getElementById('fleet-template-select');
    const deleteExtra = document.getElementById('fleet-template-delete-extra');
    const summary = document.getElementById('fleet-template-summary');
    const description = document.getElementById('fleet-template-description');
    const layoutList = document.getElementById('fleet-template-layout');
    const button = document.getElementById('fleet-template-submit');
    if (!form || !select || !summary) {
      return;
    }

    function update() {
      const option = select.selectedOptions[0];
      const layout = option ? parseLayout(option.dataset.layout) : [];
      if (description) {
        description.textContent = option ? option.dataset.description || '' : '';
      }
      if (layoutList) {
        layoutList.replaceChildren(
          ...layout.map((wing) => element('li', '', `${wing.name}: ${wing.squads.join(', ')}`)),
        );
        layoutList.hidden = !layout.length;
      }
      if (button) {
        button.disabled = !layout.length;
      }
      if (!layout.length) {
        summary.replaceChildren(element('div', 'text-muted', 'Choose a template.'));
        return;
      }
      const wings = shownWings();
      const lines = summaryLines(planTemplate(wings, layout, Boolean(deleteExtra && deleteExtra.checked)),
        Boolean(deleteExtra && deleteExtra.checked));
      const items = (lines.length ? lines : ['The fleet already has these names: nothing to change.'])
        .map((line) => element('li', '', line));
      const list = element('ul', 'mb-1');
      list.append(...items);
      const note = element('div', 'text-muted',
        wings.length ? 'Counted from the fleet as shown; the change reads the fleet from ESI first.'
          : 'The fleet structure is not known yet; the change reads it from ESI first.');
      summary.replaceChildren(element('strong', '', 'What changes'), list, note);
    }

    modal.addEventListener('show.bs.modal', update);
    select.addEventListener('change', update);
    if (deleteExtra) {
      deleteExtra.addEventListener('change', update);
    }
    form.addEventListener('submit', () => {
      sendable(form);
      // The job's progress shows above the fleet tree.
      hideModal(modal);
    });

    withoutBootstrap(modal, document.getElementById('fleet-template-open'));
    trackOpen(modal, 'fleet-template');
  }

  const inviteModal = document.getElementById('fleet-invite-modal');
  if (inviteModal) {
    setupInvites(inviteModal);
  }
  const templateModal = document.getElementById('fleet-template-modal');
  if (templateModal) {
    setupTemplates(templateModal);
  }
})();
