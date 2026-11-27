<template>
  <section class="page" :data-module="meta.key" data-testid="workbench">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div v-if="meta.key === 'speech_segment'" class="workflow-panel" data-testid="boundary-workflow">
      <div class="workflow-head">
        <h3>包围边界重算 / 手工与自动校准</h3>
        <div class="workflow-actions">
          <button class="btn" data-testid="reproduce-boundary" type="button" @click="reproduceBoundary">
            载入 0–1000ms 例题
          </button>
          <button class="btn" data-testid="recalculate-boundary" type="button" @click="recalculateBoundary">
            重算 TRCB-BOUNDARY
          </button>
        </div>
      </div>
      <p class="workflow-tip">
        内嵌插话会完整保留，外层长段拆成插话前后两段；所有输出正时长、首尾相接，1000ms 后的口播不会被吃掉。
      </p>
      <div v-if="boundaryResult" class="boundary-outputs" data-testid="boundary-outputs">
        <span
          v-for="item in boundaryResult.outputs ?? []"
          :key="String(item.output_id)"
          class="boundary-chip"
          :data-testid="`boundary-output-${item.seg_id}-${item.part_index ?? 1}`"
        >
          {{ item.output_id }}：{{ item.start_ms }}–{{ item.end_ms }}ms（{{ item.duration_ms }}ms）
        </span>
      </div>
    </div>

    <div v-else-if="meta.key === 'subtitle_align'" class="workflow-panel" data-testid="align-workflow">
      <div class="workflow-head">
        <h3>实际生效边界与来源</h3>
      </div>
      <p class="workflow-tip">
        同一分段的手工结果优先于迟到或重复的自动校准；列表中的“实际使用边界”和“边界来源”就是装包文件使用的区间。
      </p>
    </div>

    <div v-else-if="meta.key === 'subtitle_pack'" class="workflow-panel" data-testid="pack-workflow">
      <div class="workflow-head">
        <h3>字幕装包 / 发行文件</h3>
      </div>
      <p class="workflow-tip">
        在字幕包行点击“重试生成”，系统会按字幕对齐列表中实际采用的边界与来源生成时码；随后可在线预览或下载文件核对。
      </p>
    </div>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
          <th v-if="extraColumn" data-testid="extra-column-head">{{ extraColumn }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
          <td v-if="meta.key === 'speech_segment'" class="manual-cell">
            <div class="manual-inputs">
              <label>
                <span>起始</span>
                <input
                  v-model="draftFor(row).start"
                  :data-testid="`manual-start-${row.id}`"
                  type="number"
                  min="0"
                />
              </label>
              <label>
                <span>结束</span>
                <input
                  v-model="draftFor(row).end"
                  :data-testid="`manual-end-${row.id}`"
                  type="number"
                  min="1"
                />
              </label>
              <label class="text-draft">
                <span>确认字句</span>
                <input
                  v-model="draftFor(row).text"
                  :data-testid="`manual-text-${row.id}`"
                  type="text"
                  placeholder="人工确认后进入交付"
                />
              </label>
            </div>
            <div class="manual-buttons">
              <button class="link" type="button" :data-testid="`save-manual-${row.id}`" @click="saveManual(row, false)">
                保存输入边界
              </button>
              <button class="link" type="button" :data-testid="`shift-20-${row.id}`" @click="shiftTwenty(row)">
                +20ms 并保存
              </button>
              <button class="link" type="button" :data-testid="`confirm-manual-${row.id}`" @click="saveManual(row, true)">
                保存并确认字句
              </button>
              <button class="link" type="button" :data-testid="`auto-duplicate-${row.id}`" @click="sendAutoDuplicate(row)">
                自动结果重复到达
              </button>
            </div>
          </td>
          <td v-else-if="meta.key === 'subtitle_align'" class="preview-cell">
            <button class="link" type="button" :data-testid="`align-detail-${row.id}`" @click="previewAlignment(row)">
              字幕详情预览
            </button>
          </td>
          <td v-else-if="meta.key === 'subtitle_pack'" class="preview-cell">
            <button class="link" type="button" :data-testid="`preview-pack-${row.id}`" @click="previewPack(row)">
              预览字幕
            </button>
            <a
              class="link download-link"
              :href="`/api/subtitle_pack/${row.id}/file`"
              :data-testid="`download-pack-${row.id}`"
            >
              下载文件
            </a>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2 + (extraColumn ? 1 : 0)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="(field, index) in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" :required="index < 3" />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="preview" class="modal-backdrop" data-testid="preview-modal" @click.self="preview = null">
      <section class="modal preview-modal" role="dialog" aria-modal="true" aria-label="字幕详情预览">
        <div class="preview-head">
          <h3 data-testid="preview-title">{{ preview.title }}</h3>
          <button class="btn ghost" type="button" data-testid="close-preview" @click="preview = null">关闭</button>
        </div>
        <div v-if="preview.timecodes?.length" class="timecode-list" data-testid="preview-timecodes">
          <span v-for="(code, index) in preview.timecodes" :key="index" class="timecode-chip">{{ code }}</span>
        </div>
        <div v-if="preview.cues?.length" class="cue-list" data-testid="playable-cue-list">
          <article
            v-for="cue in preview.cues"
            :key="`${cue.index}-${cue.source_segment_id}`"
            class="cue-card"
            :data-testid="`playable-cue-${cue.source_segment_id}`"
          >
            <header>
              <strong>#{{ cue.index }}</strong>
              <code>{{ cue.timecode }}</code>
            </header>
            <p>{{ cue.text }}</p>
            <small>原分段：{{ cue.source_segment_id }} · 边界来源：{{ cue.boundary_source }}</small>
          </article>
        </div>
        <pre class="subtitle-preview" data-testid="subtitle-file-content">{{ preview.content }}</pre>
        <div v-if="preview.downloadUrl" class="page-actions">
          <a class="btn primary" :href="preview.downloadUrl" data-testid="preview-download">下载字幕文件</a>
        </div>
      </section>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="infoMessage" class="info-text" data-testid="workflow-message">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  postJson,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type Draft = { start: string; end: string; text: string }
type BoundaryOutput = {
  output_id?: string
  seg_id?: string
  part_index?: number
  start_ms: number
  end_ms: number
  duration_ms: number
}
type BoundaryResult = { outputs?: BoundaryOutput[] }
type PreviewCue = {
  index: number
  text: string
  timecode: string
  source_segment_id: string
  boundary_source: string
}
type PreviewState = {
  title: string
  content: string
  timecodes?: string[]
  cues?: PreviewCue[]
  downloadUrl?: string
}

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const manualDrafts = reactive<Record<string, Draft>>({})
const boundaryResult = ref<BoundaryResult | null>(null)
const preview = ref<PreviewState | null>(null)

const extraColumn = computed(() => {
  if (meta.key === 'speech_segment') return '手工调整 / 竞争结果'
  if (meta.key === 'subtitle_align') return '详情预览'
  if (meta.key === 'subtitle_pack') return '字幕文件'
  return ''
})

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function draftFor(row: EntryRow): Draft {
  const key = String(row.id)
  if (!manualDrafts[key]) {
    manualDrafts[key] = {
      start: String(row['起始毫秒'] ?? row['实际起始毫秒'] ?? '0'),
      end: String(row['结束毫秒'] ?? row['实际结束毫秒'] ?? '0'),
      text: String(row['段内文本'] ?? row['人工确认字句'] ?? ''),
    }
  }
  return manualDrafts[key]
}

function flash(message: string) {
  infoMessage.value = message
  errorMessage.value = ''
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function saveManual(row: EntryRow, confirmed: boolean) {
  errorMessage.value = ''
  infoMessage.value = ''
  const draft = draftFor(row)
  try {
    const result = await applyAction(meta.key, Number(row.id), '保存手工调整', {
      values: {
        起始毫秒: Number(draft.start),
        结束毫秒: Number(draft.end),
        段内文本: draft.text,
      },
      confirmed,
    })
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '手工保存失败'
  }
}

async function shiftTwenty(row: EntryRow) {
  const draft = draftFor(row)
  draft.start = String(Number(draft.start || 0) + 20)
  draft.end = String(Number(draft.end || 0) + 20)
  await saveManual(row, false)
}

async function sendAutoDuplicate(row: EntryRow) {
  errorMessage.value = ''
  try {
    const segmentRef = String(row['分段编号'] ?? row.id)
    const payload = {
      start_ms: Number(row['原始起始毫秒'] ?? row['起始毫秒'] ?? 0),
      end_ms: Number(row['原始结束毫秒'] ?? row['结束毫秒'] ?? 0),
      idempotency_key: `ui-repeat-${segmentRef}-${row['原始起始毫秒'] ?? row['起始毫秒']}-${row['原始结束毫秒'] ?? row['结束毫秒']}`,
    }
    const first = await postJson<{ message?: string; ok?: boolean }>(
      `/api/workflow/speech-segments/${encodeURIComponent(segmentRef)}/auto`,
      payload,
    )
    const second = await postJson<{ duplicate?: boolean; message?: string }>(
      `/api/workflow/speech-segments/${encodeURIComponent(segmentRef)}/auto`,
      payload,
    )
    flash(`${first.message ?? '自动结果已接收'}；${second.message ?? '重复结果已幂等处理'}`)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '自动校准结果送达失败'
  }
}

async function reproduceBoundary() {
  errorMessage.value = ''
  try {
    boundaryResult.value = await postJson<BoundaryResult>('/api/workflow/reproduce-boundary-case')
    filters.value = { 关联任务: 'TRCB-BOUNDARY' }
    await reload()
    flash('已载入 0–1000ms 长段、100–200ms 插话和 1000ms 后口播')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '例题载入失败'
  }
}

async function recalculateBoundary() {
  errorMessage.value = ''
  try {
    boundaryResult.value = await postJson<BoundaryResult>('/api/speech_segment/recalculate', {
      task_id: 'TRCB-BOUNDARY',
    })
    flash('边界重算完成')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '边界重算失败'
  }
}

async function previewPack(row: EntryRow) {
  errorMessage.value = ''
  try {
    const data = await (await fetch(`/api/subtitle_pack/${row.id}/preview`)).json() as {
      content?: string
      timecodes?: string[]
      cues?: PreviewCue[]
      message?: string
    }
    if (!data.content) throw new Error(data.message || '字幕预览生成失败')
    preview.value = {
      title: `字幕包预览 · ${String(row['字幕包编号'] ?? row.id)}`,
      content: data.content,
      timecodes: data.timecodes,
      cues: data.cues,
      downloadUrl: `/api/subtitle_pack/${row.id}/file?download=1`,
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字幕预览失败'
  }
}

async function previewAlignment(row: EntryRow) {
  errorMessage.value = ''
  const segmentRef = String(row['关联分段'] ?? '')
  try {
    const response = await fetch(`/api/workflow/provenance/${encodeURIComponent(segmentRef)}`)
    if (!response.ok) throw new Error('原分段溯源信息读取失败')
    const data = await response.json() as {
      segment?: EntryRow
      outputs?: BoundaryOutput[]
      packs?: { 字幕包编号?: string }[]
    }
    const lines = [
      `对齐编号：${String(row['对齐编号'] ?? row.id)}`,
      `关联原分段：${segmentRef}`,
      `实际使用边界：${String(row['实际使用边界'] ?? `${row['对齐起始毫秒']}-${row['对齐结束毫秒']}`)}ms`,
      `边界来源：${String(row['边界来源'] ?? row['对齐方式'] ?? '—')}`,
      '',
      '重算输出：',
      ...(data.outputs ?? []).map((item) =>
        `- ${item.output_id ?? item.seg_id}: ${item.start_ms}–${item.end_ms}ms（${item.duration_ms}ms）`),
      '',
      `可查询到的交付包：${(data.packs ?? []).map((pack) => pack.字幕包编号).filter(Boolean).join('、') || '尚未生成'}`,
    ]
    preview.value = {
      title: `字幕详情 · ${String(row['对齐编号'] ?? row.id)}`,
      content: lines.join('\n'),
      timecodes: [String(row['实际使用边界'] ?? '')].filter(Boolean),
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '字幕详情预览失败'
  }
}

async function runAction(action: string, row: EntryRow) {
  if (action === '保存手工调整') {
    await saveManual(row, false)
    return
  }
  if (action === '后移20毫秒') {
    await shiftTwenty(row)
    return
  }

  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    flash(result.message)
    await reload()
    if (meta.key === 'subtitle_pack' && action === '重试生成') {
      await previewPack(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
    for (const row of payload.items) {
      draftFor(row)
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.workflow-panel {
  margin: 12px 0 16px;
  padding: 14px 16px;
  border: 1px solid #d8e2ef;
  border-radius: 12px;
  background: linear-gradient(180deg, #f7faff 0%, #eef4ff 100%);
}

.workflow-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.workflow-head h3 {
  margin: 0;
  font-size: 16px;
}

.workflow-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.workflow-tip {
  margin: 8px 0 10px;
  color: #5b6b80;
  line-height: 1.6;
}

.boundary-outputs,
.timecode-list {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.boundary-chip,
.timecode-chip {
  display: inline-flex;
  align-items: center;
  padding: 4px 8px;
  border-radius: 999px;
  background: #fff;
  border: 1px solid #c8d7ee;
  font-size: 12px;
  color: #244064;
}

.manual-cell {
  min-width: 280px;
}

.manual-inputs {
  display: grid;
  grid-template-columns: 80px 80px minmax(160px, 1fr);
  gap: 6px;
  margin-bottom: 6px;
}

.manual-inputs label {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 12px;
  color: #607086;
}

.manual-inputs input {
  width: 100%;
  min-width: 0;
  padding: 4px 6px;
}

.manual-buttons {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.inline-hint {
  color: #7a8798;
  font-size: 12px;
}

.preview-cell {
  white-space: nowrap;
}

.download-link {
  margin-left: 8px;
}

.preview-modal {
  width: min(920px, calc(100vw - 48px));
  max-height: 82vh;
  overflow: auto;
}

.preview-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.preview-head h3 {
  margin: 0;
}

.subtitle-preview {
  margin-top: 12px;
  padding: 12px;
  border-radius: 8px;
  background: #101826;
  color: #d7e7ff;
  white-space: pre-wrap;
  line-height: 1.6;
  overflow: auto;
}

.cue-list {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.cue-card {
  padding: 10px;
  border: 1px solid #d8e2ef;
  border-radius: 8px;
  background: #f8fbff;
}

.cue-card header {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
}

.cue-card p {
  margin: 8px 0;
  line-height: 1.5;
}

.cue-card small {
  color: #607086;
}

.info-text {
  color: #176b3a;
  margin-right: 12px;
}
</style>
