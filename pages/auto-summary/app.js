const bridge = window.AstrBotPluginPage;
const source = document.getElementById('source');
const reader = document.getElementById('reader');
const jobs = document.getElementById('jobs');
const status = document.getElementById('status');
const form = document.getElementById('add-form');
const formStatus = document.getElementById('form-status');
const formHeading = document.getElementById('add-heading');
const saveButton = form.querySelector('button[type="submit"]');
const cancelEdit = document.getElementById('cancel-edit');
const dateOffset = document.getElementById('date-offset');
const readDate = document.getElementById('read-date');
const emailTo = document.getElementById('email-to');
const dateButton = document.getElementById('read-date-button');
const calendar = document.getElementById('read-calendar');
const toast = document.getElementById('toast');
const jobsNotice = document.getElementById('jobs-notice');
let toastTimer;
let calendarMonth = '';
let retentionDays = 30;
let editingId = null;
let groupNames = {};
let previousPushTime = document.getElementById('time').value;
let editingLegacyEnd = false;

function notice(message, failed = false) {
  clearTimeout(toastTimer);
  toast.hidden = true;
  jobsNotice.hidden = true;
  const target = document.getElementById('tasks-view').hidden ? toast : jobsNotice;
  const fullMessage = String(message || '').replace(/[。；;]+/g, ' ').replace(/\s+/g, ' ').trim();
  const shortMessage = fullMessage.length > 72 ? `${fullMessage.slice(0, 71)}…` : fullMessage;
  target.querySelector('.notice-message').textContent = shortMessage;
  target.title = shortMessage === fullMessage ? '' : fullMessage;
  target.classList.toggle('is-error', failed);
  target.setAttribute('role', failed ? 'alert' : 'status');
  target.hidden = false;
  toastTimer = setTimeout(() => { target.hidden = true; }, failed ? 6000 : 3500);
}

for (const target of [toast, jobsNotice]) {
  target.querySelector('.notice-close').addEventListener('click', () => {
    clearTimeout(toastTimer);
    target.hidden = true;
  });
}
document.addEventListener('plugin-notice', (event) => notice(event.detail.message, event.detail.failed));

function formFeedback(message = '', failed = false) {
  formStatus.textContent = message;
  formStatus.classList.toggle('is-error', failed);
}

function syncReadEndToPushTime(newTime) {
  const readEnd = document.getElementById('read-end');
  if (choice('schedule-kind') === 'daily' && dateOffset.value === '0' &&
      /^\d{2}:\d{2}$/.test(newTime) &&
      readEnd.value === (editingLegacyEnd ? previousPushTime : minuteBefore(previousPushTime))) {
    readEnd.value = editingLegacyEnd ? newTime : minuteBefore(newTime);
  }
  previousPushTime = newTime;
}

function beijingPart(type) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date());
  return parts.find((part) => part.type === type).value;
}

function beijingToday() {
  return `${beijingPart('year')}-${beijingPart('month')}-${beijingPart('day')}`;
}

function shiftDay(iso, offset) {
  return new Date(Date.parse(`${iso}T00:00:00Z`) + offset * 86400000).toISOString().slice(0, 10);
}

function monthShift(month, offset) {
  const [year, value] = month.split('-').map(Number);
  return new Date(Date.UTC(year, value - 1 + offset, 1)).toISOString().slice(0, 7);
}

function setDate(iso) {
  readDate.value = iso;
  dateButton.textContent = displayDate(iso);
  calendarMonth = iso.slice(0, 7);
  renderCalendar();
}

function renderCalendar() {
  const [year, month] = calendarMonth.split('-').map(Number);
  const today = beijingToday();
  const earliest = shiftDay(today, -retentionDays);
  document.getElementById('calendar-month').textContent = `${year}年${month}月`;
  document.getElementById('calendar-prev').disabled = monthShift(calendarMonth, -1) < earliest.slice(0, 7);
  document.getElementById('calendar-next').disabled = monthShift(calendarMonth, 1) > today.slice(0, 7);
  const days = document.getElementById('calendar-days');
  days.replaceChildren();
  const firstWeekday = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  for (let i = 0; i < firstWeekday; i++) days.append(document.createElement('span'));
  const count = new Date(Date.UTC(year, month, 0)).getUTCDate();
  for (let day = 1; day <= count; day++) {
    const iso = `${calendarMonth}-${String(day).padStart(2, '0')}`;
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = String(day);
    button.disabled = iso < earliest || iso > today;
    button.className = iso === readDate.value ? 'selected' : '';
    button.setAttribute('aria-label', displayDate(iso));
    button.addEventListener('click', () => {
      setDate(iso);
      fitTodayReadEnd();
      closeCalendar();
      showDateFields();
    });
    days.append(button);
  }
}

