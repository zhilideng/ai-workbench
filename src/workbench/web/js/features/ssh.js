// 项目的 SSH 连接：管理弹窗、连接 / 断开、执行位置
import {apiq} from '../core/api.js';

export default {
  data(){ return {
      projSsh:[], ssh:{open:false, project:'', list:[], sel:null, mode:'view', form:null, tests:{}, hosts:[], busy:false, err:'', bad:{}, act:{}, confirmDel:null, confirmDisc:null},
  }; },
  computed:{
    curSsh(){ return this.ssh.list.find(c=>c.id===this.ssh.sel) || null; },
    selSsh(){ return this.draft.ssh_id>0 ? this.projSsh.find(c=>c.id===this.draft.ssh_id) || null : null; },
    // 表单里实时预览生成的 ssh 命令（与后端 ssh_command 的拼法一致，仅作展示）
    sshPreview(){
      const f=this.ssh.form; if (!f || !f.host) return 'ssh …';
      const a=f.auth==='password' ? ['env SSH_ASKPASS=… WB_SSH_ID=…', 'ssh', '-o PubkeyAuthentication=no'] : ['ssh'];
      if (f.remote_dir) a.push('-t');
      if (f.port) a.push('-p', f.port);
      if (f.identity_file && f.auth!=='password') a.push('-i', f.identity_file);
      if (f.proxy_jump) a.push('-J', f.proxy_jump);
      if (f.socks_proxy) a.push(`-o 'ProxyCommand=nc -X 5 -x ${f.socks_proxy.replace(/^socks5h?:\/\//i,'')} %h %p'`);
      a.push(f.user ? f.user+'@'+f.host : f.host);
      if (f.remote_dir) a.push(`'cd ${f.remote_dir} && exec $SHELL -l'`);
      return a.join(' ');
    },
  },
  methods:{
    async openSsh(selId){
      const p=this.cur; if (!p) return;
      Object.assign(this.ssh, {open:true, project:p, list:[], sel:null, mode:'view', form:null, tests:{}, err:'', bad:{}, act:{}, confirmDel:null, confirmDisc:null, busy:false});
      try{
        const [list, hosts] = await Promise.all([apiq(this.sshUrl()), apiq('/api/ssh/config-hosts')]);
        this.ssh.list=list; this.ssh.hosts=hosts;
        if (list.length) this.selectSsh(typeof selId==='number' && list.some(c=>c.id===selId) ? selId : list[0].id);
        else this.newSsh();
      }catch(e){ this.ssh.err=e.message; }
    },
    closeSsh(){ this.ssh.open=false; this.ssh.form=null; },
    sshUrl(id, project){ return `/api/projects/${encodeURIComponent(project||this.ssh.project)}/ssh`+(id ? '/'+id : ''); },
    async loadProjSsh(){
      const p=this.cur; if (!p){ this.projSsh=[]; return; }
      try{ const l=await apiq(`/api/projects/${encodeURIComponent(p)}/ssh`); if (this.cur===p) this.projSsh=l; }catch(e){}
    },
    sshLabel(c){ return c.name+' · '+(c.remote_dir||'~'); },
    setSshList(list){
      this.ssh.list=list;
      if (this.ssh.project===this.cur){ this.projSsh=list; if (this.draft.ssh_id>0 && !list.some(c=>c.id===this.draft.ssh_id)) this.draft.ssh_id=0; }
      const p=this.projects.find(x=>x.name===this.ssh.project); if (p) p.ssh_count=list.length;
    },
    resetSshUi(){ Object.assign(this.ssh, {err:'', bad:{}, confirmDel:null, confirmDisc:null}); },
    selectSsh(id){ this.resetSshUi(); Object.assign(this.ssh, {sel:id, mode:'view', form:null}); },
    newSsh(){
      this.resetSshUi();
      this.ssh.mode='new';
      this.ssh.form={id:null, name:'', host:'', user:'', port:'', identity_file:'', proxy_jump:'', socks_proxy:'', remote_dir:'', note:'', share_with_ai:true,
                     auth:'key', password:'', has_password:false, remote_claude:'', remote_codex:'', allow_root_bypass:false};
      this.$nextTick(()=>this.$refs.sshFirst && this.$refs.sshFirst.focus());
    },
    editSsh(c){
      this.resetSshUi();
      this.ssh.mode='edit';
      this.ssh.form={id:c.id, name:c.name, host:c.host, user:c.user, port:c.port||'', identity_file:c.identity_file,
                     proxy_jump:c.proxy_jump, socks_proxy:c.socks_proxy||'', remote_dir:c.remote_dir, note:c.note, share_with_ai:c.share_with_ai,
                     auth:c.auth||'key', password:'', has_password:!!c.has_password, remote_claude:c.remote_claude||'', remote_codex:c.remote_codex||'', allow_root_bypass:!!c.allow_root_bypass};
      this.$nextTick(()=>this.$refs.sshFirst && this.$refs.sshFirst.focus());
    },
    cancelSshForm(){
      if (!this.ssh.list.length) return this.closeSsh();
      this.selectSsh(this.ssh.form && this.ssh.form.id || this.ssh.sel || this.ssh.list[0].id);
    },
    async saveSsh(){
      const f=this.ssh.form;
      const bad={name:!f.name.trim(), host:!f.host.trim(), password:f.auth==='password' && !f.has_password && !f.password};
      this.ssh.bad=bad;
      const toTop=()=>this.$nextTick(()=>{ const el=document.querySelector('.sshf .sshm-scroll'); if (el) el.scrollTo({top:0, behavior:'smooth'}); });
      if (bad.name || bad.host || bad.password){ this.ssh.err = bad.name||bad.host ? '请填写名称和主机' : '请填写密码'; toTop(); return; }
      const {id, has_password, ...body}=f; body.port=Number(body.port)||0;
      if (body.auth!=='password' || !body.password) body.password=null;
      this.ssh.busy=true; this.ssh.err='';
      try{
        const list=await apiq(this.sshUrl(id), {method:id?'PUT':'POST', body});
        this.setSshList(list);
        const saved = id ? list.find(c=>c.id===id) : list.find(c=>c.name===body.name.trim());
        if (id) delete this.ssh.tests[id];
        this.selectSsh(saved ? saved.id : list[0].id);
      }catch(e){ this.ssh.err=e.message; toTop(); } finally{ this.ssh.busy=false; }
    },
    async delSsh(c){
      try{
        const list=await apiq(this.sshUrl(c.id), {method:'DELETE'});
        this.setSshList(list);
        if (list.length) this.selectSsh(list[0].id); else this.newSsh();
      }catch(e){ this.ssh.confirmDel=null; this.ssh.err=e.message; }
    },
    async testSsh(c){
      this.ssh.err=''; this.ssh.tests[c.id]={ok:null};
      try{ this.ssh.tests[c.id] = await apiq(this.sshUrl(c.id)+'/test', {method:'POST'}); }
      catch(e){ this.ssh.tests[c.id]={ok:false, message:e.message||'测试失败'}; }
    },
    async openSshTerm(c){ this.ssh.err=''; try{ await apiq(this.sshUrl(c.id)+'/open', {method:'POST'}); }catch(e){ this.ssh.err=e.message; } },
    async connectSsh(c, project){
      const k=c.id; this.ssh.act[k]='connect'; this.ssh.err='';
      try{ const list=await apiq(this.sshUrl(k, project)+'/connect', {method:'POST'}); this.applySshList(list, project); }
      catch(e){ if (this.ssh.open) this.ssh.err=e.message; else alert(e.message); }
      finally{ delete this.ssh.act[k]; }
    },
    askDisconnect(c){ if (c.running){ this.ssh.confirmDisc=c.id; return; } this.disconnectSsh(c); },
    async disconnectSsh(c, project){
      const k=c.id; this.ssh.act[k]='disconnect'; this.ssh.confirmDisc=null; this.ssh.err='';
      try{ const list=await apiq(this.sshUrl(k, project)+'/disconnect', {method:'POST'}); this.applySshList(list, project); this.refreshTasks(); }
      catch(e){ if (this.ssh.open) this.ssh.err=e.message; else alert(e.message); }
      finally{ delete this.ssh.act[k]; }
    },
    // 弹窗外（输入框、任务页）操作连接后同步各处状态
    applySshList(list, project){
      const p=project||this.ssh.project;
      if (this.ssh.open && p===this.ssh.project) this.setSshList(list);
      if (p===this.cur) this.projSsh=list;
      if (this.sel && this.sel.ssh_id){ const c=list.find(x=>x.id===this.sel.ssh_id); if (c) this.sel.ssh_connected=c.connected; }
    },
    // 任务页 / 输入框里的断开
    async disconnectFromTask(){
      const t=this.sel; if (!t || !t.ssh_id) return;
      if (this.isActive(t) && !confirm(`这个任务正在服务器上执行，断开会停止它。继续？`)) return;
      await this.disconnectSsh({id:t.ssh_id}, t.project);
      await this.openTask(t.id);
    },
    async connectFromTask(){
      const t=this.sel; if (!t || !t.ssh_id) return;
      await this.connectSsh({id:t.ssh_id}, t.project);
    },
  },
};
