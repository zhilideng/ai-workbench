// 底部输入框：新建任务（引擎、模型、权限、执行位置、worktree）
import {api} from '../core/api.js';

export default {
  data(){ return {
      draftImgs:[],
      draft:{prompt:'', engine:['claude','codex'].includes(localStorage.getItem('wb.engine')) ? localStorage.getItem('wb.engine') : 'claude', permission_mode:'acceptEdits', model:'', effort:localStorage.getItem('wb.effort')||'', use_worktree:false, ssh_id:0},
  }; },
  computed:{
    canSend(){ return !!(this.curProject && this.draft.prompt.trim() && this.draftImgs.every(a=>a.path)); },
  },
  watch:{
    'draft.effort'(v){ localStorage.setItem('wb.effort', v); },
    'draft.model'(){ this.fixEffort(); },
    'draft.ssh_id'(v){ if (v===-1){ this.draft.ssh_id=0; this.openSsh(); } },
    'draft.engine'(v){ localStorage.setItem('wb.engine', v); this.draft.model=''; this.fixEffort();
      if (!this.modesFor(v).some(m=>m.v===this.draft.permission_mode)) this.draft.permission_mode='acceptEdits'; },
  },
  methods:{
    async submit(runNow){
      if (!this.canSend) return;
      const d=this.draft;
      const t = await api('/api/tasks',{method:'POST', body:{project:this.cur, prompt:d.prompt, permission_mode:d.permission_mode,
                                                           model:d.model, effort:d.effort, use_worktree:d.use_worktree && !d.ssh_id, ssh_id:d.ssh_id>0?d.ssh_id:0, engine:d.engine, run_now:runNow,
                                                           images:this.draftImgs.map(a=>a.path)}});
      d.prompt=''; this.clearAtts(this.draftImgs); this.$nextTick(()=>this.grow(this.$refs.promptInput));
      await this.refreshTasks();
      if (runNow) this.openTask(t.id);
    },
  },
};
