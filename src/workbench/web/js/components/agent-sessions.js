// 任务列表行内的子会话折叠区。
export default {
  props:{sessions:Array, statusLabels:Object},
  emits:['open-child'],
  computed:{
    completed(){ return this.sessions.filter(s=>s.status==='review' || s.status==='done').length; },
  },
  template:`
    <details class="agent-sessions" @click.stop @keydown.enter.stop>
      <summary><ic class="chev" n="chevr" :s="12"></ic>子会话<span class="agent-count">已完成 {{ completed }}/{{ sessions.length }}</span></summary>
      <ul class="agent-session-list">
        <li v-for="s in sessions" :key="s.id">
          <button type="button" class="agent-session" :title="s.title" @click="$emit('open-child',s.id)">
            <sg :s="s.status"></sg><span class="agent-session-name ell">{{ s.title }}</span>
            <span class="agent-session-state">{{ statusLabels[s.status] }}</span>
          </button>
        </li>
      </ul>
    </details>`,
};
