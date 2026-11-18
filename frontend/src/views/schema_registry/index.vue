<template>
  <section class="page" data-module="schema_registry">
    <header class="page-head">
      <div>
        <h2>结构注册管理</h2>
        <p class="page-desc">维护结构定义的输入、执行约束与结果记录，为结构注册提供可验证的数据工程流程。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记结构定义</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出结构注册清单</button>
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
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无结构注册数据，可先登记结构定义</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记结构定义</h3>
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
      <span>共 {{ total }} 条结构注册记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section class="detail-panel" data-testid="product-dim-schema">
      <header class="detail-head">
        <div>
          <h3>商品维度最新版表 · 结构版本</h3>
          <p class="page-desc">结构注册记录与最新版表的刷新水位、来源批次互相定位。</p>
        </div>
        <button class="btn" data-testid="reload-product-schema" type="button" @click="loadProductSchema()">回查结构版本</button>
      </header>
      <div class="detail-grid">
        <div class="detail-section">
          <h4>表级定位</h4>
          <dl><dt>表名称</dt><dd data-testid="schema-table">{{ productSchema.table ?? '—' }}</dd></dl>
          <dl><dt>结构版本</dt><dd data-testid="schema-version">{{ productSchema.schema?.['定义版本'] ?? '—' }}</dd></dl>
          <dl><dt>刷新水位</dt><dd>{{ productSchema.meta?.['刷新水位'] || '—' }}</dd></dl>
          <dl><dt>来源批次</dt><dd>{{ productSchema.meta?.['来源批次'] || '—' }}</dd></dl>
          <dl><dt>字段名称</dt><dd>{{ productSchema.schema?.['字段名称'] ?? '—' }}</dd></dl>
        </div>
        <div class="detail-section">
          <h4>结构版本历史</h4>
          <table class="data-table small">
            <thead><tr><th>定义编号</th><th>定义版本</th><th>生效批次</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="item in productSchema.schemas ?? []" :key="String(item.id)">
                <td>{{ item['定义编号'] }}</td>
                <td>{{ item['定义版本'] }}</td>
                <td>{{ item['生效批次'] || '—' }}</td>
                <td>{{ item.status }}</td>
              </tr>
              <tr v-if="!(productSchema.schemas ?? []).length">
                <td colspan="4" class="empty-state">暂无结构定义</td>
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

const ENDPOINT = '/api/schema_registry'
const columns = ["定义编号", "表名称", "字段名称", "字段类型", "可空标识", "默认值", "定义版本", "生效批次"]
const actions = ["提交校验", "标记生效", "标记废弃"]
const statuses = ["草稿", "待校验", "已生效", "已废弃"]
const metricNames = ["结构定义总数", "待校验定义", "已生效定义"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const productSchema = ref<JsonRow>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
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
    link.download = 'schema_registry.csv'
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
      throw new Error('结构注册动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结构注册操作失败'
  }
}

async function loadProductSchema() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/product-dim/latest`)
    if (!response.ok) throw new Error('商品维度最新版表结构读取失败')
    productSchema.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '商品维度最新版表结构读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('结构定义列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结构注册列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadProductSchema()
})
</script>
