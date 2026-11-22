import {GLYPH} from '../core/icons.js';

// 状态信号灯：s 可以是任务状态（review、cancelled…）或直接是样式名（run、fail…）
export default {
  props:{s:String, idle:Boolean, label:String},
  computed:{ k(){ return GLYPH[this.s] || this.s; } },
  template:`<span class="sti" :class="[k,{idle}]" :role="label?'img':null" :aria-label="label||null" :aria-hidden="label?null:'true'" :title="label||null"><ic v-if="k==='done'" n="check" :s="9" :w="3.2"></ic><ic v-else-if="k==='fail'||k==='cancel'" n="x" :s="8" :w="3.2"></ic></span>`,
}
