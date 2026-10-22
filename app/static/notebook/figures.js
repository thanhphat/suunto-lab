/* Vẽ hình người que nhìn từ bên hông (mặt hướng sang phải).
   Pose: {h:[x,y], t: góc thân (0 = thẳng đứng, + nghiêng về trước/phải, -90 = nằm, đầu bên trái),
          n:[đùi, cẳng chân, bàn chân?] chân gần, f:[...] chân xa,
          an:[cánh tay, cẳng tay] tay gần, af:[...] tay xa,
          props:[...] đồ vật, arrows:[[x1,y1,x2,y2],...] hướng chuyển động }
   Góc chi: 0 = thẳng xuống, 90 = chĩa ngang ra trước (phải), -90 = ra sau (trái), 180 = thẳng lên. */
(function (g) {
  var L = { trunk: 52, neck: 7, head: 9, thigh: 40, shin: 38, foot: 13, ua: 28, fa: 25 };
  function rad(d) { return d * Math.PI / 180; }
  function limb(p, ang, len) { return [p[0] + Math.sin(rad(ang)) * len, p[1] + Math.cos(rad(ang)) * len]; }
  function up(p, ang, len) { return [p[0] + Math.sin(rad(ang)) * len, p[1] - Math.cos(rad(ang)) * len]; }
  function ln(a, b, cls) { return '<line class="' + cls + '" x1="' + a[0].toFixed(1) + '" y1="' + a[1].toFixed(1) + '" x2="' + b[0].toFixed(1) + '" y2="' + b[1].toFixed(1) + '"/>'; }
  function leg(h, l, cls) {
    var k = limb(h, l[0], L.thigh), a = limb(k, l[1], L.shin);
    var fa = l.length > 2 ? l[2] : 90;
    var t = limb(a, fa, L.foot);
    return ln(h, k, cls) + ln(k, a, cls) + ln(a, t, cls + ' foot');
  }
  function arm(s, a, cls) {
    var e = limb(s, a[0], L.ua), w = limb(e, a[1], L.fa);
    return ln(s, e, cls) + ln(e, w, cls);
  }
  function prop(p) {
    switch (p.t) {
      case 'box': return '<rect class="prop" x="' + p.x + '" y="' + p.y + '" width="' + p.w + '" height="' + p.h + '" rx="2"/>';
      case 'wall': return '<line class="wall" x1="' + p.x + '" y1="' + (p.y1 || 20) + '" x2="' + p.x + '" y2="175"/>';
      case 'bar': return '<line class="wall" x1="' + p.x1 + '" y1="' + p.y + '" x2="' + p.x2 + '" y2="' + p.y + '"/>';
      case 'band': return '<path class="band" d="' + p.d + '"/>';
      case 'roller': return '<circle class="prop" cx="' + p.x + '" cy="' + p.y + '" r="' + (p.r || 9) + '"/>';
      case 'weight': return '<rect class="prop solid" x="' + (p.x - 5) + '" y="' + (p.y - 4) + '" width="10" height="12" rx="2"/>';
      case 'pole': return '<line class="pole" x1="' + p.x1 + '" y1="' + p.y1 + '" x2="' + p.x2 + '" y2="' + p.y2 + '"/>';
      case 'slope': return '<path class="prop" d="M' + p.x1 + ' ' + p.y1 + ' L' + p.x2 + ' ' + p.y2 + ' L' + p.x2 + ' 175 L' + p.x1 + ' 175 Z"/>';
      case 'text': return '<text class="lbl" x="' + p.x + '" y="' + p.y + '">' + p.s + '</text>';
      case 'arc': return '<path class="arc" d="M' + (p.cx - p.r) + ' ' + p.cy + ' A' + p.r + ' ' + p.r + ' 0 1 1 ' + p.cx + ' ' + (p.cy + p.r) + '" marker-end="url(#ah)"/>';
      case 'wheel': return '<circle class="wheel" cx="' + p.x + '" cy="' + p.y + '" r="' + (p.r || 24) + '"/>';
      case 'sun': var r = '<circle class="sun" cx="' + p.x + '" cy="' + p.y + '" r="9"/>'; for (var i = 0; i < 8; i++) { var aa = i * Math.PI / 4; r += '<line class="sunray" x1="' + (p.x + Math.cos(aa) * 13).toFixed(1) + '" y1="' + (p.y + Math.sin(aa) * 13).toFixed(1) + '" x2="' + (p.x + Math.cos(aa) * 19).toFixed(1) + '" y2="' + (p.y + Math.sin(aa) * 19).toFixed(1) + '"/>'; } return r;
      case 'post': return '<line class="wall" x1="' + p.x + '" y1="' + p.y + '" x2="' + p.x + '" y2="175"/><circle class="prop solid" cx="' + p.x + '" cy="' + p.y + '" r="3"/>';
      default: return '';
    }
  }
  function figure(pose) {
    if (pose.raw) return pose.raw;
    var h = pose.h, t = pose.t || 0, out = '';
    (pose.props || []).forEach(function (p) { out += prop(p); });
    if (pose.front) {
      var sh0 = up(h, t, L.trunk), nk0 = up(sh0, pose.nk != null ? pose.nk : t, L.neck), hd0 = up(nk0, pose.nk != null ? pose.nk : t, L.head);
      var hl = [h[0] - 8, h[1]], hr = [h[0] + 8, h[1]], sl = [sh0[0] - 17, sh0[1]], sr = [sh0[0] + 17, sh0[1]];
      out += ln(hl, hr, 'body') + ln(sl, sr, 'body') + ln(h, sh0, 'body') + ln(sh0, nk0, 'body');
      var pn = pose.n || [6, 0];
      out += leg(hr, pn, 'near') + leg(hl, pose.f || pn.map(function (a) { return -a; }), 'near');
      out += arm(sr, pose.an || [10, 10], 'near') + arm(sl, pose.af || (pose.an || [10, 10]).map(function (a) { return -a; }), 'near');
      out += '<circle class="head" cx="' + hd0[0].toFixed(1) + '" cy="' + hd0[1].toFixed(1) + '" r="' + L.head + '"/>';
    } else {
      var sh = up(h, t, L.trunk), na = pose.nk != null ? pose.nk : t, nk = up(sh, na, L.neck), hd = up(nk, na, L.head);
      out += leg(h, pose.f || pose.n, 'far');
      out += arm(sh, pose.af || pose.an || [0, 0], 'far');
      out += ln(h, sh, 'body') + ln(sh, nk, 'body');
      out += '<circle class="head" cx="' + hd[0].toFixed(1) + '" cy="' + hd[1].toFixed(1) + '" r="' + L.head + '"/>';
      if (pose.face) { var fx = hd[0] + Math.cos(rad(na)) * 7, fy = hd[1] + Math.sin(rad(na)) * 7; out += '<circle class="eye" cx="' + fx.toFixed(1) + '" cy="' + (fy - 1).toFixed(1) + '" r="1.8"/>'; }
      out += leg(h, pose.n, 'near');
      out += arm(sh, pose.an || [0, 0], 'near');
    }
    (pose.arrows || []).forEach(function (a) {
      out += '<line class="arrow" x1="' + a[0] + '" y1="' + a[1] + '" x2="' + a[2] + '" y2="' + a[3] + '" marker-end="url(#ah)"/>';
    });
    (pose.notes || []).forEach(function (n) { out += '<text class="lbl" x="' + n[0] + '" y="' + n[1] + '"' + (n[3] ? ' text-anchor="' + n[3] + '"' : '') + '>' + n[2] + '</text>'; });
    return out;
  }
  /* Bàn chân nhìn từ trên xuống: toes = [cái, 2, 3, 4, 5] trạng thái 'up' (nhấc) | 'down' | 'hl'; spread = độ xòe */
  function footTop(cx, opt) {
    opt = opt || {}; var sp = opt.spread || 0, o = '';
    o += '<path class="footsh" d="M' + (cx - 16) + ' 168 C' + (cx - 26) + ' 150 ' + (cx - 30) + ' 110 ' + (cx - 26) + ' 82 C' + (cx - 22) + ' 66 ' + (cx + 20) + ' 64 ' + (cx + 26) + ' 82 C' + (cx + 30) + ' 104 ' + (cx + 22) + ' 150 ' + (cx + 14) + ' 168 C' + (cx + 8) + ' 178 ' + (cx - 10) + ' 178 ' + (cx - 16) + ' 168 Z"/>';
    var toes = [[-18, 58, 9], [-5, 54, 6.5], [6, 56, 6], [15, 61, 5.5], [23, 68, 5]];
    toes.forEach(function (tt, i) {
      var st = (opt.toes || [])[i] || 'down', dx = sp * (i - 1.2) * 2.2;
      o += '<circle class="toe ' + st + '" cx="' + (cx + tt[0] + dx) + '" cy="' + (tt[1] - (st === 'up' ? 6 : 0)) + '" r="' + tt[2] + '"/>';
    });
    return o;
  }
  /* Bàn chân nhìn ngang: arch = độ cao vòm (0..1) */
  function footSide(x, arch, extra) {
    var a = 6 + arch * 14, o = '';
    o += '<path class="footsh" d="M' + x + ' 172 C' + (x - 6) + ' 150 ' + (x + 2) + ' 128 ' + (x + 22) + ' 124 C' + (x + 50) + ' 120 ' + (x + 70) + ' 140 ' + (x + 112) + ' 158 C' + (x + 124) + ' 162 ' + (x + 126) + ' 172 ' + (x + 116) + ' 173 L' + (x + 92) + ' 173 C' + (x + 70) + ' ' + (173 - a) + ' ' + (x + 40) + ' ' + (173 - a) + ' ' + (x + 22) + ' 173 Z"/>';
    o += '<line class="shin" x1="' + (x + 18) + '" y1="126" x2="' + (x + 14) + '" y2="40"/>';
    return o + (extra || '');
  }
  /* Thân trên nhìn từ trên xuống, góc tay so với hướng trước mặt */
  function topArms(ang, band) {
    var cx = 100, cy = 120, o = '<ellipse class="footsh" cx="100" cy="120" rx="34" ry="12"/><circle class="head" cx="100" cy="118" r="11"/>';
    [-1, 1].forEach(function (sd) {
      var shx = cx + sd * 30, a = rad(ang), ex = shx + sd * Math.sin(a) * 60, ey = cy - Math.cos(a) * 60;
      o += '<line class="near" x1="' + shx + '" y1="' + cy + '" x2="' + ex.toFixed(1) + '" y2="' + ey.toFixed(1) + '"/>';
    });
    if (band) { var a2 = rad(ang); o += '<line class="bandl" x1="' + (cx - 30 - Math.sin(a2) * 60).toFixed(1) + '" y1="' + (cy - Math.cos(a2) * 60).toFixed(1) + '" x2="' + (cx + 30 + Math.sin(a2) * 60).toFixed(1) + '" y2="' + (cy - Math.cos(a2) * 60).toFixed(1) + '"/>'; }
    return o + '<text class="lbl" x="100" y="172" text-anchor="middle">nhìn từ trên xuống</text>';
  }
  function svg(poses, labels) {
    if (!poses || !poses.length) return '';
    var w = 200 * poses.length;
    var s = '<svg class="fig" viewBox="0 0 ' + w + ' 190" role="img" aria-label="Minh họa tư thế">' +
      '<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="ahead"/></marker></defs>';
    poses.forEach(function (p, i) {
      s += '<g transform="translate(' + (i * 200) + ',0)">' + (p.raw ? '' : '<line class="ground" x1="6" y1="175" x2="194" y2="175"/>') + figure(p) +
        '<text class="step" x="10" y="18">' + (labels && labels[i] ? labels[i] : (poses.length > 1 ? (i === 0 ? 'Bắt đầu' : 'Kết thúc') : '')) + '</text></g>';
      if (i > 0) s += '<line class="divider" x1="' + (i * 200) + '" y1="10" x2="' + (i * 200) + '" y2="180"/>';
    });
    return s + '</svg>';
  }
  g.TFig = { svg: svg, footTop: footTop, footSide: footSide, topArms: topArms };
})(typeof window !== 'undefined' ? window : globalThis);
