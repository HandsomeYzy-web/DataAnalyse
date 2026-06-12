<script setup lang="ts">
import { computed, ref } from 'vue';
import { FileSpreadsheet, Loader2, Send, UploadCloud } from 'lucide-vue-next';

type Coverage = {
  data_rows: number;
  value_columns: number;
  expected_cells: number;
  covered_cells: number;
  missing_cells: string[];
  skipped_rows: number[];
  is_complete: boolean;
};

type ParseResponse = {
  filename: string;
  sheet_name: string;
  mode: string;
  raw_plan_output: string;
  parse_plan: Record<string, unknown>;
  validation_warnings: string[];
  coverage: Coverage;
  tree_with_cell_refs: Record<string, unknown>;
  tree: Record<string, unknown>;
};

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || '';

const selectedFile = ref<File | null>(null);
const sheetName = ref('');
const result = ref<ParseResponse | null>(null);
const error = ref('');
const isLoading = ref(false);

const selectedFileLabel = computed(() => selectedFile.value?.name || '未选择文件');
const planJson = computed(() => JSON.stringify(result.value?.parse_plan ?? {}, null, 2));
const treeJson = computed(() => JSON.stringify(result.value?.tree ?? {}, null, 2));
const refsJson = computed(() => JSON.stringify(result.value?.tree_with_cell_refs ?? {}, null, 2));

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  selectedFile.value = input.files?.[0] ?? null;
  error.value = '';
}

async function parseTable() {
  if (!selectedFile.value) {
    error.value = '请先选择一个 .xlsx 或 .xlsm 文件。';
    return;
  }

  const formData = new FormData();
  formData.append('file', selectedFile.value);

  const params = new URLSearchParams();
  if (sheetName.value.trim()) {
    params.set('sheet_name', sheetName.value.trim());
  }
  const query = params.toString() ? `?${params.toString()}` : '';

  isLoading.value = true;
  error.value = '';
  result.value = null;

  try {
    const response = await fetch(`${apiBaseUrl}/api/table-parse-plan/parse${query}`, {
      method: 'POST',
      body: formData,
    });

    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || '解析失败');
    }

    result.value = payload;
  } catch (err) {
    error.value = err instanceof Error ? err.message : '解析失败';
  } finally {
    isLoading.value = false;
  }
}
</script>

<template>
  <main class="single-app">
    <section class="topbar">
      <div class="brand">
        <FileSpreadsheet :size="28" />
        <div>
          <h1>解析计划验证</h1>
          <p>LLM 只生成 TableParsePlan，程序按坐标确定性构建语义树。</p>
        </div>
      </div>
      <div class="status-strip">
        <span>接口：/api/table-parse-plan/parse</span>
        <span>模式：plan</span>
      </div>
    </section>

    <section class="upload-band">
      <label class="upload-action">
        <UploadCloud :size="18" />
        选择 Excel
        <input accept=".xlsx,.xlsm" type="file" @change="onFileChange" />
      </label>
      <input
        v-model="sheetName"
        class="sheet-input"
        placeholder="Sheet 名称，可留空使用第一个 sheet"
      />
      <button class="button primary" :disabled="isLoading || !selectedFile" @click="parseTable">
        <Loader2 v-if="isLoading" class="spin" :size="18" />
        <Send v-else :size="18" />
        开始解析
      </button>
      <p class="notice">{{ selectedFileLabel }}</p>
    </section>

    <p v-if="error" class="error-message">{{ error }}</p>

    <section v-if="result" class="summary-grid">
      <div class="panel">
        <div class="panel-title">解析结果</div>
        <div class="status-row">
          <span>文件</span>
          <strong>{{ result.filename }}</strong>
        </div>
        <div class="status-row">
          <span>Sheet</span>
          <strong>{{ result.sheet_name }}</strong>
        </div>
        <div class="status-row">
          <span>模式</span>
          <strong>{{ result.mode }}</strong>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">覆盖率</div>
        <div class="metric-row">
          <span>数据行：{{ result.coverage.data_rows }}</span>
          <span>值列：{{ result.coverage.value_columns }}</span>
          <span>应覆盖：{{ result.coverage.expected_cells }}</span>
          <span>已覆盖：{{ result.coverage.covered_cells }}</span>
          <span>{{ result.coverage.is_complete ? '完整' : '不完整' }}</span>
        </div>
        <p v-if="result.validation_warnings.length">
          {{ result.validation_warnings.join('；') }}
        </p>
      </div>
    </section>

    <section v-if="result" class="split">
      <div class="preview">
        <div class="section-title">解析计划</div>
        <pre class="tree-json">{{ planJson }}</pre>
      </div>
      <div class="preview">
        <div class="section-title">模型原始计划输出</div>
        <pre class="tree-json">{{ result.raw_plan_output }}</pre>
      </div>
    </section>

    <section v-if="result" class="split">
      <div class="preview">
        <div class="section-title">最终语义树</div>
        <pre class="tree-json">{{ treeJson }}</pre>
      </div>
      <div class="preview">
        <div class="section-title">单元格引用树</div>
        <pre class="tree-json">{{ refsJson }}</pre>
      </div>
    </section>

    <section v-if="result && (!result.coverage.is_complete || result.coverage.skipped_rows.length)" class="preview">
      <div class="section-title">覆盖异常</div>
      <pre>{{ JSON.stringify({
        missing_cells: result.coverage.missing_cells,
        skipped_rows: result.coverage.skipped_rows,
      }, null, 2) }}</pre>
    </section>

    <section v-if="!result && !isLoading" class="empty">
      选择 Excel 文件后点击开始解析。
    </section>
  </main>
</template>