function closeCalendar() {
  calendar.hidden = true;
  dateButton.setAttribute('aria-expanded', 'false');
}

function nextSingleTime() {
  const minutes = Number(beijingPart('hour')) * 60 + Number(beijingPart('minute'));
  return hhmm(Math.min(minutes + 3, 1439));
}

function minuteBefore(value) {
  const [hour, minute] = value.split(':').map(Number);
  return hhmm((hour * 60 + minute + 1439) % 1440);
}

function fitTodayReadEnd() {
  if (readDate.value !== beijingToday()) return;
  const readEnd = document.getElementById('read-end');
  const readStart = document.getElementById('read-start');
  const now = `${beijingPart('hour')}:${beijingPart('minute')}`;
  if (readEnd.value > now) readEnd.value = now;
  if (readStart.value >= readEnd.value) readStart.value = '00:00';
}

function displayDate(iso) {
  const [year, month, day] = iso.split('-').map(Number);
  return `${year}年${month}月${day}日`;
}

function beijingStamp(seconds) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(new Date(seconds * 1000));
  const value = (name) => parts.find((part) => part.type === name)?.value || '';
  return `${value('year')}-${value('month')}-${value('day')} ${value('hour')}:${value('minute')}`;
}

function lastReadLabel(job) {
  if (job.last_read_count == null || job.last_read_start == null || job.last_read_end == null) {
    return '暂无执行统计';
  }
  const start = beijingStamp(job.last_read_start);
  const end = beijingStamp(job.last_read_end - (job.read_end_inclusive ? 60 : 0));
  const endLabel = start.slice(0, 10) === end.slice(0, 10) ? end.slice(11) : end;
  const partial = job.last_read_partial ? '（仍有记录未纳入）' : '';
  return `${start}—${endLabel} · 已读 ${job.last_read_count} 条${partial}`;
}

function groupLabel(id) {
  return groupNames[id] ? `${groupNames[id]}（${id}）` : `群聊 ${id}`;
}

function choice(name) {
  return form.querySelector(`input[name="${name}"]:checked`).value;
}

function selectChoice(name, value) {
  const selected = form.querySelector(`input[name="${name}"][value="${value}"]`);
  if (selected) selected.checked = true;
}

function showDeliveryFields() {
  const email = choice('delivery-kind') === 'email';
  document.getElementById('email-field').hidden = !email;
  document.getElementById('reader-field').hidden = email;
  emailTo.required = email;
  reader.required = !email;
  saveButton.disabled = !source.options.length || (!email && !reader.options.length);
}

function showDateFields() {
  const fixed = choice('schedule-kind') === 'once';
  document.getElementById('daily-date-field').hidden = fixed;
  document.getElementById('fixed-read-field').hidden = !fixed;
  document.getElementById('time-label').textContent = fixed ? '今天推送时间' : '每天推送时间';
  document.getElementById('read-end-label').firstChild.textContent =
    editingLegacyEnd ? '结束时间（旧任务不含）' : '结束时间';
  document.getElementById('fixed-read-label').textContent =
    (editingLegacyEnd ? document.getElementById('read-start').value >= document.getElementById('read-end').value :
      document.getElementById('read-start').value > document.getElementById('read-end').value) ?
      '跨日结束日期' : '消息日期';
  updateRangePreview();
  if (!fixed) closeCalendar();
}

function updateRangePreview() {
  const start = document.getElementById('read-start').value;
  const end = document.getElementById('read-end').value;
  if (!start || !end) {
    document.getElementById('range-preview').textContent = '';
    return;
  }
  const overnight = editingLegacyEnd ? start >= end : start > end;
  let label;
  if (choice('schedule-kind') === 'once') {
    const endDay = readDate.value;
    const startDay = overnight ? shiftDay(endDay, -1) : endDay;
    label = `${displayDate(startDay)} ${start}—` +
      `${overnight ? `${displayDate(endDay)} ` : ''}${end}`;
  } else {
    const previous = dateOffset.value === '1';
    const endDay = previous ? '推送前一天' : '推送当天';
    const startDay = overnight ? previous ? '推送前两天' : '推送前一天' : endDay;
    label = `${startDay} ${start}—${overnight ? `${endDay} ` : ''}${end}`;
  }
  document.getElementById('range-preview').textContent =
    `读取范围：${label}${editingLegacyEnd ? '（旧任务不含结束时间）' : ''}`;
}

