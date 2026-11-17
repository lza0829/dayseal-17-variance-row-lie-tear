<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const dash = ref(null)
const err = ref('')

onMounted(async () => {
  try { dash.value = await api.dashboard() }
  catch (e) { err.value = e.message }
})
</script>

<template>
  <section class="hero">
    <h1>日封台</h1>
    <p>把当日收银流水钉进营业日封账：差额当场认损/认溢并写入处置记录后才锁日，封账后流水不可再改。</p>
  </section>
  <p v-if="err" class="msg err">{{ err }}</p>
  <div class="stats" v-if="dash">
    <div class="stat"><div class="label">营业日</div><div class="value">{{ dash.business_day }}</div></div>
    <div class="stat"><div class="label">今日流水笔数</div><div class="value">{{ dash.line_count_today }}</div></div>
    <div class="stat"><div class="label">系统合计</div><div class="value">{{ dash.system_total_today.toFixed(2) }}</div></div>
    <div class="stat"><div class="label">封账状态</div><div class="value">{{ dash.sealed_today ? '已封 · 已锁日' : '未封' }}</div></div>
    <div class="stat"><div class="label">差额策略</div><div class="value small">{{ dash.variance_policy.variance_policy_label }}</div></div>
    <div class="stat"><div class="label">今日处置</div><div class="value small">{{ dash.disposition_label || '—' }}</div></div>
  </div>
  <p v-if="dash && !dash.sealed_today" class="msg">
    {{ dash.variance_policy.variance_policy_description }}
  </p>
  <p v-if="dash && dash.integrity_violation_count" class="msg err">
    同源自检发现 {{ dash.integrity_violation_count }} 处不一致，请到封账页查看。
  </p>
</template>
