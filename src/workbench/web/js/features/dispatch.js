// 并行会话组：拆分、重试和改用单会话
import {apiq} from '../core/api.js';

export default {
  data(){ return {dispatchGroups:[], groupAction:{}, groupErrors:{}}; },
  computed:{
    parallelById(){ return new Map(this.dispatchGroups.map(g=>[g.id,g])); },
  },
  methods:{
    parallelProgress(t){
      if (['failed','cancelled','queued'].includes(t.status) || !Number.isFinite(t.progress)) return null;
      return Math.round(Math.max(0,Math.min(100,t.progress)));
    },
    parallelTaskState(t){
      const progress=this.parallelProgress(t);
      return progress===null ? this.statusLabel[t.status] : progress+'%';
    },
    parallelKey(id){ return 'parallel-'+id; },
    rememberDispatchGroup(group){
      const index=this.dispatchGroups.findIndex(g=>g.id===group.id);
      if (index===-1) this.dispatchGroups.unshift(group);
      else this.dispatchGroups.splice(index,1,group);
      // 正在返回的旧轮询不能覆盖刚拿到的操作结果。
      this.tasksRefreshApplied=this.tasksRefreshCount+1;
    },
    showDispatchGroup(group){
      this.rememberDispatchGroup(group);
      this.toggleGroup('active',false);
      this.toggleGroup(this.parallelKey(group.id),false);
      this.$nextTick(()=>document.getElementById('parallel-thread-'+group.id)?.scrollIntoView({block:'nearest'}));
      if (group.state==='planning') apiq(`/api/dispatch/${group.id}`).then(()=>this.refreshTasks()).catch(()=>{});
    },
    restoreDispatchPrompt(group){
      const existing=this.draft.prompt.trim();
      if (existing && this.draft.prompt!==group.prompt && !confirm('用该组的原提示词替换输入框中的文字？')) return;
      this.pick(group.project);
      this.draft.prompt=group.prompt;
      this.$nextTick(()=>{
        this.grow(this.$refs.promptInput);
        this.$refs.promptInput?.focus();
      });
    },
    async changeDispatchGroup(group, action){
      if (this.groupAction[group.id]) return;
      this.groupAction[group.id]=action;
      delete this.groupErrors[group.id];
      try{
        const result=await apiq(`/api/dispatch/${group.id}/${action}`,{method:'POST'});
        if (action==='retry' && result.group) this.showDispatchGroup(result.group);
        await this.refreshTasks().catch(()=>{});
        if (action==='single' && result.task) await this.openTask(result.task.id);
      }catch(e){
        this.groupErrors[group.id]=e.message;
      }finally{
        delete this.groupAction[group.id];
      }
    },
  },
};
