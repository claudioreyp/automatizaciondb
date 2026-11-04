export const qrPreviewPage = String.raw`<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Impulsa · Conectar WhatsApp</title>
<style>body{font:18px system-ui;background:#f4f5f7;color:#182529;margin:0;padding:40px}main{max-width:620px;margin:auto;background:white;padding:32px;border-radius:18px}h1{font-size:28px}#qr{display:block;width:300px;max-width:100%;margin:24px auto}#qr[hidden]{display:none}.notice{background:#fff4ce;padding:16px;border-radius:10px}#status{color:#526168}</style>
<main><h1>Conectar WhatsApp de Impulsa</h1><p class="notice" role="status"><strong id="notice-title">Comprobando conexión.</strong> <span id="notice-body">Esperando el estado del agente.</span></p><p>En WhatsApp, abre Dispositivos vinculados → Vincular un dispositivo.</p><p id="status">Esperando el código de WhatsApp…</p><img id="qr" alt="QR de vinculación de WhatsApp" hidden><p id="error" role="status"></p></main>
<script>
const image=document.getElementById('qr'),status=document.getElementById('status'),error=document.getElementById('error');
const noticeTitle=document.getElementById('notice-title'),noticeBody=document.getElementById('notice-body');
let displayedQr=null,pending=false;
function clearQr(){image.hidden=true;image.removeAttribute('src');displayedQr=null;}
image.onload=()=>{if(displayedQr){image.hidden=false;status.textContent='QR listo para escanear desde el celular.';}};
image.onerror=()=>{clearQr();status.textContent='No se pudo mostrar el código QR.';error.textContent='Esperando un nuevo código de WhatsApp…';};
async function refresh(){if(pending)return;pending=true;try{
  const r=await fetch('/api/bots/impulsa/qr-state',{cache:'no-store'});if(!r.ok)throw Error();
  const s=await r.json();
  const active=s.consumerMessaging==='active',attention=s.consumerMessaging==='attention_required';
  noticeTitle.textContent=active?'Agente activo.':attention?'Requiere revisión.':'Atención pausada.';
  noticeBody.textContent=active?'WhatsApp está conectado y el agente atiende los mensajes nuevos.':
    attention?'La conexión del agente necesita revisión antes de continuar.':
    s.status==='connected'?'WhatsApp está vinculado. Esperando la conexión del agente.':'Vincula WhatsApp para continuar.';
  if(s.qr&&/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(s.qr)){
    if(displayedQr!==s.qr){clearQr();displayedQr=s.qr;image.src=s.qr;status.textContent='Cargando el código QR…';}
  }else{
    clearQr();
    status.textContent=s.status==='connected'?(active?'WhatsApp vinculado. Atención activa.':'WhatsApp vinculado. Atención pausada.'):
      ['error','disconnected'].includes(s.status)?'WhatsApp no pudo generar el QR.':'Esperando el código de WhatsApp…';
  }
  error.textContent='';
}catch{
  clearQr();status.textContent='El servidor local no está disponible.';
  noticeTitle.textContent='Sin conexión.';noticeBody.textContent='No se puede confirmar que el agente esté atendiendo.';
  error.textContent='No se pudo actualizar el QR. Comprueba que el servidor siga abierto.';
}finally{pending=false;}}
refresh();setInterval(refresh,2500);
</script></html>`;
