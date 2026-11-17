<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api'

const preview = ref(null)
const declared = ref(0)
const mode = ref('')          // '' | 'loss' | 'gain' | 'correct'
const correctionAmount = ref(0)
const correctionNote = ref('')
const correctionTill = ref('TILL-A')
const note = ref('')
const tills = ref([])
const batches = ref([])
const policy = ref(null)
const integrity = ref(null)
const msg = ref('')
const err = ref('')

const variance = computed(() => {
  if (!preview.value) return 0
  return Math.round((Number(declared.value || 0) - preview.value.system_total) * 100) / 100
})

const isZero = computed(() => Math.abs(variance.value) < 1e-9)
const isLoss = computed(() => variance.value < -1e-9)
const requiredDisposition = computed(() => (isLoss.value ? 'loss' : 'gain'))
const correctionBalances = computed(
  () => Math.abs(Math.round((Number(correctionAmount.value || 0) - variance.value) * 100) / 100) < 1e-9,
)
const canSeal = computed(() => {
  if (!preview.value || preview.value.sealed) return false
  if (isZero.value) return true
  if (mode.value === 'correct') return correctionBalances.value
  return mode.value === requiredDisposition.value
})

const DISPOSITION_TEXT = { balanced: '零差', loss: '认损', gain: '认溢', pending: '未处置' }

async function load() {
  err.value = ''
  preview.value = await api.closePreview()
  declared.value = preview.value.system_total
  mode.value = ''
  correctionAmount.value = 0
  correctionNote.value = ''
  tills.value = await api.tills()
  batches.value = await api.batches()
  policy.value = await api.policy()
  integrity.value = await api.integrity()
}

function pickMode(next) {
  mode.value = mode.value === next ? '' : next
  if (mode.value === 'correct' && !correctionAmount.value) correctionAmount.value = variance.value
}

async function seal() {
  msg.value = ''; err.value = ''
  try {
    const body = {
      business_day: preview.value.business_day,
      declared_cash: declared.value,
      note: note.value || undefined,
    }
    if (!isZero.value && mode.value !== 'correct') body.disposition = mode.value
    if (mode.value === 'correct') {
      body.correction_lines = [{
        amount: Number(correctionAmount.value),
        till_code: correctionTill.value,
        note: correctionNote.value || '零差补录校正',
      }]
    }
    const r = await api.seal(body)
    msg.value = `已封账：系统 ${r.system_total.toFixed(2)} / 实盘 ${r.declared_cash.toFixed(2)} / 差额 ${r.variance.toFixed(2)} · ${DISPOSITION_TEXT[r.disposition] || r.disposition}`
    await load()
  } catch (e) { err.value = e.message }
}

onMounted(load)

async function repair() {
  msg.value = ''; err.value = ''
  try {
    const r = await api.repair()
    msg.value = r.repaired_count
      ? `已按批次快照修复 ${r.repaired_count} 处分叉，自检一致。`
      : '无需修复：封账批次 / 长短款行 / 日锁定一致。'
    await load()
  } catch (e) { err.value = e.message }
}
</script>

