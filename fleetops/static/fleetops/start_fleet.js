// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Start Fleet and Edit Scheduled Fleet pages: fleet detection, the quick fleet time buttons, ping targets, the
// group a fleet is started for, the live Discord and in-game preview and saving presets. Uses window.FleetOps
// (fleetops.js), window.FleetOpsPreview (preview.js) and the JavaScript catalog.
// The labels of the submit button come from data-label-* attributes of the button, rendered by the page.
(() => {
 const form = document.getElementById('fleet-start-form');
 if (!form) { return; }
 const editing = form.dataset.mode === 'edit';
 const char = document.getElementById('id_fc_character_id');
 const detect = document.getElementById('fleet-detection');
 const doctrine = document.getElementById('id_doctrine_choice');
 const customWrap = document.getElementById('custom-doctrine-wrap');
 const submitButton = document.getElementById('fleet-start-submit');
 const modeRadios = form.querySelectorAll('input[name="operation_mode"]');
 const timingRadios = form.querySelectorAll('input[name="fleet_timing"]');
 // A user who can only start group fleets gets Pre ping disabled by the page; the script keeps it so.
 const prePingLocked = Array.from(timingRadios).some(radio => radio.value === 'pre_ping' && radio.disabled);
 const prePingFields = document.getElementById('pre-ping-fields');
 const dateInput = document.getElementById('id_fleet_date');
 const clockInput = document.getElementById('id_fleet_clock');
 const eveClock = document.getElementById('eve-clock');
 const relative = document.getElementById('fleet-time-relative');
 const channel = document.getElementById('id_ping_channel');
 const fleetType = document.getElementById('id_fleet_type');
 const mentionBoxes = Array.from(form.querySelectorAll('input[name="ping_mentions"]'));
 const channelRules = JSON.parse(document.getElementById('ping-channel-rules').textContent);
 const groupRulesElement = document.getElementById('fleet-group-rules');
 const groupRules = groupRulesElement ? JSON.parse(groupRulesElement.textContent) : null;
 const groupInputs = Array.from(form.querySelectorAll('[name="fleet_group"]'));
 const groupHint = document.getElementById('fleet-group-hint');
 const groupPill = document.getElementById('fo-group-pill');
 const moreSettings = document.getElementById('start-more');
 const previewWarnings = document.getElementById('preview-warnings');
 const previewBlocking = document.getElementById('preview-blocking');
 const previewBlockingPill = document.getElementById('preview-blocking-pill');
 const previewNotes = document.getElementById('preview-notes');
 const previewMissing = document.getElementById('preview-missing');
 const pingPreview = document.getElementById('ping-preview');
 const motdPreview = document.getElementById('motd-preview');
 const pingLook = document.getElementById('ping-look');
 const motdLook = document.getElementById('motd-look');
 const pingLength = document.getElementById('ping-length');
 const motdLength = document.getElementById('motd-length');
 const sendUpdate = document.getElementById('id_send_update');
 const PING_LIMIT = Number(form.dataset.pingLimit);
 const SINGLE_TARGETS = ['none', 'everyone', 'here'];
 const PREVIEW_DELAY = 400;
 const NOT_NEEDED = gettext('Not needed for a pre ping.');
 let previewTimer;
 let previewRequest = 0;
 function attendanceOnly(){
   const selected = form.querySelector('input[name="operation_mode"]:checked');
   return !!selected && selected.value === 'attendance_only';
 }
 function prePing(){
   if(editing){ return true; }
   const selected = form.querySelector('input[name="fleet_timing"]:checked');
   return !!selected && selected.value === 'pre_ping';
 }
 // The group the fleet is started for: the id of a group, "" for an alliance fleet.
 function groupValue(){
   if(!groupInputs.length){ return ''; }
   if(groupInputs[0].type === 'radio'){
     const checked = groupInputs.find(input => input.checked);
     return checked ? checked.value : '';
   }
   return groupInputs[0].value;
 }
 function isGroupFleet(){ return !!groupRules && groupValue() !== ''; }
 function updateMode(){
   const attendance = attendanceOnly();
   const group = isGroupFleet();
   // A pre ping is a Discord message, so PAP-only fleets start now. A group fleet is never pre pinged either.
   timingRadios.forEach(radio => {
     if(radio.value !== 'pre_ping'){ return; }
     radio.disabled = prePingLocked || attendance || group;
     if((prePingLocked || attendance || group) && radio.checked){ form.querySelector('input[name="fleet_timing"][value="now"]').checked = true; }
   });
   if(groupHint){ groupHint.classList.toggle('d-none', !group); }
   // PAP-only fleets never post, so their ping is only copied.
   channel.disabled = attendance;
   const pre = prePing();
   prePingFields.classList.toggle('d-none', !pre);
   // Hidden date and time are neither checked by the browser nor sent.
   dateInput.disabled = clockInput.disabled = !pre;
   if(editing){ submitButton.textContent = submitButton.dataset.labelSave; }
   else if(attendance){ submitButton.textContent = submitButton.dataset.labelPapOnly; }
   else { submitButton.textContent = pre ? submitButton.dataset.labelPrePing : submitButton.dataset.labelNow; }
   applyChannelRules();
   showGroupPill();
   updateConfirmation();
   if(detect && pre){ detect.textContent = NOT_NEEDED; detect.className = 'form-text'; }
   else if(detect && detect.textContent === NOT_NEEDED){ detectFleet(); }
 }
 function toggleCustom(){ customWrap.style.display = doctrine.value ? 'none' : 'block'; }
 async function detectFleet(){
   if(!detect || prePing()){ return; }
   if(!char.value){ detect.textContent = FleetOps.detection.select(); detect.className='form-text'; return; }
   detect.textContent = FleetOps.detection.checking();
   const url = form.dataset.detectUrl.replace('999999', char.value);
   try { const r=await fetch(url); const d=await r.json(); detect.textContent = FleetOps.detection.result(d); detect.className=d.ok&&d.is_fleet_boss?'form-text text-success':'form-text text-warning'; }
   catch(e){ detect.textContent = FleetOps.detection.failed(); }
 }
 function oneTargetChecked(){
   if(mentionBoxes.some(box => box.checked)){ return; }
   const none = mentionBoxes.find(box => box.value === 'none');
   if(none){ none.checked = true; }
 }
 // No ping, @everyone and @here stand alone; roles can be combined with each other.
 function exclusiveTargets(event){
   const box = event.target;
   if(box.checked){
     const single = SINGLE_TARGETS.includes(box.value);
     mentionBoxes.forEach(other => { if(other !== box && (single || SINGLE_TARGETS.includes(other.value))){ other.checked = false; } });
   }
   oneTargetChecked();
 }
 function targetAllowed(rule, value){
   if(!rule){ return true; }
   if(value === 'everyone'){ return rule.everyone; }
   if(value === 'here'){ return rule.here; }
   if(value.startsWith('role:')){ return rule.roles === null || rule.roles.includes(Number(value.slice(5))); }
   return true;
 }
 function applyChannelRules(){
   const rule = !channel.disabled && channel.value ? channelRules[channel.value] : null;
   mentionBoxes.forEach(box => {
     const allowed = targetAllowed(rule, box.value);
     box.disabled = !allowed;
     if(!allowed){ box.checked = false; }
   });
   oneTargetChecked();
 }
 // Only what the chosen kind of fleet may use is offered; the server checks the choice again.
 function limitOptions(select, allowed){
   const options = Array.from(select.options);
   options.forEach(option => {
     const fits = !allowed || option.value === '' || allowed.includes(Number(option.value));
     option.hidden = !fits;
     option.disabled = !fits;
   });
   const current = select.selectedOptions[0];
   if(current && current.disabled){
     const open = options.filter(option => option.value !== '' && !option.disabled);
     select.value = open.length === 1 ? open[0].value : '';
   }
 }
 function applyGroupRules(){
   if(!groupRules){ return; }
   const rule = groupRules[groupValue()] || null;
   limitOptions(fleetType, rule && rule.fleet_types);
   limitOptions(channel, rule && rule.channels);
 }
 function groupName(){
   const rule = isGroupFleet() ? groupRules[groupValue()] : null;
   return rule ? rule.name : '';
 }
 // The group of the fleet stays in sight next to the button that starts it.
 function showGroupPill(){
   if(!groupPill){ return; }
   const name = groupName();
   groupPill.replaceChildren();
   groupPill.classList.toggle('d-none', !name);
   if(!name){ return; }
   const pill = document.createElement('span');
   pill.className = 'fo-pill fo-pill-neutral fo-pill-icon fo-pill-group';
   pill.title = groupPill.dataset.title || '';
   const icon = document.createElement('i');
   icon.className = 'fa-solid fa-users';
   icon.setAttribute('aria-hidden', 'true');
   pill.append(icon, document.createTextNode(name));
   groupPill.append(pill);
 }
 // Posting to Discord asks first (the question is kept up to date; without a post the form sends at once).
 function mentionLabels(){
   return mentionBoxes.filter(box => box.checked && box.value !== 'none').map(box => {
     const label = form.querySelector(`label[for="${box.id}"]`);
     return label ? label.textContent.trim() : box.value;
   });
 }
 function channelName(){
   const option = channel.selectedOptions[0];
   return channel.value && option ? option.textContent.trim() : '';
 }
 function willPost(){
   if(attendanceOnly() || channel.disabled || !channel.value){ return false; }
   return !editing || (!!sendUpdate && sendUpdate.checked);
 }
 function updateConfirmation(){
   if(!willPost()){
     delete form.dataset.foConfirm;
     return;
   }
   const targets = mentionLabels();
   form.dataset.foConfirm = targets.length
     ? interpolate(gettext('Post this ping to %(channel)s? It notifies %(targets)s. A posted ping cannot be taken back.'), { channel: channelName(), targets: targets.join(', ') }, true)
     : interpolate(gettext('Post this ping to %(channel)s? It notifies nobody. A posted ping cannot be taken back.'), { channel: channelName() }, true);
   form.dataset.foConfirmDanger = 'false';
   form.dataset.foConfirmLabel = submitButton.textContent.trim();
 }
 function chosenTime(){
   if(!dateInput.value || !/^([01]\d|2[0-3]):[0-5]\d$/.test(clockInput.value)){ return NaN; }
   return Date.parse(`${dateInput.value}T${clockInput.value}:00Z`);
 }
 function showRelative(){
   const minutes = Math.round((chosenTime() - Date.now()) / 60000);
   relative.className = 'form-text';
   if(Number.isNaN(minutes)){ relative.textContent = ''; return; }
   if(minutes <= 0){ relative.textContent = gettext('This time has already passed.'); relative.className = 'form-text text-danger'; return; }
   relative.textContent = interpolate(gettext('in %(duration)s'), { duration: FleetOps.duration(minutes) }, true);
 }
 function showClock(){ eveClock.textContent = FleetOps.eveClock(new Date()); showRelative(); }
 function setFleetTime(offsetMinutes){
   const time = FleetOps.roundedEveTime(new Date(), offsetMinutes);
   dateInput.value = time.date;
   clockInput.value = time.clock;
   showRelative();
   queuePreview();
 }
 function showList(box, list){
   const items = (list||[]).map(text=>{const item=document.createElement('div'); item.textContent=text; return item;});
   box.firstElementChild.replaceChildren(...items);
   box.classList.toggle('d-none', !items.length);
 }
 function markFields(errors){
   form.querySelectorAll('.is-invalid').forEach(field=>field.classList.remove('is-invalid'));
   Object.keys(errors||{}).forEach(name=>{
     const field=form.querySelector(`[name="${name}"]`);
     if(field && field.type!=='hidden'){
       field.classList.add('is-invalid');
       // A field the FC cannot see would hide its own error.
       if(moreSettings && moreSettings.contains(field)){ moreSettings.open = true; }
     }
   });
 }
 function showMissing(missing, errors){
   let text = '';
   if((missing||[]).length){ text = interpolate(gettext('Missing: %(fields)s'), { fields: missing.join(', ') }, true); }
   else if(Object.keys(errors||{}).length){ text = gettext('Check the highlighted fields.'); }
   previewMissing.firstElementChild.textContent = text;
   previewMissing.classList.toggle('d-none', !text);
   markFields(errors);
 }
 function showLengths(d){
   pingLength.textContent = d ? interpolate(gettext('Ping %(length)s / %(limit)s'), { length: d.ping_length, limit: PING_LIMIT }, true) : '';
   pingLength.classList.toggle('text-danger', !!d && d.ping_length > PING_LIMIT);
   motdLength.textContent = d ? interpolate(gettext('MOTD %(visible)s visible, %(length)s with tags'), { visible: d.motd_visible_length, length: d.motd_length }, true) : '';
   // The warning limit counts the tags, as the game stores them; 0 switches the warning off.
   motdLength.classList.toggle('text-danger', !!d && d.motd_limit > 0 && d.motd_length > d.motd_limit);
 }
 // The Look panes show a sentence while there is nothing to render, and grey lines before the first answer.
 function lookNote(container, text){
   const note = document.createElement('div');
   note.className = 'fo-preview-note';
   note.textContent = text;
   container.replaceChildren(note);
 }
 function clearPreview(text){
   pingPreview.textContent = text; motdPreview.textContent = text;
   delete pingPreview.dataset.copyText;
   lookNote(pingLook, text); lookNote(motdLook, text);
   showLengths(null); showList(previewWarnings, []); showList(previewBlocking, []); showList(previewNotes, []);
   previewBlockingPill.classList.add('d-none');
 }
 async function preview(){
   const body = new FormData(form);
   body.append('preview', '1');
   const current = ++previewRequest;
   try {
     const r=await fetch(form.dataset.previewUrl,{method:'POST',body}); const d=await r.json();
     if(current !== previewRequest){ return; }
     if(d.ok){
       // Roles are shown by name; Copy Ping copies the text that is posted.
       pingPreview.textContent=d.ping_display; pingPreview.dataset.copyText=d.ping; motdPreview.textContent=d.motd;
       FleetOpsPreview.discord(pingLook, d.ping_display, { channel: channelName(), mentions: mentionLabels() });
       FleetOpsPreview.motd(motdLook, d.motd);
       showLengths(d); showList(previewWarnings, d.warnings); showList(previewBlocking, d.blocking); showList(previewNotes, d.notes); showMissing([], {});
       previewBlockingPill.classList.toggle('d-none', !(d.blocking || []).length);
     }
     else if(d.missing || d.errors){ clearPreview(gettext('Complete the form to render a preview.')); showMissing(d.missing, d.errors); }
     else if(d.error){ clearPreview(d.error); showMissing([], {}); }
   }
   catch(e){}
 }
 // Wait until the FC stops typing instead of sending a request for every character.
 function queuePreview(){ clearTimeout(previewTimer); previewTimer=setTimeout(preview,PREVIEW_DELAY); }
 const presetSaveButton = document.getElementById('preset-save-button');
 const presetName = document.getElementById('id_preset-name');
 const presetOverwrite = document.getElementById('id_preset-overwrite');
 const presetResult = document.getElementById('preset-save-result');
 const presetSelect = document.getElementById('preset-select');
 function showPresetResult(text, ok){
   presetResult.textContent = text;
   presetResult.className = `small mt-1 ${ok ? 'text-success' : 'text-danger'}`;
 }
 // The preset fields belong to the start form, so the preset is saved from the same values.
 async function savePreset(overwrite){
   const name = presetName.value.trim();
   presetOverwrite.value = overwrite ? '1' : '';
   const body = new FormData(form);
   presetSaveButton.disabled = true;
   try {
     const r = await fetch(presetSaveButton.dataset.url, {method:'POST', body, headers:{'Accept':'application/json'}});
     const d = await r.json();
     if(d.conflict && !overwrite){
       if(await FleetOps.confirm(interpolate(gettext('A preset named "%(name)s" exists. Overwrite?'), { name }, true), { danger: false })){ return await savePreset(true); }
       showPresetResult(gettext('Preset not saved.'), false);
       return;
     }
     showPresetResult(d.message, d.ok);
     if(d.ok && d.preset_id){
       let option = presetSelect.querySelector(`option[value="${d.preset_id}"]`);
       if(!option){ option = new Option(name, d.preset_id); presetSelect.add(option); }
       presetSelect.value = String(d.preset_id);
     }
   }
   catch(e){ showPresetResult(gettext('The preset could not be saved.'), false); }
   finally { presetSaveButton.disabled = false; presetOverwrite.value = ''; }
 }
 if(presetSaveButton){
   presetSaveButton.classList.remove('d-none');
   presetSaveButton.addEventListener('click', () => savePreset(false));
   // Enter in the preset name saves the preset; it must never start the fleet.
   presetName.addEventListener('keydown', event => { if(event.key === 'Enter'){ event.preventDefault(); savePreset(false); } });
 }
 if(char){ char.addEventListener('change',()=>{detectFleet();queuePreview();}); }
 doctrine.addEventListener('change',()=>{toggleCustom();queuePreview();});
 modeRadios.forEach(x=>x.addEventListener('change',()=>{updateMode();queuePreview();}));
 timingRadios.forEach(x=>x.addEventListener('change',()=>{updateMode();queuePreview();}));
 groupInputs.forEach(x=>x.addEventListener('change',()=>{applyGroupRules();updateMode();queuePreview();}));
 channel.addEventListener('change',()=>{applyChannelRules();updateConfirmation();queuePreview();});
 mentionBoxes.forEach(box=>box.addEventListener('change',event=>{exclusiveTargets(event);updateConfirmation();queuePreview();}));
 if(sendUpdate){ sendUpdate.addEventListener('change', updateConfirmation); }
 [dateInput, clockInput].forEach(input=>input.addEventListener('input',showRelative));
 form.querySelectorAll('[data-offset]').forEach(btn=>btn.addEventListener('click',()=>setFleetTime(Number(btn.dataset.offset))));
 form.querySelectorAll('input,select,textarea').forEach(x=>x.addEventListener('input',queuePreview));
 // A double click must not send the form twice (the confirmation of a post comes first, see fleetops.js).
 form.addEventListener('submit', () => { submitButton.disabled = true; });
 FleetOpsPreview.skeleton(pingLook, 3); FleetOpsPreview.skeleton(motdLook, 2);
 toggleCustom(); applyGroupRules(); updateMode(); if(char && char.value) detectFleet(); showClock(); setInterval(showClock, 15000); queuePreview();
})();
