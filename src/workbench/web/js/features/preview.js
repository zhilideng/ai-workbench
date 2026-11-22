// 图片预览：缩放、拖动

export default {
  data(){ return {
      pv:null,
  }; },
  computed:{
    pvPct(){ return this.pv ? Math.round(this.pv.s*this.pv.fit*100) : 100; },
  },
  methods:{
    openPreview(e){
      const img=e.target.closest && e.target.closest('.ai-text img, .bubble-imgs img');
      if (!img) return;
      e.preventDefault(); window.getSelection && window.getSelection().removeAllRanges();
      const src=img.getAttribute('src')||'';
      let name=img.getAttribute('alt')||'image';
      try{ const p=new URL(src, location.href).searchParams.get('path'); name=(p||new URL(src, location.href).pathname).split('/').pop()||name; }catch(_){}
      this.pv={src, alt:img.getAttribute('alt')||'', name, s:1, x:0, y:0, fit:1, drag:false};
    },
    pvLoaded(){ const im=this.$refs.pvImg; if (im && im.naturalWidth) this.pv.fit = (im.clientWidth - 24) / im.naturalWidth; },
    pvZoom(f, cx, cy){
      const pv=this.pv, st=this.$refs.pvStage; if (!pv || !st) return;
      const r=st.getBoundingClientRect();
      const px=(cx==null ? r.left+r.width/2 : cx)-(r.left+r.width/2), py=(cy==null ? r.top+r.height/2 : cy)-(r.top+r.height/2);
      const ns=Math.min(20/pv.fit, Math.max(0.1, pv.s*f)), k=ns/pv.s;
      pv.x = px-(px-pv.x)*k; pv.y = py-(py-pv.y)*k; pv.s = ns;
      if (ns<=1){ pv.x=pv.y=0; }   // 缩到适应大小以下时居中
    },
    pvReset(){ Object.assign(this.pv, {s:1, x:0, y:0}); },
    pvActual(){ Object.assign(this.pv, {s:1/(this.pv.fit||1), x:0, y:0}); },
    pvWheel(e){ this.pvZoom(Math.exp(-e.deltaY*(e.ctrlKey?0.01:0.0015)), e.clientX, e.clientY); },
    pvDown(e){
      if (e.button!==0) return;
      const pv=this.pv, sx=e.clientX, sy=e.clientY, ox=pv.x, oy=pv.y, onImg=e.target===this.$refs.pvImg;
      let moved=false;
      const mv=ev=>{ const dx=ev.clientX-sx, dy=ev.clientY-sy;
        if (!moved && Math.hypot(dx,dy)>4){ moved=true; pv.drag=true; }
        if (moved){ pv.x=ox+dx; pv.y=oy+dy; } };
      const up=ev=>{ window.removeEventListener('mousemove',mv); window.removeEventListener('mouseup',up); pv.drag=false;
        if (moved) return;
        if (onImg) this.pvZoom(ev.altKey ? 1/1.6 : 1.6, ev.clientX, ev.clientY);
        else this.pv=null; };
      window.addEventListener('mousemove',mv); window.addEventListener('mouseup',up);
      e.preventDefault();
    },
  },
};
