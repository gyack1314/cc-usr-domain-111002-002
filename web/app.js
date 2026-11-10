const $=(s,root=document)=>root.querySelector(s);
const $$=(s,root=document)=>[...root.querySelectorAll(s)];
const data=await fetch('/data.json').then(r=>r.json());
const runtime=()=>fetch('/api/runtime').then(r=>r.json());
const action=(name,fields={})=>fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,...fields})}).then(r=>r.json());
const tick=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const html=(value)=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const show=(s)=>{$('#status').textContent=s;};

$('#contracts').innerHTML=data.contracts.map(c=>`<button data-contract="${c.id}">${c.id} ${c.name}</button>`).join('');
for(const button of $$('[data-contract]'))button.onclick=async()=>{const id=button.dataset.contract;await action('open-contract',{id});const old=$('#detail');const frame=document.createElement('iframe');frame.id='detail';frame.title='合同详情';frame.src='/detail.html?id='+id;old.replaceWith(frame);show('已打开合同 '+id);};
show('请选择合同');
