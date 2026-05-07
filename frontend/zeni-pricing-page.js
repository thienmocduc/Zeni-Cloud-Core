/*
 * ZENI CLOUD CORE · PRICING LANDING PAGE MODULE
 * Standalone module loaded by /pricing.html.
 *
 *   - window.ZeniPricingPage.render(rootEl)
 *   - Tự fetch GET /api/v1/pricing/plans, render hero + 5 tier cards +
 *     so sánh competitor + FAQ + CTA footer.
 *   - Toggle Tháng / Năm (giảm 17%).
 *   - Subscribe button: nếu logged-in → POST /pricing/subscribe;
 *     nếu chưa → /signup.html?plan=<id>.
 */
(function () {
  'use strict';

  // ─── Color tokens (đồng bộ với landing.html / zeni-usage.js) ───
  const C = {
    bgVoid:    '#030014',
    bgBase:    '#08051F',
    bgCard:    'rgba(255,255,255,0.03)',
    bgCardHi:  'rgba(255,255,255,0.06)',
    border:    'rgba(255,255,255,0.06)',
    borderStr: 'rgba(168,139,250,0.18)',
    ink50:     '#FAF5FF',
    ink100:    '#EDE9FE',
    ink200:    '#C4B5FD',
    ink300:    '#9E8BE5',
    ink400:    '#7C6BB0',
    gold:      '#FDE68A',
    goldDeep:  '#F59E0B',
    crown:     '#A855F7',
    crownLight:'#D8B4FE',
    ajna:      '#6366F1',
    cyan:      '#22D3EE',
    green:     '#4ade80',
    red:       '#f87171',
  };
  const FONT      = "'Inter', system-ui, -apple-system, sans-serif";
  const FONT_MONO = "'Roboto Mono', ui-monospace, 'SF Mono', Consolas, monospace";

  // ─── Helpers ──────────────────────────────────────────────
  function fmtVnd(n) { return Math.round(Number(n) || 0).toLocaleString('vi-VN') + 'đ'; }
  function fmtUsd(n) { return '$' + (Number(n) || 0).toFixed(2); }
  function esc(s)    {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }
  function isAuthed() {
    return !!localStorage.getItem('zeni.jwt.access');
  }
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k in attrs) {
        if (k === 'style' && typeof attrs[k] === 'object') Object.assign(node.style, attrs[k]);
        else if (k === 'class') node.className = attrs[k];
        else if (k === 'html')  node.innerHTML = attrs[k];
        else if (k.startsWith('on') && typeof attrs[k] === 'function') node.addEventListener(k.slice(2).toLowerCase(), attrs[k]);
        else if (attrs[k] != null) node.setAttribute(k, attrs[k]);
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

  // ─── Default tiers (fallback nếu API /pricing/plans chưa sẵn sàng) ──
  const DEFAULT_PLANS = [
    {
      id: 'free', name: 'Free',
      price_vnd: 0, price_vnd_yearly: 0,
      price_usd: 0, price_usd_yearly: 0,
      tagline: 'Khám phá Zeni Cloud',
      features: [
        '10,000 yêu cầu / tháng',
        '1M AI tokens / tháng',
        '1 GB lưu trữ',
        '$5 router credit',
        '1 dự án Cloud Run',
        '1 thành viên',
        'Cộng đồng support',
        'Compliance NĐ13 cơ bản',
      ],
      cta: 'Bắt đầu miễn phí',
    },
    {
      id: 'starter', name: 'Starter',
      price_vnd: 199000, price_vnd_yearly: 1990000,
      price_usd: 8, price_usd_yearly: 80,
      tagline: 'Cho cá nhân & freelancer',
      features: [
        '100,000 yêu cầu / tháng',
        '10M AI tokens / tháng',
        '10 GB lưu trữ',
        '$20 router credit',
        '3 dự án Cloud Run',
        '3 thành viên',
        'Email support 24h',
        'Backup hằng ngày',
      ],
      cta: 'Chọn Starter',
    },
    {
      id: 'pro', name: 'Pro',
      price_vnd: 499000, price_vnd_yearly: 4990000,
      price_usd: 20, price_usd_yearly: 200,
      tagline: 'Cho startup đang lớn',
      features: [
        '1M yêu cầu / tháng',
        '100M AI tokens / tháng',
        '100 GB lưu trữ',
        '$100 router credit',
        '10 dự án Cloud Run',
        '10 thành viên',
        'Priority support 4h',
        'Audit log 90 ngày',
      ],
      cta: 'Chọn Pro',
      recommended: true,
    },
    {
      id: 'business', name: 'Business',
      price_vnd: 1999000, price_vnd_yearly: 19990000,
      price_usd: 80, price_usd_yearly: 800,
      tagline: 'Cho công ty 10–50 người',
      features: [
        '10M yêu cầu / tháng',
        '1B AI tokens / tháng',
        '1 TB lưu trữ',
        '$500 router credit',
        'Không giới hạn dự án',
        'Không giới hạn thành viên',
        'SSO + RBAC nâng cao',
        'SLA 99.9% + on-call',
      ],
      cta: 'Chọn Business',
    },
    {
      id: 'enterprise', name: 'Enterprise',
      price_vnd: null, price_vnd_yearly: null,
      price_usd: null, price_usd_yearly: null,
      tagline: 'Tập đoàn & cơ quan nhà nước',
      features: [
        'Hạn mức tuỳ chỉnh',
        'Single tenant / VPC riêng',
        'On-premise option',
        'Compliance NĐ53 + ISO 27001',
        'Dedicated success manager',
        'SLA 99.99% + audit log vĩnh viễn',
        'Đào tạo onboarding',
        'Hợp đồng khung VND',
      ],
      cta: 'Liên hệ Sales',
      contactSales: true,
    },
  ];

  // ─── State ────────────────────────────────────────────────
  let billingCycle = 'monthly'; // monthly | yearly

  // ─── API helpers (không phụ thuộc ZeniAPI._fetch private) ──
  async function fetchPlans() {
    try {
      const r = await fetch('/api/v1/pricing/plans', {
        headers: { 'Accept': 'application/json' },
      });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      if (Array.isArray(data) && data.length) return data;
      if (data && Array.isArray(data.plans) && data.plans.length) return data.plans;
      return DEFAULT_PLANS;
    } catch (_) {
      return DEFAULT_PLANS;
    }
  }
  async function postSubscribe(workspace_code, plan_id) {
    const tok = localStorage.getItem('zeni.jwt.access');
    const r = await fetch('/api/v1/pricing/subscribe', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json', 'Accept': 'application/json',
        ...(tok ? { Authorization: 'Bearer ' + tok } : {}),
      },
      credentials: 'same-origin',
      body: JSON.stringify({ workspace_code, plan_id, payment_method: 'manual' }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      throw new Error(j.detail || ('HTTP ' + r.status));
    }
    return r.json();
  }

  // ─── Background decoration ────────────────────────────────
  function injectBackground() {
    if (document.getElementById('zeni-pricing-bg')) return;
    const s = document.createElement('style');
    s.id = 'zeni-pricing-bg';
    s.textContent = ''
      + 'body{background:' + C.bgVoid + ';color:' + C.ink100 + ';font-family:' + FONT + ';margin:0;min-height:100vh;}'
      + 'body::before{content:"";position:fixed;inset:0;z-index:-2;background:'
      +   'radial-gradient(ellipse 1200px 900px at 80% 5%, rgba(168,85,247,0.14), transparent 55%),'
      +   'radial-gradient(ellipse 1000px 700px at 5% 90%, rgba(99,102,241,0.11), transparent 55%),'
      +   'radial-gradient(ellipse at 50% 50%, #0A0520 0%, #030014 70%);pointer-events:none;}'
      + 'a{color:' + C.gold + ';}'
      + '*,*::before,*::after{box-sizing:border-box;}'
      + '.zp-hover-card:hover{transform:translateY(-4px);transition:transform 0.2s;}'
      + '@media(max-width:720px){.zp-tier-grid{grid-template-columns:1fr !important;}}';
    document.head.appendChild(s);
  }

  // ─── Hero ─────────────────────────────────────────────────
  function renderHero(onToggle) {
    const wrap = el('section', {
      style: {
        textAlign: 'center', padding: '80px 20px 40px',
        maxWidth: '900px', margin: '0 auto',
      },
    });
    wrap.appendChild(el('div', {
      style: {
        display: 'inline-flex', alignItems: 'center', gap: '8px',
        padding: '6px 14px', borderRadius: '999px',
        background: 'rgba(168,85,247,0.1)', border: '1px solid ' + C.borderStr,
        color: C.crownLight, fontFamily: FONT_MONO, fontSize: '11px',
        letterSpacing: '0.16em', textTransform: 'uppercase', marginBottom: '20px',
      },
    }, 'Pricing · Trả tiền VND'));
    wrap.appendChild(el('h1', {
      style: {
        fontSize: 'clamp(32px, 5vw, 52px)', fontWeight: '800',
        letterSpacing: '-0.03em', lineHeight: '1.1',
        margin: '0 0 18px 0', color: C.ink50,
      },
    }, 'Pricing đơn giản, minh bạch'));
    wrap.appendChild(el('p', {
      style: {
        fontSize: '17px', color: C.ink200, lineHeight: '1.6',
        maxWidth: '640px', margin: '0 auto 28px',
      },
    }, 'Trả tiền VND, hoá đơn VAT, hỗ trợ tiếng Việt. Rẻ hơn AWS / GCP 30% cho cùng workload.'));

    // Toggle Tháng / Năm
    const toggle = el('div', {
      style: {
        display: 'inline-flex', padding: '4px',
        background: C.bgCard, border: '1px solid ' + C.border,
        borderRadius: '999px', gap: '4px',
      },
    });
    function makeBtn(label, value, sub) {
      const btn = el('button', {
        type: 'button',
        style: {
          padding: '8px 18px', borderRadius: '999px',
          background: billingCycle === value
            ? 'linear-gradient(135deg, ' + C.gold + ', ' + C.goldDeep + ')'
            : 'transparent',
          color: billingCycle === value ? '#1a0f00' : C.ink200,
          border: 'none', cursor: 'pointer',
          fontFamily: FONT, fontSize: '13px', fontWeight: '600',
          transition: 'all 0.15s',
          display: 'inline-flex', alignItems: 'center', gap: '6px',
        },
        onclick: () => { billingCycle = value; onToggle(); },
      });
      btn.textContent = label;
      if (sub) {
        btn.appendChild(el('span', {
          style: { fontSize: '10px', opacity: 0.85, fontWeight: '700' },
        }, sub));
      }
      return btn;
    }
    toggle.appendChild(makeBtn('Tháng', 'monthly'));
    toggle.appendChild(makeBtn('Năm', 'yearly', '−17%'));
    wrap.appendChild(toggle);
    return wrap;
  }

  // ─── Tier cards ───────────────────────────────────────────
  function tierCard(plan, onSubscribe) {
    const isYearly = billingCycle === 'yearly';
    const priceVnd = plan.contactSales ? null : (isYearly ? plan.price_vnd_yearly : plan.price_vnd);
    const priceUsd = plan.contactSales ? null : (isYearly ? plan.price_usd_yearly : plan.price_usd);
    const isFree = !plan.contactSales && (plan.price_vnd === 0 || plan.price_vnd == null);
    const recommended = !!plan.recommended;

    const card = el('div', {
      class: 'zp-hover-card',
      style: {
        position: 'relative',
        background: recommended
          ? 'linear-gradient(180deg, rgba(253,230,138,0.08), rgba(168,85,247,0.05))'
          : C.bgCard,
        border: '1px solid ' + (recommended ? C.gold : C.border),
        borderRadius: '16px',
        padding: '28px 24px',
        display: 'flex', flexDirection: 'column', gap: '18px',
        boxShadow: recommended ? '0 0 30px rgba(253,230,138,0.18)' : 'none',
      },
    });

    if (recommended) {
      card.appendChild(el('div', {
        style: {
          position: 'absolute', top: '-12px', left: '50%',
          transform: 'translateX(-50%)',
          padding: '4px 14px', borderRadius: '999px',
          background: 'linear-gradient(135deg, ' + C.gold + ', ' + C.goldDeep + ')',
          color: '#1a0f00', fontWeight: '700', fontSize: '11px',
          letterSpacing: '0.12em', textTransform: 'uppercase',
        },
      }, 'Khuyên dùng'));
    }

    card.appendChild(el('div', null, [
      el('div', {
        style: {
          fontSize: '20px', fontWeight: '800', color: C.ink50,
          letterSpacing: '-0.01em', marginBottom: '4px',
        },
      }, plan.name),
      el('div', {
        style: { fontSize: '13px', color: C.ink300 },
      }, plan.tagline || ''),
    ]));

    // Price block
    const priceBlock = el('div', {
      style: { borderTop: '1px solid ' + C.border, paddingTop: '18px' },
    });
    if (plan.contactSales) {
      priceBlock.appendChild(el('div', {
        style: { fontSize: '28px', fontWeight: '800', color: C.ink50, fontFamily: FONT },
      }, 'Liên hệ'));
      priceBlock.appendChild(el('div', {
        style: { fontSize: '12px', color: C.ink300, marginTop: '6px' },
      }, 'Báo giá theo nhu cầu'));
    } else {
      priceBlock.appendChild(el('div', {
        style: { display: 'flex', alignItems: 'baseline', gap: '6px', flexWrap: 'wrap' },
      }, [
        el('span', {
          style: { fontSize: '32px', fontWeight: '800', color: C.gold, fontFamily: FONT_MONO, letterSpacing: '-0.02em' },
        }, fmtVnd(priceVnd)),
        el('span', {
          style: { fontSize: '13px', color: C.ink300 },
        }, isYearly ? '/ năm' : '/ tháng'),
      ]));
      priceBlock.appendChild(el('div', {
        style: { fontSize: '12px', color: C.ink400, marginTop: '4px', fontFamily: FONT_MONO },
      }, '≈ ' + fmtUsd(priceUsd) + (isYearly ? '/yr' : '/mo')));
    }
    card.appendChild(priceBlock);

    // Features
    const ul = el('ul', {
      style: { listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: '8px' },
    });
    (plan.features || []).slice(0, 8).forEach(f => {
      ul.appendChild(el('li', {
        style: { display: 'flex', gap: '10px', fontSize: '13px', color: C.ink100, lineHeight: '1.5' },
      }, [
        el('span', {
          style: {
            flexShrink: 0, width: '18px', height: '18px', borderRadius: '50%',
            background: 'rgba(74,222,128,0.15)',
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            color: C.green, fontSize: '11px', fontWeight: '700',
          },
        }, '✓'),
        f,
      ]));
    });
    card.appendChild(ul);

    // CTA
    const ctaLabel = plan.cta || (isFree ? 'Bắt đầu miễn phí' : 'Chọn ' + plan.name);
    const cta = el('button', {
      type: 'button',
      style: {
        marginTop: 'auto',
        padding: '12px 18px', borderRadius: '10px',
        background: recommended
          ? 'linear-gradient(135deg, ' + C.gold + ', ' + C.goldDeep + ')'
          : 'rgba(168,85,247,0.12)',
        color: recommended ? '#1a0f00' : C.ink50,
        border: '1px solid ' + (recommended ? C.gold : C.borderStr),
        fontSize: '14px', fontWeight: '700',
        cursor: 'pointer', fontFamily: FONT,
        letterSpacing: '0.02em',
        transition: 'all 0.15s',
      },
      onclick: () => onSubscribe(plan),
    }, ctaLabel);
    card.appendChild(cta);

    return card;
  }

  function renderTiers(plans, onSubscribe) {
    const wrap = el('section', {
      style: { padding: '20px', maxWidth: '1280px', margin: '0 auto' },
    });
    const grid = el('div', {
      class: 'zp-tier-grid',
      style: {
        display: 'grid',
        gridTemplateColumns: 'repeat(5, minmax(0, 1fr))',
        gap: '16px',
      },
    });
    plans.forEach(p => grid.appendChild(tierCard(p, onSubscribe)));
    wrap.appendChild(grid);
    return wrap;
  }

  // ─── Competitor comparison ────────────────────────────────
  function renderComparison() {
    const wrap = el('section', {
      style: { padding: '60px 20px', maxWidth: '1100px', margin: '0 auto' },
    });
    wrap.appendChild(el('h2', {
      style: {
        textAlign: 'center', fontSize: '28px', fontWeight: '800',
        color: C.ink50, margin: '0 0 12px 0', letterSpacing: '-0.02em',
      },
    }, 'So sánh với competitor'));
    wrap.appendChild(el('p', {
      style: { textAlign: 'center', color: C.ink300, fontSize: '14px', margin: '0 0 28px 0' },
    }, 'Cùng workload tương đương, Zeni Cloud rẻ hơn 25–35% và có hoá đơn VAT VND.'));

    const tableWrap = el('div', {
      style: { overflowX: 'auto', background: C.bgCard, border: '1px solid ' + C.border, borderRadius: '12px' },
    });
    const tbl = el('table', {
      style: { width: '100%', borderCollapse: 'collapse', fontSize: '13px', minWidth: '600px' },
    });
    const th = (t, hl) => el('th', {
      style: {
        padding: '14px 16px', textAlign: 'left',
        fontFamily: FONT_MONO, fontSize: '11px', letterSpacing: '0.12em', textTransform: 'uppercase',
        color: hl ? C.gold : C.ink300, fontWeight: '700',
        borderBottom: '1px solid ' + C.borderStr,
        background: hl ? 'rgba(253,230,138,0.05)' : 'transparent',
      },
    }, t);
    tbl.appendChild(el('thead', null, el('tr', null, [
      th('Tính năng'), th('AWS'), th('Vercel'), th('Supabase'), th('Zeni Cloud', true),
    ])));
    const rows = [
      ['Hoá đơn VAT VND',          'Không',            'Không',            'Không',          'Có'],
      ['Hỗ trợ tiếng Việt',         'Không',            'Hạn chế',          'Không',          'Có (24/7)'],
      ['Compliance NĐ13',           'Tự lo',            'Tự lo',            'Tự lo',          'Sẵn'],
      ['Pricing tier 100K req',     '~$45',             '$20',              '$25',            'Starter $8'],
      ['AI router gộp tokens',      'Không',            'Không',            'Không',          'Có'],
      ['Web3 + Cloud + AI',         'Tách biệt',        'Tách biệt',        'Tách biệt',      'Một dashboard'],
      ['Setup time',                'Vài ngày',         'Vài giờ',          'Vài giờ',        '10 phút'],
    ];
    const tbody = el('tbody');
    rows.forEach((r, i) => {
      const tr = el('tr', { style: { borderBottom: '1px solid ' + C.border } });
      r.forEach((cell, j) => {
        const isZeni = j === 4;
        tr.appendChild(el('td', {
          style: {
            padding: '12px 16px',
            color: isZeni ? C.gold : C.ink200,
            fontWeight: isZeni ? '600' : '400',
            background: isZeni ? 'rgba(253,230,138,0.04)' : 'transparent',
          },
        }, cell));
      });
      tbody.appendChild(tr);
    });
    tbl.appendChild(tbody);
    tableWrap.appendChild(tbl);
    wrap.appendChild(tableWrap);
    return wrap;
  }

  // ─── FAQ accordion ────────────────────────────────────────
  const FAQ = [
    ['Tôi có thể đổi gói bất cứ lúc nào không?',
     'Có. Bạn nâng cấp ngay lập tức (tính prorated phần ngày còn lại của kỳ). Hạ cấp áp dụng vào kỳ tiếp theo.'],
    ['Thanh toán bằng VND hay USD?',
     'Mặc định VND qua chuyển khoản, VNPay, MoMo. Hoá đơn VAT phát hành trong 24h. Hỗ trợ USD qua Stripe nếu cần.'],
    ['Có hợp đồng dài hạn ràng buộc không?',
     'Không. Gói tháng có thể huỷ bất kỳ lúc nào. Gói năm hoàn tiền tỷ lệ trong 30 ngày đầu.'],
    ['Vượt quota thì sao?',
     'Hệ thống cảnh báo từ 80%. Bạn có thể nâng cấp gói hoặc bật pay-as-you-go (tính theo đơn giá đã công bố).'],
    ['Dữ liệu có lưu ở Việt Nam không?',
     'Có. Region mặc định là asia-southeast1 (Singapore) hoặc data center Việt Nam (option Enterprise) — compliance NĐ13.'],
    ['Có discount cho startup / education không?',
     '50% off năm đầu cho startup gọi vốn dưới $1M, 30% off cho cơ sở giáo dục được kiểm chứng.'],
    ['Router AI tính phí thế nào?',
     'Bạn trả đúng giá nhà cung cấp (OpenAI/Anthropic/Gemini) + 5% margin. Mỗi gói có credit miễn phí ban đầu.'],
    ['Tôi có thể tự host không?',
     'Có. Gói Enterprise hỗ trợ on-premise hoặc VPC riêng, kèm hợp đồng SLA và training onboarding.'],
  ];

  function renderFAQ() {
    const wrap = el('section', {
      style: { padding: '60px 20px', maxWidth: '900px', margin: '0 auto' },
    });
    wrap.appendChild(el('h2', {
      style: {
        textAlign: 'center', fontSize: '28px', fontWeight: '800',
        color: C.ink50, margin: '0 0 28px 0', letterSpacing: '-0.02em',
      },
    }, 'Câu hỏi thường gặp'));

    const list = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } });
    FAQ.forEach((qa) => {
      const item = el('details', {
        style: {
          background: C.bgCard, border: '1px solid ' + C.border,
          borderRadius: '10px', padding: '16px 18px',
          cursor: 'pointer',
        },
      });
      item.appendChild(el('summary', {
        style: {
          fontSize: '14.5px', fontWeight: '600', color: C.ink50,
          cursor: 'pointer', listStyle: 'none', display: 'flex',
          justifyContent: 'space-between', alignItems: 'center', gap: '12px',
        },
      }, [
        el('span', null, qa[0]),
        el('span', { style: { color: C.gold, fontFamily: FONT_MONO, fontWeight: '700' } }, '+'),
      ]));
      item.appendChild(el('div', {
        style: {
          fontSize: '13.5px', color: C.ink200, lineHeight: '1.7',
          marginTop: '12px', paddingTop: '12px', borderTop: '1px solid ' + C.border,
        },
      }, qa[1]));
      list.appendChild(item);
    });
    wrap.appendChild(list);
    return wrap;
  }

  // ─── Footer CTA ───────────────────────────────────────────
  function renderFooterCTA() {
    const wrap = el('section', {
      style: {
        margin: '40px 20px 80px', padding: '60px 32px',
        maxWidth: '1100px', marginLeft: 'auto', marginRight: 'auto',
        background: 'linear-gradient(135deg, rgba(168,85,247,0.18), rgba(99,102,241,0.10))',
        border: '1px solid ' + C.borderStr,
        borderRadius: '20px',
        textAlign: 'center',
      },
    });
    wrap.appendChild(el('h2', {
      style: {
        fontSize: 'clamp(24px, 4vw, 36px)', fontWeight: '800',
        color: C.ink50, margin: '0 0 14px 0', letterSpacing: '-0.02em',
      },
    }, 'Sẵn sàng triển khai?'));
    wrap.appendChild(el('p', {
      style: { fontSize: '15px', color: C.ink200, margin: '0 0 28px 0' },
    }, 'Tạo tài khoản miễn phí — không cần thẻ. 10,000 yêu cầu + $5 router credit ngay.'));
    wrap.appendChild(el('a', {
      href: '/signup.html',
      style: {
        display: 'inline-flex', alignItems: 'center', gap: '10px',
        padding: '14px 32px', borderRadius: '12px',
        background: 'linear-gradient(135deg, ' + C.gold + ', ' + C.goldDeep + ')',
        color: '#1a0f00', fontWeight: '700', fontSize: '15px',
        textDecoration: 'none', letterSpacing: '0.02em',
        boxShadow: '0 8px 28px rgba(245,158,11,0.4)',
      },
    }, 'Bắt đầu miễn phí →'));
    return wrap;
  }

  // ─── Subscribe handler ────────────────────────────────────
  async function handleSubscribe(plan) {
    if (plan.contactSales) {
      window.location.href = 'mailto:sales@zenicloud.io?subject=Enterprise%20Plan';
      return;
    }
    if (!isAuthed()) {
      window.location.href = '/signup.html?plan=' + encodeURIComponent(plan.id);
      return;
    }
    const ws = (window.state && window.state.currentWs) ||
               (window.__ZENI_REAL_USER && window.__ZENI_REAL_USER.workspaces &&
                window.__ZENI_REAL_USER.workspaces[0]) || 'holdings';
    try {
      await postSubscribe(ws, plan.id);
      window.alert('Đăng ký gói ' + plan.name + ' thành công! Sẽ chuyển về trang sử dụng.');
      window.location.href = '/index.html#usage';
    } catch (err) {
      window.alert('Đăng ký thất bại: ' + (err.message || 'lỗi không xác định'));
    }
  }

  // ─── Public API ──────────────────────────────────────────
  window.ZeniPricingPage = {
    async render(rootEl) {
      const root = rootEl || document.getElementById('pricing-root') || document.body;
      injectBackground();

      // Skeleton during load
      root.innerHTML = '';
      root.appendChild(el('div', {
        style: {
          padding: '120px 20px', textAlign: 'center',
          color: C.ink300, fontFamily: FONT_MONO, fontSize: '13px',
          letterSpacing: '0.08em',
        },
      }, 'Đang tải bảng giá…'));

      const plans = await fetchPlans();

      function paint() {
        root.innerHTML = '';
        root.appendChild(renderHero(paint));
        root.appendChild(renderTiers(plans, handleSubscribe));
        root.appendChild(renderComparison());
        root.appendChild(renderFAQ());
        root.appendChild(renderFooterCTA());
      }
      paint();
    },
    _renderHero: renderHero,
    _renderTiers: renderTiers,
    _renderComparison: renderComparison,
    _renderFAQ: renderFAQ,
    _renderFooterCTA: renderFooterCTA,
  };

  console.log('[zeni-pricing-page] module loaded');
})();
