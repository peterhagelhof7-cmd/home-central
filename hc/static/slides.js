/* Live-Slides: Nachbau der Infoscreen-Slides (firmware/main/slides.c) im Browser.
   Buehne 800x480 wie das Display, per CSS auf die Kartenbreite skaliert.
   Daten: /ui/screen.json (dieselben Daten, die der Infoscreen bekommt). */
(function () {
  'use strict';
  const BIG = 48, MED = 28, SM = 14;
  const C = { title: '#8ab4f8', white: '#ffffff', grey: '#b0b8d0', light: '#c8d0e4', cell: '#16203c',
              line: '#33406a', green: '#7ce38b', yellow: '#f5c542', red: '#ef6b6b', orange: '#f5a742' };
  const WD = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
  const TITLES = { clock: 'Uhrzeit & Datum', owm: 'Wetter', planes: 'Flugzeuge',
    night: 'Nachtobjekte', cal: 'Termine', dwd: 'Warnungen', spessart: 'Spessartwetter',
    innen: 'Innenraum', heizung: 'Heizung', kurse: 'Kurse', fritz: 'Internet (Fritzbox)', net: 'Netzwerk',
    wifi: 'WLAN-Empfang', msg: 'Message Board' };
  const ORDER = ['clock', 'owm', 'planes', 'night', 'cal', 'dwd', 'spessart', 'innen', 'heizung', 'kurse',
    'fritz', 'net', 'wifi', 'msg'];

  let D = null, list = [], idx = 0, started = 0, paused = false, content, stage;

  // --- kleine DOM-Helfer (Positionen wie LVGL-Align, relativ zum Inhalt 800x372) ---
  function el(parent, css, text) {
    const e = document.createElement('div');
    e.style.cssText = 'position:absolute;white-space:nowrap;' + css;
    if (text !== undefined) e.textContent = text;
    parent.appendChild(e);
    return e;
  }
  // Zentriert mit Versatz (LV_ALIGN_CENTER dx,dy)
  const center = (p, size, color, text, dy, dx) =>
    el(p, `left:50%;top:50%;transform:translate(calc(-50% + ${dx || 0}px),calc(-50% + ${dy || 0}px));font-size:${size}px;color:${color}`, text);
  // Oben mittig (LV_ALIGN_TOP_MID)
  const topmid = (p, size, color, text, y, dx) =>
    el(p, `left:50%;top:${y}px;transform:translateX(calc(-50% + ${dx || 0}px));font-size:${size}px;color:${color}`, text);
  const box = (p, css) => el(p, `background:${C.cell};border:2px solid ${C.line};border-radius:8px;box-sizing:border-box;` + css);
  const msgCenter = (p, text) => center(p, MED, C.grey, text);
  const pad2 = n => String(n).padStart(2, '0');
  const ddmm = iso => `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
  const deNum = (v, d) => v.toFixed(d).replace('.', ',');

  // --- Wetter-Icons (wie weather_icon() in slides.c) ---
  function disc(c, d, col, dx, dy) {
    el(c, `width:${d}px;height:${d}px;border-radius:50%;background:${col};left:50%;top:50%;transform:translate(calc(-50% + ${dx}px),calc(-50% + ${dy}px))`);
  }
  function bar(c, w, h, r, col, dx, dy) {
    el(c, `width:${w}px;height:${h}px;border-radius:${r}px;background:${col};left:50%;top:50%;transform:translate(calc(-50% + ${dx}px),calc(-50% + ${dy}px))`);
  }
  function cloud(c, s, col, dx, dy) {
    bar(c, s * .66, s * .26, s * .13, col, dx, dy + s * .10);
    disc(c, s * .34, col, dx - s * .18, dy + s * .02);
    disc(c, s * .44, col, dx + s * .02, dy - s * .06);
    disc(c, s * .30, col, dx + s * .22, dy + s * .04);
  }
  function icon(p, code, s, css) {
    const c = el(p, `width:${s}px;height:${s}px;` + css);
    const pfx = code && code.length >= 2 ? parseInt(code.slice(0, 2), 10) : 3;
    const night = code && code[2] === 'n';
    const dc = night ? '#d6def0' : '#ffd23f', CLL = '#e6ecf7', CLG = '#9aa4c0', RAIN = '#5b9bd5';
    switch (pfx) {
      case 1:
        disc(c, s * .52, dc, 0, 0);
        if (!night) {
          bar(c, s * .08, s * .20, s * .04, dc, 0, -s * .38); bar(c, s * .08, s * .20, s * .04, dc, 0, s * .38);
          bar(c, s * .20, s * .08, s * .04, dc, -s * .38, 0); bar(c, s * .20, s * .08, s * .04, dc, s * .38, 0);
        }
        break;
      case 2: disc(c, s * .36, dc, -s * .18, -s * .20); cloud(c, s * .88, CLL, s * .06, s * .12); break;
      case 3: cloud(c, s, CLL, 0, -s * .04); break;
      case 4: cloud(c, s, CLG, 0, -s * .04); break;
      case 9: case 10:
        cloud(c, s * .92, CLL, 0, -s * .16);
        [[-.18, .30], [0, .34], [.18, .30]].forEach(([x, y]) => bar(c, s * .07, s * .20, s * .03, RAIN, s * x, s * y));
        break;
      case 11: cloud(c, s * .92, CLG, 0, -s * .16); bar(c, s * .09, s * .30, s * .02, '#ffd23f', 0, s * .30); break;
      case 13:
        cloud(c, s * .92, CLL, 0, -s * .16);
        [[-.18, .30], [0, .34], [.18, .30]].forEach(([x, y]) => disc(c, s * .10, '#fff', s * x, s * y));
        break;
      case 50:
        cloud(c, s * .80, CLG, 0, -s * .22);
        bar(c, s * .64, s * .07, s * .03, CLG, 0, s * .20); bar(c, s * .54, s * .07, s * .03, CLG, 0, s * .36);
        break;
      default: cloud(c, s, CLL, 0, -s * .04);
    }
    return c;
  }
  const cap1 = t => t ? t[0].toUpperCase() + t.slice(1) : t;

  // --- Slides ---
  const S = {};
  S.clock = p => {
    const n = new Date();
    center(p, BIG, C.title, WD[n.getDay()], -110).dataset.clock = 'wd';
    center(p, BIG, C.white, `${pad2(n.getHours())}:${pad2(n.getMinutes())}`, -10).dataset.clock = 'hm';
    center(p, BIG, C.light, `${pad2(n.getDate())}.${pad2(n.getMonth() + 1)}.${n.getFullYear()}`, 90).dataset.clock = 'date';
  };
  S.owm = p => {
    const d = D.owm || {};
    if (!d.has_key || !d.valid) return msgCenter(p, !d.has_key ? 'Kein API-Key (Einstellungen)' : 'Wetter nicht verfügbar');
    topmid(p, BIG, C.white, `${d.temp} °C`, 6);
    topmid(p, MED, C.title, cap1(d.desc), 66);
    topmid(p, MED, C.grey, `gefühlt ${d.feels} °C  -  ${d.humidity}% rF  -  Wind ${d.wind_kmh} km/h`, 112);
    if (d.icon) icon(p, d.icon, 104, 'left:44px;top:18px');
    if (!d.fc_valid) return;
    [-258, 0, 258].forEach((cx, k) => {
      const f = (d.fc || [])[k];
      if (!f || !f.valid) return;
      const cell = box(p, `width:244px;height:178px;left:${400 + cx - 122}px;bottom:4px`);
      topmid(cell, MED, C.title, f.label, 4);
      if (f.icon) icon(cell, f.icon, 50, 'left:50%;top:50%;transform:translate(-50%,calc(-50% - 22px))');
      center(cell, MED, C.white, `${f.tmin}° / ${f.tmax}°`, 32);
      el(cell, `left:6px;right:6px;bottom:4px;white-space:normal;text-align:center;font-size:${SM}px;color:${C.grey}`, cap1(f.desc));
    });
  };
  const COMPASS = ['N', 'NO', 'O', 'SO', 'S', 'SW', 'W', 'NW'];
  S.planes = p => {
    const d = D.planes;
    el(p, `left:24px;top:12px;font-size:${MED}px;color:${C.title}`, d && d.valid ? `Flugzeuge in der Nähe: ${d.count}` : 'Flugzeuge - kein Abruf');
    if (!d || !d.valid || !d.count) return msgCenter(p, d && d.valid ? 'Zurzeit keine Flugzeuge in der Nähe' : 'Daten noch nicht verfügbar');
    d.ac.slice(0, 6).forEach((a, i) => {
      const row = box(p, `width:752px;height:52px;left:24px;top:${60 + i * 62}px`);
      const cs = a.flight || a.reg || '?';
      const left = a.from || a.to ? `${cs}  ${a.from || '?'}>${a.to || '?'}` : cs;
      el(row, `left:14px;top:50%;transform:translateY(-50%);width:470px;overflow:hidden;text-overflow:ellipsis;font-size:${MED}px;color:${C.white}`, left);
      let info = (a.type ? a.type + '   ' : '') + `${Math.round(a.dst_nm)} nm`;
      if (a.track >= 0) info += `   ${a.track}° ${COMPASS[Math.floor((a.track + 22) / 45) % 8]}`;
      el(row, `right:14px;top:50%;transform:translateY(-50%);font-size:${MED}px;color:${C.grey}`, info);
    });
  };
  function nrow(p, y, left, right) {
    el(p, `left:24px;top:${y}px;width:470px;overflow:hidden;text-overflow:ellipsis;font-size:${MED}px;color:${C.white}`, left);
    if (right) el(p, `right:24px;top:${y}px;font-size:${MED}px;color:${C.grey}`, right);
  }
  S.night = p => {
    const sp = (D.space || {}).passes || {}, ne = (D.space || {}).neos || {};
    let y = 10;
    el(p, `left:24px;top:${y}px;font-size:${MED}px;color:${C.title}`, 'Sichtbare Überflüge (heute Nacht)'); y += 40;
    if (!sp.has_key) { nrow(p, y, 'N2YO-Key fehlt (Einstellungen)'); y += 38; }
    else if (!sp.valid || !sp.count) { nrow(p, y, sp.valid ? 'keine sichtbaren Überflüge' : 'Daten noch nicht verfügbar'); y += 38; }
    else sp.p.slice(0, 4).forEach(a => {
      const t = a.max_utc ? new Date(a.max_utc * 1000) : null;
      const hm = t ? `${pad2(t.getHours())}:${pad2(t.getMinutes())}` : '--:--';
      nrow(p, y, `${hm}  ${a.name}`, `${a.max_el}° ${a.dir}` + (a.mag < 90 ? `   mag ${a.mag.toFixed(1)}` : '')); y += 38;
    });
    y += 12;
    el(p, `left:24px;top:${y}px;font-size:${MED}px;color:${C.title}`, 'Erdnahe Objekte (heute)'); y += 40;
    if (!ne.valid || !ne.count) nrow(p, y, ne.valid ? 'keine erdnahen Objekte' : 'Daten noch nicht verfügbar');
    else ne.n.slice(0, 3).forEach(o => {
      nrow(p, y, o.name + (o.hazard ? '  !' : ''), `${(o.miss_km / 1e6).toFixed(2)} Mio km   ${Math.round(o.diam_m)} m`); y += 38;
    });
  };
  S.cal = p => {
    const n = new Date(), today = `${n.getFullYear()}-${pad2(n.getMonth() + 1)}-${pad2(n.getDate())}`;
    let ev = [];
    (D.termine || []).forEach(t => { if (t.date >= today) ev.push([t.date, t.time ? `${t.time} ${t.title}` : t.title]); });
    (D.muell || []).forEach(m => ev.push([m.day, `Abfuhr: ${m.title}`]));
    (D.feiertage || []).forEach(f => { if (f.date >= today) ev.push([f.date, f.name]); });
    ev.sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0);
    ev = ev.slice(0, 5);
    if (!ev.length) return msgCenter(p, 'Keine Termine');
    const h = Math.floor((372 - 8 - (ev.length - 1) * 6) / ev.length);
    ev.forEach(([d, lbl], i) => {
      const b = box(p, `width:760px;height:${h}px;left:20px;top:${4 + i * (h + 6)}px`);
      el(b, `left:16px;right:24px;top:50%;transform:translateY(-50%);overflow:hidden;text-overflow:ellipsis;font-size:${MED}px;color:#e6e6e6`, `${ddmm(d)}   ${lbl}`);
    });
  };
  S.dwd = p => {
    const n = D.nina || {}, d = D.dwd || {};
    const hn = n.valid && n.count > 0 && n.headline, hd = d.valid && d.count > 0 && d.headline;
    const big = (txt, col) => el(p, `left:30px;right:30px;top:50%;transform:translateY(calc(-50% - 20px));white-space:normal;text-align:center;font-size:${BIG}px;color:${col}`, txt);
    if (!hn && !hd) {
      const any = n.valid || d.valid;
      return big(any ? 'Keine Warnung' : 'Warnquellen nicht erreichbar', any ? C.green : C.grey);
    }
    const w = hn ? { hl: n.headline, sev: n.severity, src: n.provider || 'BBK', cnt: n.count }
                 : { hl: d.headline, sev: d.severity, src: 'DWD', cnt: d.count };
    big(w.hl || 'Warnung aktiv', w.sev === 'Severe' || w.sev === 'Extreme' ? C.red : C.yellow);
    el(p, `left:50%;bottom:20px;transform:translateX(-50%);font-size:${MED}px;color:${C.title}`,
      w.cnt > 1 ? `Quelle: ${w.src}  -  ${w.cnt} Warnungen` : `Quelle: ${w.src}`);
  };
  S.spessart = p => {
    const s = D.spessart;
    if (!s || !s.valid) return msgCenter(p, 'spessartwetter nicht erreichbar');
    center(p, BIG, C.white, `${s.temp || '?'} °C`, -70);
    center(p, MED, C.title, `Wind: ${s.wind || '?'} km/h`, 5);
    const g = parseFloat((s.gust || '0').replace(',', '.')) || 0;
    center(p, MED, g >= 50 ? C.red : C.orange, `Böen: ${s.gust || '?'} km/h`, 60);
    if (s.has_stand) {
      const n = new Date(), ts = new Date(s.st_year, s.st_mon - 1, s.st_day, s.st_hour, s.st_min);
      const today = n.getFullYear() === s.st_year && n.getMonth() + 1 === s.st_mon && n.getDate() === s.st_day;
      const alt = (n - ts) > 90 * 60 * 1000;
      const t = today ? `Messwerte von ${pad2(s.st_hour)}:${pad2(s.st_min)} Uhr`
                      : `Messwerte vom ${pad2(s.st_day)}.${pad2(s.st_mon)}., ${pad2(s.st_hour)}:${pad2(s.st_min)} Uhr`;
      el(p, `left:50%;bottom:20px;transform:translateX(-50%);font-size:${MED}px;color:${alt ? C.orange : '#8a90a8'}`, t + (alt ? ' (veraltet)' : ''));
    }
  };
  S.innen = p => {
    const r = (D.device || {}).dht22;
    if (!r || !r.valid) return msgCenter(p, D.device && D.device.online ? 'Sensor nicht erreichbar' : 'Keine Daten vom Infoscreen');
    topmid(p, BIG, '#ff9d5c', `${r.temp_c.toFixed(1)} °C`, 40, -170);
    topmid(p, BIG, '#5cc8ff', `${Math.round(r.hum_pct)} %`, 40, 170);
    topmid(p, SM, C.grey, 'Innenraum  -  Temperatur / Luftfeuchte', 130);
    const h = (D.device || {}).dht22_hist;
    if (!h || !h.temp || h.temp.length < 2) {
      return el(p, `left:50%;bottom:70px;transform:translateX(-50%);font-size:${MED}px;color:${C.grey}`, 'Verlauf wird gesammelt (Punkt alle 30 min) ...');
    }
    chart(p, h.temp, h.hum);
  };
  function chart(p, ht, hh) {
    const W = 720, H = 200, n = ht.length;
    let tlo = Math.min(...ht) - 1, thi = Math.max(...ht) + 1; if (thi - tlo < 2) thi = tlo + 2;
    let hlo = Math.max(0, Math.min(...hh) - 5), hhi = Math.min(100, Math.max(...hh) + 5); if (hhi - hlo < 10) hhi = hlo + 10;
    const x = i => 10 + i * (W - 20) / (n - 1);
    const pts = (arr, lo, hi) => arr.map((v, i) => `${x(i)},${H - 10 - (v - lo) / (hi - lo) * (H - 20)}`).join(' ');
    const wrap = box(p, `width:${W}px;height:${H}px;left:40px;bottom:46px;overflow:hidden`);
    let grid = '';
    for (let i = 1; i < 4; i++) grid += `<line x1="0" x2="${W}" y1="${i * H / 4}" y2="${i * H / 4}" stroke="#2a3556"/>`;
    for (let i = 1; i < 6; i++) grid += `<line y1="0" y2="${H}" x1="${i * W / 6}" x2="${i * W / 6}" stroke="#2a3556"/>`;
    wrap.innerHTML = `<svg width="${W}" height="${H}" style="position:absolute;left:-2px;top:-2px">${grid}
      <polyline points="${pts(ht, tlo, thi)}" fill="none" stroke="#ff9d5c" stroke-width="3"/>
      <polyline points="${pts(hh, hlo, hhi)}" fill="none" stroke="#5cc8ff" stroke-width="3"/></svg>`;
    el(p, `left:44px;bottom:18px;font-size:${SM}px;color:#7a8290`, `vor ${deNum((n - 1) * 0.5, 1)} h`);
    el(p, `right:44px;bottom:18px;font-size:${SM}px;color:#7a8290`, 'jetzt');
  }
  const grpDE = v => Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '.');
  function krow(p, cy, name, value, chg) {
    el(p, `left:48px;top:${186 + cy - 32}px;transform:translateY(-50%);font-size:${MED}px;color:${C.title}`, name);
    el(p, `left:48px;top:${186 + cy + 14}px;transform:translateY(-50%);font-size:${BIG}px;color:${C.white}`, value);
    const col = chg > 0.05 ? '#43d17a' : chg < -0.05 ? C.red : '#8a92a6';
    el(p, `right:40px;top:${186 + cy}px;transform:translateY(-50%);font-size:${MED}px;color:${col}`,
      (chg >= 0 ? '+' : '') + deNum(chg, 2) + ' %');
    const arrow = chg > 0.05 ? '0,17 14,0 28,17' : chg < -0.05 ? '0,0 14,17 28,0' : '0,9 28,9';
    el(p, `right:190px;top:${186 + cy}px;transform:translateY(-50%);width:36px;height:26px`).innerHTML =
      `<svg width="36" height="26"><polyline points="${arrow}" transform="translate(4,4)" fill="none" stroke="${col}" stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  }
  S.kurse = p => {
    const k = D.kurse;
    topmid(p, SM, C.grey, 'Kurse  -  24h-Trend', 8);
    if (!k || !k.valid) return msgCenter(p, 'Kurse werden geladen ...');
    krow(p, -80, 'BTC-EUR', `${grpDE(k.btc_eur)} EUR`, k.has_trend ? k.btc_chg : 0);
    krow(p, 80, 'EUR-USD', `${deNum(k.eur_usd, 4)} $`, k.has_trend ? k.eur_chg : 0);
  };
  // Heizung (wie heizung_build() in slides.c): Zustand gross, ggf. Alter + Banner, Werte in 2 Spalten
  function hzBanner(p, y, bg, text) {
    el(p, `left:30px;top:${y}px;width:740px;height:44px;line-height:44px;border-radius:8px;background:${bg};` +
      `text-align:center;overflow:hidden;text-overflow:ellipsis;font-size:${MED}px;color:#fff`, text);
  }
  S.heizung = p => {
    const h = D.heizung;
    if (!h || !h.on) return msgCenter(p, 'Heizung in home-central noch nicht eingerichtet.');
    if (h.age < 0) return msgCenter(p, 'Warte auf Daten vom esp-p4d ...');
    const st = el(p, `left:30px;top:4px;width:740px;text-align:center;overflow:hidden;text-overflow:ellipsis;` +
      `font-size:${BIG}px;color:${h.online ? C.white : C.grey}`, h.online && h.state ? h.state : 'Kessel antwortet nicht');
    st.style.whiteSpace = 'nowrap';
    let top = 72;
    if (h.age >= 120) {
      el(p, `left:30px;top:${top - 6}px;width:740px;text-align:center;font-size:${MED}px;color:${C.title}`, `Stand vor ${Math.floor(h.age / 60)} min`);
      top += 38;
    }
    if (h.error) { hzBanner(p, top, '#b42318', `Störung: ${h.error}`); top += 52; }
    else if (!h.reach) { hzBanner(p, top, '#b86e00', 'esp-p4d nicht erreichbar - letzte Werte'); top += 52; }
    else if (!h.online) { hzBanner(p, top, '#b86e00', 'Kessel antwortet nicht (COM1)'); top += 52; }
    const v = (h.values || []).slice(0, 12);
    if (!v.length) return topmid(p, MED, C.grey, 'Noch keine Werte (Auswahl im esp-p4d)', top + 40);
    const rows = Math.ceil(v.length / 2), rh = Math.max(34, Math.min(48, Math.floor((372 - top) / rows)));
    v.forEach((x, i) => {
      const col = Math.floor(i / rows), row = i % rows, xx = 30 + col * 390, y = top + row * rh;
      if (y + rh > 372) return;
      el(p, `left:${xx}px;top:${y}px;width:350px;height:${rh}px;box-sizing:border-box;border-bottom:1px solid #1f2a4a`);
      const ty = y + Math.floor((rh - 32) / 2);
      el(p, `left:${xx}px;top:${ty}px;width:235px;overflow:hidden;text-overflow:ellipsis;font-size:${MED}px;color:${C.grey}`, x.n);
      el(p, `left:${xx + 235}px;top:${ty}px;width:115px;text-align:right;font-size:${MED}px;color:${C.white}`, x.v);
    });
  };
  S.fritz = p => {
    const d = D.fritzbox;
    if (!d || !d.reachable) return center(p, BIG, C.yellow, 'Fritzbox nicht erreichbar', -70);
    center(p, BIG, C.white, `IP: ${d.external_ip || '-'}`, -70);
    center(p, MED, '#7ce38b', `Down: ${(d.down_rate_Bps * 8 / 1e6).toFixed(1)} / ${Math.floor(d.down_max_bps / 1e6)} Mbit/s`, 10);
    center(p, MED, C.title, `Up: ${(d.up_rate_Bps * 8 / 1e6).toFixed(1)} / ${Math.floor(d.up_max_bps / 1e6)} Mbit/s`, 60);
  };
  S.net = p => {
    const r = D.device || {};
    if (!r.online) { center(p, BIG, C.title, 'Keine Daten vom Infoscreen', -60); return; }
    center(p, BIG, '#7ce38b', 'WLAN verbunden', -60);
    center(p, MED, '#e0e0e0', `SSID: ${r.ssid || '-'}`, 20);
    center(p, MED, C.grey, `IP: ${r.ip || '-'}`, 70);
  };
  S.wifi = p => {
    const r = D.device || {};
    const rssi = r.rssi_avg || r.rssi;
    if (!r.online || !rssi) {
      center(p, BIG, C.white, '-- dBm', -50);
      center(p, BIG, C.grey, r.online ? 'nicht verbunden' : 'Keine Daten vom Infoscreen', 30);
      return;
    }
    const [q, c] = rssi >= -55 ? ['Ausgezeichnet', '#7ce38b'] : rssi >= -65 ? ['Gut', '#9ade7b'] : rssi >= -75 ? ['Mittel', C.yellow] : ['Schwach', C.red];
    center(p, BIG, C.white, `${rssi} dBm`, -50);
    center(p, BIG, c, q, 30);
    center(p, MED, '#8a94b0', 'Mittelwert der letzten 20 s', 110);
  };
  S.msg = p => {
    const t = D.telegram || {};
    const e = el(p, `left:16px;top:4px;width:760px;white-space:pre-wrap;font-size:${SM + 2}px;line-height:1.35;color:#e6e6e6`);
    if (!t.configured) { e.style.color = C.grey; e.textContent = 'Telegram nicht konfiguriert.\nToken und Gruppen-ID im Webinterface hinterlegen.'; return; }
    if (!t.msgs || !t.msgs.length) { e.style.color = C.grey; e.textContent = 'Noch keine Nachrichten.'; return; }
    e.textContent = t.msgs.slice(-12).map(m => `${m.from}: ${m.text}`).join('\n');
  };

  // --- Rahmen: Titel, Seitenzahl, Countdown, Rotation ---
  // Slide-Katalog: vom Infoscreen gemeldet (variabel, neue Slides kommen automatisch dazu)
  function catalog() {
    const c = D && D.catalog && D.catalog.length ? D.catalog : ORDER.map(id => ({ id, title: TITLES[id] }));
    c.forEach(e => { if (!TITLES[e.id] || e.title) TITLES[e.id] = e.title || e.id; });
    return c.map(e => e.id);
  }
  function enabled() {
    const sl = (D && D.display && D.display.slides) || {};
    return catalog().filter(id => sl[id] !== false);
  }
  function interval() { return ((D && D.display && D.display.slide_sec) || 10) * 1000; }
  function show(i) {
    list = enabled();
    if (!list.length) return;
    idx = (i + list.length) % list.length;
    started = Date.now();
    const id = list[idx];
    stage.querySelector('.ls-title').textContent = TITLES[id];
    stage.querySelector('.ls-foot').textContent = `${idx + 1} / ${list.length}`;
    content.innerHTML = '';
    try {
      if (S[id]) S[id](content);
      else msgCenter(content, 'Vorschau für diesen Slide noch nicht verfügbar');
    } catch (e) { msgCenter(content, 'Darstellungsfehler: ' + e.message); }
    document.querySelectorAll('.ls-dot').forEach((d, k) => d.classList.toggle('on', k === idx));
  }
  function tick() {
    if (!D) return;
    const rem = Math.max(0, Math.ceil((interval() - (Date.now() - started)) / 1000));
    stage.querySelector('.ls-cd').textContent = paused ? 'Pause' : `${rem} s`;
    if (!paused && Date.now() - started >= interval()) show(idx + 1);
    if (list[idx] === 'clock') {   // Uhr live halten
      const n = new Date(), hm = content.querySelector('[data-clock=hm]');
      if (hm) hm.textContent = `${pad2(n.getHours())}:${pad2(n.getMinutes())}`;
    }
  }
  async function load(first) {
    try {
      const r = await fetch('/ui/screen.json', { cache: 'no-store' });
      if (!r.ok) return;
      D = await r.json();
      const dots = document.getElementById('ls-dots');
      const ids = enabled();
      if (dots.childElementCount !== ids.length) {
        dots.innerHTML = '';
        ids.forEach((id, k) => {
          const b = document.createElement('button');
          b.className = 'ls-dot'; b.title = TITLES[id];
          b.onclick = () => show(k);
          dots.appendChild(b);
        });
      }
      if (first) show(0);
    } catch (e) { /* naechster Versuch */ }
  }
  function fit() {
    const w = stage.parentElement.clientWidth;
    stage.style.transform = `scale(${w / 800})`;
    stage.parentElement.style.height = (480 * w / 800) + 'px';
  }
  window.addEventListener('DOMContentLoaded', () => {
    stage = document.getElementById('ls-stage');
    if (!stage) return;
    content = stage.querySelector('.ls-content');
    fit(); window.addEventListener('resize', fit);
    document.getElementById('ls-prev').onclick = () => show(idx - 1);
    document.getElementById('ls-next').onclick = () => show(idx + 1);
    document.getElementById('ls-pause').onclick = e => {
      paused = !paused; e.target.textContent = paused ? '▶' : '❚❚';
      if (!paused) started = Date.now();
    };
    load(true);
    setInterval(() => load(false), 15000);
    setInterval(tick, 500);
  });
})();
