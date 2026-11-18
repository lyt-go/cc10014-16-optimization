<template>
  <section class="page" data-module="dim_model">
    <header class="page-head">
      <div>
        <h2>维度建模管理</h2>
        <p class="page-desc">维护维度模型的输入、执行约束与结果记录，为维度建模提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记维度模型</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出维度建模清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

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
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-if="String(row['维度名称'] ?? '').includes('商品')"
              class="link"
              type="button"
              :data-testid="`refresh-dim-${row.id}`"
              @click="refreshProductDim(row)"
            >
              刷新商品维度最新版表
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无维度建模数据，可先登记维度模型</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记维度模型</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条维度建模记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="detail-panel" data-testid="product-dim-panel">
      <header class="detail-head">
        <div>
          <h3>商品维度最新版表</h3>
          <p class="page-desc">
            按业务键合并 DWD 商品明细，每个业务键只保留一行当前值；合并结果、刷新水位与结构版本同批提交。
          </p>
        </div>
        <div class="page-actions">
          <select v-model="conflictPolicy" data-testid="conflict-policy">
            <option v-for="policy in conflictPolicies" :key="policy" :value="policy">{{ policy }}</option>
          </select>
          <button class="btn" data-testid="reload-product-dim" type="button" @click="loadProductDim()">刷新</button>
        </div>
      </header>
      <div class="stat-row">
        <article v-for="item in productDimStats" :key="item.label" class="stat-card">
          <span class="stat-label">{{ item.label }}</span>
          <strong class="stat-value">{{ item.value }}</strong>
        </article>
      </div>
      <table class="data-table small" data-testid="product-dim-current">
        <thead>
          <tr>
            <th>业务键</th>
            <th>商品名称</th>
            <th>价格</th>
            <th>来源业务时间</th>
            <th>来源批次</th>
            <th>结构版本</th>
            <th>水位序号</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in productRows" :key="String(item['业务键'])" :data-testid="`product-row-${item['业务键']}`">
            <td>{{ item['业务键'] }}</td>
            <td>{{ item['product_name'] ?? '—' }}</td>
            <td>{{ item['price'] ?? '—' }}</td>
            <td>{{ item['来源业务时间'] ?? '—' }}</td>
            <td>{{ item['来源批次'] ?? '—' }}</td>
            <td>{{ item['结构版本'] ?? '—' }}</td>
            <td>{{ item['水位序号'] ?? '—' }}</td>
          </tr>
          <tr v-if="!productRows.length">
            <td colspan="7" class="empty-state">最新版表暂无数据，可先刷新合并</td>
          </tr>
        </tbody>
      </table>
      <div v-if="lastMergeMessage" class="detail-note" data-testid="product-dim-message">{{ lastMergeMessage }}</div>

      <div v-if="dimensionResult" class="detail-grid" data-testid="dimension-result">
        <div class="detail-section">
          <h4>维度模型结果回查</h4>
          <dl><dt>模型编号</dt><dd>{{ dimensionResult['维度模型']?.['模型编号'] ?? '—' }}</dd></dl>
          <dl><dt>维度名称</dt><dd>{{ dimensionResult['维度模型']?.['维度名称'] ?? '—' }}</dd></dl>
          <dl><dt>自然键列</dt><dd>{{ dimensionResult['维度模型']?.['自然键列'] ?? '—' }}</dd></dl>
          <dl><dt>最新版表</dt><dd>{{ dimensionResult['最新版表'] ?? '—' }}</dd></dl>
          <dl><dt>刷新水位</dt><dd data-testid="result-watermark">{{ dimensionResult['刷新水位'] ?? '—' }}</dd></dl>
          <dl><dt>来源批次</dt><dd data-testid="result-batch">{{ dimensionResult['来源批次'] ?? '—' }}</dd></dl>
          <dl><dt>结构版本</dt><dd data-testid="result-schema-version">{{ dimensionResult['结构版本'] ?? '—' }}</dd></dl>
          <dl><dt>当前值行数</dt><dd>{{ dimensionResult['当前值行数'] ?? 0 }}</dd></dl>
          <dl><dt>待确认更正</dt><dd>{{ dimensionResult['待确认更正数'] ?? 0 }}</dd></dl>
        </div>
        <div class="detail-section">
          <h4>更正待确认清单</h4>
          <table class="data-table small" data-testid="pending-corrections">
            <thead><tr><th>业务键</th><th>更正商品名称</th><th>判定原因</th><th>来源批次</th></tr></thead>
            <tbody>
              <tr v-for="(item, idx) in pendingCorrections" :key="`${item['业务键']}-${idx}`">
                <td>{{ item['业务键'] }}</td>
                <td>{{ item['更正']?.['product_name'] ?? '—' }}</td>
                <td>{{ item['判定原因'] }}</td>
                <td>{{ item['来源批次'] }}</td>
              </tr>
              <tr v-if="!pendingCorrections.length">
                <td colspan="4" class="empty-state">暂无待确认更正</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type JsonRow = Record<string, any>

const ENDPOINT = '/api/dim_model'
const columns = ["模型编号", "维度名称", "自然键列", "代理键列", "缓慢变化策略", "生效日期", "失效日期", "刷新周期"]
const actions = ["提交校验", "标记生效", "停用模型"]
const statuses = ["草稿", "待校验", "已生效", "已停用"]
const metricNames = ["维度模型总数", "待校验模型", "已生效模型"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const conflictPolicy = ref('保留原值')
const conflictPolicies = ref<string[]>(['保留原值', '覆盖当前值'])
const productMeta = ref<JsonRow>({})
const productRows = ref<JsonRow[]>([])
const pendingCorrections = ref<JsonRow[]>([])
const dimensionResult = ref<JsonRow | null>(null)
const selectedEntryId = ref<number | null>(null)
const lastMergeMessage = ref('')
const productDimStats = computed(() => [
  { label: '最新版表', value: productMeta.value['最新版表'] ?? '—' },
  { label: '刷新水位', value: productMeta.value['刷新水位'] || '—' },
  { label: '来源批次', value: productMeta.value['来源批次'] || '—' },
  { label: '结构版本', value: productMeta.value['结构版本'] || '—' },
  { label: '当前值行数', value: productRows.value.length },
  { label: '待确认更正', value: pendingCorrections.value.length },
])
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[2]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'dim_model.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('维度建模动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '维度建模操作失败'
  }
}

async function loadProductDim(entryId?: number) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/product-dim/latest`)
    if (!response.ok) throw new Error('商品维度最新版表读取失败')
    const payload = await response.json()
    productMeta.value = payload.meta ?? {}
    productRows.value = payload.rows ?? []
    pendingCorrections.value = payload.pending ?? []
    if (Array.isArray(payload.conflict_policies) && payload.conflict_policies.length) {
      conflictPolicies.value = payload.conflict_policies
    }
    const targetId = entryId ?? selectedEntryId.value
    if (targetId) {
      const detail = await request(`${ENDPOINT}/${targetId}/dimension-result`)
      if (detail.ok) {
        dimensionResult.value = await detail.json()
      }
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '商品维度最新版表读取失败'
  }
}

async function refreshProductDim(row: Row) {
  errorMessage.value = ''
  selectedEntryId.value = Number(row.id)
  try {
    const response = await request(`${ENDPOINT}/${row.id}/product-dim/merge`, {
      method: 'POST',
      body: JSON.stringify({ conflict_policy: conflictPolicy.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '合并失败')
    lastMergeMessage.value = payload.message
    await loadProductDim(Number(row.id))
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '商品维度合并失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('维度模型列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '维度建模列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadProductDim()
})
</script>
