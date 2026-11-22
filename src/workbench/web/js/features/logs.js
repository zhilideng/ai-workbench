// 运行日志：增量拉取事件，把 Claude / Codex 的事件流转成对话视图
import {api} from '../core/api.js';
import {toolSummary} from '../core/tools.js';
import {relPath} from '../core/paths.js';

export default {
  data(){ return {
      logs:{}, offsets:{}, workState:{},
      runLabel:{running:'执行中',success:'成功',error:'出错',cancelled:'已停止',interrupted:'服务重启中断'},
  }; },
  methods:{
    async pullLogs(scroll){
      if (!this.sel) return;
      const box=this.$refs.logBox, atBottom = box && box.scrollHeight-box.scrollTop-box.clientHeight<80;
      for (const r of this.sel.runs){
        if (this.offsets[r.id]==='done' && r.status!=='running') continue;
        const off = typeof this.offsets[r.id]==='number' ? this.offsets[r.id] : 0;
        const res = await api(`/api/runs/${r.id}/events?offset=${off}`);
        const arr = this.logs[r.id] || [];
        res.events.forEach(e=>this.ingest(arr,e));
        this.logs[r.id]=arr;
        this.offsets[r.id] = res.status==='running' ? res.offset : 'done';
      }
      if (scroll||atBottom) this.$nextTick(()=>{ if(this.$refs.logBox) this.$refs.logBox.scrollTop=1e9; });
    },
    ingest(arr,e){
      if (this.ingestCodex(arr,e)) return;
      if (e.type==='wb'){
        if (e.subtype==='start') arr.push({cls:'prompt', text:e.prompt, cwd:e.cwd, meta:this.runMeta(e), images:e.images||[]});
        else if (e.subtype==='stderr' && e.text.trim()) arr.push({cls:'err', text:e.text});
        else if (e.subtype==='error') arr.push({cls:'err', text:'工作台错误：'+e.text});
        return;
      }
      if (e.type==='system' && e.subtype==='init'){ arr.push({cls:'sys', text:`会话 ${e.session_id} · 模型 ${e.model} · 权限 ${e.permissionMode}`}); return; }
      if (e.type==='assistant'){
        for (const c of (e.message?.content||[])){
          if (c.type==='text' && c.text.trim()) arr.push({cls:'text', text:c.text});
          else if (c.type==='tool_use') arr.push({cls:'tool', id:c.id, name:c.name, summary:toolSummary(c.name,c.input), output:''});
        }
        return;
      }
      if (e.type==='user'){
        for (const c of (e.message?.content||[])){
          if (c.type!=='tool_result') continue;
          const tool = arr.find(x=>x.cls==='tool'&&x.id===c.tool_use_id);
          const out = Array.isArray(c.content) ? c.content.map(x=>x.text||'').join('\n') : String(c.content||'');
          if (tool){ tool.output=out; tool.isErr=!!c.is_error; }
        }
        return;
      }
      if (e.type==='result') arr.push({cls:e.is_error?'result err':'result', text:e.result||'', cost:e.total_cost_usd, ms:e.duration_ms||0, turns:e.num_turns});
    },
    ingestCodex(arr,e){
      const t=e.type||'';
      if (t==='thread.started'){ arr.push({cls:'sys', text:`Codex 会话 ${e.thread_id}`}); return true; }
      if (t==='turn.started') return true;
      if (t==='turn.completed'){ arr.push({cls:'result', text:'', tokens:e.usage||{}}); return true; }
      if (t==='turn.failed'){ arr.push({cls:'result err', text:(e.error||{}).message||'执行失败'}); return true; }
      if (t==='error'){ arr.push({cls:'err', text:e.message||JSON.stringify(e)}); return true; }
      if (!t.startsWith('item.')) return false;
      const it=e.item||{}, done=t==='item.completed';
      let row=arr.find(x=>x.cls==='tool'&&x.id===it.id);
      const tool=(name,summary)=>{ if(!row){ row={cls:'tool', id:it.id, name, summary, output:''}; arr.push(row); } else { row.summary=summary; } };
      switch(it.type){
        case 'agent_message': if (done && (it.text||'').trim()) arr.push({cls:'text', text:it.text}); break;
        case 'reasoning': break;
        case 'command_execution':
          tool('Bash', (it.command||'').replace(/^\/bin\/(zsh|bash) -lc /,''));
          if (done){ row.output=it.aggregated_output||''; row.isErr=it.exit_code!==0 && it.exit_code!=null; }
          break;
        case 'file_change': tool('Edit', (it.changes||[]).map(c=>`${c.kind||''} ${relPath(c.path)}`).join(', ')); break;
        case 'mcp_tool_call': tool(`${it.server}.${it.tool}`, JSON.stringify(it.arguments||{}).slice(0,160));
          if (done && it.result) row.output=JSON.stringify(it.result).slice(0,6000); break;
        case 'web_search': tool('WebSearch', it.query||''); break;
        case 'todo_list': tool('Todo', (it.items||[]).map(x=>(x.completed?'☑':'☐')+' '+x.text).join('  ')); break;
        case 'error': if (/unrecognized configuration setting/i.test(it.message||'')) break;
          if (done && !arr.some(x=>x.text==='⚠ '+(it.message||''))) arr.push({cls:'sys', text:'⚠ '+(it.message||'')}); break;
        default: if (done) arr.push({cls:'sys', text:`${it.type}: ${JSON.stringify(it).slice(0,200)}`});
      }
      return true;
    },
    // 一轮执行拆成：用户消息 / 过程（工具调用、中间输出，可折叠）/ 错误 / 最终回复
    runView(r){
      const a=this.logs[r.id]||[];
      const prompt=a.find(e=>e.cls==='prompt')||null;
      const notes=[], seq=[];
      for (const e of a){
        if (e.cls==='prompt') continue;
        if (e.cls==='err' || (e.cls.startsWith('result') && e.cls!=='result')){ if (e.text) notes.push(e); continue; }
        if (e.cls==='result'){
          const last=[...seq].reverse().find(x=>x.cls==='text');
          if (e.text && !(last && last.text.trim()===e.text.trim())) seq.push({cls:'text', text:e.text});
          continue;
        }
        if (e.cls==='text' || e.cls==='tool' || (e.cls==='sys' && e.text.startsWith('⚠'))) seq.push(e);
      }
      let work=seq, final=[];
      if (r.status!=='running'){
        let k=-1; seq.forEach((e,i)=>{ if (e.cls!=='text') k=i; });
        work=seq.slice(0,k+1); final=seq.slice(k+1);
      }
      return {prompt, work, final, notes, tools:work.filter(e=>e.cls==='tool').length};
    },
    workOpen(r){ const v=this.workState[r.id]; return v===undefined ? r.status==='running' : v; },
    toggleWork(r){ this.workState[r.id]=!this.workOpen(r); },
    runMeta(e){
      if (!('model' in e)) return '';
      const eng=e.engine||'claude';
      return [e.model||'默认模型', '思考 '+(e.effort ? this.effortLabel(e.effort, eng) : (eng==='codex' && this.codexDefault(e.model) ? this.effortLabel(this.codexDefault(e.model),'codex')+'（默认）' : '默认'))].join(' · ');
    },
    runTokens(runId){ const r=(this.logs[runId]||[]).find(x=>x.tokens); return r ? `${r.tokens.input_tokens} 入 / ${r.tokens.output_tokens} 出 tokens` : ''; },
    runStatusClass(s){ return {success:'st-done',error:'st-failed',cancelled:'st-cancelled',interrupted:'st-failed',running:'st-running'}[s]||'st-todo'; },
    toolIcon(n){ n=(n||'').toLowerCase();
      if (n==='bash') return 'terminal';
      if (n==='grep'||n==='glob') return 'search';
      if (/edit|write|read|notebook/.test(n)) return 'file';
      if (/web|fetch/.test(n)) return 'globe';
      if (/todo/.test(n)) return 'list';
      return 'tool'; },
    md(t){
      const html = DOMPurify.sanitize(marked.parse(t||''));
      if (!this.sel || !/(src|href)=/.test(html)) return html;
      // AI 回复里的本地路径（/Users/…、file://…、相对路径）改走 /api/tasks/:id/file，否则浏览器加载不到
      const tpl=document.createElement('template'); tpl.innerHTML=html;
      const local=u=>u && !/^(https?:|mailto:|data:|blob:|#|\/api\/|\/\/)/i.test(u);
      const fileUrl=u=>`/api/tasks/${this.sel.id}/file?path=${encodeURIComponent(decodeURI(u.replace(/[?#].*$/,'')))}`;
      tpl.content.querySelectorAll('img[src]').forEach(el=>{ const u=el.getAttribute('src'); if (local(u)) el.setAttribute('src', fileUrl(u)); el.setAttribute("title","点击预览"); });
      tpl.content.querySelectorAll('a[href]').forEach(el=>{ const u=el.getAttribute('href'); if (local(u)){ el.setAttribute('href', fileUrl(u)); el.setAttribute('data-wb-file', decodeURI(u.replace(/[?#].*$/,''))); el.setAttribute('target','_blank'); el.setAttribute('rel','noopener'); } });
      return tpl.innerHTML;
    },
  },
};
