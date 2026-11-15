<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJSON, postJSON, putJSON } from '../api'

const packs = ref([])
const packId = ref(null)
const report = ref(null)
const audits = ref([])
const policy = ref('reject_only')
const err = ref('')
const notice = ref('')
const busy = ref(false)

const POLICY_LABEL = {
  reject_only: '仅拒绝（保持指纹失配态，须重新打包）',
  rebuild_and_recheck: '按现行条目重建指纹并强制复检',
}
const EVENT_LABEL = {
  verify: '核验一致',
  mismatch: '指纹失配',
  rebuild: '重建指纹',
  rebuild_denied: '拒绝重建',
  repack_required: '须重新打包',
}
const STATUS_LABEL = {
  built: '已生成',
  checked_ok: '校验通过',
  checked_fail: '校验未过',
  released: '已放行',
  rejected: '已驳回',
  fingerprint_mismatch: '指纹失配',
  fingerprint_rebuilt: '指纹已重建待复检',
}

const current = computed(() => packs.value.find((p) => p.id === packId.value) || null)

async function refresh() {
  packs.value = await getJSON('/api/packs')
  if (!packId.value && packs.value.length) packId.value = packs.value[0].id
  if (packId.value) {
    report.value = await getJSON(`/api/fingerprint/${packId.value}`)
    audits.value = report.value.audits || []
  }
  const s = await getJSON('/api/settings')
  policy.value = s.fingerprint_mismatch_policy || 'reject_only'
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})

async function changePolicy() {
  err.value = ''
  notice.value = ''
  busy.value = true
  try {
    const s = await putJSON('/api/settings', {
      key: 'fingerprint_mismatch_policy',
      value: policy.value,
    })
    policy.value = s.fingerprint_mismatch_policy
    notice.value = '处置策略已更新'
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function act(kind) {
  err.value = ''
  notice.value = ''
  busy.value = true
  try {
    const r = await postJSON(`/api/fingerprint/${packId.value}/${kind}`)
    if (kind === 'rebuild') {
      notice.value = r.disposition === 'rebuilt'
        ? `已重建指纹：${r.old_fingerprint?.slice(0, 12)}… → ${r.new_fingerprint?.slice(0, 12)}…，须重新跑校验清单`
        : '已按策略处置'
    } else {
      notice.value = r.matched ? '指纹一致' : '指纹失配，已按策略处置并留痕'
    }
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
    await refresh()
  } finally {
    busy.value = false
  }
}

function short(fp) {
  return fp ? `${fp.slice(0, 12)}…` : '—'
}

function eventClass(ev) {
  if (ev === 'verify') return 'ok'
  if (ev === 'rebuild') return 'warn'
  return 'danger'
}
</script>
<template>
  <div class="page">
    <h1>指纹核验</h1>
    <p class="muted">
      放行前按现行包内条目重算指纹并与落库指纹比对；指纹只写一次，条目增删改任一发生即失配。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>
    <p v-if="notice" class="tag ok">{{ notice }}</p>

    <div class="panel">
      <h3>失配处置策略（互斥）</h3>
      <div class="row">
        <div class="field" style="flex:1;min-width:20rem">
          <label>当前策略</label>
          <select v-model="policy">
            <option v-for="(label, value) in POLICY_LABEL" :key="value" :value="value">
              {{ label }}
            </option>
          </select>
        </div>
        <button :disabled="busy" @click="changePolicy">保存策略</button>
      </div>
      <p class="muted" style="margin:0">
        仅拒绝：包保持指纹失配态，必须显式重新打包生成新指纹与新清单后才可再申请。
        重建复检：按现行条目重建指纹并作废旧清单，重建后须强制复检，不得沿用旧通过态放行。
      </p>
    </div>

    <div class="panel">
      <h3>选择证据包核验</h3>
      <div class="row">
        <div class="field" style="flex:1;min-width:18rem">
          <label>证据包</label>
          <select v-model="packId" @change="refresh">
            <option v-for="p in packs" :key="p.id" :value="p.id">
              {{ p.pack_no }}（{{ STATUS_LABEL[p.status] || p.status }}）
            </option>
          </select>
        </div>
        <button class="ghost" :disabled="busy || !packId" @click="act('verify')">重新核验</button>
        <button :disabled="busy || !packId" @click="act('rebuild')">按现行条目重建指纹</button>
      </div>
      <div v-if="report">
        <p>
          <span class="tag" :class="report.matched && !report.locked ? 'ok' : 'danger'">
            {{ report.matched ? (report.locked ? '指纹一致但包已锁定失配态' : '指纹一致') : '指纹失配' }}
          </span>
          · 可放行
          <strong>{{ report.release_allowed ? '是' : '否' }}</strong>
          · 包状态
          <span class="tag">{{ STATUS_LABEL[report.pack_status] || report.pack_status }}</span>
        </p>
        <table>
          <tbody>
            <tr><td>落库指纹</td><td class="mono">{{ report.stored_fingerprint }}</td></tr>
            <tr><td>重算指纹</td><td class="mono">{{ report.computed_fingerprint }}</td></tr>
            <tr>
              <td>包内条目</td>
              <td>现行 {{ report.actual_item_count }} 条 · 落库 {{ report.declared_item_count }} 条</td>
            </tr>
            <tr v-if="report.reasons.length">
              <td>不符原因</td>
              <td class="tag danger">{{ report.reasons.join('；') }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel">
      <h3>指纹留痕</h3>
      <table>
        <thead>
          <tr>
            <th>事件</th>
            <th>策略</th>
            <th>落库指纹</th>
            <th>重算指纹</th>
            <th>说明</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in audits" :key="a.id">
            <td><span class="tag" :class="eventClass(a.event)">{{ EVENT_LABEL[a.event] || a.event }}</span></td>
            <td>{{ POLICY_LABEL[a.policy]?.split('（')[0] || a.policy }}</td>
            <td class="mono">{{ short(a.stored_fingerprint) }}</td>
            <td class="mono">{{ short(a.computed_fingerprint) }}</td>
            <td>{{ a.detail }}</td>
            <td class="mono">{{ a.created_at }}</td>
          </tr>
          <tr v-if="!audits.length"><td colspan="6" class="muted">暂无核验记录</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
