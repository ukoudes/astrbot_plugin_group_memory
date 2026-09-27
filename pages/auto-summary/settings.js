const bridge = window.AstrBotPluginPage;
const settingsForm = document.getElementById('settings-form');
const settingsStatus = document.getElementById('settings-status');
const settingsButton = document.getElementById('save-settings');
const fields = {
  retention_days: 'setting-retention-days',
  enable_media_read: 'setting-enable-media-read',
  auto_summary_images: 'setting-auto-summary-images',
  media_vision_provider: 'setting-media-vision-provider',
  summary_media_scope: 'setting-summary-media-scope',
  smtp_host: 'setting-smtp-host',
  smtp_port: 'setting-smtp-port',
  smtp_security: 'setting-smtp-security',
  smtp_username: 'setting-smtp-username',
  smtp_sender: 'setting-smtp-sender',
  media_batch_concurrency: 'setting-media-batch-concurrency',
  media_get_image_timeout_seconds: 'setting-media-get-image-timeout-seconds',
  enable_webchat: 'setting-enable-webchat',
  webchat_qq_platform_id: 'setting-webchat-qq-platform-id',
  webchat_qq_bot_id: 'setting-webchat-qq-bot-id',
};
const boolKeys = new Set(['enable_media_read', 'auto_summary_images', 'enable_webchat']);
const intKeys = new Set(['retention_days', 'smtp_port', 'media_batch_concurrency',
  'media_get_image_timeout_seconds']);
const defaults = {
  retention_days: 30, smtp_port: 465, media_batch_concurrency: 4,
  media_get_image_timeout_seconds: 25, smtp_security: 'SSL/TLS',
  summary_media_scope: '全部媒体', enable_media_read: true, auto_summary_images: true,
};
let groupItems = [];
let readerIds = [];
let webchatIds = [];
let passwordSaved = false;
let secretTimer;
let secretRequestToken = 0;

function markDirty() {
  settingsStatus.textContent = '有未保存的修改';
}

function settingsNotice(message, failed = false) {
  document.dispatchEvent(new CustomEvent('plugin-notice', {
    detail: {message, failed},
  }));
}

function hideSavedPassword() {
  clearTimeout(secretTimer);
  secretRequestToken++;
  const input = document.getElementById('setting-smtp-current');
  input.type = 'password';
  input.value = '••••••••';
  document.getElementById('reveal-smtp-password').textContent = '查看';
}

function updatePasswordUi() {
  hideSavedPassword();
  const input = document.getElementById('setting-smtp-password');
  input.placeholder = passwordSaved ? '留空保留原授权码' : '输入 SMTP 授权码';
  document.getElementById('smtp-saved-panel').hidden = !passwordSaved;
}

function entryButton(label, remove) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'danger';
  button.textContent = label;
  button.addEventListener('click', remove);
  return button;
}

function renderEntries() {
  const groups = document.getElementById('group-entries');
  const readers = document.getElementById('reader-entries');
  const webchat = document.getElementById('webchat-entries');
  groups.replaceChildren();
  readers.replaceChildren();
  webchat.replaceChildren();
  for (const item of groupItems) {
    const row = document.createElement('div');
    row.className = 'entry-row group-entry';
    const number = document.createElement('strong');
    number.textContent = `群号 ${item.id}`;
    const name = document.createElement('input');
    name.type = 'text';
    name.maxLength = 40;
    name.value = item.name;
    name.placeholder = '显示名称（可选）';
    name.setAttribute('aria-label', `QQ群 ${item.id} 的显示名称`);
    name.addEventListener('input', () => { item.name = name.value; markDirty(); });
    row.append(number, name, entryButton('移除', () => {
      groupItems = groupItems.filter((entry) => entry !== item);
      renderEntries();
      markDirty();
    }));
    groups.append(row);
  }
  for (const id of readerIds) {
    const row = document.createElement('div');
    row.className = 'entry-row reader-entry';
    const number = document.createElement('strong');
    number.textContent = `QQ 号 ${id}`;
    row.append(number, entryButton('移除', () => {
      readerIds = readerIds.filter((entry) => entry !== id);
      renderEntries();
      markDirty();
    }));
    readers.append(row);
  }
  for (const id of webchatIds) {
    const row = document.createElement('div');
    row.className = 'entry-row reader-entry';
    const name = document.createElement('strong');
    name.textContent = id;
    row.append(name, entryButton('移除', () => {
      webchatIds = webchatIds.filter((entry) => entry !== id);
      renderEntries();
      markDirty();
    }));
    webchat.append(row);
  }
  if (!groupItems.length) groups.textContent = '尚未添加要记录的 QQ 群。';
  if (!readerIds.length) readers.textContent = '尚未添加授权 QQ 号。';
  if (!webchatIds.length) webchat.textContent = '尚未添加网页查询者 ID。';
}

