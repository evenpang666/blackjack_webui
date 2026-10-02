const $ = id => document.getElementById(id);
const home = $('home'), game = $('game'), modal = $('modal');
const views = ['create-form', 'join-form', 'settings-form'];
let state = null, socket = null, session = null, lastResultHand = 0, reconnectTimer = null;
let leavingRoom = false, speedTimer = null;
const seenChatIds = new Set();
let renderedEventCount = 0, logVisible = true;
let pendingConfirmation = null;

function confirmAction(title, message, action) {
  pendingConfirmation = action;
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = message;
  $('confirm-panel').classList.remove('hidden');
  $('confirm-cancel').focus();
}
function cancelConfirmation() {
  pendingConfirmation = null;
  $('confirm-panel').classList.add('hidden');
}
$('confirm-cancel').onclick = cancelConfirmation;
$('confirm-accept').onclick = () => {
  const action = pendingConfirmation;
  cancelConfirmation();
  action?.();
};
$('confirm-panel').onclick = event => { if (event.target === $('confirm-panel')) cancelConfirmation(); };
document.addEventListener('keydown', event => { if (event.key === 'Escape') { cancelConfirmation(); closeModal(); } });

function showModal(which) {
  views.forEach(id => $(id).classList.toggle('hidden', id !== which));
  $('modal-error').classList.add('hidden');
  modal.classList.remove('hidden');
  if (which === 'join-form') {
    const recent = JSON.parse(localStorage.getItem('blackjack-addresses') || '[]');
    $('recent-addresses').innerHTML = recent.map(address => `<option value="${escapeHTML(address)}"></option>`).join('');
    $('join-ip').value ||= recent[0] || location.host;
    updateJoinIdentity();
  }
}
function closeModal() { modal.classList.add('hidden'); }
function error(text) { $('modal-error').textContent = text; $('modal-error').classList.remove('hidden'); }
function toast(text) { const el = $('toast'); el.textContent = text; el.classList.remove('hidden'); clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.add('hidden'), 3200); }
function escapeHTML(text) { return String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function rememberAddress(address) { const old = JSON.parse(localStorage.getItem('blackjack-addresses') || '[]'); localStorage.setItem('blackjack-addresses', JSON.stringify([address, ...old.filter(x => x !== address)].slice(0, 6))); }
function returnKeyFor(room) { return localStorage.getItem(`blackjack-return-${room.toUpperCase()}`); }
function updateJoinIdentity() {
  const returning = !!returnKeyFor($('join-room').value.trim());
  $('join-return-note').classList.toggle('hidden', !returning);
  $('join-new-fields').classList.toggle('hidden', returning);
  $('join-submit').firstChild.textContent = returning ? '恢复原座位 ' : '加入牌桌 ';
}
function validBuyin(value) { const n = Number(value); return Number.isInteger(n) && n >= 5 && n <= 1000 && n % 5 === 0 ? n : null; }
async function post(path, data) {
  const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const failure = new Error(body.error || '请求失败');
    failure.status = response.status;
    throw failure;
  }
  return body;
}
function enter(result) {
  session = result;
  if (result.returnKey) localStorage.setItem(`blackjack-return-${result.room}`, result.returnKey);
  localStorage.setItem('blackjack-last-room', result.room);
  lastResultHand = 0;
  leavingRoom = false;
  seenChatIds.clear();
  renderedEventCount = 0;
  $('chat-messages').innerHTML = '<p class="panel-empty">发送一条消息，和牌友聊聊。</p>';
  $('events').innerHTML = '<li class="panel-empty">等待牌局开始…</li>';
  setLogVisible(true);
  localStorage.setItem('blackjack-session', JSON.stringify(session));
  $('resume').classList.remove('hidden');
  $('return-room').classList.add('hidden');
  home.classList.add('hidden'); game.classList.remove('hidden'); closeModal();
  connect();
}
function connect() {
  if (!session) return;
  clearTimeout(reconnectTimer);
  const url = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws?token=${encodeURIComponent(session.token)}`;
  socket = new WebSocket(url);
  const currentSocket = socket;
  $('connection').classList.add('offline'); $('connection').lastChild.textContent = ' 连接中';
  socket.onopen = () => { $('connection').classList.remove('offline'); $('connection').lastChild.textContent = ' 已连接'; };
  socket.onmessage = event => {
    if (socket !== currentSocket) return;
    let data;
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type === 'state') render(data.state);
    if (data.type === 'chat') appendChat(data.message);
    if (data.type === 'error') { toast(data.message); if (state) render(state); }
    if (data.type === 'kicked') {
      localStorage.removeItem(`blackjack-return-${session.room}`);
      leave(data.reason);
    }
    if (data.type === 'settlement') showSettlement(data.ranking, data.houseNet);
  };
  socket.onclose = async event => {
    if (socket !== currentSocket || !session || leavingRoom) return;
    $('connection').classList.add('offline'); $('connection').lastChild.textContent = ' 重连中';
    if (event.code === 1008 || event.code === 1002) { leave('会话已失效，请重新加入'); return; }
    try {
      const check = await fetch(`/api/session?token=${encodeURIComponent(session.token)}`);
      if (check.status === 401) { leave('房间会话已失效，请重新加入'); return; }
    } catch {}
    reconnectTimer = setTimeout(connect, 1800);
  };
}
function send(type, extra = {}) {
  if (socket?.readyState === WebSocket.OPEN) { socket.send(JSON.stringify({type, ...extra})); return true; }
  toast('连接尚未恢复');
  return false;
}
function leave(message = '') {
  cancelConfirmation();
  const room = session?.room;
  session = null; state = null;
  seenChatIds.clear();
  renderedEventCount = 0;
  leavingRoom = false;
  localStorage.removeItem('blackjack-session');
  $('resume').classList.add('hidden');
  if (room) $('join-room').value = room;
  $('return-room').classList.toggle('hidden', !room || !returnKeyFor(room));
  clearTimeout(reconnectTimer);
  if (socket) { socket.onclose = null; socket.close(); socket = null; }
  game.classList.add('hidden'); home.classList.remove('hidden');
  $('result-banner').classList.add('hidden');
  if (message) toast(message);
}
function showSettlement(ranking, houseNet) {
  const room = session?.room;
  leave();
  if (room) {
    localStorage.removeItem(`blackjack-return-${room}`);
    if (localStorage.getItem('blackjack-last-room') === room) localStorage.removeItem('blackjack-last-room');
  }
  $('return-room').classList.add('hidden');
  $('settlement-list').innerHTML = ranking.map(p => `<div class="settlement-row"><span>${p.rank}</span><strong>${escapeHTML(p.name)}<small>累计带入 ¥${fmt(p.buyin)} · 剩余 ¥${fmt(p.stack)}</small></strong><b class="${p.net < 0 ? 'negative' : 'positive'}">${p.net > 0 ? '+' : ''}¥${fmt(p.net)}</b></div>`).join('') + `<p class="settings-note">电脑赌场累计净输赢 ¥${fmt(houseNet || 0)}</p>`;
  $('settlement').classList.remove('hidden');
}
function setLogVisible(visible) {
  logVisible = visible;
  $('event-panel').classList.toggle('collapsed', !visible);
  $('toggle-log').textContent = visible ? '隐藏' : '展示';
  $('toggle-log').setAttribute('aria-expanded', String(visible));
}
function renderEvents(entries) {
  if (entries.length === renderedEventCount) return;
  const list = $('events');
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 30;
  if (entries.length < renderedEventCount) {
    list.replaceChildren();
    renderedEventCount = 0;
  }
  if (entries.length) list.querySelector('.panel-empty')?.remove();
  const fragment = document.createDocumentFragment();
  for (const entry of entries.slice(renderedEventCount)) {
    const item = document.createElement('li');
    const time = document.createElement('time');
    time.textContent = entry.time;
    const text = document.createElement('span');
    text.textContent = entry.text;
    item.append(time, text);
    fragment.append(item);
  }
  if (!entries.length) {
    const empty = document.createElement('li');
    empty.className = 'panel-empty';
    empty.textContent = '等待牌局开始…';
    fragment.append(empty);
  }
  list.append(fragment);
  renderedEventCount = entries.length;
  if (atBottom) list.scrollTop = list.scrollHeight;
}
function appendChat(message) {
  if (!message || seenChatIds.has(message.id)) return;
  seenChatIds.add(message.id);
  const list = $('chat-messages');
  const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 30;
  list.querySelector('.panel-empty')?.remove();
  const item = document.createElement('div');
  item.className = `chat-message ${message.senderId === state?.you ? 'mine' : ''}`;
  item.dataset.chatId = message.id;
  const meta = document.createElement('div');
  meta.className = 'chat-meta';
  const sender = document.createElement('strong');
  sender.textContent = message.sender;
  const time = document.createElement('time');
  time.textContent = message.time;
  meta.append(sender, time);
  const body = document.createElement('div');
  body.className = 'chat-text';
  body.textContent = message.text;
  item.append(meta, body);
  const following = [...list.children].find(node => Number(node.dataset.chatId) > message.id);
  list.insertBefore(item, following || null);
  while (list.children.length > 200) {
    list.firstElementChild.remove();
  }
  if (atBottom || message.senderId === state?.you) list.scrollTop = list.scrollHeight;
}
function card(text, delay = 0, glowing = false, dealt = false) {
  const dealtClass = dealt ? ' dealt' : '';
  const delayStyle = dealt ? ` style="animation-delay:${delay}ms"` : '';
  if (text === '??') return `<div class="card back${dealtClass}"${delayStyle}></div>`;
  const rank = text[0] === 'T' ? '10' : text[0];
  const suit = {s:'♠',h:'♥',d:'♦',c:'♣'}[text[1]];
  return `<div class="card ${'hd'.includes(text[1]) ? 'red' : ''}${glowing ? ' best-card' : ''}${dealtClass}"${delayStyle}>${rank}<span>${suit}</span></div>`;
}
const fmt = amount => Number(amount).toLocaleString('zh-CN', {maximumFractionDigits:2});
const seatPositions = [[50,80],[23,72],[17,35],[83,35],[77,72]];
function render(next) {
  const previous = state;
  state = next;
  if (previous?.handNo !== next.handNo) $('result-banner').classList.add('hidden');
  $('room-code').textContent = next.room;
  $('pot').textContent = `¥${fmt(next.houseNet)}`;
  $('hand-number').textContent = `第 ${next.handNo} 局 · 单副牌 · 闲家 ${next.players.length}/5`;
  $('street-label').textContent = {waiting:'等待开局',betting:'请先下注',insurance:'保险选择',playing:'闲家行动',dealer:'庄家行动',complete:'本局结束'}[next.phase];
  $('dealer-total').textContent = next.dealerTotal === null ? '暗牌保密' : `${next.dealerTotal} 点${next.dealerTotal > 21 ? ' · 爆牌' : ''}`;
  $('cards-left').textContent = next.phase === 'waiting' ? 52 : next.cardsLeft;
  $('board').innerHTML = next.dealerCards.length ? next.dealerCards.map((c, i) => card(c, i * 60, false, !!previous && previous.dealerCards[i] !== c)).join('') : '<div class="card empty"></div><div class="card empty"></div>';
  const viewerIndex = Math.max(0, next.players.findIndex(p => p.id === next.you));
  $('seat-layer').innerHTML = next.players.map((_, j) => {
    const p = next.players[(viewerIndex + j) % next.players.length];
    const position = seatPositions[j];
    const active = next.turn === p.id;
    const win = next.result?.players?.find(row => row.id === p.id)?.net > 0;
    const kick = next.host === next.you && p.id !== next.you ? `<button class="kick-player" data-player-id="${escapeHTML(p.id)}" data-player-name="${escapeHTML(p.name)}" title="移出 ${escapeHTML(p.name)}">×</button>` : '';
    const hands = p.hands.map((h, i) => `<div class="blackjack-hand ${active && next.phase === 'playing' && next.handIndex === i ? 'current-hand' : ''}"><div class="seat-cards">${h.cards.map(c => card(c)).join('')}</div><div class="hand-caption">${p.hands.length > 1 ? `${i + 1} 手 · ` : ''}${h.blackjack ? 'Blackjack' : `${h.soft ? '软 ' : ''}${h.total} 点`}${h.total > 21 ? ' · 爆牌' : ''} · ¥${fmt(h.bet)}</div>${h.outcome ? `<div class="seat-hand">${escapeHTML(h.outcome)}</div>` : h.status === 'stand' ? '<div class="seat-hand">已停牌</div>' : h.status === 'surrender' ? '<div class="seat-hand">已投降</div>' : ''}</div>`).join('');
    const status = p.bankrupt ? '破产 · 请重新带入' : p.pendingBuyin ? '等待重新入座' : !p.connected ? '离线' : active ? '轮到你行动' : p.lastAction || '已入座';
    return `<div class="seat blackjack-seat ${active ? 'active' : ''} ${active && p.id === next.you ? 'my-turn' : ''} ${p.bankrupt ? 'bust' : ''} ${win ? 'winner' : ''}" style="--x:${position[0]}%;--y:${position[1]}%"><div class="seat-head"><span class="seat-name">${escapeHTML(p.name)}${p.id === next.you ? ' · 你' : ''}</span>${next.host === p.id ? '<span class="seat-meta">房主</span>' : ''}${kick}</div><div class="seat-stack">¥${fmt(p.stack)} <small>净 ¥${fmt(p.stack - p.buyin)}</small></div><div class="player-hands">${hands}</div><div class="seat-action">${escapeHTML(status)}</div>${p.insurance ? `<div class="insurance-badge">保险 ¥${fmt(p.insurance)}</div>` : ''}</div>`;
  }).join('');
  const my = next.players.find(p => p.id === next.you);
  const actor = next.players.find(p => p.id === next.turn);
  $('status-text').textContent = my?.pendingBuyin ? '已申请重新入座，等待下一局' : next.phase === 'waiting' ? '房主可开始下注，一人也能开局' : next.phase === 'betting' ? next.options?.bet ? '选择下注金额，或本局旁观' : '等待其他玩家完成下注' : next.phase === 'complete' ? my?.bankrupt ? '筹码已用尽，请重新带筹码入座' : '本局结束，房主可开始下一局' : next.phase === 'dealer' ? '电脑庄家自动补牌至 17 点' : actor ? `${actor.name} ${next.phase === 'insurance' ? '正在选择保险' : `正在行动 · 第 ${next.handIndex + 1} 手`}` : '等待发牌';
  renderEvents(next.events || []);
  for (const message of next.chat || []) appendChat(message);
  $('host-controls').classList.toggle('hidden', next.host !== next.you);
  $('bot-speed').value = next.botDelayMs;
  $('bot-speed-value').textContent = `${(next.botDelayMs / 1000).toFixed(1)} 秒`;
  $('start-hand').disabled = !next.canStart;
  $('start-hand').textContent = next.phase === 'complete' ? '下一局' : '开始下注';
  $('settle-game').disabled = !['waiting','complete'].includes(next.phase);
  $('rebuy-controls').classList.toggle('hidden', !next.canRebuy);
  $('rebuy-pending').classList.toggle('hidden', !my?.pendingBuyin);
  if (my?.pendingBuyin) $('rebuy-pending').textContent = `已申请带入 ¥${fmt(my.pendingBuyin)}，等待下一局。`;
  $('rebuy-submit').textContent = ['waiting','complete'].includes(next.phase) ? '重新入座' : '申请下一局入座';
  const o = next.options;
  $('bet-controls').classList.toggle('hidden', !o?.bet);
  if (o?.bet) {
    $('bet-hint').textContent = `最多 ¥${fmt(o.maxBet)} · 5 的倍数或全部筹码`;
    $('bet-amount').max = o.maxBet;
    if (Number($('bet-amount').value) > o.maxBet) $('bet-amount').value = o.maxBet;
  }
  $('insurance-controls').classList.toggle('hidden', !o?.decline);
  if (o?.decline) {
    $('insurance').disabled = !o.insurance;
    $('insurance').textContent = `保险 ¥${fmt(o.insuranceAmount)} · 赔付 2:1`;
  }
  $('action-controls').classList.toggle('hidden', !o?.stand);
  for (const action of ['hit','stand','double','split','surrender']) $(action).disabled = !o?.[action];
  if (next.phase === 'complete' && next.handNo !== lastResultHand) {
    lastResultHand = next.handNo;
    const row = next.result?.players?.find(p => p.id === next.you);
    const banner = $('result-banner');
    banner.classList.toggle('bust-banner', !!my?.bankrupt);
    banner.innerHTML = my?.bankrupt ? '<strong>筹码已用尽</strong><span>请在下方重新带入筹码</span>' : `<strong>${escapeHTML(row ? row.outcomes.join(' / ') : '本局结束')}</strong><span>${escapeHTML(row ? `本局净输赢 ${row.net > 0 ? '+' : ''}¥${fmt(row.net)} · 庄家 ${next.result.dealer}` : next.result?.dealer || '')}</span>`;
    banner.classList.remove('hidden');
    setTimeout(() => banner.classList.add('hidden'), 4200);
  }
}

$('open-create').onclick = () => showModal('create-form');
$('open-join').onclick = () => showModal('join-form');
$('open-settings').onclick = () => showModal('settings-form');
$('close-modal').onclick = closeModal;
modal.onclick = e => { if (e.target === modal) closeModal(); };
$('create-submit').onclick = async () => {
  const buyin = validBuyin($('create-buyin').value);
  if (!buyin) return error('带入金额须为 5～1000 元，且为 5 的倍数');
  try { enter(await post('/api/create', {name:$('create-name').value.trim(), buyin})); }
  catch (e) { error(e.message); }
};
$('join-submit').onclick = async () => {
  const room = $('join-room').value.trim().toUpperCase();
  if (!/^[A-Z2-9]{6}$/.test(room)) return error('请输入 6 位房间码');
  const returnKey = returnKeyFor(room);
  const buyin = returnKey ? null : validBuyin($('join-buyin').value);
  if (!returnKey && !buyin) return error('带入金额须为 5～1000 元，且为 5 的倍数');
  let origin;
  try { origin = new URL(/^https?:\/\//i.test($('join-ip').value.trim()) ? $('join-ip').value.trim() : `http://${$('join-ip').value.trim()}`).origin; }
  catch { return error('房主 IP 地址格式不正确'); }
  rememberAddress(new URL(origin).host);
  if (origin !== location.origin) {
    const url = new URL(origin);
    url.searchParams.set('join', room);
    if (!returnKey) {
      url.searchParams.set('name', $('join-name').value.trim());
      url.searchParams.set('buyin', buyin);
    }
    location.href = url.href;
    return;
  }
  try { enter(await post('/api/join', returnKey ? {room, returnKey} : {room, name:$('join-name').value.trim(), buyin})); }
  catch (e) {
    if (returnKey && e.message.includes('凭证无效')) { localStorage.removeItem(`blackjack-return-${room}`); updateJoinIdentity(); }
    error(e.message);
  }
};
$('join-room').oninput = updateJoinIdentity;
for (const action of ['hit', 'stand', 'double', 'split', 'surrender', 'insurance', 'decline']) {
  $(action).onclick = () => {
    if (send('action', {action})) {
      $('action-controls').classList.add('hidden');
      $('insurance-controls').classList.add('hidden');
    }
  };
}
$('bet-submit').onclick = () => send('action', {action:'bet', amount:Number($('bet-amount').value)});
$('bet-all').onclick = () => send('action', {action:'bet', amount:state.players.find(p => p.id === state.you).stack});
$('bet-skip').onclick = () => send('action', {action:'skip'});
$('bot-speed').oninput = () => {
  const delayMs = Number($('bot-speed').value);
  $('bot-speed-value').textContent = `${(delayMs / 1000).toFixed(1)} 秒`;
  clearTimeout(speedTimer);
  speedTimer = setTimeout(() => send('bot_speed', {delayMs}), 100);
};
$('start-hand').onclick = () => send('start');
$('rebuy-submit').onclick = () => {
  const buyin = validBuyin($('rebuy-amount').value);
  if (!buyin) return toast('带入金额须为 5～1000 元，且为 5 的倍数');
  send('rebuy', {buyin});
};
$('settle-game').onclick = () => {
  confirmAction('结束并结算', '结算后将关闭房间，并向所有在线玩家显示最终排行。', () => send('settle'));
};
$('settlement-close').onclick = () => $('settlement').classList.add('hidden');
$('seat-layer').onclick = event => {
  const kickButton = event.target.closest('.kick-player');
  if (kickButton) {
    confirmAction('移出玩家', `确定将 ${kickButton.dataset.playerName} 移出房间？`, () => send('kick', {playerId:kickButton.dataset.playerId}));
    return;
  }
};
$('toggle-log').onclick = () => setLogVisible(!logVisible);
$('chat-form').onsubmit = event => {
  event.preventDefault();
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text) return;
  if (send('chat', {text})) input.value = '';
};
$('leave').onclick = async () => {
  if (!session || leavingRoom) return;
  leavingRoom = true;
  try {
    await post('/api/leave', {token:session.token});
    leave('已退出房间');
  } catch (e) {
    if (e.status === 401 || e.status === 404) { leave('已退出房间'); return; }
    leavingRoom = false;
    toast(e.message);
  }
};
$('resume').onclick = () => { home.classList.add('hidden'); game.classList.remove('hidden'); connect(); };
$('return-room').onclick = async () => {
  const room = localStorage.getItem('blackjack-last-room');
  const returnKey = room && returnKeyFor(room);
  if (!returnKey) return toast('没有可恢复的房间');
  try { enter(await post('/api/join', {room, returnKey})); }
  catch (e) {
    if (e.message.includes('凭证无效') || e.message.includes('找不到该房间')) {
      localStorage.removeItem(`blackjack-return-${room}`);
      $('return-room').classList.add('hidden');
    }
    toast(e.message);
  }
};
$('copy-link').onclick = async () => {
  try {
    const response = await fetch('/api/network');
    const data = await response.json();
    const url = `${location.protocol}//${data.ip || location.hostname}:${location.port || 8766}/?room=${state.room}`;
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(url);
    else {
      const field = document.createElement('textarea');
      field.value = url; document.body.appendChild(field); field.select();
      if (!document.execCommand('copy')) throw new Error('copy');
      field.remove();
    }
    toast('邀请链接已复制');
  } catch { toast(`房间码：${state.room}`); }
};

