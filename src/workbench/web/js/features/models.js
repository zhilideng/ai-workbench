// 引擎、模型、思考等级、权限模式

export default {
  data(){ return {
      permModes:[
        {v:'acceptEdits',s:'自动改文件',l:'acceptEdits（自动改文件）',d:'自动允许编辑文件；需要审批的命令（如部分 Bash）会被拒绝，Claude 会绕过或在总结中说明。'},
        {v:'bypassPermissions',s:'全部放行',l:'bypassPermissions（全部放行）',d:'⚠️ 跳过所有权限检查，Claude 可执行任意命令。建议配合 worktree 使用。'},
        {v:'plan',s:'只出方案',l:'plan（只出方案）',d:'只读分析并给出实施方案，不修改任何文件。适合先评估再执行。'},
        {v:'default',s:'只读',l:'default（需审批的全部拒绝）',d:'非交互模式下需要审批的操作都会被拒绝，基本只能读代码。'},
      ],
  }; },
  methods:{
    engLabel(e){ return e==='codex' ? 'Codex' : 'Claude'; },
    modesFor(engine){
      if (engine!=='codex') return this.permModes;
      return [
        {v:'acceptEdits',s:'工作区可写',l:'workspace-write（可改项目内文件）',d:'codex 沙箱 workspace-write：可读写项目目录并执行命令，网络受限。'},
        {v:'bypassPermissions',s:'全部放行',l:'无沙箱（全部放行）',d:'⚠️ --dangerously-bypass-approvals-and-sandbox：不受沙箱限制。建议配合 worktree。'},
        {v:'plan',s:'只读',l:'read-only（只读分析）',d:'codex 沙箱 read-only：只能读，不能改文件。'},
      ];
    },
    modeOf(engine, v){ return this.modesFor(engine).find(m=>m.v===v) || this.modesFor(engine)[0]; },
    effortsFor(engine, model){
      if (engine!=='codex') return this.cfg.claude_efforts || ['low','medium','high','xhigh','max'];
      const all=this.cfg.codex_efforts||{}, five=['low','medium','high','xhigh','ultra'];
      if (model && all[model]) return all[model].levels;
      return five;
    },
    codexDefault(model){
      const m=(this.cfg.codex_efforts||{})[model];
      return this.cfg.codex_default_effort || (m && m.default) || '';
    },
    defaultEffortText(engine, model){
      const d = engine==='codex' ? this.codexDefault(model) : '';
      return d ? '默认（'+this.effortLabel(d,'codex')+'）' : '默认思考';
    },
    effortLabel(e, engine){
      // Codex 用官方客户端的中文命名；Claude 沿用 CLI 的 low…max
      const codex={none:'无',minimal:'极低',low:'轻度',medium:'中',high:'高',xhigh:'极高',max:'Max',ultra:'Ultra',persistent:'持续'};
      const claude={low:'低',medium:'中',high:'高',xhigh:'超高',max:'最高'};
      return (engine==='codex' ? codex : claude)[e] || e;
    },
    effortTitle(engine, model){
      const d = engine==='codex' && this.codexDefault(model);
      return '思考等级，只作用于本任务' + (engine==='codex' ? '（-c model_reasoning_effort）' : '（--effort）') + (d ? '；不选则用 Codex 配置的 '+this.effortLabel(d,'codex') : '；不选则沿用 CLI 配置');
    },
    fixEffort(){ const d=this.draft; if (d.effort && !this.effortsFor(d.engine, d.model).includes(d.effort)) d.effort=''; },
    modelsFor(engine){ return engine==='codex' ? (this.cfg.codex_models||[]) : ['opus','sonnet','haiku']; },
  },
};
