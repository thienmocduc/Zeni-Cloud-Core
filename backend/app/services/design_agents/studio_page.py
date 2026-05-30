# -*- coding: utf-8 -*-
"""Served HTML for the Design Studio — guided selection form (thay cho viết prompt).

Khách CHỌN từ các mục mô tả chi tiết + nhập mong muốn cá nhân hoá → bấm "Tạo thiết kế"
→ 6 agent ra mặt bằng + render khớp đúng lựa chọn. Trả qua GET /api/v1/design/studio.
Same-origin nên gọi /api/v1 trực tiếp, không vướng CORS.
"""

STUDIO_HTML = r"""<!doctype html>
<html lang="vi"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Zeni Design Studio · Thiết kế theo lựa chọn</title>
<style>
  :root{--bg:#0e0d0b;--panel:#161410;--line:#3a342a;--gold:#d9c9a3;--gold2:#c0a85f;--mut:#9c8f74;--txt:#efe7d8;--accent:#7a6a3f}
  *{box-sizing:border-box}
  /* Vietnamese-safe luxury serif: Constantia/Cambria compose ấ/ầ/ế/ề… correctly on Windows
     (Georgia/Times float the tone marks). Falls back to VN-capable system-ui sans elsewhere. */
  body{margin:0;background:var(--bg);color:var(--txt);font-family:'Constantia','Cambria','Palatino Linotype',Palatino,'Iowan Old Style',system-ui,-apple-system,'Segoe UI',sans-serif}
  .wrap{max-width:1180px;margin:0 auto;padding:24px 20px 140px}
  h1{font-size:26px;color:var(--gold);margin:0;letter-spacing:.4px}
  .sub{color:var(--mut);font-size:13px;margin:6px 0 0}
  .topbar{display:flex;align-items:baseline;gap:14px;border-bottom:1px solid var(--line);padding-bottom:14px;flex-wrap:wrap}
  .auth{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin:18px 0;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .auth input{background:#0b0a08;border:1px solid var(--line);color:var(--txt);border-radius:7px;padding:8px 11px;font-family:inherit;font-size:14px}
  .btn{background:linear-gradient(180deg,#2a251d,#1c1812);color:var(--gold);border:1px solid #4a4233;border-radius:8px;padding:10px 18px;cursor:pointer;font-family:inherit;font-size:15px}
  .btn:hover{border-color:var(--gold2)}
  .btn.primary{background:linear-gradient(180deg,#5a4d2c,#3c3320);color:#fff8e6;border-color:#6e5d36;font-size:16px;padding:12px 26px}
  .sec{margin:22px 0 6px;color:var(--gold);font-size:18px;border-bottom:1px solid var(--line);padding-bottom:6px}
  .q{margin:16px 0}
  .qlabel{font-size:15px;color:var(--txt)}
  .qhelp{font-size:12.5px;color:var(--mut);margin:2px 0 9px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:11px 13px;cursor:pointer;transition:.12s}
  .card:hover{border-color:var(--accent)}
  .card.on{border-color:var(--gold2);background:#211c12;box-shadow:0 0 0 1px var(--gold2) inset}
  .card .t{color:var(--gold);font-size:14.5px}
  .card .d{color:var(--mut);font-size:12px;margin-top:3px;line-height:1.35}
  .num{display:flex;align-items:center;gap:8px}
  .num button{width:34px;height:34px;font-size:18px;border-radius:7px;background:#1c1812;color:var(--gold);border:1px solid var(--line);cursor:pointer}
  .num input{width:90px;text-align:center;background:#0b0a08;border:1px solid var(--line);color:var(--txt);border-radius:7px;padding:8px;font-size:15px;font-family:inherit}
  .num .unit{color:var(--mut);font-size:13px}
  textarea{width:100%;min-height:84px;background:#0b0a08;border:1px solid var(--line);color:var(--txt);border-radius:9px;padding:11px;font-family:inherit;font-size:14px;resize:vertical}
  .bar{position:fixed;left:0;right:0;bottom:0;background:rgba(14,13,11,.96);border-top:1px solid var(--line);padding:14px 20px;display:flex;gap:16px;align-items:center;justify-content:center;z-index:50}
  .bar .note{color:var(--mut);font-size:13px}
  .status{color:var(--gold2);font-size:14px;min-height:18px;margin:8px 0}
  .results{margin-top:18px}
  .grid{display:grid;gap:16px}
  .plans{grid-template-columns:repeat(auto-fit,minmax(330px,1fr))}
  .gallery{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
  .plan{background:#fff;border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .plan .h{background:#1b1810;color:var(--gold);font-size:13px;padding:6px 10px}
  .plan img,.shot img{width:100%;display:block}
  .shot{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
  .shot .c{padding:9px 12px;color:var(--gold);font-size:14px}
  .sumtab{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:13.5px}
  .sumtab td{border:1px solid var(--line);padding:6px 10px}
  .sumtab td:first-child{color:var(--mut);width:38%}
  .sumtab td:last-child{color:var(--gold)}
  .pill{display:inline-block;background:#241f15;border:1px solid var(--gold2);color:var(--gold);border-radius:20px;padding:3px 12px;font-size:13px;margin-right:8px}
  .hidden{display:none}
  .errbox{background:#2a1714;border:1px solid #6e3b34;color:#e7b9b1;border-radius:8px;padding:10px 13px;font-size:13px;margin:10px 0}
</style></head>
<body><div class="wrap">
  <div class="topbar">
    <h1>Zeni Design Studio</h1>
    <span class="sub">Chọn mô tả — không cần viết prompt · 6 KTS AI agents ra mặt bằng + phối cảnh luxury</span>
  </div>

  <div id="auth" class="auth">
    <span style="color:var(--mut);font-size:13px">Đăng nhập workspace:</span>
    <input id="a-email" placeholder="email" autocomplete="off"/>
    <input id="a-pass" type="password" placeholder="mật khẩu" autocomplete="off"/>
    <input id="a-ws" placeholder="workspace (ws)" autocomplete="off"/>
    <button class="btn" id="a-btn">Đăng nhập</button>
    <span id="a-msg" style="color:var(--mut);font-size:13px"></span>
  </div>

  <div id="form"></div>

  <div class="status" id="status"></div>
  <div id="results" class="results"></div>

  <div class="bar">
    <span class="note">AI dựng concept ~70-80% · KTS chứng chỉ vẫn cần tinh chỉnh &amp; ký</span>
    <button class="btn primary" id="go">✦ Tạo thiết kế</button>
    <span class="note" id="eta">~150s · gồm mặt bằng + 3 ảnh render</span>
  </div>
</div>

<script>
const API = location.origin + '/api/v1';
const P = new URLSearchParams(location.search);
let WS = P.get('ws') || localStorage.getItem('zeni_ws') || '';
let TOKEN = ((location.hash.match(/token=([^&]+)/)||[])[1]) || localStorage.getItem('zeni_token') || '';
let QS = [];          // questions catalog
const A = {};         // answers {qid: value}

const $ = (id) => document.getElementById(id);
const setStatus = (m) => { $('status').textContent = m || ''; };

function refreshAuthUI(){
  $('auth').classList.toggle('hidden', !!TOKEN);
  if (WS) $('a-ws').value = WS;
}
async function jf(url, opt){ const r = await fetch(url, opt); const t = await r.text(); let d; try{d=JSON.parse(t);}catch{d={_raw:t};} return {ok:r.ok,status:r.status,d}; }

async function login(){
  const email=$('a-email').value.trim(), pass=$('a-pass').value, ws=$('a-ws').value.trim();
  if(!email||!pass||!ws){ $('a-msg').textContent='Nhập đủ email, mật khẩu, ws'; return; }
  $('a-msg').textContent='Đang đăng nhập…';
  const r = await jf(API+'/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password:pass})});
  if(!r.ok){ $('a-msg').textContent='Lỗi '+r.status; return; }
  TOKEN=r.d.access_token; WS=ws;
  localStorage.setItem('zeni_token',TOKEN); localStorage.setItem('zeni_ws',WS);
  $('a-msg').textContent='✓ Đã đăng nhập'; refreshAuthUI();
}

function card(qid, opt, multi){
  const el=document.createElement('div'); el.className='card'; el.dataset.v=opt.value;
  el.innerHTML='<div class="t">'+opt.label+'</div>'+(opt.desc?('<div class="d">'+opt.desc+'</div>'):'');
  el.onclick=()=>{
    if(multi){
      const arr=A[qid]||[]; const i=arr.indexOf(opt.value);
      if(i>=0){arr.splice(i,1);el.classList.remove('on');} else {arr.push(opt.value);el.classList.add('on');}
      A[qid]=arr;
    } else {
      A[qid]=opt.value;
      el.parentElement.querySelectorAll('.card').forEach(c=>c.classList.remove('on'));
      el.classList.add('on');
    }
  };
  return el;
}

function renderForm(){
  const root=$('form'); root.innerHTML=''; let curSec=null, secEl=null;
  QS.forEach(q=>{
    if(q.section && q.section!==curSec){ curSec=q.section; const h=document.createElement('div'); h.className='sec'; h.textContent=q.section; root.appendChild(h); }
    const qd=document.createElement('div'); qd.className='q';
    qd.innerHTML='<div class="qlabel">'+q.label+'</div>'+(q.help?('<div class="qhelp">'+q.help+'</div>'):'');
    if(q.type==='single'||q.type==='multi'){
      const wrap=document.createElement('div'); wrap.className='cards';
      (q.options||[]).forEach(o=>wrap.appendChild(card(q.id,o,q.type==='multi')));
      qd.appendChild(wrap);
      if(q.type==='single'){ A[q.id]=q.default; }
      else { A[q.id]=Array.isArray(q.default)?q.default.slice():[]; }
      // reflect defaults
      setTimeout(()=>wrap.querySelectorAll('.card').forEach(c=>{
        const v=c.dataset.v; const on=(q.type==='multi')?(A[q.id]||[]).includes(v):A[q.id]===v; if(on)c.classList.add('on');
      }),0);
    } else if(q.type==='number'){
      A[q.id]=q.default;
      const n=document.createElement('div'); n.className='num';
      const dec=document.createElement('button'); dec.textContent='−';
      const inp=document.createElement('input'); inp.type='number'; inp.value=q.default; inp.min=q.min; inp.max=q.max;
      const inc=document.createElement('button'); inc.textContent='+';
      const clamp=(v)=>Math.max(q.min,Math.min(q.max,v|0));
      dec.onclick=()=>{inp.value=clamp((+inp.value)-1);A[q.id]=+inp.value;};
      inc.onclick=()=>{inp.value=clamp((+inp.value)+1);A[q.id]=+inp.value;};
      inp.oninput=()=>{A[q.id]=clamp(+inp.value);};
      n.appendChild(dec);n.appendChild(inp);n.appendChild(inc);
      if(q.unit){const u=document.createElement('span');u.className='unit';u.textContent=q.unit;n.appendChild(u);}
      qd.appendChild(n);
    } else if(q.type==='text'){
      A[q.id]='';
      const ta=document.createElement('textarea'); ta.placeholder=q.placeholder||''; ta.oninput=()=>{A[q.id]=ta.value;};
      qd.appendChild(ta);
    }
    root.appendChild(qd);
  });
}

async function loadForm(){
  const r=await jf(API+'/design/brief-form');
  if(!r.ok){ setStatus('Không tải được form ('+r.status+')'); return; }
  QS=r.d; renderForm();
}

function labelOf(qid,val){ const q=QS.find(x=>x.id===qid); if(!q||!q.options)return val; const o=q.options.find(o=>o.value===val); return o?o.label:val; }
function clientSummary(){
  const pick=(id)=>labelOf(id,A[id]);
  const sp=(A.special_spaces||[]).map(v=>labelOf('special_spaces',v)).join(', ')||'không';
  return [
    ['Loại công trình',pick('building_type')],
    ['Khu đất / hướng',(A.lot_width_m+'×'+A.lot_length_m+'m · hướng '+pick('lot_orientation'))],
    ['Quy mô',(A.num_floors+' tầng · '+A.num_bedrooms+' phòng ngủ · '+A.num_residents+' người')],
    ['Gian thờ',pick('altar')],['Gara',pick('garage')],
    ['Phong cách',pick('style')],
    ['Hoàn thiện / ngân sách',pick('material_level')+' · '+pick('budget_band')],
    ['Không gian đặc biệt',sp],
    ['Cá nhân hoá',(A.personalization||'—')],
  ];
}

function renderResults(d){
  const R=$('results'); R.innerHTML='';
  // summary of choices (client-side, echo "đúng cái khách chọn")
  const sum=clientSummary();
  let html='<div class="sec">Tóm tắt lựa chọn của bạn</div><table class="sumtab">';
  sum.forEach(([k,v])=>{html+='<tr><td>'+k+'</td><td>'+v+'</td></tr>';});
  html+='</table>';
  const m=d.metrics||{};
  const fsm=(d.fengshui&&d.fengshui.enabled&&d.fengshui.cung_menh)?d.fengshui.cung_menh:null;
  html+='<div style="margin:10px 0"><span class="pill">Verdict: '+(d.verdict||'?')+'</span>'+
        (fsm?('<span class="pill">Cung '+fsm.cung+' · '+fsm.menh_group+'</span>'):'')+
        '<span class="pill">Render '+((d.renders||{}).count||0)+'/3</span>'+
        '<span class="pill">$'+((m.total_cost_usd||0).toFixed(4))+'</span>'+
        '<span class="pill">'+Math.round((m.duration_ms||0)/1000)+'s</span></div>';
  R.innerHTML=html;
  // floor plans
  const geo=d.geometry;
  if(geo&&(geo.drawings||[]).length){
    const fp=geo.footprint||{}, ss=geo.structural_seed||{}, grid=ss.column_grid||{};
    const meta=document.createElement('div'); meta.className='qhelp';
    meta.innerHTML='Footprint <b style="color:var(--gold)">'+fp.w_m+'×'+fp.d_m+'m</b> · GFA <b>'+geo.total_gfa_m2+'m²</b> · Cao <b>'+geo.building_height_m+'m</b> · Lưới cột <b>'+(grid.count||'?')+'</b>';
    const sh=document.createElement('div'); sh.className='sec'; sh.textContent='Mặt bằng bố trí nội thất từng tầng';
    R.appendChild(sh); R.appendChild(meta);
    const g=document.createElement('div'); g.className='grid plans';
    geo.drawings.forEach((dr,i)=>{const c=document.createElement('div');c.className='plan';c.innerHTML='<div class="h">Tầng '+((dr.floor)||(i+1))+'</div><img src="'+dr.svg_data_uri+'"/>';g.appendChild(c);});
    R.appendChild(g);
  }
  // mặt đứng + mặt cắt (L1.b/c)
  if(geo){
    const els=(geo.elevations||[]).concat(geo.sections||[]);
    if(els.length){
      const sh=document.createElement('div'); sh.className='sec'; sh.textContent='Mặt đứng & Mặt cắt';
      R.appendChild(sh);
      const g2=document.createElement('div'); g2.className='grid plans';
      els.forEach(e=>{const c=document.createElement('div');c.className='plan';
        c.innerHTML='<div class="h">'+(e.label||e.view)+'</div><img src="'+e.svg_data_uri+'"/>';g2.appendChild(c);});
      R.appendChild(g2);
    }
  }
  // phong thủy Bát Trạch + Lỗ Ban (deterministic)
  const fs=d.fengshui;
  if(fs){
    const sh=document.createElement('div'); sh.className='sec'; sh.textContent='Phong thủy — Bát Trạch + Lỗ Ban';
    R.appendChild(sh);
    const box=document.createElement('div'); box.className='qhelp'; let h='';
    if(fs.enabled&&fs.cung_menh){
      const cm=fs.cung_menh, fv=fs.facing_verdict||{};
      h+='<div>Gia chủ <b>'+(cm.gender||'')+' '+(cm.birth_year||'')+'</b> → cung <b style="color:var(--gold)">'+cm.cung+'</b> ('+cm.ngu_hanh+' · '+cm.menh_group+')</div>';
      h+='<div>Hướng nhà <b>'+fs.facing+'</b>: <b style="color:'+(fv.good?'#5fb98f':'#d98a8a')+'">'+(fv.du_nien||'?')+' ('+(fv.nature||'')+')</b></div>';
      h+='<div>Hướng tốt cho gia chủ: <b>'+(cm.huong_tot||[]).join(', ')+'</b> · đẹp nhất <b>'+cm.huong_dep_nhat+'</b> (Sinh Khí)</div>';
      if(fs.room_summary) h+='<div>Đối chiếu phòng (sơ bộ theo mặt bằng): <b>'+fs.room_summary.pass+'/'+fs.room_summary.total+'</b> hợp hướng mệnh</div>';
    } else {
      h+='<div>Chưa nhập năm sinh gia chủ → chưa luận cung mệnh. Hướng nhà: <b>'+(fs.facing||'?')+'</b></div>';
    }
    const lb=fs.lo_ban_doors||[];
    if(lb.length){ h+='<div style="margin-top:6px">Lỗ Ban (thông thủy 52.2cm): '+
      lb.map(x=>x.label+' '+x.mm+'mm→<b style="color:'+(x.good?'#5fb98f':'#d98a8a')+'">'+x.cung+(x.good?'':(' · nên '+(x.suggest_mm||'?')+'mm'))+'</b>').join(' · ')+'</div>'; }
    if(fs.disclaimer) h+='<div style="margin-top:6px;opacity:.6;font-size:11px">'+fs.disclaimer+'</div>';
    box.innerHTML=h; R.appendChild(box);
    const wn=fs.warnings||[];
    if(wn.length){ const wb=document.createElement('div'); wb.className='errbox';
      wb.innerHTML='<b>Lưu ý phong thủy:</b><br>'+wn.slice(0,6).map(w=>'• '+w).join('<br>'); R.appendChild(wb); }
  }
  // check functions + aesthetic critic
  const ck=d.checks, ae=d.aesthetic;
  if((ck&&ck.checks)||ae){
    const sh=document.createElement('div'); sh.className='sec'; sh.textContent='Kiểm tra kỹ thuật & thẩm mỹ';
    R.appendChild(sh);
    const box=document.createElement('div'); box.className='qhelp'; let h='';
    if(ck&&ck.checks){
      h+='<div><b>Check tự động ('+(ck.summary||'')+'):</b> '+ck.checks.map(c=>{
        const p=c.passed, col=p===true?'#5fb98f':(p===false?'#d98a8a':'#999');
        const lbl=(c.check||'').replace('_CHECK','').replace(/_/g,' ').toLowerCase();
        return '<span style="color:'+col+'">'+lbl+': '+(p===true?'đạt':(p===false?'lỗi':'n/a'))+'</span>';
      }).join(' · ')+'</div>';
      ck.checks.forEach(c=>{const probs=c.isolated_rooms||c.tight_rooms||c.missing||[];
        if(c.passed===false&&probs.length) h+='<div style="color:#d98a8a;font-size:11px">• '+(c.check||'')+': '+probs.slice(0,4).join(', ')+'</div>';});
    }
    if(ae&&ae.weighted_total!=null){
      const v=ae.weighted_total, col=v>=8?'#5fb98f':(v>=6.5?'var(--gold)':'#d98a8a');
      h+='<div style="margin-top:6px"><b>Thẩm mỹ (rubric 8 tiêu chí):</b> <b style="color:'+col+'">'+v+'/10</b> · '+(ae.verdict||'')+(ae.tieu_chi_yeu_nhat?(' · yếu nhất: '+ae.tieu_chi_yeu_nhat):'')+'</div>';
      if((ae.cai_thien||[]).length) h+='<div style="font-size:11px;opacity:.8">Gợi ý cải thiện: '+ae.cai_thien.slice(0,3).join('; ')+'</div>';
    }
    box.innerHTML=h; R.appendChild(box);
  }
  // renders
  const rv=(d.renders||{}).views||[];
  if(rv.length){
    const sh=document.createElement('div'); sh.className='sec'; sh.textContent='Phối cảnh luxury (Imagen 3)';
    R.appendChild(sh);
    const g=document.createElement('div'); g.className='grid gallery';
    rv.forEach(v=>{const c=document.createElement('div');c.className='shot';
      c.innerHTML=v.data_uri?('<img src="'+v.data_uri+'"/><div class="c">'+(v.label||v.view)+'</div>'):('<div class="c" style="color:#a06a6a">'+(v.label||v.view)+': '+(v.error||'no image')+'</div>');
      g.appendChild(c);});
    R.appendChild(g);
  }
  if((d.errors||[]).length){const e=document.createElement('div');e.className='errbox';e.textContent='Ghi chú QA: '+d.errors.join(' · ');R.appendChild(e);}
  R.scrollIntoView({behavior:'smooth',block:'start'});
}

async function submit(){
  if(!TOKEN){ setStatus('Cần đăng nhập trước.'); $('auth').classList.remove('hidden'); return; }
  if(!WS){ setStatus('Thiếu workspace (ws).'); return; }
  $('go').disabled=true; $('results').innerHTML='';
  setStatus('Đang chạy 6 KTS agents + render Imagen 3… (~150s, vui lòng đợi)');
  const t0=performance.now();
  const r=await jf(API+'/design/orchestrate?ws='+encodeURIComponent(WS),{
    method:'POST',headers:{'Content-Type':'application/json','Authorization':'Bearer '+TOKEN},
    body:JSON.stringify({form:A})});
  $('go').disabled=false;
  if(!r.ok){ setStatus('Lỗi '+r.status+': '+JSON.stringify(r.d).slice(0,300)); return; }
  setStatus('Hoàn tất trong '+Math.round((performance.now()-t0)/1000)+'s · verdict '+r.d.verdict);
  renderResults(r.d);
}

$('a-btn').onclick=login;
$('go').onclick=submit;
refreshAuthUI();
loadForm();
</script>
</body></html>
"""
