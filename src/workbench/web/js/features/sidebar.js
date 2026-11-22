// 侧边栏：按状态分组的任务列表

export default {
  data(){ return {
      sideOpen:false, pswOpen:false, sideLimit:{},
      sideClosed:(()=>{ try{ return JSON.parse(localStorage.getItem('wb.sideClosed')||'{}'); }catch(e){ return {}; } })(),
  }; },
  computed:{
    // 侧边栏：按状态分组的任务（跟随当前项目和搜索）
    sideGroups(){
      const ts=this.filteredTasks, by=s=>ts.filter(t=>s.includes(t.status)).sort((a,b)=>b.updated_at-a.updated_at);
      return [{key:'active',label:'进行中',items:by(['running','queued'])},{key:'review',label:'待验收',items:by(['review'])},
              {key:'todo',label:'待办',items:by(['todo'])},{key:'recent',label:'最近',items:by(['done','failed','cancelled'])}]
        .filter(g=>g.items.length).map(g=>({...g, limit:this.sideLimit[g.key]||(g.key==='recent'?20:50)}));
    },
  },
  methods:{
    toggleGroup(k){ this.sideClosed[k]=!this.sideClosed[k]; try{ localStorage.setItem('wb.sideClosed', JSON.stringify(this.sideClosed)); }catch(e){} },
  },
};