(async function init() {
  const params = new URLSearchParams(location.search);
  const invitedRoom = params.get('join') || params.get('room');
  const invitedName = params.get('name');
  const invitedBuyin = Number(params.get('buyin'));
  if (invitedRoom) {
    history.replaceState(null, '', location.pathname);
    $('join-room').value = invitedRoom.toUpperCase();
    $('join-ip').value = location.host;
    if (invitedName) $('join-name').value = invitedName;
    if (validBuyin(invitedBuyin)) $('join-buyin').value = invitedBuyin;
    rememberAddress(location.host);
    const returnKey = returnKeyFor(invitedRoom);
    if (returnKey) {
      try { enter(await post('/api/join', {room:invitedRoom.toUpperCase(), returnKey})); return; }
      catch (e) {
        if (e.message.includes('凭证无效')) localStorage.removeItem(`blackjack-return-${invitedRoom.toUpperCase()}`);
        showModal('join-form'); error(e.message); return;
      }
    }
    if (invitedName && validBuyin(invitedBuyin) && params.has('join')) {
      try { enter(await post('/api/join', {room:invitedRoom.toUpperCase(), name:invitedName, buyin:invitedBuyin})); return; }
      catch (e) { showModal('join-form'); error(e.message); return; }
    }
    showModal('join-form');
    return;
  }
  try { const saved = JSON.parse(localStorage.getItem('blackjack-session') || 'null'); if (saved?.token) enter(saved); }
  catch { localStorage.removeItem('blackjack-session'); }
  if (!session) {
    const room = localStorage.getItem('blackjack-last-room');
    $('return-room').classList.toggle('hidden', !room || !returnKeyFor(room));
  }
})();
