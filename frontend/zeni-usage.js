/*
 * ZENI CLOUD CORE · USAGE / COST DASHBOARD MODULE
 * Standalone "Sử dụng" tab renderer.
 *
 *   - window.ZeniUsageUI.render(rootEl, ws)
 *   - Adds window.ZeniAPI.usage namespace (current / history / routerQuota / plans /
 *     currentSub / subscribe / cancel) without touching zeni-api.js source.
 *
 * Design rules:
 *   - Vanilla JS only, IIFE wrapper, dark theme aligned với landing.html.
 *   - Vietnamese labels throughout.
 *   - Mobile-first: card stack, breakpoint 720px.
 *   - No external chart lib — bar chart 30 ngày tự vẽ bằng <canvas>.
 *   - Số liệu format vi-VN; tiền VND có hậu tố "đ", USD tiền tố "$".
 */
(function () {
  'use strict';

  // ─── Color palette (đồng bộ với landing.html) ─────────────
  const C = {
    bgCard:     'rgba(255,255,255,0.03)',
    bgCardHi:   'rgba(255,255,255,0.05)',
    border:     'rgba(255,255,255,0.06)',
    borderStr:  'rgba(168,139,250,0.18)',
    ink50:      '#FAF5FF',
    ink100:     '#EDE9FE',
    ink200:     '#C4B5FD',
    ink300:     '#9E8BE5',
    ink400:     '#7C6BB0',
    gold:       '#FDE68A',
    goldDeep:   '#F59E0B',
    crown:      '#A855F7',
    crownLight: '#D8B4FE',
    ajna:       '#6366F1',
    cyan:       '#22D3EE',
    green:      '#4ade80',
    amber:      '#fbbf24',
    red:        '#f87171',
  };
  const FONT      = "'Inter', system-ui, -apple-system, sans-serif";
  const FONT_MONO = "'Roboto Mono', ui-monospace, 'SF Mono', Consolas, monospace";

  // ─── Helpers ──────────────────────────────────────────────
  function fmtN(n)   { return (Number(n) || 0).toLocaleString('vi-VN'); }
  function fmtVnd(n) { return Math.round(Number(n) || 0).toLocaleString('vi-VN') + 'đ'; }
  function fmtUsd(n) { return '$' + (Number(n) || 0).toFixed(2); }
  function fmtGb(n)  { return (Number(n) || 0).toFixed(2) + ' GB'; }
  function fmtDate(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleDateString('vi-VN', { day: '2-digit', month: '2-digit', year: 'numeric' });
  }
  function daysLeft(periodEndIso) {
    if (!periodEndIso) return 0;
    const end = new Date(periodEndIso).getTime();
    const ms = end - Date.now();
    return Math.max(0, Math.ceil(ms / 86400000));
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function quotaColor(p) {
    const n = Number(p) || 0;
    if (n >= 80) return C.red;
    if (n >= 50) return C.amber;
    return C.green;
  }

  // ─── Light DOM builder ────────────────────────────────────
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'style' && typeof attrs[k] === 'object') {
          Object.assign(node.style, attrs[k]);
        } else if (k === 'class') {
          node.className = attrs[k];
        } else if (k === 'html') {
          node.innerHTML = attrs[k];
        } else if (k.startsWith('on') && typeof attrs[k] === 'function') {
          node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        } else if (attrs[k] != null) {
          node.setAttribute(k, attrs[k]);
        }
      }
    }
    if (children != null) {
      const list = Array.isArray(children) ? children : [children];
      for (const c of list) {
        if (c == null || c === false) continue;
        node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      }
    }
    return node;
  }

  // ─── ZeniAPI extensions ──────────────────────────────────
  // ZeniAPI._fetch is private; expose pricing/usage helpers wrapping fetch + JWT.
  function _authHeaders() {
    const tok = localStorage.getItem('zeni.jwt.access');
    const h = { 'Content-Type': 'application/json', 'Accept': 'application/json' };
    if (tok) h.Authorization = 'Bearer ' + tok;
    return h;
  }
  async function _req(method, path, body) {
    const opts = { method, headers: _authHeaders(), credentials: 'same-origin' };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch('/api/v1' + path, opts);
    if (r.status === 204) return null;
    const ct = r.headers.get('content-type') || '';
    const data = ct.includes('application/json') ? await r.json().catch(() => null) : await r.text();
    if (!r.ok) {
      const err = new Error((data && data.detail) || ('HTTP ' + r.status));
      err.status = r.status; err.body = data;
      throw err;
    }
    return data;
  }
  if (window.ZeniAPI && !window.ZeniAPI.usage) {
    window.ZeniAPI.usage = {
      current:     (ws) => _req('GET',  '/pricing/usage?ws=' + encodeURIComponent(ws)),
      history:     (ws) => _req('GET',  '/pricing/usage/history?ws=' + encodeURIComponent(ws)),
      routerQuota: (ws) => _req('GET',  '/router/quota?ws=' + encodeURIComponent(ws)),
      byModel:     (ws) => _req('GET',  '/router/usage/by-model?ws=' + encodeURIComponent(ws)),
      plans:       ()   => _req('GET',  '/pricing/plans'),
      currentSub:  (ws) => _req('GET',  '/pricing/subscription?ws=' + encodeURIComponent(ws)),
      subscribe:   (workspace_code, plan_id) =>
                          _req('POST', '/pricing/subscribe',
                               { workspace_code, plan_id, payment_method: 'manual' }),
      cancel:      (ws) => _req('POST', '/pricing/cancel?ws=' + encodeURIComponent(ws)),
    };
  }

  // ─── Section 1: Header / current plan card ───────────────
  function renderHeader(usage, sub) {
    const planName = (usage && usage.plan && usage.plan.name) ||
                     (sub && sub.plan && sub.plan.name) || 'Free';
    const priceVnd = (usage && usage.plan && usage.plan.price_vnd) ||
                     (sub && sub.plan && sub.plan.price_vnd) || 0;
    const priceUsd = (usage && usage.plan && usage.plan.price_usd) ||
                     (sub && sub.plan && sub.plan.price_usd) || 0;
    const status   = (sub && sub.status) || 'active';
    const periodStart = (usage && usage.period_start) || (sub && sub.period_start);
    const periodEnd   = (sub && sub.period_end);

    const wrap = el('section', {
      style: {
        position: 'relative',
        background: 'linear-gradient(135deg, rgba(168,85,247,0.12), rgba(99,102,241,0.08) 60%, rgba(253,230,138,0.05))',
        border: '1px solid ' + C.borderStr,
        borderRadius: '16px',
        padding: '28px',
        marginBottom: '20px',
        overflow: 'hidden',
      },
    });

    // Decorative orb
    wrap.appendChild(el('div', {
      style: {
        position: 'absolute', top: '-40px', right: '-40px',
        width: '180px', height: '180px',
        background: 'radial-gradient(circle, rgba(253,230,138,0.18), transparent 70%)',
        borderRadius: '50%', pointerEvents: 'none',
      },
    }));

    const top = el('div', {
      style: { display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: '16px', position: 'relative' },
    });

    const left = el('div', null, [
      el('div', {
        style: {
          fontFamily: FONT_MONO, fontSize: '11px', letterSpacing: '0.18em',
          color: C.crownLight, textTransform: 'uppercase', marginBottom: '8px',
        },
      }, 'Gói hiện tại'),
      el('h2', {
        style: {
          fontSize: '32px', fontWeight: '800', color: C.ink50,
          letterSpacing: '-0.02em', margin: '0 0 6px 0',
        },
      }, planName),
      el('div', {
        style: { display: 'flex', alignItems: 'baseline', gap: '12px', flexWrap: 'wrap' },
      }, [
        el('span', { style: { fontSize: '22px', fontWeight: '700', color: C.gold, fontFamily: FONT_MONO } }, fmtVnd(priceVnd)),
        el('span', { style: { fontSize: '13px', color: C.ink300 } }, '/ tháng'),
        el('span', { style: { fontSize: '13px', color: C.ink400, fontFamily: FONT_MONO } }, '· ' + fmtUsd(priceUsd)),
      ]),
    ]);

    const right = el('div', {
      style: { textAlign: 'right', minWidth: '200px' },
    }, [
      el('div', {
        style: {
          display: 'inline-flex', alignItems: 'center', gap: '8px',
          padding: '6px 12px', borderRadius: '999px',
          background: status === 'active' ? 'rgba(34,211,238,0.12)' : 'rgba(248,113,113,0.12)',
          border: '1px solid ' + (status === 'active' ? 'rgba(34,211,238,0.3)' : 'rgba(248,113,113,0.3)'),
          color: status === 'active' ? C.cyan : C.red,
          fontSize: '12px', fontWeight: '600', textTransform: 'uppercase', letterSpacing: '0.08em',
        },
      }, [
        el('span', { style: { width: '6px', height: '6px', borderRadius: '50%', background: 'currentColor' } }),
        status,
      ]),
      el('div', {
        style: { marginTop: '12px', fontSize: '12px', color: C.ink300, lineHeight: '1.6' },
      }, [
        el('div', null, 'Bắt đầu kỳ: ' + fmtDate(periodStart)),
        periodEnd ? el('div', null, 'Kết thúc: ' + fmtDate(periodEnd)) : null,
      ]),
    ]);

    top.appendChild(left);
    top.appendChild(right);
    wrap.appendChild(top);
    return wrap;
  }

  // ─── Section 2: 4 quota meters ───────────────────────────
  function meterRow(label, current, limit, percent, suffix, accentColor) {
    const safePct = Math.min(100, Math.max(0, Number(percent) || 0));
    const color = quotaColor(safePct);

    const card = el('div', {
      style: {
        background: C.bgCard, border: '1px solid ' + C.border,
        borderRadius: '12px', padding: '20px', display: 'flex',
        flexDirection: 'column', gap: '12px',
      },
    });

    card.appendChild(el('div', {
      style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' },
    }, [
      el('div', null, [
        el('div', {
          style: {
            fontFamily: FONT_MONO, fontSize: '10.5px', letterSpacing: '0.16em',
            color: accentColor || C.ink300, textTransform: 'uppercase', marginBottom: '4px',
          },
        }, label),
        el('div', {
          style: { fontFamily: FONT_MONO, fontSize: '20px', fontWeight: '700', color: C.ink50 },
        }, suffix === 'usd' ? fmtUsd(current) : (suffix === 'gb' ? fmtGb(current) : fmtN(current))),
      ]),
      el('div', {
        style: { textAlign: 'right' },
      }, [
        el('div', { style: { fontSize: '11px', color: C.ink400 } }, 'Trên'),
        el('div', {
          style: { fontFamily: FONT_MONO, fontSize: '13px', color: C.ink200, fontWeight: '600' },
        }, suffix === 'usd' ? fmtUsd(limit) : (suffix === 'gb' ? fmtGb(limit) : fmtN(limit))),
      ]),
    ]));

    // Progress bar
    const bar = el('div', {
      style: {
        position: 'relative', height: '8px', borderRadius: '999px',
        background: 'rgba(255,255,255,0.06)', overflow: 'hidden',
      },
    });
    bar.appendChild(el('div', {
      style: {
        height: '100%', width: '0%',
        background: 'linear-gradient(90deg, ' + color + ', ' + color + 'cc)',
        borderRadius: '999px',
        transition: 'width 0.6s cubic-bezier(0.4,0,0.2,1)',
        boxShadow: '0 0 12px ' + color + '66',
      },
    }));
    card.appendChild(bar);
    // Animate after attach
    requestAnimationFrame(() => {
      const fill = bar.firstChild;
      if (fill) fill.style.width = safePct + '%';
    });

    card.appendChild(el('div', {
      style: { display: 'flex', justifyContent: 'space-between', fontSize: '11.5px', color: C.ink300 },
    }, [
      el('span', { style: { color: color, fontWeight: '600' } }, safePct.toFixed(1) + '%'),
      el('span', null, safePct >= 80 ? 'Sắp hết quota' : (safePct >= 50 ? 'Đã dùng quá nửa' : 'Còn nhiều')),
    ]));

    return card;
  }

  function renderQuotaMeters(usage, routerQuota) {
    const wrap = el('section', { style: { marginBottom: '20px' } });
    wrap.appendChild(el('h3', {
      style: {
        fontSize: '13px', fontWeight: '700', color: C.gold, textTransform: 'uppercase',
        letterSpacing: '0.12em', margin: '0 0 14px 0',
      },
    }, 'Hạn mức sử dụng'));

    const grid = el('div', {
      style: {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
        gap: '14px',
      },
    });

    const plan = (usage && usage.plan) || {};
    const qp = (usage && usage.quota_percent) || {};

    grid.appendChild(meterRow(
      'Yêu cầu / tháng',
      (usage && usage.requests_count) || 0,
      plan.requests_limit || 0,
      qp.requests || 0,
      'count',
      C.cyan,
    ));
    grid.appendChild(meterRow(
      'AI Tokens / tháng',
      (usage && usage.ai_tokens_count) || 0,
      plan.ai_tokens_limit || 0,
      qp.ai_tokens || 0,
      'count',
      C.crownLight,
    ));
    grid.appendChild(meterRow(
      'Lưu trữ',
      (usage && usage.storage_gb_avg) || 0,
      plan.storage_gb_limit || 0,
      qp.storage || 0,
      'gb',
      C.ajna,
    ));

    // Router USD: prefer Stream-α data when available
    const routerCurrent = (routerQuota && routerQuota.current_usage_usd) ||
                          (usage && usage.router_cost_usd) || 0;
    const routerLimit   = (routerQuota && routerQuota.monthly_quota_usd) ||
                          plan.router_quota_usd || 0;
    const routerPct     = (routerQuota && routerQuota.percent_used) || qp.router || 0;
    grid.appendChild(meterRow(
      'Router (USD)',
      routerCurrent, routerLimit, routerPct,
      'usd',
      C.gold,
    ));

    wrap.appendChild(grid);

    // "Còn lại X ngày trong kỳ" footer
    const periodEnd = (usage && usage.period_end) || null;
    if (periodEnd) {
      wrap.appendChild(el('div', {
        style: {
          marginTop: '12px', fontSize: '12px', color: C.ink300,
          fontFamily: FONT_MONO, textAlign: 'right',
        },
      }, 'Còn lại ' + daysLeft(periodEnd) + ' ngày trong kỳ'));
    }

    return wrap;
  }

  // ─── Section 3: Usage chart 30 days (canvas) ─────────────
  async function renderUsageChart(ws) {
    const wrap = el('section', {
      style: {
        background: C.bgCard, border: '1px solid ' + C.border,
        borderRadius: '12px', padding: '24px', marginBottom: '20px',
      },
    });

    const head = el('div', {
      style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', flexWrap: 'wrap', gap: '8px' },
    });
    head.appendChild(el('h3', {
      style: { fontSize: '13px', fontWeight: '700', color: C.gold, textTransform: 'uppercase', letterSpacing: '0.12em', margin: 0 },
    }, 'Sử dụng 30 ngày'));
    const legend = el('div', { style: { display: 'flex', gap: '14px', fontSize: '11px', color: C.ink300 } }, [
      el('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '6px' } }, [
        el('span', { style: { width: '10px', height: '10px', borderRadius: '2px', background: C.crown } }),
        'Yêu cầu',
      ]),
      el('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '6px' } }, [
        el('span', { style: { width: '10px', height: '10px', borderRadius: '2px', background: C.cyan } }),
        'AI Tokens',
      ]),
    ]);
    head.appendChild(legend);
    wrap.appendChild(head);

    const canvas = el('canvas', { style: { width: '100%', height: '220px', display: 'block' } });
    wrap.appendChild(canvas);

    // Defer drawing until in DOM (need real width)
    setTimeout(async () => {
      let history = null;
      try {
        history = await window.ZeniAPI.usage.history(ws);
      } catch (_) { history = null; }

      // Normalize: expect array of {date, requests, ai_tokens}
      let days = [];
      if (Array.isArray(history)) {
        days = history.slice(-30);
      } else if (history && Array.isArray(history.daily)) {
        days = history.daily.slice(-30);
      }
      // Pad to 30 if needed
      while (days.length < 30) {
        const d = new Date();
        d.setDate(d.getDate() - (29 - days.length));
        days.unshift({ date: d.toISOString().slice(0, 10), requests: 0, ai_tokens: 0 });
      }

      drawBarChart(canvas, days);
    }, 0);

    return wrap;
  }

  function drawBarChart(canvas, days) {
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 600;
    const cssH = 220;
    canvas.width  = cssW * dpr;
    canvas.height = cssH * dpr;
    const ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);

    const padL = 44, padR = 16, padT = 10, padB = 28;
    const w = cssW - padL - padR;
    const h = cssH - padT - padB;

    const maxReq = Math.max(1, ...days.map(d => Number(d.requests || 0)));
    const maxTok = Math.max(1, ...days.map(d => Number(d.ai_tokens || 0)));

    // Background grid
    ctx.strokeStyle = 'rgba(255,255,255,0.05)';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = padT + (h * i / 4);
      ctx.beginPath();
      ctx.moveTo(padL, y); ctx.lineTo(padL + w, y); ctx.stroke();
    }
    // Y-axis labels
    ctx.fillStyle = '#7C6BB0';
    ctx.font = '10px ' + FONT_MONO;
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {
      const y = padT + (h * (4 - i) / 4);
      const v = Math.round((maxReq * i) / 4);
      ctx.fillText(fmtN(v), padL - 6, y + 3);
    }

    // Bars (grouped: requests + ai_tokens normalised to its own scale)
    const groupW = w / days.length;
    const barW = Math.max(2, groupW * 0.36);
    days.forEach((d, i) => {
      const x = padL + i * groupW + groupW * 0.1;

      const reqH = (Number(d.requests || 0) / maxReq) * h;
      ctx.fillStyle = C.crown;
      ctx.fillRect(x, padT + h - reqH, barW, reqH);

      const tokH = (Number(d.ai_tokens || 0) / maxTok) * h;
      ctx.fillStyle = C.cyan;
      ctx.fillRect(x + barW + 1, padT + h - tokH, barW, tokH);
    });

    // X-axis labels — every ~5 days
    ctx.fillStyle = '#9E8BE5';
    ctx.font = '10px ' + FONT_MONO;
    ctx.textAlign = 'center';
    days.forEach((d, i) => {
      if (i % 5 !== 0 && i !== days.length - 1) return;
      const x = padL + i * groupW + groupW / 2;
      const dt = d.date ? new Date(d.date) : null;
      const lbl = dt && !isNaN(dt.getTime())
        ? (dt.getDate() + '/' + (dt.getMonth() + 1))
        : String(i + 1);
      ctx.fillText(lbl, x, padT + h + 16);
    });

    // Frame
    ctx.strokeStyle = 'rgba(168,139,250,0.18)';
    ctx.strokeRect(padL, padT, w, h);
  }

  // ─── Section 4: Top models used ──────────────────────────
  function renderTopModels(usage) {
    const wrap = el('section', {
      style: {
        background: C.bgCard, border: '1px solid ' + C.border,
        borderRadius: '12px', padding: '24px', marginBottom: '20px',
      },
    });
    wrap.appendChild(el('h3', {
      style: { fontSize: '13px', fontWeight: '700', color: C.gold, textTransform: 'uppercase', letterSpacing: '0.12em', margin: '0 0 14px 0' },
    }, 'Top model AI sử dụng'));

    // Try usage.top_models first, then fetch by-model lazily.
    let rows = (usage && Array.isArray(usage.top_models)) ? usage.top_models : null;

    const tbl = el('table', {
      style: { width: '100%', borderCollapse: 'collapse', fontSize: '13px', color: C.ink100 },
    });
    const thead = el('thead', null,
      el('tr', null, [
        el('th', { style: thStyle() }, 'Model'),
        el('th', { style: thStyle('right') }, 'Yêu cầu'),
        el('th', { style: thStyle('right') }, 'Tokens'),
        el('th', { style: thStyle('right') }, 'Chi phí'),
      ]),
    );
    tbl.appendChild(thead);
    const tbody = el('tbody');
    tbl.appendChild(tbody);
    wrap.appendChild(tbl);

    function fillRows(list) {
      tbody.innerHTML = '';
      if (!list || !list.length) {
        tbody.appendChild(el('tr', null,
          el('td', {
            colspan: '4',
            style: { padding: '20px', textAlign: 'center', color: C.ink400, fontSize: '12px' },
          }, 'Chưa có dữ liệu sử dụng AI trong kỳ này.'),
        ));
        return;
      }
      list.slice(0, 5).forEach((m, i) => {
        const name = m.model || m.name || ('Model ' + (i + 1));
        const reqs = m.requests || m.requests_count || 0;
        const toks = m.tokens || m.tokens_count || 0;
        const cost = m.cost_usd || m.cost || 0;
        tbody.appendChild(el('tr', { style: { borderTop: '1px solid ' + C.border } }, [
          el('td', { style: tdStyle() }, [
            el('span', {
              style: {
                display: 'inline-block', width: '6px', height: '6px', borderRadius: '50%',
                background: [C.crown, C.cyan, C.gold, C.ajna, C.crownLight][i % 5],
                marginRight: '8px',
              },
            }),
            name,
          ]),
          el('td', { style: tdStyle('right', true) }, fmtN(reqs)),
          el('td', { style: tdStyle('right', true) }, fmtN(toks)),
          el('td', { style: tdStyle('right', true) }, fmtUsd(cost)),
        ]));
      });
    }

    if (rows) {
      fillRows(rows);
    } else {
      // Lazy fetch
      tbody.appendChild(el('tr', null,
        el('td', { colspan: '4', style: { padding: '12px', textAlign: 'center', color: C.ink400, fontSize: '12px' } }, 'Đang tải…'),
      ));
      const ws = (window.state && window.state.currentWs) || '';
      window.ZeniAPI.usage.byModel(ws)
        .then(data => {
          const list = Array.isArray(data) ? data : (data && data.models) || [];
          fillRows(list);
        })
        .catch(() => fillRows([]));
    }

    return wrap;
  }

  function thStyle(align) {
    return {
      padding: '8px 10px',
      textAlign: align || 'left',
      fontFamily: FONT_MONO,
      fontSize: '10.5px',
      fontWeight: '600',
      letterSpacing: '0.12em',
      textTransform: 'uppercase',
      color: C.ink300,
      borderBottom: '1px solid ' + C.border,
    };
  }
  function tdStyle(align, mono) {
    return {
      padding: '12px 10px',
      textAlign: align || 'left',
      fontFamily: mono ? FONT_MONO : FONT,
      fontSize: '13px',
      color: C.ink100,
    };
  }

  // ─── Section 5: Upgrade prompt ───────────────────────────
  function anyQuotaWarning(usage) {
    if (!usage || !usage.quota_percent) return false;
    return Object.values(usage.quota_percent).some(p => Number(p) > 80);
  }

  function renderUpgradePrompt(usage) {
    const qp = (usage && usage.quota_percent) || {};
    const overs = Object.entries(qp).filter(([_, v]) => Number(v) > 80);
    const labelMap = {
      requests: 'Yêu cầu', ai_tokens: 'AI Tokens', storage: 'Lưu trữ', router: 'Router',
    };
    const overText = overs.map(([k, v]) => labelMap[k] || k).join(', ');
    const planName = (usage && usage.plan && usage.plan.name) || 'Free';
    const nextPlan = planName === 'Free' ? 'Starter'
                  : planName === 'Starter' ? 'Pro'
                  : planName === 'Pro' ? 'Business'
                  : 'Enterprise';

    const wrap = el('section', {
      style: {
        background: 'linear-gradient(135deg, rgba(248,113,113,0.10), rgba(253,230,138,0.06))',
        border: '1px solid rgba(248,113,113,0.32)',
        borderRadius: '12px',
        padding: '24px',
        marginBottom: '20px',
        display: 'flex',
        flexWrap: 'wrap',
        gap: '16px',
        alignItems: 'center',
        justifyContent: 'space-between',
      },
    });

    wrap.appendChild(el('div', { style: { flex: '1 1 320px' } }, [
      el('div', {
        style: {
          fontFamily: FONT_MONO, fontSize: '11px', letterSpacing: '0.16em',
          color: C.red, textTransform: 'uppercase', marginBottom: '6px',
        },
      }, 'Cảnh báo · Hạn mức'),
      el('h3', {
        style: { fontSize: '18px', fontWeight: '700', color: C.ink50, margin: '0 0 6px 0' },
      }, 'Bạn sắp hết quota — Nâng cấp lên ' + nextPlan + ' để tăng giới hạn'),
      el('div', {
        style: { fontSize: '13px', color: C.ink200, lineHeight: '1.6' },
      }, 'Đã vượt 80% ở: ' + overText + '. Nâng cấp ngay để tránh gián đoạn dịch vụ.'),
    ]));

    const btn = el('a', {
      href: '/pricing.html',
      style: {
        display: 'inline-flex', alignItems: 'center', gap: '8px',
        padding: '12px 22px', borderRadius: '10px',
        background: 'linear-gradient(135deg, ' + C.gold + ', ' + C.goldDeep + ')',
        color: '#1a0f00', fontWeight: '700', fontSize: '13.5px',
        textDecoration: 'none', letterSpacing: '0.02em',
        boxShadow: '0 6px 20px rgba(245,158,11,0.35)',
      },
    }, 'Xem các gói →');
    wrap.appendChild(btn);

    return wrap;
  }

  // ─── Section 6: Subscription details + cancel ────────────
  function renderSubDetails(sub) {
    const wrap = el('section', {
      style: {
        background: C.bgCard, border: '1px solid ' + C.border,
        borderRadius: '12px', padding: '24px',
      },
    });
    wrap.appendChild(el('h3', {
      style: { fontSize: '13px', fontWeight: '700', color: C.gold, textTransform: 'uppercase', letterSpacing: '0.12em', margin: '0 0 14px 0' },
    }, 'Chi tiết đăng ký'));

    if (!sub) {
      wrap.appendChild(el('div', {
        style: { fontSize: '13px', color: C.ink300, lineHeight: '1.7' },
      }, 'Chưa có đăng ký nào. Bạn đang dùng gói Free mặc định.'));
      return wrap;
    }

    const grid = el('div', {
      style: {
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: '16px',
        marginBottom: '18px',
      },
    });

    function kvCell(label, value) {
      return el('div', null, [
        el('div', {
          style: { fontFamily: FONT_MONO, fontSize: '10.5px', letterSpacing: '0.12em', color: C.ink400, textTransform: 'uppercase', marginBottom: '4px' },
        }, label),
        el('div', {
          style: { fontSize: '14px', color: C.ink100, fontWeight: '600' },
        }, value),
      ]);
    }

    grid.appendChild(kvCell('Trạng thái', (sub.status || '—').toUpperCase()));
    grid.appendChild(kvCell('Bắt đầu kỳ',  fmtDate(sub.period_start)));
    grid.appendChild(kvCell('Kết thúc kỳ', fmtDate(sub.period_end)));
    grid.appendChild(kvCell('Phương thức', sub.payment_method || 'manual'));
    if (sub.next_renewal) grid.appendChild(kvCell('Gia hạn tiếp theo', fmtDate(sub.next_renewal)));
    if (sub.plan && sub.plan.name) grid.appendChild(kvCell('Gói', sub.plan.name));

    wrap.appendChild(grid);

    // Action buttons
    const actions = el('div', { style: { display: 'flex', gap: '10px', flexWrap: 'wrap' } });
    actions.appendChild(el('a', {
      href: '/pricing.html',
      style: {
        padding: '10px 18px', borderRadius: '8px',
        border: '1px solid ' + C.borderStr,
        color: C.ink100, fontSize: '13px', fontWeight: '600',
        textDecoration: 'none',
        background: 'rgba(168,85,247,0.06)',
      },
    }, 'Đổi gói'));

    if (sub.status === 'active' && sub.plan && sub.plan.name && sub.plan.name !== 'Free') {
      const cancelBtn = el('button', {
        style: {
          padding: '10px 18px', borderRadius: '8px',
          border: '1px solid rgba(248,113,113,0.3)',
          background: 'rgba(248,113,113,0.08)',
          color: C.red, fontSize: '13px', fontWeight: '600',
          cursor: 'pointer', fontFamily: FONT,
        },
        onclick: async () => {
          const ok = window.confirm('Bạn chắc chắn huỷ gói? Quyền lợi sẽ kết thúc cuối kỳ hiện tại.');
          if (!ok) return;
          cancelBtn.disabled = true;
          cancelBtn.textContent = 'Đang huỷ…';
          try {
            const ws = (window.state && window.state.currentWs) || '';
            await window.ZeniAPI.usage.cancel(ws);
            cancelBtn.textContent = 'Đã huỷ ✓';
            cancelBtn.style.color = C.green;
            cancelBtn.style.borderColor = 'rgba(74,222,128,0.4)';
            cancelBtn.style.background = 'rgba(74,222,128,0.08)';
          } catch (err) {
            cancelBtn.disabled = false;
            cancelBtn.textContent = 'Huỷ gói';
            window.alert('Huỷ thất bại: ' + (err.message || 'lỗi không xác định'));
          }
        },
      }, 'Huỷ gói');
      actions.appendChild(cancelBtn);
    }
    wrap.appendChild(actions);
    return wrap;
  }

  // ─── Loading skeleton ────────────────────────────────────
  function renderLoading() {
    const wrap = el('div', {
      style: {
        padding: '60px 24px', textAlign: 'center',
        color: C.ink300, fontSize: '13px', fontFamily: FONT_MONO,
        letterSpacing: '0.08em',
      },
    }, 'Đang tải dữ liệu sử dụng…');
    return wrap;
  }

  function renderError(err) {
    return el('div', {
      style: {
        padding: '24px', borderRadius: '12px',
        background: 'rgba(248,113,113,0.08)',
        border: '1px solid rgba(248,113,113,0.3)',
        color: C.red, fontSize: '13px',
      },
    }, 'Không tải được dữ liệu sử dụng: ' + (err.message || 'lỗi không xác định'));
  }

  // ─── Public API ──────────────────────────────────────────
  window.ZeniUsageUI = {
    async render(rootEl, ws) {
      if (!rootEl) return;
      rootEl.innerHTML = '';
      rootEl.style.maxWidth = '1200px';
      rootEl.style.margin = '0 auto';
      rootEl.style.padding = '24px 20px';
      rootEl.style.fontFamily = FONT;
      rootEl.style.color = C.ink100;

      const loading = renderLoading();
      rootEl.appendChild(loading);

      const wsCode = ws || (window.state && window.state.currentWs) || 'holdings';

      let usage = null, sub = null, routerQuota = null;
      try {
        const results = await Promise.allSettled([
          window.ZeniAPI.usage.current(wsCode),
          window.ZeniAPI.usage.currentSub(wsCode),
          window.ZeniAPI.usage.routerQuota(wsCode),
        ]);
        usage       = results[0].status === 'fulfilled' ? results[0].value : null;
        sub         = results[1].status === 'fulfilled' ? results[1].value : null;
        routerQuota = results[2].status === 'fulfilled' ? results[2].value : null;
      } catch (err) {
        rootEl.innerHTML = '';
        rootEl.appendChild(renderError(err));
        return;
      }

      if (!usage) {
        // Friendly fallback when backend β chưa sẵn sàng
        usage = {
          workspace_code: wsCode,
          period_start: new Date(new Date().setDate(1)).toISOString(),
          period_end:   new Date(new Date(new Date().getFullYear(), new Date().getMonth() + 1, 0)).toISOString(),
          requests_count: 0, ai_tokens_count: 0, storage_gb_avg: 0, router_cost_usd: 0,
          plan: { name: 'Free', price_vnd: 0, price_usd: 0,
                  requests_limit: 10000, ai_tokens_limit: 1000000,
                  storage_gb_limit: 1, router_quota_usd: 5 },
          quota_percent: { requests: 0, ai_tokens: 0, storage: 0, router: 0 },
        };
      }

      rootEl.innerHTML = '';
      rootEl.appendChild(this._renderHeader(usage, sub));
      rootEl.appendChild(this._renderQuotaMeters(usage, routerQuota));
      try { rootEl.appendChild(await this._renderUsageChart(wsCode)); } catch (_) {}
      rootEl.appendChild(this._renderTopModels(usage));
      if (this._anyQuotaWarning(usage)) {
        rootEl.appendChild(this._renderUpgradePrompt(usage));
      }
      rootEl.appendChild(this._renderSubDetails(sub));
    },
    _renderHeader: renderHeader,
    _renderQuotaMeters: renderQuotaMeters,
    _renderUsageChart: renderUsageChart,
    _renderTopModels: renderTopModels,
    _renderUpgradePrompt: renderUpgradePrompt,
    _renderSubDetails: renderSubDetails,
    _anyQuotaWarning: anyQuotaWarning,
  };

  console.log('[zeni-usage] module loaded');
})();