<template>
  <h2>日结封账</h2>

  <div v-if="policy" class="panel policy">
    <div class="policy-head">
      <span class="tag warn">{{ policy.variance_policy_label }}</span>
      <small>{{ policy.variance_policy }}</small>
    </div>
    <p class="policy-desc">{{ policy.variance_policy_description }}</p>
  </div>

  <div v-if="preview" class="panel">
    <p>营业日 {{ preview.business_day }} · 笔数 {{ preview.line_count }} · 系统合计 {{ preview.system_total.toFixed(2) }}
      <span class="tag" :class="preview.sealed ? 'ok' : 'warn'">{{ preview.sealed ? '已封' : '未封' }}</span>
      <span v-if="preview.sealed" class="tag" :class="preview.disposition === 'balanced' ? 'ok' : 'warn'">
        {{ preview.disposition_label || preview.disposition || '无处置记录' }}
      </span>
    </p>

    <div class="row">
      <label>实盘现金 <input type="number" step="0.01" v-model.number="declared" /></label>
      <div class="delta" :class="{ ok: isZero, warn: !isZero }">
        本次差额 <strong>{{ variance.toFixed(2) }}</strong>
      </div>
    </div>

    <div v-if="!isZero && !preview.sealed" class="panel choose">
      <p class="choose-title">差额 {{ variance.toFixed(2) }} 非零：封账前必须当场选定一种处置</p>
      <div class="choices">
        <button
          class="choice"
          :class="{ on: mode === requiredDisposition, off: mode && mode !== requiredDisposition }"
          @click="pickMode(requiredDisposition)"
        >
          <strong>{{ requiredDisposition === 'loss' ? '认损 loss' : '认溢 gain' }}</strong>
          <small>{{ isLoss ? '短款，按损失确认' : '长款，按溢余确认' }}</small>
        </button>
        <button
          class="choice"
          :class="{ on: mode === 'correct' }"
          @click="pickMode('correct')"
        >
          <strong>零差补录校正</strong>
          <small>补一笔真实流水，把差额补到 0</small>
        </button>
      </div>

      <div v-if="mode === requiredDisposition" class="row">
        <label>处置记录
          <input v-model="correctionNote" placeholder="写入长短款行" />
        </label>
      </div>

      <div v-else-if="mode === 'correct'" class="row">
        <label>补录金额
          <input type="number" step="0.01" v-model.number="correctionAmount" />
        </label>
        <label>收银机
          <select v-model="correctionTill">
            <option v-for="t in tills" :key="t.till_code" :value="t.till_code">{{ t.title }}</option>
          </select>
        </label>
        <label>补录说明
          <input v-model="correctionNote" placeholder="写入新旧流水" />
        </label>
        <div class="delta" :class="correctionBalances ? 'ok' : 'warn'">
          补录后差额 <strong>{{ (variance - Number(correctionAmount || 0)).toFixed(2) }}</strong>
        </div>
      </div>
    </div>

    <div class="row">
      <label>批次备注 <input v-model="note" placeholder="可选" /></label>
      <button class="btn" :disabled="!canSeal" @click="seal">封账</button>
      <button class="btn ghost" @click="load">刷新</button>
    </div>
    <p v-if="!preview.sealed && !isZero && !mode" class="msg err">
      差额非零：必须当场认损 / 认溢，或用零差补录校正把差额补到 0；未处置不会生成批次、不会锁日。
    </p>
    <p v-else-if="!preview.sealed && mode === 'correct' && !correctionBalances" class="msg err">
      补录金额须恰好等于差额，否则差额未归零，不能封账。
    </p>
  </div>

  <p v-if="msg" class="msg">{{ msg }}</p>
  <p v-if="err" class="msg err">{{ err }}</p>

  <div class="panel">
    <h2>封账批次</h2>
    <table>
      <thead><tr><th>批次</th><th>营业日</th><th>系统</th><th>实盘</th><th>差额</th><th>状态</th><th>处置</th></tr></thead>
      <tbody>
        <tr v-for="b in batches" :key="b.id">
          <td>{{ b.batch_no }}</td><td>{{ b.business_day }}</td>
          <td>{{ b.system_total.toFixed(2) }}</td><td>{{ b.declared_cash.toFixed(2) }}</td>
          <td>{{ b.variance.toFixed(2) }}</td><td>{{ b.status }}</td>
          <td>
            <span v-if="b.disposition" class="tag" :class="b.disposition === 'balanced' ? 'ok' : 'warn'">
              {{ b.disposition_label || b.disposition }}
            </span>
            <span v-else class="tag bad">缺处置记录</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>

  <div v-if="integrity" class="panel">
    <h2>同源自检</h2>
    <p class="msg" :class="{ err: !integrity.ok }">
      {{ integrity.ok ? '封账批次 / 长短款行 / 日锁定一致。' : `发现 ${integrity.violations.length} 处不一致` }}
      <span v-if="integrity.open_variance_count">（未决 {{ integrity.open_variance_count }}）</span>
    </p>
    <ul v-if="!integrity.ok" class="violations">
      <li v-for="(v, i) in integrity.violations" :key="i">{{ v.business_day }} · {{ v.code }} · {{ v.detail }}</li>
    </ul>
    <button v-if="!integrity.ok" class="btn" @click="repair">以批次快照为准修复分叉</button>
  </div>
</template>
