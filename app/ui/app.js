'use strict';
const $ = selector => document.querySelector(selector);
const token = $('meta[name="csrf-token"]').content;
let state, editorMode = 'import', editingId, selectedId, confirmMode, toastTimer, previewGeneration = 0;
const icons = () => window.lucide?.createIcons();
const esc = text => String(text ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon = name => `<i data-lucide="${name}"></i>`;
let loginTimer, loginRequest = false;
function showLoginStatus(result) {
  if (result.state === 'idle' && $('#login-status').textContent) return;
  $('#login-status').textContent = result.message || '';
  $('#login-start').disabled = ['waiting', 'ready'].includes(result.state);
  $('#login-name').disabled = ['waiting', 'ready'].includes(result.state);
  $('#login-save').hidden = result.state !== 'ready';
}
async function pollLogin() {
  if (loginRequest || !$('#login-dialog').open) return;
  loginRequest = true;
  try { showLoginStatus(await api('login/status', {})); }
  catch(e) { $('#login-status').textContent = e.message; }
  finally { loginRequest = false; }
}
$('#chatgpt-login').onclick = () => {
  $('#login-status').textContent = '';
  $('#login-dialog').showModal(); pollLogin();
  clearInterval(loginTimer); loginTimer = setInterval(pollLogin, 1500);
};
$('#login-form').onsubmit = async e => {
  e.preventDefault(); $('#login-start').disabled = true;
  try { showLoginStatus(await api('login/start', {name:$('#login-name').value})); }
  catch(e) { $('#login-status').textContent = e.message; $('#login-start').disabled = false; }
};
$('#login-save').onclick = async () => {
  $('#login-save').disabled = true;
  try {
    clearInterval(loginTimer);
    await api('login/finish', {}); $('#login-dialog').close();
    toast('ChatGPT 账号已保存，可在账号列表切换'); await refresh();
  } catch(e) { $('#login-status').textContent = e.message; }
  finally { $('#login-save').disabled = false; }
};
$('#login-dialog').addEventListener('close', () => {
  clearInterval(loginTimer);
  api('login/cancel', {}).catch(e => toast(e.message));
});
async function api(path, data) {
  const response = await fetch('/api/' + path, {method: data === undefined ? 'GET' : 'POST', headers: {'Content-Type':'application/json','X-CSRF-Token':token}, body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求失败');
  return result;
}
function toast(message) { clearTimeout(toastTimer); $('#toast').textContent = message; $('#toast').hidden = false; toastTimer = setTimeout(() => $('#toast').hidden = true, 5000); }
function busy() { return ['waiting','running','closing','restarting'].includes(state?.job.state); }
const selectedBackups = new Set();
let deletingBackups = false;
const canDeleteBackup = b => ['applied', 'reverted'].includes(b.state);
let maintenanceLoaded = false, maintenanceDirty = false;
const dateText = value => value ? new Date(value * 1000).toLocaleString('zh-CN') : '—';
function render() {
  const active = state.profiles.find(p => p.active || p.matches);
  $('#current-name').textContent = state.current.unsaved ? '未保存配置' : (active?.name || state.current.save_name || '当前本机配置');
  $('#conversation-recover').hidden = !state.import_recovery_required;
  $('#conversation-recover').disabled = busy();
  $('#home').textContent = state.home;
  $('#current-provider').textContent = state.current.provider;
  $('#current-model').textContent = state.current.model || 'Codex 默认';
  $('#current-files').textContent = state.current.error ? '配置需检查' : state.current.has_auth ? (state.current.has_config ? '已就绪' : '待自动生成配置') : '缺少 auth.json';
  $('#current-save-status').textContent = state.current.saved ? `已保存：${state.current.save_name}` : state.current.has_auth ? '未命名，已自动保留，可随时命名保存' : '未检测到当前 auth.json';
  $('#current-save-status').className = 'current-save-status ' + (state.current.saved ? 'saved' : 'unsaved');
  $('#onboarding').hidden = !state.current.unsaved;
  $('#onboarding-text').textContent = state.current.first_use ? '这是首次使用，请先为当前 auth.json 和 config.toml 命名；系统已自动保留临时快照。' : '当前配置尚未关联到已命名账号，系统已自动保留临时快照。';
  $('#footer-path').textContent = state.home + (state.home.includes('\\') ? '\\' : '/') + 'account-manager';
  $('#backup-path').textContent = state.home + '/account-manager/backups';
  $('#profile-count').textContent = $('#nav-count').textContent = state.profiles.length;
  $('#profiles').innerHTML = state.profiles.length ? state.profiles.map(p => `<article class="profile ${p.active || p.matches ? 'active-profile' : ''}"><div class="profile-top"><button class="avatar" data-appearance="${p.id}" data-color="${esc(p.color || '#79dfbb')}" title="更改图标颜色">${icon(p.auth_type === 'ChatGPT' ? 'user-round' : 'key-round')}</button>${p.active || p.matches ? `<span class="profile-badge">${icon('circle-check')}当前配置</span>` : `<span class="muted">${esc(p.auth_type)}</span>`}</div><h3><button class="profile-name" data-details="${p.id}">${esc(p.name)}</button></h3><div class="profile-meta"><span>${esc(p.auth_type)}</span><span>·</span><span>${p.auto_config ? '自动生成 config' : '独立配置文件'}</span></div><div class="profile-details"><span>Provider</span><span class="provider">${esc(p.provider)}</span></div><div class="profile-bottom"><button class="icon" data-rename="${p.id}" aria-label="重命名 ${esc(p.name)}" title="重命名">${icon('pencil')}</button><button class="icon" data-details="${p.id}" aria-label="编辑 ${esc(p.name)} 的配置" title="编辑配置">${icon('settings')}</button><button class="icon" data-export="${p.id}" aria-label="导出 ${esc(p.name)}" title="导出此账号" ${busy() ? 'disabled' : ''}>${icon('download')}</button><button class="icon danger" data-delete="${p.id}" aria-label="删除 ${esc(p.name)}" title="删除" ${p.active || p.matches || busy() ? 'disabled' : ''}>${icon('trash-2')}</button><button class="secondary" data-switch="${p.id}" ${busy() ? 'disabled' : ''}>${icon('arrow-right-left')}切换到此账号</button></div></article>`).join('') : '<div class="empty">暂无保存的账号</div>';
  $('#discovery-count').textContent = `${state.candidates.length} 组文件`;
  $('#candidates').innerHTML = state.candidates.length ? state.candidates.map(c => `<div class="file-row">${icon('files')}<div class="file-info"><b>${esc(c.name)}</b><small>${esc(c.auth)}${c.config ? ' + ' + esc(c.config) : ' · 自动生成 config.toml'}</small></div><button class="secondary" data-discover="${esc(c.key)}" ${busy() ? 'disabled' : ''}>${icon('download')}导入</button></div>`).join('') : '<div class="empty">没有发现其他账号文件</div>';
  const statuses = {applied:'已完成',prepared:'待恢复',recovery_required:'需要恢复',reverted:'已恢复'};
  const latest = state.backups.find(b => ['applied','prepared','recovery_required'].includes(b.state));
  const availableIds = new Set(state.backups.filter(canDeleteBackup).map(b => b.id));
  for (const id of selectedBackups) if (!availableIds.has(id)) selectedBackups.delete(id);
  $('#backup-count').textContent = state.backups.length;
  $('#backups').innerHTML = state.backups.length ? state.backups.map(b => `<div class="file-row backup-row"><input type="checkbox" data-backup-select="${esc(b.id)}" aria-label="选择备份 ${esc(b.id)}" ${selectedBackups.has(b.id) ? 'checked' : ''} ${!canDeleteBackup(b) || busy() || deletingBackups ? 'disabled' : ''}>${icon('archive-restore')}<div class="file-info"><b>${esc(b.name)} <span class="restore-state">${statuses[b.state] || esc(b.state)}</span></b><small>${esc((b.created || '').replace('T',' '))} · ${b.count || 0} 个对话 · ${esc(b.id)}</small></div><div class="backup-row-actions">${latest?.id === b.id ? `<button class="secondary" data-restore="${esc(b.id)}" ${busy() || deletingBackups ? 'disabled' : ''}>${icon('undo-2')}恢复</button>` : ''}<button class="secondary danger" data-delete-backup="${esc(b.id)}" ${!canDeleteBackup(b) || busy() || deletingBackups ? 'disabled' : ''} title="${canDeleteBackup(b) ? '永久删除此备份及关联迁移备份' : '待恢复备份不可删除'}">${icon('trash-2')}删除</button></div></div>`).join('') : '<div class="empty">暂无切换记录</div>';
  updateBackupSelection();
  $('#job').hidden = state.job.state === 'idle';
  $('#job').className = 'notice ' + state.job.state;
  const jobIcon = $('#job > svg, #job > i');
  if (jobIcon) jobIcon.outerHTML = icon(state.job.state === 'done' ? 'circle-check' : state.job.state === 'error' ? 'circle-alert' : state.job.state === 'cancelled' ? 'circle-minus' : 'loader-circle');
  $('#job-title').textContent = ({waiting:'等待退出 Codex',closing:'正在退出 Codex',restarting:'正在重启 Codex',running:'正在切换',done:'操作完成',error:'操作未完成',cancelled:'已取消'})[state.job.state] || '';
  $('#job-message').textContent = state.job.message;
  if (state.recovery_required && !busy()) {
    $('#job').hidden = false; $('#job').className = 'notice error';
    $('#job-title').textContent = '存在未完成的切换';
    $('#job-message').textContent = '请在备份记录中恢复最近一次切换。' + (state.job.state === 'error' ? state.job.message : '');
  }
  $('#cancel').hidden = !['waiting','closing'].includes(state.job.state);
  for (const id of ['launch','import','capture']) $('#' + id).disabled = busy();
  document.querySelectorAll('[data-color]').forEach(el => { if (/^#[0-9a-f]{6}$/i.test(el.dataset.color)) { el.style.color = el.dataset.color; el.style.backgroundColor = el.dataset.color + '18'; el.style.borderColor = el.dataset.color + '66'; } });
  renderMaintenance();
  icons();
}
function renderMaintenance() {
  const maintenance = state.maintenance;
  if (!maintenance) return;
  if (!maintenanceLoaded || !maintenanceDirty) {
    $('#maintenance-enabled').checked = maintenance.settings.enabled;
    $('#maintenance-probe').checked = maintenance.settings.probe;
    $('#maintenance-interval').value = maintenance.settings.interval_minutes;
    $('#maintenance-model').value = maintenance.settings.model;
    maintenanceLoaded = true;
  }
  $('#maintenance-running').textContent = maintenance.running ? '正在维护' : maintenance.settings.enabled ? '定时维护已启用' : '已暂停';
  $('#maintenance-running').classList.toggle('is-running', maintenance.running);
  updateIntervalSummary();
  $('#maintenance-save').disabled = maintenance.running || busy();
  const profiles = state.profiles.filter(p => p.auth_type === 'ChatGPT');
  const statuses = {scheduled:'已计划',running:'检查中',ok:'正常',deferred:'等待空闲',error:'失败',login_required:'需要重新登录'};
  $('#maintenance-accounts').innerHTML = profiles.length ? profiles.map(p => {
    const record = maintenance.accounts[p.id] || {};
    return `<div class="file-row" data-status="${esc(record.status || 'scheduled')}">${icon('heart-pulse')}<div class="file-info"><b>${esc(p.name)} <span class="restore-state">${statuses[record.status] || '待计划'}</span></b><small>${esc(record.message || '等待首次维护')}<br>令牌刷新：${dateText(record.last_refresh)} · 消息探测：${dateText(record.last_probe)}<br>下次检查：${record.status === 'login_required' ? '重新登录后恢复' : dateText(record.next_due)}</small></div><button class="secondary" data-maintain="${p.id}" ${maintenance.running || busy() || record.status === 'login_required' ? 'disabled' : ''}>${icon('refresh-cw')}立即维护</button></div>`;
  }).join('') : '<div class="empty">暂无 OAuth 登录账号</div>';
}
async function refresh() {
  const next = await api('state');
  if (JSON.stringify(next) !== JSON.stringify(state)) { state = next; render(); }
}
function openEditor(mode, id) {
  editorMode = mode; editingId = id;
  $('#editor-form').reset(); $('#editor-error').textContent = '';
  $('#editor-title').textContent = ({import:'添加账号',capture:'保存当前配置',rename:'重命名账号'})[mode];
  $('#file-fields').hidden = mode !== 'import';
  $('#auth-file').required = mode === 'import';
  $('#name').value = mode === 'rename' ? state.profiles.find(p => p.id === id).name : '';
  $('#editor').showModal(); $('#name').focus();
}
async function updatePreview() {
  const generation = ++previewGeneration;
  $('#confirm-go').disabled = true; $('#confirm-error').textContent = '';
  $('#preview-count').textContent = '读取中';
  try {
    const preview = await api('preview', {id:selectedId, archived:$('#archived').checked});
    if (generation !== previewGeneration) return;
    $('#preview-count').textContent = `${preview.count} 个对话`;
    $('#preview-list').innerHTML = preview.records.map(r => `<div class="preview-item"><span>${esc(r.title || '未命名对话')}</span><small>${esc(r.source)} → ${esc(preview.provider)}</small></div>`).join('');
    $('#confirm-go').disabled = false;
  } catch (e) { if (generation === previewGeneration) $('#confirm-error').textContent = e.message; }
}
async function openSwitch(id) {
  selectedId = id; confirmMode = 'switch';
  const p = state.profiles.find(p => p.id === id);
  $('#confirm-title').textContent = `切换到 ${p.name}`;
  $('#confirm-go').textContent = '切换并重启 Codex';
  $('#confirm-content').innerHTML = `<div class="confirm-summary"><span>目标 provider</span><strong>${esc(p.provider)}</strong></div><label class="check-label"><input type="checkbox" id="archived">包含已归档对话</label><div class="confirm-summary"><span>同步旧会话 provider</span><strong id="preview-count">读取中</strong></div><div id="preview-list" class="preview-list"></div><p class="warning">确认后请完全退出 Codex 桌面端、CLI 和 IDE 会话，保留此浏览器页面。继续旧对话时，历史上下文将发送给所选 provider。</p>${p.auto_config ? '<div class="form-note">将自动生成 openai 默认配置，不沿用前一账号的服务地址。</div>' : ''}`;
  $('#confirm-content .warning').textContent = '确认后将自动关闭 Codex（会中断正在进行的任务），迁移完成后重新启动。独立 CLI / IDE 会话请先退出。继续旧对话时，历史上下文将发送给所选 provider。';
  $('#confirm-error').textContent = ''; $('#confirm').showModal();
  $('#archived').onchange = updatePreview;
  await updatePreview();
}
function openRestore(id) {
  selectedId = id; confirmMode = 'restore'; ++previewGeneration;
  $('#confirm-title').textContent = '恢复切换前的状态';
  $('#confirm-go').textContent = '确认恢复'; $('#confirm-go').disabled = false;
  $('#confirm-error').textContent = '';
  $('#confirm-content').innerHTML = '<p class="warning">将自动退出 Codex，恢复此前的 auth.json、config.toml 和会话 provider，然后重新启动。正在进行的任务会中断；如果会话或配置已变化，将停止恢复。</p>';
  $('#confirm').showModal();
}
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button) return;
  if (button.classList.contains('close')) { button.closest('dialog').close(); return; }
  if (button.dataset.tab) {
    for (const name of ['accounts','backups','maintenance','conversations']) $('#' + name + '-view').hidden = button.dataset.tab !== name;
    $('#heading').textContent = $('#breadcrumb').textContent = ({accounts:'账号配置',backups:'备份记录',maintenance:'凭证维护',conversations:'对话迁移'})[button.dataset.tab];
    document.querySelectorAll('.nav').forEach(b => b.classList.toggle('active', b === button));
    if (button.dataset.tab === 'conversations' && !conversationsLoaded) await loadConversations();
    return;
  }
  try {
    if (button.dataset.export) await exportAccounts(button.dataset.export);
    if (button.dataset.appearance) openAppearance(button.dataset.appearance);
    if (button.dataset.details) await openDetails(button.dataset.details);
    if (button.dataset.rename) openEditor('rename',button.dataset.rename);
    if (button.dataset.delete) {
      const profile = state.profiles.find(p => p.id === button.dataset.delete);
      if (profile && window.confirm(`确定删除账号“${profile.name}”吗？只会删除管理器保存的副本，不会删除当前 Codex 配置。`)) {
        button.disabled = true; await api('delete',{id:button.dataset.delete}); toast('账号已删除'); await refresh();
      }
    }
    if (button.dataset.switch) await openSwitch(button.dataset.switch);
    if (button.dataset.deleteBackup) await removeBackups([button.dataset.deleteBackup]);
    if (button.dataset.restore) openRestore(button.dataset.restore);
    if (button.dataset.maintain) { button.disabled = true; await api('maintenance/run',{id:button.dataset.maintain}); toast('已提交凭据维护'); await refresh(); }
    if (button.dataset.discover) { button.disabled = true; await api('discover',{key:button.dataset.discover}); toast('账号已导入'); await refresh(); }
  } catch (e) { toast(e.message); button.disabled = false; }
});
$('#import').onclick = () => openEditor('import');
$('#export').onclick = () => exportAccounts();
let exportingAccounts = false;
async function exportAccounts(id) {
  if (exportingAccounts) return;
  if (!state?.profiles.length) return toast('暂无已保存账号可导出');
  const profile = id ? state.profiles.find(p => p.id === id) : null;
  if (id && !profile) return toast('账号不存在，请刷新页面');
  const scope = profile ? `账号“${profile.name}”` : `列表中的 ${state.profiles.length} 个已保存账号`;
  if (!window.confirm(`将导出${scope}（auth、config、名称和颜色）。ZIP 含明文登录凭据，请勿公开分享。是否下载？`)) return;
  exportingAccounts = true;
  $('#export').disabled = true;
  try {
    const label = profile ? profile.name.replace(/[\\/:*?"<>|\x00-\x1f]/g, '_').slice(0,60) + '-' : '';
    const filename = 'Codex-Accounts-' + label + new Date().toISOString().replace(/[:.]/g,'-') + '.zip';
    let handle, directory;
    if (typeof window.showSaveFilePicker === 'function') {
      handle = await window.showSaveFilePicker({suggestedName:filename, types:[{description:'ZIP 压缩包', accept:{'application/zip':['.zip']}}]});
    } else {
      directory = window.prompt('当前浏览器不支持保存位置选择器。请输入本机已有文件夹的完整路径，ZIP 将直接保存到该目录：');
      if (directory === null) return;
      directory = directory.trim();
      if (!directory) throw new Error('请输入完整文件夹路径');
    }
    const response = await fetch('/api/export', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':token}, body:JSON.stringify({...(id ? {id} : {}), ...(directory ? {directory} : {})})});
    if (!response.ok) { const error = await response.json(); throw new Error(error.error || '导出失败'); }
    if (directory) { const result = await response.json(); toast('已导出到：' + result.path); }
    else {
      const content = await response.blob();
      const writable = await handle.createWritable();
      try { await writable.write(content); await writable.close(); }
      catch(e) { await writable.abort().catch(() => {}); throw e; }
      toast('账号配置已保存到所选位置');
    }
  } catch(e) { if (e.name !== 'AbortError') toast(e.message); }
  finally { exportingAccounts = false; $('#export').disabled = false; }
};
$('#capture').onclick = () => openEditor('capture');
$('#save-current').onclick = () => openEditor('capture');
$('#refresh').onclick = async () => {
  $('#refresh').classList.add('refreshing');
  try { await refresh(); } catch (e) { toast(e.message); }
  finally { setTimeout(() => $('#refresh').classList.remove('refreshing'), 400); }
};
$('#shutdown').onclick = async () => {
  if (!window.confirm('关闭账号管理器服务？这不会关闭 Codex。')) return;
  $('#shutdown').disabled = true;
  try { await api('shutdown',{}); toast('服务已关闭'); }
  catch (e) { toast(e.message); $('#shutdown').disabled = false; }
};
$('#cancel').onclick = async () => { try { await api('cancel',{}); await refresh(); } catch (e) { toast(e.message); } };
$('#launch').onclick = async () => { try { $('#launch').disabled = true; await api('launch',{}); toast('已请求启动 Codex'); await refresh(); } catch (e) { toast(e.message); $('#launch').disabled = false; } };
$('#editor-form').onsubmit = async event => {
  event.preventDefault(); $('#save').disabled = true; $('#editor-error').textContent = '';
  try {
    const data = {name:$('#name').value.trim()};
    if (editorMode === 'import') {
      const auth = $('#auth-file').files[0], config = $('#config-file').files[0];
      if (!auth) throw new Error('请选择 auth.json');
      if (auth.size + (config?.size || 0) > 1_000_000) throw new Error('配置文件总大小不能超过 1 MB');
      data.auth = await auth.text(); data.config = config ? await config.text() : null;
    }
    if (editorMode === 'rename') data.id = editingId;
    await api(editorMode, data); $('#editor').close(); toast('账号配置已保存'); await refresh();
  } catch (e) { $('#editor-error').textContent = e.message; }
  finally { $('#save').disabled = false; }
};
$('#confirm-go').onclick = async () => {
  $('#confirm-go').disabled = true;
  try { await api(confirmMode,{id:selectedId,archived:confirmMode === 'switch' && $('#archived').checked}); $('#confirm').close(); await refresh(); }
  catch (e) { $('#confirm-error').textContent = e.message; }
  finally { $('#confirm-go').disabled = false; }
};
function updateIntervalSummary() {
  const minutes = Number($('#maintenance-interval').value);
  $('#interval-summary').textContent = minutes === 1440 ? '每天一次' : minutes > 0 && minutes % 1440 === 0 ? `每 ${minutes / 1440} 天一次` : minutes > 0 && minutes % 60 === 0 ? `每 ${minutes / 60} 小时一次` : minutes > 0 ? `每 ${minutes} 分钟一次` : '';
}
$('#maintenance-form').oninput = () => { maintenanceDirty = true; updateIntervalSummary(); };
$('#maintenance-form').onsubmit = async event => {
  event.preventDefault();
  try {
    await api('maintenance/settings', {enabled:$('#maintenance-enabled').checked,probe:$('#maintenance-probe').checked,
      interval_minutes:Number($('#maintenance-interval').value),model:$('#maintenance-model').value.trim()});
    maintenanceDirty = false; toast('维护设置已保存'); await refresh();
  } catch (e) { toast(e.message); }
};
icons(); refresh().catch(e => toast(e.message));
setInterval(() => { if (!document.hidden) refresh().catch(e => { $('#job').hidden = false; $('#job-title').textContent = '本机服务连接中断'; $('#job-message').textContent = e.message; }); }, 3000);

let detailData;
async function openDetails(id) {
  detailData = await api('details', {id});
  $('#details-title').textContent = detailData.name + ' · 配置文件';
  $('#detail-path').textContent = detailData.auth_path + '\n' + detailData.config_path;
  $('#detail-auth').value = detailData.auth;
  $('#detail-config').value = detailData.config;
  $('#detail-error').textContent = '';
  $('#details').showModal();
}
async function saveDetails() {
  detailData = await api('edit', {id:detailData.id, revision:detailData.revision,
    auth:$('#detail-auth').value, config:$('#detail-config').value});
  await refresh();
}
$('#details-form').onsubmit = async e => {
  e.preventDefault(); $('#detail-save').disabled = true;
  try { await saveDetails(); toast('配置副本已保存，旧版本已备份'); }
  catch(e) { $('#detail-error').textContent = e.message; }
  finally { $('#detail-save').disabled = false; }
};
$('#reveal-profile').onclick = async () => {
  try { await api('reveal', {id:detailData.id}); } catch(e) { $('#detail-error').textContent = e.message; }
};
$('#detail-update').onclick = async () => {
  if (!window.confirm('将保存并应用此配置，然后关闭并重启 Codex。正在进行的任务会中断，确定继续？')) return;
  $('#detail-update').disabled = true;
  try { await saveDetails(); await api('update', {id:detailData.id, restart:true}); $('#details').close(); await refresh(); }
  catch(e) { $('#detail-error').textContent = e.message; }
  finally { $('#detail-update').disabled = false; }
};
$('#details').addEventListener('close', () => { $('#detail-auth').value = ''; $('#detail-config').value = ''; detailData = null; });

function updateBackupSelection() {
  const eligible = state.backups.filter(canDeleteBackup);
  const blocked = busy() || deletingBackups || state.maintenance?.running || state.recovery_required;
  $('#backup-selected-count').textContent = `已选 ${selectedBackups.size} 条`;
  $('#backup-select-all').checked = eligible.length > 0 && eligible.every(b => selectedBackups.has(b.id));
  $('#backup-select-all').indeterminate = selectedBackups.size > 0 && !$('#backup-select-all').checked;
  $('#backup-select-all').disabled = blocked || !eligible.length;
  $('#backup-delete-selected').disabled = blocked || !selectedBackups.size;
  document.querySelectorAll('[data-delete-backup], [data-backup-select]').forEach(el => {
    const id = el.dataset.deleteBackup || el.dataset.backupSelect;
    el.disabled = blocked || !eligible.some(b => b.id === id);
  });
}
document.addEventListener('change', event => {
  const id = event.target.dataset.backupSelect;
  if (id) { event.target.checked ? selectedBackups.add(id) : selectedBackups.delete(id); updateBackupSelection(); }
});
$('#backup-select-all').onchange = event => {
  selectedBackups.clear();
  if (event.target.checked) state.backups.filter(canDeleteBackup).forEach(b => selectedBackups.add(b.id));
  render();
};
async function removeBackups(ids) {
  if (deletingBackups || !ids.length) return;
  if (!window.confirm(`确定永久删除这 ${ids.length} 条备份及其关联的会话迁移备份？删除后无法恢复这几次切换。当前配置、已保存账号和实际会话不会被删除。`)) return;
  deletingBackups = true; render();
  try {
    const result = await api('backups/delete', {ids});
    result.deleted.forEach(id => selectedBackups.delete(id));
    toast(`已删除 ${result.deleted.length} 条备份` + (result.failed.length ? `；${result.failed.length} 条未完全删除，请检查文件占用后重试` : ''));
  } catch(e) { toast(e.message); }
  finally { deletingBackups = false; await refresh().catch(e => toast(e.message)); render(); }
}
$('#backup-delete-selected').onclick = () => removeBackups([...selectedBackups]);

let appearanceId;
function openAppearance(id) {
  const profile = state.profiles.find(p => p.id === id);
  appearanceId = id;
  $('#appearance-title').textContent = profile.name + ' · 图标颜色';
  $('#appearance-color').value = profile.color || '#79dfbb';
  $('#appearance-preview').style.color = $('#appearance-color').value;
  $('#appearance-error').textContent = '';
  $('#appearance').showModal();
}
$('#appearance-color').oninput = () => { $('#appearance-preview').style.color = $('#appearance-color').value; };
$('#appearance-form').onsubmit = async event => {
  event.preventDefault(); $('#appearance-save').disabled = true;
  try { await api('color', {id:appearanceId, color:$('#appearance-color').value}); $('#appearance').close(); await refresh(); toast('图标颜色已保存'); }
  catch(e) { $('#appearance-error').textContent = e.message; }
  finally { $('#appearance-save').disabled = false; }
};

let conversationRecords = [], conversationProjects = [], conversationsLoaded = false, conversationsExporting = false, conversationExportDraft;
const selectedConversations = new Set();
function filteredConversations() {
  const query = $('#conversation-search').value.trim().toLowerCase();
  const filter = $('#conversation-filter').value;
  return conversationRecords.filter(r => (filter === 'all' || (filter === 'archived') === r.archived) &&
    (!query || [r.title, r.thread_id, r.cwd].join(' ').toLowerCase().includes(query)));
}
function renderConversations() {
  const records = filteredConversations();
  $('#conversation-count').textContent = conversationRecords.length;
  $('#conversation-selected-count').textContent = `已选 ${selectedConversations.size} 条 · 筛选显示 ${records.length} 条`;
  const select = $('#conversation-select-all');
  select.checked = records.length > 0 && records.every(r => selectedConversations.has(r.id));
  select.indeterminate = !select.checked && records.some(r => selectedConversations.has(r.id));
  select.disabled = conversationsExporting || !records.length;
  $('#conversation-export-selected').disabled = conversationsExporting || !selectedConversations.size;
  $('#conversation-export-all').disabled = conversationsExporting || !conversationRecords.length;
  $('#conversation-refresh').disabled = conversationsExporting;
  $('#conversation-list').innerHTML = records.length ? records.map(r => `<div class="file-row backup-row"><input type="checkbox" data-conversation-select="${esc(r.id)}" aria-label="选择 ${esc(r.title)}" ${selectedConversations.has(r.id) ? 'checked' : ''} ${conversationsExporting ? 'disabled' : ''}><div class="file-info"><b>${esc(r.title)} ${r.archived ? '<span class="restore-state">已归档</span>' : ''}</b><small>${esc(r.thread_id || '无索引 ID')} · ${esc(new Date(r.modified * 1000).toLocaleString('zh-CN'))} · ${(r.bytes / 1024).toFixed(1)} KB<br>${esc(r.cwd || r.path)}</small></div></div>`).join('') : '<div class="empty">没有匹配的对话</div>';
}
async function loadConversations() {
  $('#conversation-status').textContent = '正在读取本机会话索引…';
  $('#conversation-refresh').disabled = true;
  try {
    const result = await api('conversations', {});
    conversationRecords = result.records; conversationsLoaded = true;
    conversationProjects = result.projects || [];
    $('#conversation-project').innerHTML = '<option value="">选择项目</option>' + conversationProjects.map(p => `<option value="${esc(p.id)}">${esc(p.name)} · ${p.ids.length} 条对话</option>`).join('');
    const valid = new Set(conversationRecords.map(r => r.id));
    for (const id of selectedConversations) if (!valid.has(id)) selectedConversations.delete(id);
    $('#conversation-warning').textContent = result.warnings.join(' ');
    $('#conversation-status').textContent = '列表已更新。完整备份会导出全部对话，不受搜索条件限制。';
    renderConversations();
  } catch(e) { $('#conversation-status').textContent = e.message; }
  finally { $('#conversation-refresh').disabled = false; }
}
$('#conversation-refresh').onclick = loadConversations;
$('#conversation-search').oninput = renderConversations;
$('#conversation-filter').onchange = renderConversations;
$('#conversation-select-all').onchange = event => {
  filteredConversations().forEach(r => event.target.checked ? selectedConversations.add(r.id) : selectedConversations.delete(r.id));
  renderConversations();
};
document.addEventListener('change', event => {
  const id = event.target.dataset.conversationSelect;
  if (id) { event.target.checked ? selectedConversations.add(id) : selectedConversations.delete(id); renderConversations(); }
});
async function exportConversations() {
  if (conversationsExporting) return;
  if (!conversationExportDraft) return;
  const {mode, ids = []} = conversationExportDraft;
  const count = mode === 'all' ? conversationRecords.length : ids.length;
  if (!count) return toast('没有可导出的对话');
  if (!window.confirm(`将${mode === 'all' ? '完整备份全部' : '导出所选'} ${count} 条对话及本地上下文。对话正文可能包含隐私；分支对话可能包含继承的父对话内容。建议先停止生成。是否选择保存位置并导出？`)) return;
  conversationsExporting = true; renderConversations();
  try {
    let handle, directory;
    const filename = 'Codex-Conversations-' + mode + '-' + new Date().toISOString().replace(/[:.]/g,'-') + '.zip';
    if (typeof window.showSaveFilePicker === 'function') {
      handle = await window.showSaveFilePicker({suggestedName:filename, types:[{description:'ZIP 压缩包',accept:{'application/zip':['.zip']}}]});
    } else {
      directory = window.prompt('请输入本机已有文件夹的完整路径，ZIP 将保存到此目录：');
      if (directory === null) return;
      directory = directory.trim();
      if (!directory) throw new Error('请输入完整文件夹路径');
    }
    $('#conversation-status').textContent = '正在打包对话，请保持页面打开。对话较多时可能需要几分钟…';
    const response = await fetch('/api/conversations/export', {method:'POST', headers:{'Content-Type':'application/json','X-CSRF-Token':token}, body:JSON.stringify({...conversationExportDraft, ...(directory ? {directory} : {})})});
    if (!response.ok) { const result = await response.json(); throw new Error(result.error || '对话导出失败'); }
    if (directory) {
      const result = await response.json();
      $('#conversation-status').textContent = `已导出 ${result.count} 条对话到：${result.path}`;
      $('#conversation-warning').textContent = result.warnings.join(' ');
    } else {
      const writable = await handle.createWritable();
      try {
        if (response.body) await response.body.pipeTo(writable);
        else { await writable.write(await response.blob()); await writable.close(); }
      } catch(e) { await writable.abort().catch(() => {}); throw e; }
      $('#conversation-status').textContent = '对话导出已保存。导出范围和可能遗漏的记录详见包内 conversations.json。';
    }
    $('#conversation-export-preview').close();
  } catch(e) { $('#conversation-status').textContent = $('#conversation-export-error').textContent = e.name === 'AbortError' ? '已取消导出。' : e.message; }
  finally { conversationsExporting = false; renderConversations(); }
}
async function prepareConversationExport(mode) {
  if (conversationsExporting) return;
  const project = mode === 'project' ? conversationProjects.find(p => p.id === $('#conversation-project').value) : null;
  if (mode === 'project' && !project) return toast('请先选择项目');
  const ids = project ? project.ids : [...selectedConversations];
  if (mode !== 'all' && !ids.length) return toast('请选择对话');
  conversationExportDraft = {mode:mode === 'all' ? 'all' : 'selected', ...(mode !== 'all' ? {ids} : {}), ...(project ? {project_ids:[project.id]} : {})};
  $('#conversation-extra-files').value = '';
  $('#conversation-export-preview').showModal();
  await rescanConversationExport();
}
async function rescanConversationExport() {
  $('#conversation-export-save').disabled = true;
  $('#conversation-export-rescan').disabled = true;
  $('#conversation-export-error').textContent = '';
  $('#conversation-export-summary').textContent = '正在收集文件及迁移信息…';
  const draft = conversationExportDraft;
  draft.extra_paths = $('#conversation-extra-files').value.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  try {
    const result = await api('conversations/export-preview', draft);
    if (draft !== conversationExportDraft) return;
    $('#conversation-export-summary').textContent = `${draft.project_ids ? '整个项目目录' : '对话及引用文件'} · ${result.assets.length} 个文件 · ${(result.bytes / 1024 / 1024).toFixed(1)} MB（不含聊天记录大小）`;
    $('#conversation-export-files').innerHTML = result.assets.map(a => `<div class="preview-item"><span>${esc(a.source)}</span><small>${(a.bytes / 1024).toFixed(1)} KB</small></div>`).join('') || '<p>没有识别到本地文件，可手动补充。</p>';
    $('#conversation-export-warning').textContent = [...result.missing.map(x => '缺失：' + x), ...result.omitted].join('\n');
    $('#conversation-export-save').disabled = false;
  } catch(e) { $('#conversation-export-error').textContent = e.message; }
  finally { $('#conversation-export-rescan').disabled = false; }
}
$('#conversation-extra-files').oninput = () => { $('#conversation-export-save').disabled = true; };
$('#conversation-export-rescan').onclick = rescanConversationExport;
$('#conversation-export-save').onclick = exportConversations;
$('#conversation-export-selected').onclick = () => prepareConversationExport('selected');
$('#conversation-export-all').onclick = () => prepareConversationExport('all');
$('#conversation-export-project').onclick = () => prepareConversationExport('project');

let importConversationToken, importConversationRecords = [], importUploadGeneration = 0;
const importConversationIds = new Set();
$('#conversation-import').onclick = () => { $('#conversation-import-dialog').showModal(); };
$('#conversation-recover').onclick = async () => {
  if (!window.confirm('将关闭 Codex、检查并恢复中断的导入，然后重启 Codex。确定继续？')) return;
  try { await api('conversations/recover',{restart:true}); await refresh(); } catch(e) { toast(e.message); }
};
function renderImportConversations() {
  $('#conversation-import-list').innerHTML = importConversationRecords.map(r => `<label class="check-label"><input type="checkbox" data-import-conversation="${esc(r.id)}" ${r.exists ? 'disabled' : ''} ${importConversationIds.has(r.id) ? 'checked' : ''}><span>${esc(r.title)} ${r.exists ? '（已存在，跳过）' : r.archived ? '（归档）' : ''}<small class="muted">${esc(r.cwd || '无项目路径')}${r.needs_cwd ? ' · 需指定本机目录' : ''}</small></span></label>`).join('');
  const eligible = importConversationRecords.filter(r => !r.exists);
  $('#conversation-import-all').checked = !!eligible.length && eligible.every(r => importConversationIds.has(r.id));
  $('#conversation-import-all').indeterminate = importConversationIds.size > 0 && !$('#conversation-import-all').checked;
  $('#conversation-import-go').disabled = !importConversationToken || !importConversationIds.size;
}
$('#conversation-import-file').onchange = async () => {
  const generation = ++importUploadGeneration;
  const oldToken = importConversationToken;
  importConversationToken = null; importConversationRecords = []; importConversationIds.clear(); renderImportConversations();
  if (oldToken) api('conversations/discard', {token:oldToken}).catch(() => {});
  const file = $('#conversation-import-file').files[0];
  if (!file) return;
  $('#conversation-import-status').textContent = '正在上传并校验对话…';
  try {
    if (file.size > 8 * 1024 ** 3) throw new Error('ZIP 最大 8 GB，请分批导入');
    const response = await fetch('/api/conversations/upload', {method:'POST',headers:{'Content-Type':'application/zip','X-CSRF-Token':token},body:file});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '上传失败');
    if (generation !== importUploadGeneration) { api('conversations/discard',{token:result.token}).catch(() => {}); return; }
    importConversationToken = result.token; importConversationRecords = result.records;
    const portable = result.records.some(r => r.portable);
    $('#conversation-import-destination-field').hidden = !portable;
    $('#conversation-import-cwd-field').hidden = portable;
    const missing = Math.max(0, ...result.records.map(r => r.missing || 0));
    $('#conversation-import-status').textContent = `共 ${result.records.length} 条对话，请勾选要导入的内容。` + (portable ? ' 新版包将恢复生成文件及项目组织信息。' : ' 旧版包没有文件实体和完整界面元数据。') + (missing ? ` 注意：源电脑导出时有 ${missing} 个文件引用缺失，无法恢复这些文件。` : '');
    renderImportConversations();
  } catch(e) { if (generation === importUploadGeneration) $('#conversation-import-status').textContent = e.message; }
};
$('#conversation-import-all').onchange = e => {
  importConversationIds.clear();
  if (e.target.checked) importConversationRecords.filter(r => !r.exists).forEach(r => importConversationIds.add(r.id));
  renderImportConversations();
};
document.addEventListener('change', e => {
  const id = e.target.dataset.importConversation;
  if (id) { e.target.checked ? importConversationIds.add(id) : importConversationIds.delete(id); renderImportConversations(); }
});
$('#conversation-import-form').onsubmit = async e => {
  e.preventDefault();
  if (!importConversationToken || !importConversationIds.size) return;
  if (!window.confirm(`导入 ${importConversationIds.size} 条对话并重启 Codex？正在进行的任务将中断。`)) return;
  $('#conversation-import-go').disabled = true;
  try {
    await api('conversations/import',{token:importConversationToken,ids:[...importConversationIds],cwd:$('#conversation-import-cwd').value.trim(),destination:$('#conversation-import-destination').value.trim(),restart:true});
    importConversationToken = null; importConversationIds.clear(); importConversationRecords = [];
    $('#conversation-import-file').value = ''; renderImportConversations();
    $('#conversation-import-dialog').close(); conversationsLoaded = false; await refresh();
  } catch(e) { $('#conversation-import-status').textContent = e.message; renderImportConversations(); }
};
$('#conversation-import-dialog').addEventListener('close', () => {
  ++importUploadGeneration;
  if (importConversationToken) api('conversations/discard',{token:importConversationToken}).catch(() => {});
  importConversationToken = null; importConversationIds.clear(); importConversationRecords = [];
  $('#conversation-import-file').value = ''; renderImportConversations();
});
