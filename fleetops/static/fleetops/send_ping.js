// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Send Ping page: the channels of the chosen group, the targets a channel allows, the quick fleet times, the live
// Discord preview and the question before the ping is posted. Uses window.FleetOps (fleetops.js),
// window.FleetOpsPreview (preview.js) and the JavaScript catalog. Everything the script writes is text or DOM
// nodes; the server checks every choice again when the ping is sent.
(() => {
  const form = document.getElementById('send-ping-form');
  if (!form) {
    return;
  }
  const channel = document.getElementById('id_ping_channel');
  const doctrine = document.getElementById('id_doctrine_choice');
  const customWrap = document.getElementById('custom-doctrine-wrap');
  const dateInput = document.getElementById('id_fleet_date');
  const clockInput = document.getElementById('id_fleet_clock');
  const eveClock = document.getElementById('eve-clock');
  const relative = document.getElementById('fleet-time-relative');
  const moreSettings = document.getElementById('send-ping-more');
  const submitButton = document.getElementById('send-ping-submit');
  const summary = document.getElementById('send-ping-summary');
  const mentionBoxes = Array.from(form.querySelectorAll('input[name="ping_mentions"]'));
  const groupInputs = Array.from(form.querySelectorAll('[name="group"]'));
  const channelRules = JSON.parse(document.getElementById('ping-channel-rules').textContent);
  const groupRules = JSON.parse(document.getElementById('ping-group-rules').textContent);
  const pingLook = document.getElementById('ping-look');
  const pingRaw = document.getElementById('ping-preview');
  const pingLength = document.getElementById('ping-length');
  const previewMissing = document.getElementById('preview-missing');
  const previewBlocking = document.getElementById('preview-blocking');
  const previewWarnings = document.getElementById('preview-warnings');
  const blockingPill = document.getElementById('preview-blocking-pill');
  const PING_LIMIT = Number(form.dataset.pingLimit);
  const SINGLE_TARGETS = ['none', 'everyone', 'here'];
  const PREVIEW_DELAY = 400;
  let previewTimer;
  let previewRequest = 0;

  // --- Group, channel and targets ------------------------------------------------------------------------

  // The group the ping is for: its id, or "" for an alliance ping.
  function groupValue() {
    if (!groupInputs.length) {
      return '';
    }
    if (groupInputs[0].type === 'radio') {
      const checked = groupInputs.find((input) => input.checked);
      return checked ? checked.value : '';
    }
    return groupInputs[0].value;
  }

  // Only the channels of the chosen group (or the alliance channels) are offered.
  function applyGroupRules() {
    const allowed = groupRules[groupValue()];
    const options = Array.from(channel.options);
    options.forEach((option) => {
      const fits = option.value === '' || !allowed || allowed.includes(Number(option.value));
      option.hidden = !fits;
      option.disabled = !fits;
    });
    // A channel the group cannot use is cleared; when only one channel is left it is chosen.
    const open = options.filter((option) => option.value !== '' && !option.disabled);
    const current = channel.selectedOptions[0];
    if (current && current.disabled) {
      channel.value = open.length === 1 ? open[0].value : '';
    } else if (!channel.value && open.length === 1) {
      channel.value = open[0].value;
    }
  }

  function oneTargetChecked() {
    if (mentionBoxes.some((box) => box.checked)) {
      return;
    }
    const none = mentionBoxes.find((box) => box.value === 'none');
    if (none) {
      none.checked = true;
    }
  }

  // No ping, @everyone and @here stand alone; roles can be combined with each other.
  function exclusiveTargets(event) {
    const box = event.target;
    if (box.checked) {
      const single = SINGLE_TARGETS.includes(box.value);
      mentionBoxes.forEach((other) => {
        if (other !== box && (single || SINGLE_TARGETS.includes(other.value))) {
          other.checked = false;
        }
      });
    }
    oneTargetChecked();
  }

  function targetAllowed(rule, value) {
    if (!rule) {
      return true;
    }
    if (value === 'everyone') {
      return rule.everyone;
    }
    if (value === 'here') {
      return rule.here;
    }
    if (value.startsWith('role:')) {
      return rule.roles === null || rule.roles.includes(Number(value.slice(5)));
    }
    return true;
  }

  // Targets the channel does not allow are disabled (and unticked), as on the start page.
  function applyChannelRules() {
    const rule = channel.value ? channelRules[channel.value] : null;
    mentionBoxes.forEach((box) => {
      const allowed = targetAllowed(rule, box.value);
      box.disabled = !allowed;
      if (!allowed) {
        box.checked = false;
      }
    });
    oneTargetChecked();
  }

  function mentionLabels() {
    return mentionBoxes.filter((box) => box.checked && box.value !== 'none').map((box) => {
      const label = form.querySelector(`label[for="${box.id}"]`);
      return label ? label.textContent.trim() : box.value;
    });
  }

  function channelName() {
    const option = channel.selectedOptions[0];
    return channel.value && option ? option.textContent.trim() : '';
  }

  // "#channel · @role" next to the button, and the question fleetops.js asks before the form is sent.
  function updateSummary() {
    const name = channelName();
    const targets = mentionLabels();
    const parts = [];
    if (name) {
      parts.push(`#${name.replace(/^#/, '')}`);
      parts.push(targets.length ? targets.join(' ') : summary.dataset.none);
    }
    summary.textContent = parts.join(' · ');
    if (!name) {
      delete form.dataset.foConfirm;
      return;
    }
    form.dataset.foConfirm = targets.length
      ? interpolate(gettext('Post this ping to %(channel)s? It notifies %(targets)s. A posted ping cannot be taken back.'), { channel: name, targets: targets.join(', ') }, true)
      : interpolate(gettext('Post this ping to %(channel)s? It notifies nobody. A posted ping cannot be taken back.'), { channel: name }, true);
    form.dataset.foConfirmDanger = 'false';
    form.dataset.foConfirmLabel = submitButton.textContent.trim();
  }

  function toggleCustom() {
    if (customWrap && doctrine) {
      customWrap.hidden = !!doctrine.value;
    }
  }

  // --- Fleet time --------------------------------------------------------------------------------------

  function chosenTime() {
    if (!dateInput.value || !/^([01]\d|2[0-3]):[0-5]\d$/.test(clockInput.value)) {
      return NaN;
    }
    return Date.parse(`${dateInput.value}T${clockInput.value}:00Z`);
  }

  function showRelative() {
    const minutes = Math.round((chosenTime() - Date.now()) / 60000);
    relative.className = 'form-text';
    if (Number.isNaN(minutes)) {
      relative.textContent = '';
      return;
    }
    if (minutes <= 0) {
      relative.textContent = gettext('This time has already passed.');
      relative.className = 'form-text text-danger';
      return;
    }
    relative.textContent = interpolate(gettext('in %(duration)s'), { duration: FleetOps.duration(minutes) }, true);
  }

  function showClock() {
    if (eveClock) {
      eveClock.textContent = FleetOps.eveClock(new Date());
    }
    showRelative();
  }

  function setFleetTime(offsetMinutes) {
    const time = FleetOps.roundedEveTime(new Date(), offsetMinutes);
    dateInput.value = time.date;
    clockInput.value = time.clock;
    showRelative();
    queuePreview();
  }

  // --- Preview -----------------------------------------------------------------------------------------

  function showList(box, list) {
    const items = (list || []).map((text) => {
      const item = document.createElement('div');
      item.textContent = text;
      return item;
    });
    box.firstElementChild.replaceChildren(...items);
    box.classList.toggle('d-none', !items.length);
  }

  // Only real problems are marked; a field that is still empty is named in "Missing" above the preview.
  function markFields(errors) {
    form.querySelectorAll('.is-invalid').forEach((field) => field.classList.remove('is-invalid'));
    Object.keys(errors || {}).forEach((name) => {
      if ((errors[name] || []).every((error) => error.code === 'required')) {
        return;
      }
      const field = form.querySelector(`[name="${name}"]`);
      if (field && field.type !== 'hidden') {
        field.classList.add('is-invalid');
        // A field the sender cannot see would hide its own error.
        if (moreSettings && moreSettings.contains(field)) {
          moreSettings.open = true;
        }
      }
    });
  }

  function showMissing(missing, errors) {
    let text = '';
    if ((missing || []).length) {
      text = interpolate(gettext('Missing: %(fields)s'), { fields: missing.join(', ') }, true);
    } else if (Object.keys(errors || {}).length) {
      text = gettext('Check the highlighted fields.');
    }
    previewMissing.firstElementChild.textContent = text;
    previewMissing.classList.toggle('d-none', !text);
    markFields(errors);
  }

  function showLength(answer) {
    if (!pingLength) {
      return;
    }
    pingLength.textContent = answer ? interpolate(gettext('Ping %(length)s / %(limit)s'), { length: answer.ping_length, limit: PING_LIMIT }, true) : '';
    pingLength.classList.toggle('text-danger', !!answer && answer.ping_length > PING_LIMIT);
  }

  function lookNote(text) {
    const note = document.createElement('div');
    note.className = 'fo-ping-note';
    note.textContent = text;
    pingLook.replaceChildren(note);
  }

  function clearPreview(text) {
    pingRaw.textContent = text;
    delete pingRaw.dataset.copyText;
    lookNote(text);
    showLength(null);
    showList(previewBlocking, []);
    showList(previewWarnings, []);
    blockingPill.classList.add('d-none');
  }

  function skeleton() {
    if (window.FleetOpsPreview && typeof FleetOpsPreview.skeleton === 'function') {
      FleetOpsPreview.skeleton(pingLook, 3);
      return;
    }
    const lines = ['', 'w-75', 'w-50'].map((width) => {
      const line = document.createElement('span');
      line.className = `fo-skeleton ${width}`.trim();
      return line;
    });
    pingLook.replaceChildren(...lines);
  }

  async function preview() {
    const body = new FormData(form);
    const current = ++previewRequest;
    try {
      const response = await fetch(form.dataset.previewUrl, { method: 'POST', body, headers: { Accept: 'application/json' } });
      const answer = await response.json();
      if (current !== previewRequest) {
        return;
      }
      if (answer.ok) {
        // Roles are shown by name; Copy Ping copies the text that is posted.
        pingRaw.textContent = answer.ping_display;
        pingRaw.dataset.copyText = answer.ping;
        FleetOpsPreview.discord(pingLook, answer.ping_display, { channel: channelName(), mentions: mentionLabels() });
        showLength(answer);
        showList(previewBlocking, answer.blocking);
        showList(previewWarnings, answer.warnings);
        showMissing([], {});
        blockingPill.classList.toggle('d-none', !(answer.blocking || []).length);
      } else if (answer.missing || answer.errors) {
        clearPreview(gettext('Complete the form to render a preview.'));
        showMissing(answer.missing, answer.errors);
      } else if (answer.error) {
        clearPreview(answer.error);
        showMissing([], {});
      }
    } catch (error) {
      // The preview is a help only; the send itself reports its own result.
    }
  }

  // Wait until the sender stops typing instead of sending a request for every character.
  function queuePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, PREVIEW_DELAY);
  }

  // --- Events ------------------------------------------------------------------------------------------

  groupInputs.forEach((input) => input.addEventListener('change', () => {
    applyGroupRules();
    applyChannelRules();
    updateSummary();
    queuePreview();
  }));
  channel.addEventListener('change', () => {
    applyChannelRules();
    updateSummary();
    queuePreview();
  });
  mentionBoxes.forEach((box) => box.addEventListener('change', (event) => {
    exclusiveTargets(event);
    updateSummary();
    queuePreview();
  }));
  if (doctrine) {
    doctrine.addEventListener('change', () => {
      toggleCustom();
      queuePreview();
    });
  }
  [dateInput, clockInput].forEach((input) => input.addEventListener('input', showRelative));
  form.querySelectorAll('[data-offset]').forEach((button) => button.addEventListener('click', () => setFleetTime(Number(button.dataset.offset))));
  form.querySelectorAll('input, select, textarea').forEach((field) => field.addEventListener('input', queuePreview));
  // A double click must not send the form twice (the question before the post comes first, see fleetops.js).
  form.addEventListener('submit', () => {
    submitButton.disabled = true;
  });
  // Back from the browser's page cache: the button works again.
  window.addEventListener('pageshow', () => {
    submitButton.disabled = false;
  });

  skeleton();
  toggleCustom();
  applyGroupRules();
  applyChannelRules();
  updateSummary();
  showClock();
  setInterval(showClock, 15000);
  queuePreview();
})();
