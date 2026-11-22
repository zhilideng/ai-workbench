// 通用的格式化与输入框小工具
import {GLYPH} from '../core/icons.js';

export default {
  data(){ return {
      copied:'',
  }; },
  methods:{
    fmtTime(ts){ return new Date(ts*1000).toLocaleString('zh-CN',{hour12:false}); },
    fmtShort(ts){ const d=new Date(ts*1000); return `${d.getMonth()+1}/${d.getDate()} ${String(d.getHours()).padStart(2,'0')}:${String(d.getMinutes()).padStart(2,'0')}`; },
    fmtClock(ts){ return new Date(ts*1000).toLocaleTimeString('zh-CN',{hour12:false,hour:'2-digit',minute:'2-digit'}); },
    fmtNum(v){ const n=Number(v); return isNaN(n) ? v : n.toLocaleString('zh-CN',{maximumFractionDigits:0}); },
    dur(s){ s=Math.round(s); return s<60? s+'s' : Math.floor(s/60)+'m'+(s%60)+'s'; },
    ago(ts){ const s=Date.now()/1000-ts; return s<60?'刚刚':s<3600?Math.floor(s/60)+' 分钟前':s<86400?Math.floor(s/3600)+' 小时前':Math.floor(s/86400)+' 天前'; },
    agoShort(ts){ const s=Date.now()/1000-ts; return s<60?'now':s<3600?Math.floor(s/60)+'m':s<86400?Math.floor(s/3600)+'h':s<86400*30?Math.floor(s/86400)+'d':Math.floor(s/86400/30)+'mo'; },
    shortPath(p){ return (p||'').replace(/^\/Users\/[^/]+/,'~'); },
    stiClass(s){ return GLYPH[s]||'todo'; },
    async copy(t, key){
      try{ await navigator.clipboard.writeText(t); }
      catch(e){ const ta=document.createElement('textarea'); ta.value=t; ta.style.position='fixed'; ta.style.opacity='0';
        document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); }
      this.copied=key||'x'; clearTimeout(this._copyT); this._copyT=setTimeout(()=>this.copied='',1500);
    },
    grow(e){ const el=e && e.target ? e.target : e; if(!el||!el.style) return; el.style.height='auto'; el.style.height=Math.min(el.scrollHeight, innerHeight*0.4)+'px'; },
    onEnter(e, fn){
      // ↵ 发送，⇧↵ 换行；输入法组字中的回车不发送
      if (e.shiftKey || e.isComposing || e.keyCode===229) return;
      e.preventDefault(); fn();
    },
  },
};
