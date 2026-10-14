/* 骛（myagent）· 网页前端（零依赖，原生 fetch + SSE 解析） */
(() => {
  const $ = (id) => document.getElementById(id);
  const log = $('log'), input = $('in'), sendBtn = $('send'), stopBtn = $('stop');
  const panel = $('panel'), panelBody = $('panel-body'), panelTitle = $('panel-title');

  const S = {
    token: localStorage.getItem('myagent_token') || '',
    user: localStorage.getItem('myagent_user') || 'default',
    session: localStorage.getItem('myagent_session') || '',
    busy: false,
  };

  // ---------- 小工具 ----------
  const esc = (s) => String(s ?? '');
  function atBottom() { return log.scrollHeight - log.scrollTop - log.clientHeight < 120; }
  function scroll(force) { if (force || atBottom()) log.scrollTop = log.scrollHeight; }
  function node(html) { const d = document.createElement('div'); d.innerHTML = html; return d.firstElementChild; }

  function bubble(who, text) {
    const row = node(`<div class="row ${who === 'me' ? 'me' : ''}"><div class="bubble"></div></div>`);
    row.firstElementChild.textContent = text;
    log.appendChild(row); scroll(true); return row.firstElementChild;
  }
  function evt(html) { const el = node(`<div class="evt">${html}</div>`); log.appendChild(el); scroll(); return el; }
  function chip(kind, text, cls = '') { return evt(`<span class="chip ${kind} ${cls}">${text}</span>`); }

  function thoughtLine(text) {
    const el = node('<div class="evt"><div class="thought"></div></div>');
    el.firstElementChild.textContent = text;
    log.appendChild(el); scroll(); return el;
  }

  function toolCard(p) {
    const el = node(`<details class="tool"><summary></summary><pre></pre></details>`);
    el.querySelector('summary').textContent = `⚙ ${esc(p.name)} ${JSON.stringify(p.arguments ?? p.args ?? {})}`;
    log.appendChild(el); scroll(); return el;
  }

  // ---------- 状态 ----------
  function setBusy(b) {
    S.busy = b; sendBtn.disabled = b; stopBtn.hidden = !b; $('dot').className = 'dot ' + (b ? 'wait' : 'ok');
  }

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      ...opts,
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + S.token, ...(opts.headers || {}) },
    });
    return r;
  }

  async function loadMeta() {
    try {
      const r = await api('/v1/health');
      if (r.status === 401) { $('dot').className = 'dot bad'; $('meta').textContent = '需要口令'; openPanel('settings'); return; }
      const j = await r.json(); const d = j.data || {};
      if (d.app_name) { document.title = d.app_name; const el = $('app-name'); if (el) el.textContent = d.app_name; }
      $('dot').className = 'dot ok';
      $('meta').textContent = `${d.provider}/${d.model} · 工具 ${d.tools}`;
    } catch (e) { $('dot').className = 'dot bad'; $('meta').textContent = '连不上内核'; }
  }

  // ---------- 发送 ----------
  async function send() {
    const text = input.value.trim();
    if (!text || S.busy) return;
    if (!S.token) { openPanel('settings'); bubble('bot', '先填一个口令（Bearer token）再聊。'); return; }
    input.value = ''; input.style.height = 'auto';
    bubble('me', text);
    setBusy(true);
    let buf = '';
    try {
      const r = await fetch('/v1/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + S.token },
        body: JSON.stringify({ text, user_id: S.user, channel: 'webhook', session_id: S.session || undefined }),
      });
      if (r.status === 401) { setBusy(false); openPanel('settings'); bubble('bot', '口令不对（401）。'); return; }
      if (!r.body) { throw new Error('无响应体'); }
      const reader = r.body.getReader(), dec = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
          const line = chunk.split('\n').find((l) => l.startsWith('data:'));
          if (line) { try { render(JSON.parse(line.slice(5).trim())); } catch (e) { /* 忽略脏帧 */ } }
        }
      }
    } catch (e) {
      bubble('bot', `（连接中断：${e.message}）`);
    } finally {
      setBusy(false); loadMeta();
    }
  }

  function render(ev) {
    const p = ev.payload || {};
    switch (ev.type) {
      case 'memory_hit':
        if (p.count) chip('', `记忆命中 ${p.count}`, 'state'); break;
      case 'state_change':
        chip('state', `${p.from} → ${p.to}`); break;
      case 'thought':
        if (p.text) thoughtLine(p.text); break;
      case 'tool_call':
        chip('tool', `⚙ ${esc(p.name)} ${JSON.stringify(p.arguments ?? {})}`); break;
      case 'tool_result':
        chip('tool', `↳ ${p.ok ? 'ok' : 'fail'}${p.content ? ' · ' + String(p.content).slice(0, 60) : ''}`, p.ok ? 'ok' : 'bad'); break;
      case 'approval_required':
        chip('', `需要确认：${esc(p.name)}`, 'bad'); break;
      case 'error':
        evt(`<div class="err">！${esc((p.error || {}).code || '')} ${esc((p.error || {}).message || '')}</div>`); break;
      case 'final': {
        const ans = p.answer ?? (p.reply && p.reply.text) ?? '';
        const b = bubble('bot', ans || '（无输出）');
        const u = p.usage || {}; const c = p.cost || {};
        const bits = [`${p.status}`, `steps ${p.steps ?? '-'}`,
          `tokens ${(u.prompt_tokens ?? 0)}+${(u.completion_tokens ?? 0)}`];
        if (c.total != null) bits.push(`费用 ¥${Number(c.total).toFixed(4)}`);
        const m = document.createElement('div'); m.className = 'meta'; m.textContent = bits.join(' · ');
        b.appendChild(m);
        if (p.session_id) { S.session = p.session_id; localStorage.setItem('myagent_session', S.session); }
        break;
      }
    }
  }

  // 停止
  async function stop() {
    try { await api('/v1/cancel', { method: 'POST', body: '{}' }); chip('', '已请求中断', 'state'); } catch (e) { /* ignore */ }
  }

  // ---------- 面板 ----------
  function openPanel(kind) {
    panel.hidden = false;
    if (kind === 'settings') {
      panelTitle.textContent = '设置';
      panelBody.innerHTML = `
        <label>口令（Bearer token）</label>
        <input id="f-token" type="password" placeholder="config.api.tokens 里的值 / 启动时打印的临时口令">
        <label>用户 id（决定记忆会话）</label>
        <input id="f-user" placeholder="default">
        <label>会话 id（留空＝按用户自动路由）</label>
        <input id="f-session" placeholder="（可留空）">
        <div class="list"><div class="item" id="f-save">保存</div><div class="item" id="f-clear">清空会话绑定</div></div>
        <div class="hint">口令存在本机浏览器里（localStorage），不会上传别处。</div>`;
      $('f-token').value = S.token; $('f-user').value = S.user; $('f-session').value = S.session;
      $('f-save').onclick = () => {
        S.token = $('f-token').value.trim(); S.user = $('f-user').value.trim() || 'default'; S.session = $('f-session').value.trim();
        localStorage.setItem('myagent_token', S.token); localStorage.setItem('myagent_user', S.user);
        localStorage.setItem('myagent_session', S.session);
        panel.hidden = true; loadMeta();
      };
      $('f-clear').onclick = () => { S.session = ''; localStorage.removeItem('myagent_session'); $('f-session').value = ''; };
    } else if (kind === 'sessions') {
      panelTitle.textContent = '会话';
      api('/v1/sessions').then(async (r) => {
        if (r.status === 401) { panelBody.innerHTML = '<div class="hint">口令不对。</div>'; return; }
        const d = (await r.json()).data || {};
        panelBody.innerHTML = '<div class="hint">点一条即绑定该会话（跨入口共享记忆）</div><div class="list" id="s-list"></div>';
        (d.sessions || []).forEach((s) => {
          const it = node(`<div class="item"><b>${esc(s.id)}</b><div class="t">${esc(s.user_id)} · ${esc(s.channel)} · ${s.message_count ?? 0} 条</div></div>`);
          it.onclick = () => { S.session = s.id; localStorage.setItem('myagent_session', s.id); panel.hidden = true; bubble('bot', `已绑定会话 ${s.id}`); };
          $('s-list').appendChild(it);
        });
      });
    } else {
      panelTitle.textContent = '工具';
      api('/v1/tools').then(async (r) => {
        const d = r.status === 401 ? {} : (await r.json()).data || {};
        panelBody.innerHTML = '<div class="list" id="t-list"></div>';
        (d.tools || []).forEach((t) => {
          $('t-list').appendChild(node(
            `<div class="item"><b>${esc(t.name)}</b> <span class="t">[${esc(t.permission)}]</span><div class="t">${esc(t.description || '')}</div></div>`));
        });
      });
    }
  }

  // ---------- 事件绑定 ----------
  sendBtn.onclick = send;
  stopBtn.onclick = stop;
  $('btn-set').onclick = () => openPanel('settings');
  $('btn-sessions').onclick = () => openPanel('sessions');
  $('btn-tools').onclick = () => openPanel('tools');
  $('panel-close').onclick = () => (panel.hidden = true);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 160) + 'px'; });

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  loadMeta();
  if (!S.token) { bubble('bot', '第一次用：点右上角「设置」填口令，再回来聊。'); openPanel('settings'); }
  else { bubble('bot', '在。有事直接说。'); }
})();
