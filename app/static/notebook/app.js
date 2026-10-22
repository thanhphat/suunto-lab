/* Sổ tập — giáo án, nhật ký và thư viện bài tập. Dữ liệu lấy từ API của Suunto Lab (/api/nb/...). */
(function () {
  'use strict';
  var EX = [], CATS = {}, BLOCKS = {}, TESTS = [], EXM = {};
  var PLAN = null, PLANS = [], J = { notes: {}, tests: [], coach: { days: {}, weeks: {} } };
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); };
  var SLOT = { N: 'Mỗi ngày', S: 'Sáng', T: 'Trưa', C: 'Chiều / tối' };
  var DOW = ['CN', 'T2', 'T3', 'T4', 'T5', 'T6', 'T7'];
  var GCAT = { test: 'Chỉ số kiểm tra', volume: 'Khối lượng tập', habit: 'Thói quen' };

  /* ---------- ngày ---------- */
  function iso(d) { return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); }
  function parse(s) { return new Date(s + 'T00:00:00'); }
  function addDays(s, n) { var d = parse(s); d.setDate(d.getDate() + n); return iso(d); }
  function fmt(s) { var d = parse(s); return DOW[d.getDay()] + ' ' + d.getDate() + '/' + (d.getMonth() + 1); }
  function today() { return iso(new Date()); }
  function clampDate(s) { return s < PLAN.start ? PLAN.start : (s > PLAN.end ? PLAN.end : s); }
  function weekOf(s) { for (var i = 0; i < PLAN.weeks.length; i++) { var w = PLAN.weeks[i]; if (s >= w.start && s <= w.end) return w; } return PLAN.weeks[s < PLAN.start ? 0 : PLAN.weeks.length - 1]; }
  function daysOf(w) { var out = [], s = w.start; while (s <= w.end) { out.push(s); s = addDays(s, 1); } return out; }
  function dayItems(d) { return PLAN.days[d] || []; }

  /* ---------- API ---------- */
  var TOKEN = (function () { try { return localStorage.getItem('suunto-lab-token') || ''; } catch (e) { return ''; } })();
  function api(path, opt) {
    opt = opt || {};
    var headers = { Authorization: 'Bearer ' + TOKEN };
    if (opt.body !== undefined) headers['Content-Type'] = 'application/json';
    return fetch(path, { method: opt.method || 'GET', headers: headers, body: opt.body === undefined ? undefined : JSON.stringify(opt.body) })
      .then(function (r) {
        if (r.status === 401) { login(); throw new Error('auth'); }
        return r.json().catch(function () { return {}; }).then(function (b) { if (!r.ok) throw new Error(b.detail || ('HTTP ' + r.status)); return b; });
      });
  }
  var pending = 0;
  function status(s) {
    var el = $('#sync'); if (!el) return;
    var map = { ok: ['Đã lưu', 'ok'], saving: ['Đang lưu…', 'busy'], error: ['Chưa lưu được, thử lại', 'warn'], wait: ['Đang kết nối…', 'busy'] };
    var m = map[s] || map.ok; el.textContent = m[0]; el.className = 'sync ' + m[1];
  }
  function write(path, body) {
    pending++; status('saving');
    return api(path, { method: 'PUT', body: body }).then(function () { if (--pending === 0) status('ok'); })
      .catch(function (e) { pending--; if (e.message !== 'auth') status('error'); throw e; });
  }
  function login() {
    $('#main').innerHTML = '<form class="login" id="lf"><h2>Nhập API token</h2><p class="small">Dùng chung token với trang Suunto Lab (biến API_TOKEN trong .env). Trình duyệt này sẽ nhớ token.</p>' +
      '<input id="tok" type="password" autocomplete="current-password" placeholder="API token" required><button>Đăng nhập</button></form>';
    $('#lf').onsubmit = function (e) { e.preventDefault(); TOKEN = $('#tok').value.trim(); try { localStorage.setItem('suunto-lab-token', TOKEN); } catch (er) { } boot(); };
  }

  /* ---------- nhật ký: tra cứu ---------- */
  function coachHtml(txt, title) {
    if (!txt) return '';
    var paras = String(txt).split(/\n+/).map(function (p) { return p.trim(); }).filter(Boolean);
    return '<section class="coach"><h3>' + esc(title) + '</h3>' + paras.map(function (p) { return p.charAt(0) === '-' ? '<p class="cb">' + esc(p.slice(1).trim()) + '</p>' : '<p>' + esc(p) + '</p>'; }).join('') + '</section>';
  }
  // Kết quả tự kiểm tra của một tuần giáo án: {test_id: {L, R, v}}, lấy lần đo mới nhất trong tuần.
  function weekTests(w) {
    var out = {};
    J.tests.filter(function (t) { return t.day >= w.start && t.day <= w.end; }).sort(function (a, b) { return a.day < b.day ? -1 : 1; })
      .forEach(function (t) { var r = out[t.test_id] = out[t.test_id] || {}; r[t.side === 'V' ? 'v' : t.side] = t.value; });
    return out;
  }

  /* ---------- trạng thái giao diện ---------- */
  var ui = { view: 'today', date: null, week: null, cat: 'all', q: '' };

  function refLabel(it) {
    var ref = it.ref || '';
    if (it.label) return it.label;
    if (ref[0] === '#' && BLOCKS[ref.slice(1)]) return BLOCKS[ref.slice(1)].name;
    if (ref[0] === '@' && EXM[ref.slice(1)]) return EXM[ref.slice(1)].name;
    return ref;
  }

  function render() {
    document.querySelectorAll('.tab').forEach(function (t) { t.setAttribute('aria-selected', t.dataset.view === ui.view ? 'true' : 'false'); });
    var main = $('#main');
    if (ui.view === 'today') return renderDay(main);
    if (ui.view === 'week') return renderWeek(main);
    if (ui.view === 'cap') return renderCap(main);
    return renderLib(main);
  }

  /* ---------- Hôm nay ---------- */
  function renderDay(main) {
    var w = weekOf(ui.date), items = dayItems(ui.date);
    var done = items.filter(function (it) { return it.done; }).length;
    var isToday = ui.date === today();
    var h = '<section class="dayhead">' +
      '<button class="nav" data-act="prev" aria-label="Ngày trước">‹</button>' +
      '<div class="daytitle"><div class="eyebrow">Tuần ' + w.n + ' · ' + esc(w.name) + '</div><h2>' + fmt(ui.date) + (isToday ? ' <span class="pill">Hôm nay</span>' : '') + '</h2>' +
      '<div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="' + items.length + '" aria-valuenow="' + done + '"><span style="width:' + (items.length ? done / items.length * 100 : 0) + '%"></span></div>' +
      '<div class="small">' + done + '/' + items.length + ' mục đã xong</div></div>' +
      '<button class="nav" data-act="next" aria-label="Ngày sau">›</button></section>';
    if (!isToday) h += '<button class="link-btn" data-act="today">Về hôm nay</button>';
    h += coachHtml(J.coach.days[ui.date], 'Nhận xét của Claude');
    ['S', 'T', 'C', 'N'].forEach(function (slot) {
      var list = items.filter(function (it) { return it.slot === slot; });
      if (!list.length) return;
      h += '<section class="slot"><h3>' + SLOT[slot] + '</h3><ul class="items">';
      list.forEach(function (it) {
        var ref = it.ref || '', on = !!it.done;
        var isBlk = ref[0] === '#' && BLOCKS[ref.slice(1)];
        h += '<li class="item' + (on ? ' done' : '') + '"><label class="chk"><input type="checkbox" data-item="' + it.id + '"' + (on ? ' checked' : '') + '><span class="box" aria-hidden="true"></span></label>' +
          '<div class="itxt"><div class="iname">' + esc(refLabel(it)) + '</div>';
        if (isBlk) {
          var b = BLOCKS[ref.slice(1)];
          if (it.label) h += '<div class="small">' + esc(b.name) + '</div>';
          h += '<details class="blk"><summary>' + b.items.length + ' bài · ~' + b.min + ' phút</summary><ol>' +
            b.items.map(function (x) { var e = EXM[x[0]]; return '<li><a href="#ex/' + x[0] + '">' + esc(e ? e.name : x[0]) + '</a><span class="dose">' + esc(x[1]) + '</span></li>'; }).join('') +
            '</ol>' + (b.note ? '<p class="note">' + esc(b.note) + '</p>' : '') + '</details>';
        } else if (ref[0] === '@' && EXM[ref.slice(1)]) {
          h += '<a class="guide" href="#ex/' + ref.slice(1) + '">Hướng dẫn: ' + esc(EXM[ref.slice(1)].name) + '</a>';
        }
        h += '</div></li>';
      });
      h += '</ul></section>';
    });
    h += '<section class="slot" id="acts" hidden><h3>Đồng hồ ghi nhận</h3><div class="acts"></div></section>';
    h += '<section class="slot"><h3>Ghi chú ngày</h3><textarea id="note" rows="4" placeholder="Sáng _/10 · Ấn _/10 · HRV · giờ ngủ · cảm giác…">' + esc(J.notes[ui.date] || '') + '</textarea></section>';
    main.innerHTML = h;
    loadActivities(ui.date);
  }
  function hms(s) { var h = Math.floor(s / 3600), m = Math.round(s % 3600 / 60); return h ? h + 'h' + String(m).padStart(2, '0') : m + '\''; }
  function paceTxt(s) { if (!s) return ''; var m = Math.floor(s / 60), sec = Math.round(s % 60); return m + ':' + String(sec).padStart(2, '0') + '/km'; }
  function loadActivities(d) {
    api('/api/activities?start=' + d + '&end=' + d + '&limit=20').then(function (acts) {
      var sec = $('#acts'); if (!sec || ui.date !== d || !acts.length) return;
      sec.hidden = false;
      $('.acts', sec).innerHTML = acts.slice().reverse().map(function (a) {
        var bits = [a.distance_km ? a.distance_km + ' km' : '', a.moving_s ? hms(a.moving_s) : '', a.ascent_m ? '+' + Math.round(a.ascent_m) + ' m' : '',
          a.hr_avg ? 'HR ' + a.hr_avg + (a.hr_max ? '/' + a.hr_max : '') : '', a.gap_pace_s_km ? 'GAP ' + paceTxt(a.gap_pace_s_km) : '', a.trimp ? 'TRIMP ' + Math.round(a.trimp) : ''].filter(Boolean);
        return '<a class="act" href="/#a' + a.id + '"><b>' + esc(a.name) + '</b><span class="am">' + esc(bits.join(' · ')) + '</span></a>';
      }).join('');
    }).catch(function () { });
  }

  /* ---------- Tuần ---------- */
  function renderWeek(main) {
    var w = ui.week || weekOf(ui.date);
    var total = w.goals.length, got = w.goals.filter(function (g) { return g.done; }).length;
    var idx = PLAN.weeks.indexOf(w), next = PLAN.weeks[idx + 1], prev = PLAN.weeks[idx - 1];
    var h = '<section class="dayhead"><button class="nav" data-act="wprev" aria-label="Tuần trước"' + (prev ? '' : ' disabled') + '>‹</button>' +
      '<div class="daytitle"><div class="eyebrow">Tuần ' + w.n + '/' + PLAN.weeks.length + ' · ' + fmt(w.start) + ' – ' + fmt(w.end) + '</div><h2>' + esc(w.name) + '</h2>' +
      '<div class="meter"><span style="width:' + (total ? got / total * 100 : 0) + '%"></span></div><div class="small">' + got + '/' + total + ' mục tiêu đạt</div></div>' +
      '<button class="nav" data-act="wnext" aria-label="Tuần sau"' + (next ? '' : ' disabled') + '>›</button></section>' +
      '<p class="focus">' + esc(w.focus) + '</p>' + coachHtml(J.coach.weeks[w.start], 'Nhận xét tuần của Claude');
    ['test', 'volume', 'habit'].forEach(function (c) {
      var list = w.goals.filter(function (g) { return g.cat === c; });
      if (!list.length) return;
      h += '<section class="slot"><h3>' + GCAT[c] + '</h3><ul class="items">' + list.map(function (g) {
        return '<li class="item' + (g.done ? ' done' : '') + '"><label class="chk"><input type="checkbox" data-goal="' + g.id + '"' + (g.done ? ' checked' : '') + '><span class="box" aria-hidden="true"></span></label><div class="itxt"><div class="iname">' + esc(g.text) + '</div></div></li>';
      }).join('') + '</ul></section>';
    });
    h += '<section class="slot"><h3>Các ngày trong tuần</h3><div class="days">' + daysOf(w).map(function (d) {
      var its = dayItems(d), n = its.filter(function (it) { return it.done; }).length;
      var pct = its.length ? Math.round(n / its.length * 100) : 0;
      return '<button class="daycell' + (d === today() ? ' is-today' : '') + '" data-go="' + d + '"><span class="dd">' + fmt(d) + '</span><span class="pct">' + pct + '%</span><span class="bar"><span style="width:' + pct + '%"></span></span></button>';
    }).join('') + '</div></section>';
    if (next) h += '<section class="slot nextwk"><h3>Tuần sau: ' + esc(next.name) + '</h3><ul class="plain">' + next.goals.map(function (g) { return '<li><span class="tag ' + g.cat + '">' + GCAT[g.cat] + '</span>' + esc(g.text) + '</li>'; }).join('') + '</ul></section>';
    main.innerHTML = h;
  }

  /* ---------- Năng lực: tự kiểm tra hằng tuần ---------- */
  function pace(v) { if (v == null || v === '') return ''; var m = Math.floor(v), sec = Math.round((v - m) * 60); return m + ':' + String(sec).padStart(2, '0'); }
  function parsePace(t) { t = String(t).trim(); if (!t) return null; if (t.indexOf(':') > 0) { var a = t.split(':'); return +a[0] + (+a[1] || 0) / 60; } var n = parseFloat(t.replace(',', '.')); return isNaN(n) ? null : n; }
  function show(tst, v) { return v == null || v === '' ? '' : (tst.fmt === 'pace' ? pace(v) : String(v)); }
  function worst(tst, rec) { if (!rec) return null; if (!tst.side) return rec.v != null ? rec.v : null; var L = rec.L, R = rec.R; if (L == null && R == null) return null; if (L == null) return R; if (R == null) return L; return tst.better === 'down' ? Math.max(L, R) : Math.min(L, R); }
  function pass(tst, val, target) { if (val == null || target == null) return null; return tst.better === 'down' ? val <= target : val >= target; }
  function target(t, i) { var a = PLAN.test_targets[t.id]; return a ? (a[i] != null ? a[i] : null) : null; }
  function renderCap(main) {
    var w = ui.week || weekOf(ui.date), idx = PLAN.weeks.indexOf(w);
    var all = PLAN.weeks.map(weekTests);
    var cur = all[idx], prev = idx > 0 ? all[idx - 1] : {};
    var withT = TESTS.filter(function (t) { return PLAN.test_targets[t.id]; });
    var done = withT.filter(function (t) { return pass(t, worst(t, cur[t.id]), target(t, idx)); }).length;
    var h = '<section class="dayhead"><button class="nav" data-act="cprev" aria-label="Tuần trước"' + (idx > 0 ? '' : ' disabled') + '>‹</button>' +
      '<div class="daytitle"><div class="eyebrow">Tự kiểm tra · Tuần ' + w.n + ' · ' + fmt(w.end) + '</div><h2>Năng lực tuần ' + w.n + '</h2>' +
      '<div class="meter"><span style="width:' + (withT.length ? done / withT.length * 100 : 0) + '%"></span></div><div class="small">' + done + '/' + withT.length + ' chỉ số đạt mục tiêu (tính bên yếu hơn)</div></div>' +
      '<button class="nav" data-act="cnext" aria-label="Tuần sau"' + (idx < PLAN.weeks.length - 1 ? '' : ' disabled') + '>›</button></section>' +
      (PLAN.test_note ? '<p class="focus">' + esc(PLAN.test_note) + '</p>' : '');
    h += '<section class="slot"><h3>Kết quả tuần này</h3>';
    var grp = '';
    TESTS.forEach(function (t) {
      if (t.group !== grp) { if (grp) h += '</div>'; grp = t.group; h += '<h4 class="capgrp">' + esc(grp) + '</h4><div class="capcards">'; }
      var tg = target(t, idx), rec = cur[t.id] || {}, pr = prev[t.id] || {};
      var tgTxt = tg == null ? 'theo dõi xu hướng' : (t.better === 'down' ? '≤ ' : '≥ ') + show(t, tg) + ' ' + t.unit;
      var prTxt = t.side ? [pr.L, pr.R].map(function (x) { return show(t, x) || '–'; }).join(' / ') : (show(t, pr.v) || '–');
      var inp = t.side ? ['L', 'R'].map(function (sd) { return '<label>' + (sd === 'L' ? 'Trái' : 'Phải') + '<input inputmode="decimal" data-test="' + t.id + '" data-side="' + sd + '" value="' + esc(show(t, rec[sd])) + '" aria-label="' + esc(t.name) + ' ' + (sd === 'L' ? 'trái' : 'phải') + '"></label>'; }).join('')
        : '<label>Kết quả<input inputmode="decimal" data-test="' + t.id + '" data-side="V" value="' + esc(show(t, rec.v)) + '" placeholder="' + (t.fmt === 'pace' ? '8:30' : '') + '" aria-label="' + esc(t.name) + '"></label>';
      var val = worst(t, rec), pv = worst(t, pr), ps = pass(t, val, tg), st = '';
      if (ps === true) st = '<span class="st ok">Đạt</span>'; else if (ps === false) st = '<span class="st no">Chưa đạt</span>';
      if (val != null && pv != null && t.better !== 'none') { var d = val - pv, good = t.better === 'down' ? d < 0 : d > 0; st += ' <span class="delta ' + (d === 0 ? '' : (good ? 'good' : 'bad')) + '">' + (d > 0 ? '+' : '') + (t.fmt === 'pace' ? Math.round(d * 60) + ' giây' : Math.round(d * 10) / 10) + ' so với tuần trước</span>'; }
      h += '<div class="capcard' + (ps === true ? ' pass' : '') + '"><div class="tname">' + (t.ex && EXM[t.ex] ? '<a href="#ex/' + t.ex + '">' + esc(t.name) + '</a>' : esc(t.name)) + '</div>' +
        '<div class="capmeta"><span>Mục tiêu: <b>' + tgTxt + '</b></span><span>Tuần trước: ' + prTxt + '</span></div>' +
        '<div class="caprow"><div class="capin">' + inp + '<span class="small">' + esc(t.unit) + '</span></div><div>' + st + '</div></div></div>';
    });
    if (grp) h += '</div>';
    h += '</section>';
    h += '<section class="slot"><h3>Diễn biến qua các tuần</h3><div class="capwrap"><table class="cap hist"><thead><tr><th>Bài</th>' + PLAN.weeks.map(function (x) { return '<th class="num">T' + x.n + '</th>'; }).join('') + '</tr></thead><tbody>' +
      TESTS.map(function (t) {
        return '<tr><td>' + esc(t.name.split(' (')[0].split(':')[0]) + '</td>' + all.map(function (v, i) {
          var r = v[t.id]; var txt = !r ? '' : (t.side ? (show(t, r.L) || '–') + '/' + (show(t, r.R) || '–') : show(t, r.v));
          var ps = pass(t, worst(t, r), target(t, i));
          return '<td class="num"' + (ps === true ? ' style="color:var(--accent)"' : ps === false ? ' style="color:var(--marker)"' : '') + '>' + (txt && txt !== '–/–' ? txt : '') + '</td>';
        }).join('') + '</tr>';
      }).join('') + '</tbody></table></div><p class="small">Xanh: đạt mục tiêu tuần đó. Cam: chưa đạt. Với bài 2 bên, đánh giá theo bên yếu hơn.</p></section>';
    main.innerHTML = h;
  }

  /* ---------- Thư viện ---------- */
  function renderLib(main) {
    var h = '<section class="libhead"><input id="q" type="search" placeholder="Tìm bài: clamshell, bắp chân, plank…" value="' + esc(ui.q) + '" aria-label="Tìm bài tập">' +
      '<div class="chips"><button class="chip" data-cat="all" aria-pressed="' + (ui.cat === 'all') + '">Tất cả</button>' +
      Object.keys(CATS).map(function (c) { return '<button class="chip" data-cat="' + c + '" aria-pressed="' + (ui.cat === c) + '">' + esc(CATS[c]) + '</button>'; }).join('') + '</div></section>';
    h += '<section class="slot"><h3>Mẫu buổi tập</h3><div class="blkgrid">' + Object.keys(BLOCKS).map(function (k) { var b = BLOCKS[k]; return '<a class="blkcard" href="#blk/' + k + '"><b>' + esc(b.name) + '</b><span>' + b.items.length + ' bài · ~' + b.min + '\'</span></a>'; }).join('') + '</div></section>';
    h += '<section class="slot"><h3>Bài tập</h3><div id="exlist" class="exlist"></div></section>';
    h += '<section class="slot tips"><h3>Mẹo đếm không sai</h3><ul class="plain"><li>Đổi số lần thành thời gian theo nhịp: 12 lần × 6 giây = 72 giây, để đồng hồ đếm hộ.</li><li>Chia nhỏ: 12 = 3 cụm × 4, nghỉ một hơi thở giữa cụm.</li><li>Đếm ngược từ 12 về 0, đếm thành tiếng.</li><li>Đặt 3 đồng xu, xong một hiệp dời một đồng.</li><li>Quên giữa chừng: làm thêm 1–2 lần, đúng kỹ thuật quan trọng hơn đúng số.</li></ul></section>';
    main.innerHTML = h; fillLib();
  }
  function fillLib() {
    var q = ui.q.trim().toLowerCase();
    var list = EX.filter(function (e) {
      if (ui.cat !== 'all' && e.cat !== ui.cat) return false;
      if (!q) return true;
      return (e.name + ' ' + e.en + ' ' + e.muscles + ' ' + e.purpose).toLowerCase().indexOf(q) >= 0;
    });
    var el = $('#exlist'); if (!el) return;
    el.innerHTML = list.length ? list.map(function (e) {
      return '<a class="excard" href="#ex/' + e.id + '"><span class="cat">' + esc(CATS[e.cat]) + '</span><b>' + esc(e.name) + '</b><span class="mus">' + esc(e.muscles) + '</span></a>';
    }).join('') : '<p class="small">Không có bài nào khớp. Thử từ khóa khác.</p>';
  }

  /* ---------- Chi tiết bài / khối ---------- */
  function sheet(html) {
    var s = $('#sheet'); $('#sheetbody').innerHTML = html; s.hidden = false; document.body.classList.add('locked');
    $('#sheetbody').scrollTop = 0; $('#closebtn').focus();
  }
  function closeSheet() { $('#sheet').hidden = true; document.body.classList.remove('locked'); }
  function list(arr, tag) { return arr && arr.length ? '<' + tag + '>' + arr.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</' + tag + '>' : ''; }
  function exHtml(e) {
    var fig = e.poses ? window.TFig.svg(e.poses, e.labels) : '';
    var used = Object.keys(BLOCKS).filter(function (k) { return BLOCKS[k].items.some(function (x) { return x[0] === e.id; }); });
    return '<div class="eyebrow">' + esc(CATS[e.cat]) + '</div><h2>' + esc(e.name) + '</h2><p class="en">' + esc(e.en) + '</p>' +
      (fig ? '<figure class="figwrap">' + fig + '<figcaption>Hình minh họa đơn giản. Đọc phần hướng dẫn để làm đúng.</figcaption></figure>' : '') +
      '<p class="lead">' + esc(e.purpose) + '</p>' +
      '<dl class="facts"><div><dt>Cơ được tập</dt><dd>' + esc(e.muscles) + '</dd></div><div><dt>Liều chuẩn</dt><dd>' + esc(e.dose) + '</dd></div>' + (e.tempo ? '<div><dt>Nhịp</dt><dd>' + esc(e.tempo) + '</dd></div>' : '') + '</dl>' +
      '<h3>Tư thế bắt đầu</h3>' + list(e.setup, 'ul') + '<h3>Các bước</h3>' + list(e.steps, 'ol') +
      '<h3>Làm đúng</h3>' + list(e.cues, 'ul') + '<h3>Lỗi hay gặp</h3>' + list(e.mistakes, 'ul') +
      '<div class="twocol"><div><h3>Dễ hơn</h3><p>' + esc(e.easier) + '</p></div><div><h3>Khó hơn</h3><p>' + esc(e.harder) + '</p></div></div>' +
      (e.stop ? '<div class="stop"><b>Dừng khi:</b> ' + esc(e.stop) + '</div>' : '') +
      (used.length ? '<p class="small">Có trong: ' + used.map(function (k) { return '<a href="#blk/' + k + '">' + esc(BLOCKS[k].name) + '</a>'; }).join(', ') + '</p>' : '');
  }
  function blkHtml(k) {
    var b = BLOCKS[k];
    return '<div class="eyebrow">Mẫu buổi tập · ~' + b.min + ' phút</div><h2>' + esc(b.name) + '</h2>' + (b.note ? '<p class="lead">' + esc(b.note) + '</p>' : '') +
      '<ol class="blklist">' + b.items.map(function (x) { var e = EXM[x[0]]; return '<li><a href="#ex/' + x[0] + '"><b>' + esc(e ? e.name : x[0]) + '</b></a><span class="dose">' + esc(x[1]) + '</span>' + (e ? '<span class="small">' + esc(e.purpose) + '</span>' : '') + '</li>'; }).join('') + '</ol>';
  }

  /* ---------- định tuyến bằng #hash ---------- */
  function route() {
    var h = (location.hash || '').slice(1), parts = h.split('/');
    if (parts[0] === 'ex' && EXM[parts[1]]) return sheet(exHtml(EXM[parts[1]]));
    if (parts[0] === 'blk' && BLOCKS[parts[1]]) return sheet(blkHtml(parts[1]));
    closeSheet();
    if (parts[0] === 'week' || parts[0] === 'lib' || parts[0] === 'today' || parts[0] === 'cap') { ui.view = parts[0]; render(); }
  }
  function back() { if (history.length > 1 && /^#(ex|blk)\//.test(location.hash)) history.back(); else { location.hash = ui.view; } }

  /* ---------- sự kiện ---------- */
  document.addEventListener('click', function (ev) {
    if (!PLAN) return;
    var t = ev.target.closest('[data-act],[data-view],[data-go],[data-cat],#closebtn,#sheet');
    if (!t) return;
    if (t.id === 'closebtn' || (t.id === 'sheet' && ev.target === t)) { back(); return; }
    if (t.dataset.view) { ui.view = t.dataset.view; if (ui.view === 'week' || ui.view === 'cap') ui.week = weekOf(ui.date); location.hash = ui.view; render(); return; }
    if (t.dataset.go) { ui.date = t.dataset.go; ui.view = 'today'; location.hash = 'today'; render(); return; }
    if (t.dataset.cat) { ui.cat = t.dataset.cat; document.querySelectorAll('.chip').forEach(function (c) { c.setAttribute('aria-pressed', c.dataset.cat === ui.cat); }); fillLib(); return; }
    var a = t.dataset.act;
    if (a === 'prev' || a === 'next') { ui.date = clampDate(addDays(ui.date, a === 'prev' ? -1 : 1)); render(); }
    else if (a === 'today') { ui.date = clampDate(today()); render(); }
    else if (a === 'wprev' || a === 'wnext' || a === 'cprev' || a === 'cnext') { var i = PLAN.weeks.indexOf(ui.week || weekOf(ui.date)); ui.week = PLAN.weeks[i + (a[1] === 'p' ? -1 : 1)] || ui.week; render(); }
    else if (a === 'export') exportData();
    else if (a === 'import') $('#importfile').click();
  });
  function findItem(id) { for (var d in PLAN.days) { var f = PLAN.days[d].filter(function (it) { return it.id === id; })[0]; if (f) return f; } return null; }
  function findGoal(id) { for (var i = 0; i < PLAN.weeks.length; i++) { var f = PLAN.weeks[i].goals.filter(function (g) { return g.id === id; })[0]; if (f) return f; } return null; }
  document.addEventListener('change', function (ev) {
    var t = ev.target;
    if (t.dataset.item || t.dataset.goal) {
      var isItem = !!t.dataset.item, id = +(t.dataset.item || t.dataset.goal), rec = isItem ? findItem(id) : findGoal(id);
      rec.done = t.checked; t.closest('.item').classList.toggle('done', t.checked); updateMeter();
      write('/api/nb/' + (isItem ? 'items/' : 'goals/') + id, { done: t.checked }).catch(function () { rec.done = !t.checked; render(); });
    }
    if (t.dataset.test) {
      var w3 = ui.week || weekOf(ui.date), tst = TESTS.filter(function (x) { return x.id === t.dataset.test; })[0], side = t.dataset.side;
      var val = tst.fmt === 'pace' ? parsePace(t.value) : (t.value.trim() === '' ? null : parseFloat(t.value.replace(',', '.')));
      if (val != null && isNaN(val)) val = null;
      J.tests = J.tests.filter(function (r) { return !(r.test_id === tst.id && r.side === side && r.day >= w3.start && r.day <= w3.end); });
      if (val != null) J.tests.push({ test_id: tst.id, day: w3.end, side: side, value: val });
      write('/api/nb/tests/' + w3.end + '/' + tst.id, { side: side, value: val }).then(render, render);
    }
    if (t.id === 'importfile' && t.files[0]) importData(t.files[0]);
    if (t.id === 'planpick') { try { localStorage.setItem('nb-plan', t.value); } catch (e) { } openPlan(t.value); }
  });
  var noteTimer;
  document.addEventListener('input', function (ev) {
    if (ev.target.id === 'q') { ui.q = ev.target.value; fillLib(); }
    if (ev.target.id === 'note') {
      var d = ui.date, v = ev.target.value; clearTimeout(noteTimer);
      noteTimer = setTimeout(function () { if (v) J.notes[d] = v; else delete J.notes[d]; write('/api/nb/notes/' + d, { note: v }).catch(function () { }); }, 600);
    }
  });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape' && !$('#sheet').hidden) back(); });
  function updateMeter() {
    var boxes = document.querySelectorAll('#main input[type=checkbox]'), n = 0; boxes.forEach(function (b) { if (b.checked) n++; });
    var m = $('#main .meter span'); if (m) m.style.width = (boxes.length ? n / boxes.length * 100 : 0) + '%';
    var s = $('#main .daytitle .small'); if (s) s.textContent = n + '/' + boxes.length + (ui.view === 'week' ? ' mục tiêu đạt' : ' mục đã xong');
  }

  /* ---------- sao lưu: cùng định dạng với bản sổ tập cũ, nhập lại được ---------- */
  function exportData() {
    var out = { app: 'so-tap-trail', plan: PLAN.id, exported: new Date().toISOString(), weeks: {}, coach: { days: J.coach.days, weeks: {} } };
    PLAN.weeks.forEach(function (w) {
      var doc = { items: {}, goals: {}, notes: {}, tests: weekTests(w) };
      daysOf(w).forEach(function (d) { dayItems(d).forEach(function (it, i) { if (it.done) doc.items[d + '|' + i] = 1; }); if (J.notes[d]) doc.notes[d] = J.notes[d]; });
      w.goals.forEach(function (g, i) { if (g.done) doc.goals['g' + i] = 1; });
      out.weeks[PLAN.id + '-w' + w.n] = doc;
      if (J.coach.weeks[w.start]) out.coach.weeks[w.n] = J.coach.weeks[w.start];
    });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([JSON.stringify(out, null, 1)], { type: 'application/json' }));
    a.download = 'so-tap-' + PLAN.id + '-' + today() + '.json'; document.body.appendChild(a); a.click(); a.remove();
  }
  function importData(file) {
    var r = new FileReader();
    r.onload = function () {
      var d; try { d = JSON.parse(r.result); } catch (e) { $('#impmsg').textContent = 'File không phải JSON.'; return; }
      api('/api/nb/import', { method: 'POST', body: d }).then(function (n) {
        $('#impmsg').textContent = 'Đã nhập: ' + n.items + ' mục, ' + n.goals + ' mục tiêu, ' + n.notes + ' ghi chú, ' + n.tests + ' kết quả, ' + n.coach + ' nhận xét.';
        openPlan(PLAN.id);
      }).catch(function (e) { $('#impmsg').textContent = e.message; });
    };
    r.readAsText(file);
  }

  /* ---------- khởi động ---------- */
  function countdown() {
    var days = Math.round((parse(PLAN.end) - parse(today())) / 864e5);
    $('#count').textContent = days > 0 ? 'Còn ' + days + ' ngày' : (days === 0 ? 'Hôm nay đua' : 'Đã đua');
  }
  function openPlan(id) {
    status('wait');
    return Promise.all([api('/api/nb/plans/' + encodeURIComponent(id))]).then(function (r) {
      PLAN = r[0];
      return api('/api/nb/journal?start=' + PLAN.weeks[0].start + '&end=' + PLAN.weeks[PLAN.weeks.length - 1].end);
    }).then(function (j) {
      J = j; ui.date = clampDate(today()); ui.week = null;
      $('#planname').textContent = PLAN.name; $('#race').textContent = PLAN.race || ''; countdown();
      document.title = 'Sổ tập · ' + PLAN.name;
      status('ok'); render();
      if (/^#(ex|blk)\//.test(location.hash)) route();
    });
  }
  function boot() {
    status('wait');
    Promise.all([api('/api/nb/library'), api('/api/nb/plans')]).then(function (r) {
      var lib = r[0]; EX = lib.exercises; CATS = lib.cats; BLOCKS = lib.blocks; TESTS = lib.tests;
      EXM = {}; EX.forEach(function (e) { EXM[e.id] = e; });
      PLANS = r[1].plans;
      if (!PLANS.length) { $('#main').innerHTML = '<p class="loading">Chưa có giáo án. Nhờ Claude tạo bằng tool save_plan, hoặc nạp file bằng <code>python -m app.notebook load-plan</code>.</p>'; status('ok'); return; }
      var saved = null; try { saved = localStorage.getItem('nb-plan'); } catch (e) { }
      var id = PLANS.some(function (p) { return p.id === saved; }) ? saved : (r[1].active || PLANS[0].id);
      var pick = $('#planpick');
      pick.innerHTML = PLANS.map(function (p) { return '<option value="' + esc(p.id) + '"' + (p.id === id ? ' selected' : '') + '>' + esc(p.name) + ' (' + p.start_date.slice(0, 7) + ')</option>'; }).join('');
      pick.hidden = PLANS.length < 2;
      return openPlan(id);
    }).catch(function (e) { if (e.message !== 'auth') { $('#main').innerHTML = '<p class="loading">Không tải được dữ liệu: ' + esc(e.message) + '</p>'; status('error'); } });
  }
  var h0 = (location.hash || '').slice(1).split('/')[0];
  if (h0 === 'week' || h0 === 'lib' || h0 === 'cap') ui.view = h0;
  window.addEventListener('hashchange', function () { if (PLAN) route(); });
  if (TOKEN) boot(); else login();
})();
