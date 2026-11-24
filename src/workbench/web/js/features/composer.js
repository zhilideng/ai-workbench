// 底部输入框：新建任务（引擎、模型、权限、执行位置、worktree）
import {api, apiq} from '../core/api.js';

export default {
  data(){ return {
      draftImgs:[],
      draft:{prompt:'', engine:['claude','codex'].includes(localStorage.getItem('wb.engine')) ? localStorage.getItem('wb.engine') : 'claude', permission_mode:'acceptEdits', model:'', effort:localStorage.getItem('wb.effort')||'', use_worktree:false, ssh_id:0},
  }; },
  computed:{
    canSend(){ return !!(this.curProject && (this.draft.ssh_id ? this.selSsh : this.executionSource)
      && this.draft.prompt.trim() && this.draftImgs.every(a=>a.path)); },
    // 预热的设置：与发送时完全一致，后端才会用上预建的会话；远程 / worktree 任务不预热。
    // 两个引擎都预热：另一个引擎按「切换过去后」的设置（模型回到默认，思考 / 权限不支持就回到默认），切换后马上提问也不用等
    prewarmBody(){
      const d=this.draft;
      if (!this.cur || d.ssh_id || d.use_worktree || !this.executionSource) return '';
      const base={project:this.cur, source_path:this.executionSource.path||''};
      const bodies=[{...base, engine:d.engine, permission_mode:d.permission_mode, model:d.model||'', effort:d.effort||''}];
      for (const e of ['claude','codex']){
        if (e===d.engine || (this.cfg.engines && !this.cfg.engines[e])) continue;
        bodies.push({...base, engine:e, model:'',
                     permission_mode:this.modesFor(e).some(m=>m.v===d.permission_mode) ? d.permission_mode : 'acceptEdits',
                     effort:this.effortsFor(e,'').includes(d.effort) ? d.effort : ''});
      }
      return JSON.stringify(bodies);
    },
  },
  watch:{
    // 与 CLI 打开时就准备好会话一样：输入框的设置一确定就在后台建好会话，按下发送时不用再等
    prewarmBody(){ clearTimeout(this._prewarmT); this._prewarmT=setTimeout(this.prewarmDraft, 300); },
    'draft.prompt'(v, old){ if (v && !old) this.prewarmDraft(); },   // 预建的会话被用掉或空闲关闭后，再次输入时补上
    'draft.effort'(v){ localStorage.setItem('wb.effort', v); },
    'draft.model'(){ this.fixEffort(); },
    'draft.ssh_id'(v){ if (v===-1){ this.draft.ssh_id=this.activeSource?.ssh_id || 0; this.openSsh(); } },
    'draft.engine'(v){ localStorage.setItem('wb.engine', v); this.draft.model=''; this.fixEffort();
      if (!this.modesFor(v).some(m=>m.v===this.draft.permission_mode)) this.draft.permission_mode='acceptEdits'; },
  },
  methods:{
    // 后端已有同样设置的预建会话时什么也不做，可以放心多次调用
    prewarmDraft(){
      const v=this.prewarmBody;
      if (v) for (const body of JSON.parse(v)) apiq('/api/tasks/prewarm',{method:'POST', body}).catch(()=>{});
    },
    async submit(runNow){
      if (!this.canSend) return;
      const d=this.draft;
      const t = await api('/api/tasks',{method:'POST', body:{project:this.cur, prompt:d.prompt, permission_mode:d.permission_mode,
                                                           model:d.model, effort:d.effort, use_worktree:d.use_worktree && !d.ssh_id, ssh_id:d.ssh_id>0?d.ssh_id:0, engine:d.engine, run_now:runNow,
                                                           source_path:this.executionSource?.path || '', images:this.draftImgs.map(a=>a.path)}});
      d.prompt=''; this.clearAtts(this.draftImgs); this.$nextTick(()=>this.grow(this.$refs.promptInput));
      await this.refreshTasks();
      if (runNow) this.openTask(t.id);
    },
  },
};
