// 任务对话：继续对话、编辑已发送的消息
import {api} from '../core/api.js';

export default {
  data(){ return {
      followup:'', editing:null, replyImgs:[], showInfo:false,
  }; },
  methods:{
    async run(){
      if (this.isActive(this.sel) || this.dependencyLabel(this.sel)) return;
      if (this.replyImgs.some(a=>!a.path)) return alert('图片还在上传，请稍候');
      const imgs=this.replyImgs.map(a=>a.path);
      const msg=this.followup.trim() || (imgs.length ? '请查看附带的图片。' : '');
      await api(`/api/tasks/${this.sel.id}/run`,{method:'POST', body:{message:msg||null, images:imgs, ...this.runOpts()}});
      this.clearAtts(this.replyImgs);
      this.followup=''; this.$nextTick(()=>this.grow(this.$refs.replyInput));
      await this.openTask(this.sel.id); this.refreshTasks();
    },
    startEdit(run, text){
      if (run && this.isActive(this.sel)) return;
      this.editing={run, text, busy:false};
      this.$nextTick(()=>{ let el=this.$refs.editInput; el=Array.isArray(el)?el[0]:el; if(el){ this.grow(el); el.focus(); el.setSelectionRange(el.value.length, el.value.length); } });
    },
    async saveEdit(){
      const ed=this.editing; if (!ed || !ed.text.trim() || ed.busy) return;
      if (ed.run===0){ await this.patch({description:ed.text, title:ed.text.split('\n').find(l=>l.trim())?.trim().slice(0,60)||this.sel.title}); this.editing=null; return; }
      ed.busy=true;
      try{
        await api(`/api/tasks/${this.sel.id}/run`,{method:'POST', body:{message:ed.text, edit_run_id:ed.run, ...this.runOpts()}});
        this.editing=null;
        await this.openTask(this.sel.id); this.refreshTasks();
      }catch(e){ ed.busy=false; }
    },
    runOpts(){ const s=this.sel; return {engine:s.engine, model:s.model||'', effort:s.effort||'', permission_mode:s.permission_mode}; },
  },
};
