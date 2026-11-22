// 项目：当前项目、⌘K 切换、项目目录
import {api} from '../core/api.js';

export default {
  data(){ return {
      cfg:{}, projects:[], cur: localStorage.getItem('wb.cur')||'', pq:'',
  }; },
  computed:{
    visibleProjects(){ return this.projects.filter(p=>p.name.toLowerCase().includes(this.pq.toLowerCase())); },
    curProject(){ return this.projects.find(p=>p.name===this.cur); },
  },
  watch:{
    cur(v){ localStorage.setItem('wb.cur', v); this.colLimits={}; this.draft.use_worktree=false; this.draft.ssh_id=0; this.loadProjSsh(); },
  },
  methods:{
    switchProject(){ if (!this.pswOpen) this.togglePsw(); this.sideOpen=true; },
    pick(name){ this.cur=name; this.pswOpen=false; this.sideOpen=false; this.closeTask(); },
    pickFirstProject(){ const p=this.visibleProjects[0]; if (p) this.pick(p.name); },
    togglePsw(){ this.pswOpen=!this.pswOpen; if (this.pswOpen){ this.pq=''; this.$nextTick(()=>this.$refs.projSearch && this.$refs.projSearch.focus()); } },
  },
};
