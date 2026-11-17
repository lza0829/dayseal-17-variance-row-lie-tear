<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api'

const rows = ref([])
const batches = ref([])
const policy = ref(null)
const err = ref('')

const LABEL = { balanced: '零差', loss: '认损', gain: '认溢', pending: '未处置' }
const TONE = { balanced: 'ok', loss: 'warn', gain: 'warn', pending: 'bad' }

onMounted(async () => {
  try {
    rows.value = await api.variances()
    batches.value = await api.batches()
    policy.value = await api.policy()
  } catch (e) { err.value = e.message }
})

function batchNo(id) {
  return batches.value.find((b) => b.id === id)?.batch_no || `#${id}`
}
</script>

<template>
  <h2>长短款</h2>

  <div v-if="policy" class="panel policy">
    <div class="policy-head">
      <span class="tag warn">{{ policy.variance_policy_label }}</span>
      <small>{{ policy.variance_policy }}</small>
    </div>
    <p class="policy-desc">{{ policy.variance_policy_description }}</p>
  </div>

  <p v-if="err" class="msg err">{{ err }}</p>

  <div class="panel">
    <table>
      <thead><tr><th>营业日</th><th>批次</th><th>差额</th><th>处置</th><th>处置记录</th><th>时间</th></tr></thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.business_day }}</td>
          <td>{{ batchNo(r.batch_id) }}</td>
          <td>{{ r.variance.toFixed(2) }}</td>
          <td>
            <span class="tag" :class="TONE[r.disposition] || 'warn'">
              {{ r.disposition_label || LABEL[r.disposition] || r.disposition }}
            </span>
          </td>
          <td class="memo">{{ r.note || '—' }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="!rows.length" class="msg">尚无长短款行。</p>
  </div>
</template>