function leaveEdit() {
  editingId = null;
  editingLegacyEnd = false;
  document.getElementById('time').value = '20:00';
  document.getElementById('read-start').value = '08:00';
  document.getElementById('read-end').value = '19:59';
  previousPushTime = '20:00';
  formHeading.textContent = '设置自动推送';
  source.disabled = false;
  reader.disabled = false;
  document.getElementById('time').disabled = false;
  selectChoice('schedule-kind', 'daily');
  dateOffset.value = '0';
  setDate(beijingToday());
  selectChoice('delivery-kind', 'qq');
  emailTo.value = '';
  showDateFields();
  showDeliveryFields();
  cancelEdit.hidden = true;
  saveButton.textContent = '保存任务';
}

function option(select, value, label) {
  const node = document.createElement('option');
  node.value = value;
  node.textContent = label;
  select.append(node);
}

function jobField(label, value) {
  const field = document.createElement('div');
  field.className = 'job-field';
  const caption = document.createElement('span');
  caption.className = 'job-label';
  caption.textContent = label;
  const content = document.createElement('span');
  content.className = 'job-value';
  content.textContent = value;
  field.append(caption, content);
  return field;
}

function errorMessage(error) {
  return error?.message || error?.error || String(error);
}

function hhmm(minutes) {
  return `${String(Math.floor(minutes / 60)).padStart(2, '0')}:${String(minutes % 60).padStart(2, '0')}`;
}

function executionResult(job) {
  const value = job.last_status;
  if (!value || value === '待运行') return '尚未执行';
  if (value === '运行中') return '执行中：准备总结';
  if (value === '错过单次推送时间，未推送') {
    return `${displayDate(job.last_run_date || job.send_date)} ${hhmm(job.hour * 60 + job.minute)} 未推送：单次计划已过`;
  }
  if (value === '错过计划时间，未推送' || value === '超出补发时限，未推送') {
    return `${job.last_run_date ? displayDate(job.last_run_date) : '上次'} ${hhmm(job.hour * 60 + job.minute)} 未推送：已超出补发时限；下次仍按计划运行`;
  }
  if (value === 'QQ接入未恢复，未推送' && job.last_run_date) {
    return `${displayDate(job.last_run_date)} ${hhmm(job.hour * 60 + job.minute)} 未推送：QQ 接入未恢复；下次仍按计划运行`;
  }
  return value;
}

