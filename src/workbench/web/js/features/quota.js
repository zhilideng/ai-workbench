// 额度：Codex 与远程服务器上的 Claude
import {apiq} from '../core/api.js';

export default {
  data(){ return {
      quota:null, quotaLoading:false, activeCodex:new Set(), claudeRemote:[], activeRemoteClaude:new Set(),
      quotaOpen:(()=>{ try{ return localStorage.getItem('wb.quotaOpen')==='1'; }catch(e){ return false; } })(),
  }; },
  computed:{
    // 侧边栏底部的额度摘要：每个来源取最紧张的窗口
    quotaMini(){
      const out=[], c=this.quota&&this.quota.codex;
      if (c && c.available){ const ws=(c.groups||[]).flatMap(g=>g.windows); if (ws.length) out.push({k:'codex', name:'Codex', left:Math.min(...ws.map(w=>this.left(w))), title:'Codex：最紧张的额度窗口剩余'}); }
      for (const r of this.claudeRemote) if (!r.error && r.windows.length) out.push({k:'cr'+r.id, name:r.name, left:Math.min(...r.windows.map(w=>this.left(w))), title:'Claude · '+r.name+'：最紧张的额度窗口剩余'});
      return out;
    },
    // 任一额度窗口已被标记为用尽
    quotaReached(){ const c=this.quota&&this.quota.codex; return !!(c && c.groups && c.groups.some(g=>g.reached)); },
  },
  watch:{
    quotaOpen(v){ try{ localStorage.setItem('wb.quotaOpen', v?'1':'0'); }catch(e){} },
  },
  methods:{
    async loadQuota(refresh){
      if (this.quotaLoading) return;
      this.quotaLoading=true;
      try{
        const q=refresh?'?refresh=1':'';
        const [a,b]=await Promise.allSettled([apiq('/api/quota'+q), this.fetchClaudeRemote(refresh)]);
        if (a.status==='fulfilled') this.quota=a.value;
      }finally{ this.quotaLoading=false; }
    },
    async fetchClaudeRemote(refresh){
      try{ this.claudeRemote=await apiq('/api/quota/claude-remote'+(refresh?'?refresh=1':'')); }catch(e){}
    },
    // 剩余百分比 = 100 - 已用；剩余越少颜色越警示
    left(w){ return Math.max(0, Math.min(100, Math.round(100 - (w.used_percent||0)))); },
    // 后端把接口返回的 JSON 原样拼进错误里：只留其中的 message，完整内容放在 title
    prettyErr(e){ if (!e) return ''; const m=/"message"\s*:\s*"([^"]+)"/.exec(e); return m ? e.replace(/[:：]\s*\{[\s\S]*$/, '：'+m[1]) : e; },
    barLevel(left){ return left<=10 ? 'high' : left<=30 ? 'mid' : ''; },
    untilText(ts){ let s=Math.max(0, ts-Date.now()/1000); const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
      return d ? `${d} 天 ${h} 小时` : h ? `${h} 小时 ${m} 分` : `${m} 分钟`; },
  },
};