function addGroup() {
  const idInput = document.getElementById('new-group-id');
  const nameInput = document.getElementById('new-group-name');
  const id = idInput.value.trim();
  if (!/^[0-9]+$/.test(id)) { settingsNotice('群号只能填数字', true); return; }
  if (groupItems.some((item) => item.id === id)) {
    settingsNotice('群号已添加', true); return;
  }
  groupItems.push({id, name: nameInput.value.trim()});
  idInput.value = '';
  nameInput.value = '';
  renderEntries();
  markDirty();
}

function addReader() {
  const input = document.getElementById('new-reader-id');
  const id = input.value.trim();
  if (!/^[0-9]+$/.test(id)) { settingsNotice('QQ 号只能填数字', true); return; }
  if (readerIds.includes(id)) { settingsNotice('QQ 号已添加', true); return; }
  readerIds.push(id);
  input.value = '';
  renderEntries();
  markDirty();
}

function addWebchatReader() {
  const input = document.getElementById('new-webchat-reader-id');
  const id = input.value.trim();
  if (!id || id.length > 100 || /[\x00-\x1f\x7f]/.test(id)) {
    settingsNotice('查询者 ID 无效', true);
    return;
  }
  if (webchatIds.includes(id)) {
    settingsNotice('查询者 ID 已添加', true);
    return;
  }
  webchatIds.push(id);
  input.value = '';
  renderEntries();
  markDirty();
}

function statusError(error) {
  return error?.message || error?.error || String(error);
}

function setView(name) {
  const settings = name === 'settings';
  if (!settings) hideSavedPassword();
  document.getElementById('settings-view').hidden = !settings;
  document.getElementById('tasks-view').hidden = settings;
  for (const [id, selected] of [['tab-settings', settings], ['tab-tasks', !settings]]) {
    const tab = document.getElementById(id);
    tab.classList.toggle('active', selected);
    tab.setAttribute('aria-selected', String(selected));
  }
}

async function loadSettings() {
  settingsStatus.textContent = '正在读取设置…';
  try {
    const response = await bridge.apiGet('settings/get');
    const settings = response.settings || {};
    groupItems = (settings.record_groups || []).map((id) => ({
      id: String(id), name: response.group_names?.[String(id)] || '',
    }));
    readerIds = (settings.reader_qq_ids || []).map(String);
    webchatIds = (settings.webchat_reader_ids || []).map(String);
    renderEntries();
    const providerSelect = document.getElementById(fields.media_vision_provider);
    providerSelect.replaceChildren();
    const providerIds = ['', ...(response.providers || [])];
    const current = settings.media_vision_provider || '';
    if (current && !providerIds.includes(current)) providerIds.push(current);
    for (const id of providerIds) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = id || '不识别图片';
      providerSelect.append(option);
    }
    for (const [key, id] of Object.entries(fields)) {
      const input = document.getElementById(id);
      const value = settings[key] ?? defaults[key] ?? '';
      if (boolKeys.has(key)) input.checked = Boolean(value);
      else input.value = value;
    }
    hideSavedPassword();
    document.getElementById('setting-smtp-password').value = '';
    passwordSaved = Boolean(settings.smtp_password_set);
    updatePasswordUi();
    settingsStatus.textContent = '';
  } catch (error) {
    settingsStatus.textContent = `读取设置失败：${statusError(error)}`;
    settingsNotice(settingsStatus.textContent, true);
  }
}