async function load() {
  leaveEdit();
  status.textContent = '正在读取任务…';
  try {
    const data = await bridge.apiGet('auto/jobs');
    groupNames = data.group_names || {};
    retentionDays = Number(data.retention_days) || 30;
    renderCalendar();
    source.replaceChildren();
    reader.replaceChildren();
    jobs.replaceChildren();
    for (const item of data.sources || []) {
      option(source, JSON.stringify(item), `${groupLabel(item.group_id)} · 接入 ${item.platform} · 机器人 ${item.bot}`);
    }
    for (const id of data.readers || []) option(reader, id, id);
    for (const job of data.jobs || []) {
      const card = document.createElement('article');
      card.className = 'job-card';
      const top = document.createElement('div');
      top.className = 'job-top';
      const group = document.createElement('h3');
      group.className = 'job-group';
      group.dataset.groupId = job.group_id;
      group.textContent = groupLabel(job.group_id);
      const target = document.createElement('div');
      target.className = 'job-target';
      target.textContent = job.delivery_kind === 'email' ? `发送至邮箱：${job.email_to}` : `发送至 QQ 私聊：${job.reader_id}`;
      top.append(group, target);
      const fixed = job.schedule_kind === 'once';
      const plan = fixed ? `${displayDate(job.send_date)} ${hhmm(job.hour * 60 + job.minute)} · 单次` :
        `每天 ${hhmm(job.hour * 60 + job.minute)}`;
      const dateLabel = fixed ? `${displayDate(job.read_date)} ` : job.date_offset === 1 ? '前一天 ' : '当天 ';
      const range = job.read_start_minute == null ? '上次成功推送后新增记录' :
        `${dateLabel}${hhmm(job.read_start_minute)}—${hhmm(job.read_end_minute)}` +
        (job.read_end_inclusive ? '' : '（旧任务：结束时间不含）');
      const meta = document.createElement('div');
      meta.className = 'job-meta';
      meta.append(jobField('推送计划', plan), jobField('消息读取范围', range));
      const lastRead = jobField('最近一次读取', lastReadLabel(job));
      lastRead.classList.add('job-read');
      meta.append(lastRead);
      const bottom = document.createElement('div');
      bottom.className = 'job-bottom';
      const result = document.createElement('div');
      result.className = 'job-status';
      const currentStatus = executionResult(job);
      if (currentStatus.startsWith('执行中')) result.classList.add('is-running');
      else if (currentStatus.startsWith('失败')) result.classList.add('is-failed');
      else if (currentStatus.startsWith('已')) result.classList.add('is-done');
      result.textContent = currentStatus;
      const actions = document.createElement('div');
      actions.className = 'actions';
      const edit = document.createElement('button');
      edit.type = 'button';
      edit.className = 'secondary';
      edit.textContent = '修改';
      edit.addEventListener('click', () => {
        const match = Array.from(source.options).find((item) => {
          const value = JSON.parse(item.value);
          return value.group_id === job.group_id && value.platform === job.platform && value.bot === job.bot;
        });
        if (!match) { notice('该群目前没有可选的记录来源。', true); return; }
        source.value = match.value;
        reader.value = job.reader_id;
        const savedTime = hhmm(job.hour * 60 + job.minute);
        const currentTime = `${beijingPart('hour')}:${beijingPart('minute')}`;
        document.getElementById('time').value = fixed &&
          (job.send_date !== beijingToday() || savedTime <= currentTime) ? nextSingleTime() : savedTime;
        previousPushTime = document.getElementById('time').value;
        document.getElementById('read-start').value = job.read_start_minute == null ?
          (job.hour === 0 && job.minute === 0 ? '23:00' : '00:00') : hhmm(job.read_start_minute);
        document.getElementById('read-end').value = job.read_end_minute == null ?
          (job.hour === 0 && job.minute === 0 ? '23:59' : minuteBefore(savedTime)) :
          hhmm(job.read_end_minute);
        selectChoice('schedule-kind', fixed ? 'once' : 'daily');
        editingLegacyEnd = job.read_end_minute != null && !job.read_end_inclusive;
        dateOffset.value = String(job.date_offset || 0);
        const today = beijingToday();
        const earliest = shiftDay(today, -retentionDays);
        const savedDate = job.read_date || today;
        setDate(savedDate < earliest ? earliest : savedDate > today ? today : savedDate);
        if (fixed) fitTodayReadEnd();
        selectChoice('delivery-kind', job.delivery_kind || 'qq');
        emailTo.value = job.email_to || '';
        showDateFields();
        showDeliveryFields();
        editingId = job.id;
        formHeading.textContent = fixed ? `修改任务 #${job.id} · 今天推送` : `修改任务 #${job.id}`;
        cancelEdit.hidden = false;
        saveButton.textContent = '保存修改';
        formFeedback();
        status.textContent = '';
        form.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'danger';
      button.textContent = '取消';
      button.addEventListener('click', async () => {
        button.disabled = true;
        button.textContent = '取消中…';
        status.textContent = `正在取消任务 #${job.id}…`;
        try {
          const result = await bridge.apiPost('auto/delete', { id: job.id });
          if (!result?.removed) throw new Error('服务端未确认删除，请刷新并查看任务是否仍在。');
          await load();
          status.textContent = '';
          notice('任务已取消。');
        } catch (error) {
          status.textContent = '';
          notice(`取消失败：${errorMessage(error)}`, true);
          button.textContent = '重试取消';
          button.disabled = false;
        }
      });
      actions.append(edit, button);
      if (fixed && job.last_run_date !== job.send_date) {
        const backfill = document.createElement('button');
        backfill.type = 'button';
        backfill.className = 'secondary';
        backfill.textContent = '补读记录';
        backfill.addEventListener('click', async () => {
          backfill.disabled = true;
          status.textContent = `正在补读${groupLabel(job.group_id)}的群历史…`;
          try {
            const result = await bridge.apiPost('auto/backfill', {id: job.id});
            await load();
            status.textContent = '';
            notice(result.message || '补读已结束。');
          } catch (error) {
            status.textContent = '';
            notice(`补读失败：${errorMessage(error)}`, true);
            backfill.disabled = false;
          }
        });
        actions.prepend(backfill);
      }
      if (job.delivery_kind === 'email' || job.last_status?.startsWith('失败：发送QQ私聊')) {
        const testSend = document.createElement('button');
        testSend.type = 'button';
        testSend.className = 'secondary';
        testSend.textContent = job.delivery_kind === 'email' ? '发送测试邮件' : '发送测试私聊';
        testSend.addEventListener('click', async () => {
          testSend.disabled = true;
          status.textContent = job.delivery_kind === 'email' ?
            `正在向 ${job.email_to} 发送测试邮件 不生成总结` :
            `正在向 QQ ${job.reader_id} 发送测试私聊 不生成总结`;
          try {
            const result = await bridge.apiPost('auto/test-send', {id: job.id});
            status.textContent = '';
            notice(result.message || '测试消息已发送。');
          } catch (error) {
            status.textContent = '';
            notice(`测试失败：${errorMessage(error)}`, true);
          } finally {
            testSend.disabled = false;
          }
        });
        actions.prepend(testSend);
      }
      bottom.append(result, actions);
      card.append(top, meta, bottom);
      jobs.append(card);
    }
    if (!jobs.childElementCount) {
      const empty = document.createElement('p');
      empty.className = 'empty-jobs';
      empty.textContent = '暂无定时推送任务。';
      jobs.append(empty);
    }
    saveButton.disabled = !source.options.length ||
      (choice('delivery-kind') === 'qq' && !reader.options.length);
    status.textContent = data.scheduler_running ? '' : '自动推送调度器当前未运行；保存任务时会尝试启动。';
  } catch (error) {
    status.textContent = '';
    notice(`读取失败：${errorMessage(error)}`, true);
  }
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  syncReadEndToPushTime(document.getElementById('time').value);
  saveButton.disabled = true;
  formFeedback('正在保存…');
  try {
    const selected = JSON.parse(source.value);
    const result = await bridge.apiPost(editingId == null ? 'auto/add' : 'auto/update', {
      ...(editingId == null ? {} : { id: editingId }),
      group_id: selected.group_id,
      platform: selected.platform,
      bot: selected.bot,
      reader_id: choice('delivery-kind') === 'email' ? '' : reader.value,
      daily_time: document.getElementById('time').value,
      read_start: document.getElementById('read-start').value,
      read_end: document.getElementById('read-end').value,
      read_end_inclusive: !editingLegacyEnd,
      schedule_kind: choice('schedule-kind'),
      date_offset: Number(dateOffset.value),
      read_date: readDate.value,
      send_date: choice('schedule-kind') === 'once' ? beijingToday() : '',
      delivery_kind: choice('delivery-kind'),
      email_to: emailTo.value,
    });
    leaveEdit();
    await load();
    formFeedback();
    status.textContent = '';
    notice(result.message || '任务已保存。');
    document.getElementById('jobs-heading').scrollIntoView({behavior: 'smooth', block: 'start'});
  } catch (error) {
    const message = `保存失败：${errorMessage(error)}`;
    formFeedback(message, true);
    status.textContent = '';
    notice(message, true);
  }
  finally { saveButton.disabled = false; }
});

cancelEdit.addEventListener('click', () => { leaveEdit(); formFeedback(); status.textContent = ''; notice('已取消修改'); });
document.getElementById('time').addEventListener('change', (event) => {
  syncReadEndToPushTime(event.target.value);
  showDateFields();
  formFeedback();
});
document.getElementById('read-start').addEventListener('change', showDateFields);
document.getElementById('read-end').addEventListener('change', () => {
  editingLegacyEnd = false;
  showDateFields();
});
dateOffset.addEventListener('change', updateRangePreview);
for (const radio of form.querySelectorAll('input[name="schedule-kind"]')) {
  radio.addEventListener('change', () => {
    if (choice('schedule-kind') === 'once' && editingId == null) {
      document.getElementById('time').value = nextSingleTime();
      fitTodayReadEnd();
    }
    showDateFields();
  });
}
for (const radio of form.querySelectorAll('input[name="delivery-kind"]')) {
  radio.addEventListener('change', showDeliveryFields);
}
document.getElementById('refresh').addEventListener('click', load);
document.addEventListener('plugin-settings-saved', load);
dateButton.addEventListener('click', () => {
  if (calendar.hidden) {
    calendarMonth = readDate.value.slice(0, 7);
    renderCalendar();
    calendar.hidden = false;
    dateButton.setAttribute('aria-expanded', 'true');
  } else closeCalendar();
});
document.getElementById('calendar-prev').addEventListener('click', () => {
  calendarMonth = monthShift(calendarMonth, -1); renderCalendar();
});
document.getElementById('calendar-next').addEventListener('click', () => {
  calendarMonth = monthShift(calendarMonth, 1); renderCalendar();
});
calendar.addEventListener('wheel', (event) => {
  event.preventDefault();
  const next = monthShift(calendarMonth, event.deltaY > 0 ? 1 : -1);
  const today = beijingToday();
  if (next >= shiftDay(today, -retentionDays).slice(0, 7) && next <= today.slice(0, 7)) {
    calendarMonth = next; renderCalendar();
  }
}, {passive: false});
document.addEventListener('click', (event) => {
  if (!event.target.closest('.calendar-wrap')) closeCalendar();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') closeCalendar();
});
setDate(beijingToday());
showDateFields();
showDeliveryFields();
await bridge.ready();
await load();