settingsForm.addEventListener('submit', async (event) => {
  event.preventDefault();
  hideSavedPassword();
  settingsButton.disabled = true;
  settingsStatus.textContent = '正在保存插件设置…';
  const payload = {};
  for (const [key, id] of Object.entries(fields)) {
    const input = document.getElementById(id);
    payload[key] = boolKeys.has(key) ? input.checked :
      intKeys.has(key) ? Number(input.value) : input.value;
  }
  payload.record_groups = groupItems.map((item) => item.id);
  payload.reader_qq_ids = [...readerIds];
  payload.webchat_reader_ids = [...webchatIds];
  payload.group_names = Object.fromEntries(groupItems.map((item) => [item.id, item.name]));
  const secret = document.getElementById('setting-smtp-password').value;
  if (secret) payload.smtp_password = secret;
  try {
    const result = await bridge.apiPost('settings/save', payload);
    await loadSettings();
    document.dispatchEvent(new Event('plugin-settings-saved'));
    settingsStatus.textContent = '';
    settingsStatus.textContent = result.warning || '';
    settingsNotice(result.warning ? '设置已保存 部分任务需检查授权' : '设置已保存');
  } catch (error) {
    settingsStatus.textContent = `保存失败：${statusError(error)}`;
    settingsNotice(settingsStatus.textContent, true);
  } finally {
    settingsButton.disabled = false;
  }
});

document.getElementById('tab-settings').addEventListener('click', () => setView('settings'));
document.getElementById('add-group').addEventListener('click', addGroup);
document.getElementById('add-reader').addEventListener('click', addReader);
document.getElementById('add-webchat-reader').addEventListener('click', addWebchatReader);
document.getElementById('setting-smtp-password').addEventListener('input', () => {
  markDirty();
});
document.getElementById('delete-smtp-password').addEventListener('click', async (event) => {
  hideSavedPassword();
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await bridge.apiPost('settings/save', {smtp_password_clear: true});
    passwordSaved = false;
    updatePasswordUi();
    settingsNotice('授权码已删除');
  } catch (error) {
    settingsNotice(`删除授权码失败：${statusError(error)}`, true);
  } finally {
    button.disabled = false;
  }
});
document.getElementById('reveal-smtp-password').addEventListener('click', async (event) => {
  const input = document.getElementById('setting-smtp-current');
  if (input.type === 'text') { hideSavedPassword(); return; }
  const button = event.currentTarget;
  const requestId = ++secretRequestToken;
  button.disabled = true;
  try {
    const result = await bridge.apiPost('settings/reveal-password', {});
    if (requestId !== secretRequestToken || !passwordSaved ||
        document.getElementById('settings-view').hidden) return;
    input.type = 'text';
    input.value = result.smtp_password;
    button.textContent = '隐藏';
    secretTimer = setTimeout(hideSavedPassword, 30000);
  } catch (error) {
    settingsNotice(`查看授权码失败：${statusError(error)}`, true);
  } finally {
    button.disabled = false;
  }
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden) hideSavedPassword();
});
for (const [id, add] of [['new-group-id', addGroup], ['new-group-name', addGroup],
                         ['new-reader-id', addReader],
                         ['new-webchat-reader-id', addWebchatReader]]) {
  document.getElementById(id).addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); add(); }
  });
}
document.getElementById('tab-tasks').addEventListener('click', () => setView('tasks'));
document.getElementById('refresh').addEventListener('click', () => {
  if (!document.getElementById('settings-view').hidden) loadSettings();
});
await bridge.ready();
await loadSettings();
