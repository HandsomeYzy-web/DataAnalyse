<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue';
import {
  CircleAlert,
  CircleCheck,
  Clock3,
  Database,
  Download,
  Eye,
  FileSpreadsheet,
  FileText,
  GitBranch,
  ListTree,
  Loader2,
  RefreshCw,
  RotateCcw,
  Search,
  TableProperties,
  UploadCloud,
} from 'lucide-vue-next';

type IngestResult = {
  table_id: string;
  filename: string;
  normalized_filename: string;
  sheet_name: string;
  table_title?: string;
  summary_text: string;
  candidate_fields: string[];
  minio_objects: MinioObjects;
  indexed: boolean;
  indexed_vector: boolean;
  tree?: Record<string, unknown>;
  links?: {
    summary: string;
    tree: string;
    source: string;
    normalized: string;
  };
};

type JobStatus = {
  job_id: string;
  table_id: string;
  filename: string;
  sheet_name?: string | null;
  status: 'queued' | 'running' | 'completed' | 'failed';
  celery_state?: string;
  step?: string | null;
  submitted_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  result?: IngestResult | null;
  queue_size: number;
  source_object?: string;
};

type UploadRecord = JobStatus & {
  file?: File;
};

type MinioObjects = {
  source_object?: string;
  xlsx_object?: string;
  tree_object?: string;
};

type TableSummary = {
  table_id: string;
  filename?: string;
  normalized_filename?: string;
  source_extension?: string;
  sheet_name?: string;
  table_title?: string;
  summary_text?: string;
  candidate_fields?: string[];
  created_at?: string;
  indexed?: boolean;
  indexed_vector?: boolean;
  minio_objects?: MinioObjects;
};

type TableTreeResponse = {
  table_id: string;
  filename?: string;
  sheet_name?: string;
  table_title?: string;
  tree: Record<string, unknown>;
  tree_with_cell_refs: Record<string, unknown>;
};

type PipelineAnswerResponse = {
  answer: string;
  mode: string;
  table_candidates: Array<{
    score?: number;
    table_id: string;
    filename: string;
    sheet_name: string;
    table_title?: string;
    tree_object: string;
  }>;
  table_answers: unknown[];
};

type TreeRow = {
  id: string;
  depth: number;
  name: string;
  value: string;
  type: 'branch' | 'leaf';
};

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || '';

const fileInput = ref<HTMLInputElement | null>(null);
const selectedFiles = ref<File[]>([]);
const sheetName = ref('');
const uploadRecords = ref<UploadRecord[]>([]);
const uploadedTables = ref<TableSummary[]>([]);
const selectedTableId = ref('');
const selectedJobId = ref('');
const tableDetail = ref<TableSummary | null>(null);
const treeResult = ref<TableTreeResponse | null>(null);
const question = ref('');
const answerResult = ref<PipelineAnswerResponse | null>(null);
const uploadError = ref('');
const listError = ref('');
const detailError = ref('');
const answerError = ref('');
const isSubmitting = ref(false);
const isLoadingTables = ref(false);
const isLoadingJobs = ref(false);
const isLoadingDetail = ref(false);
const isAsking = ref(false);
const batchProgress = ref<{ done: number; total: number } | null>(null);
const pollTimers = new Map<string, number>();

const selectedFileLabel = computed(() => {
  if (!selectedFiles.value.length) {
    return '未选择文件';
  }
  return selectedFiles.value.length === 1 ? selectedFiles.value[0].name : `已选择 ${selectedFiles.value.length} 个文件`;
});
const selectedFileMeta = computed(() => {
  if (!selectedFiles.value.length) {
    return '';
  }
  const totalSize = selectedFiles.value.reduce((sum, file) => sum + file.size, 0);
  return selectedFiles.value.length === 1 ? formatFileSize(totalSize) : `总计 ${formatFileSize(totalSize)}`;
});
const selectedFilePreview = computed(() => selectedFiles.value.slice(0, 5));
const selectedFileOverflow = computed(() => Math.max(selectedFiles.value.length - selectedFilePreview.value.length, 0));
const uploadButtonText = computed(() => {
  if (isSubmitting.value && batchProgress.value) {
    return `提交中 ${batchProgress.value.done}/${batchProgress.value.total}`;
  }
  return selectedFiles.value.length > 1 ? `批量上传 ${selectedFiles.value.length}` : '上传解析';
});
const activeUploadCount = computed(() => uploadRecords.value.filter((item) => isActiveStatus(item.status)).length);
const failedUploadCount = computed(() => uploadRecords.value.filter((item) => item.status === 'failed').length);
const completedUploadCount = computed(() => uploadedTables.value.length);
const selectedRecord = computed(() => uploadRecords.value.find((item) => item.job_id === selectedJobId.value) || null);
const selectedSummary = computed<TableSummary | null>(() => {
  if (tableDetail.value?.table_id === selectedTableId.value) {
    return tableDetail.value;
  }

  const listed = uploadedTables.value.find((item) => item.table_id === selectedTableId.value);
  if (listed) {
    return listed;
  }

  const completedRecord = uploadRecords.value.find((item) => item.table_id === selectedTableId.value && item.result);
  return completedRecord?.result ? resultToSummary(completedRecord.result) : null;
});
const currentTableId = computed(() => selectedSummary.value?.table_id || selectedRecord.value?.table_id || '');
const sourceUrl = computed(() => currentTableId.value ? `${apiBaseUrl}/api/table-pipeline/tables/${currentTableId.value}/source` : '');
const normalizedUrl = computed(() => currentTableId.value ? `${apiBaseUrl}/api/table-pipeline/tables/${currentTableId.value}/normalized` : '');
const treeJson = computed(() => JSON.stringify(treeResult.value?.tree ?? {}, null, 2));
const tableAnswersJson = computed(() => JSON.stringify(answerResult.value?.table_answers ?? [], null, 2));
const visibleTreeRows = computed(() => flattenTree(treeResult.value?.tree ?? {}).slice(0, 360));
const tableAnswersText = computed(() => JSON.stringify(answerResult.value?.table_answers ?? [], null, 2));

onMounted(() => {
  refreshWorkspace();
});

onUnmounted(() => {
  for (const timer of pollTimers.values()) {
    window.clearTimeout(timer);
  }
  pollTimers.clear();
});

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement;
  setSelectedFiles(Array.from(input.files ?? []));
  input.value = '';
}

function openFilePicker() {
  fileInput.value?.click();
}

function onDrop(event: DragEvent) {
  const files = Array.from(event.dataTransfer?.files ?? []);
  if (!files.length) {
    return;
  }
  setSelectedFiles(files);
}

function setSelectedFiles(files: File[]) {
  selectedFiles.value = files;
  uploadError.value = '';
}

async function refreshWorkspace() {
  await Promise.all([loadUploadedTables(), loadRunningJobs()]);
}

async function loadUploadedTables() {
  isLoadingTables.value = true;
  listError.value = '';

  try {
    const payload = await requestJson<{ tables: TableSummary[] }>('/api/table-pipeline/tables?limit=100');
    uploadedTables.value = payload.tables || [];

    if (!selectedTableId.value && uploadedTables.value.length) {
      await selectTable(uploadedTables.value[0]);
    }
  } catch (err) {
    listError.value = errorMessage(err, '加载已上传文件失败');
  } finally {
    isLoadingTables.value = false;
  }
}

async function loadRunningJobs() {
  isLoadingJobs.value = true;

  try {
    const payload = await requestJson<{ jobs: JobStatus[] }>('/api/table-pipeline/jobs?limit=50');
    for (const job of payload.jobs || []) {
      upsertUploadRecord(job);
      if (isActiveStatus(job.status)) {
        startPolling(job.job_id);
      }
    }
  } catch {
    // 上传接口本身也依赖 Celery；这里不覆盖文件列表的错误提示。
  } finally {
    isLoadingJobs.value = false;
  }
}

async function uploadTable() {
  if (!selectedFiles.value.length) {
    uploadError.value = '请先选择 csv、xls、xlsx 或 xlsm 文件。';
    return;
  }

  const files = [...selectedFiles.value];
  const sheet = sheetName.value.trim() || undefined;
  let failedCount = 0;

  isSubmitting.value = true;
  batchProgress.value = { done: 0, total: files.length };
  uploadError.value = '';
  detailError.value = '';

  for (const file of files) {
    const submitted = await submitFile(file, sheet, false, true);
    if (!submitted) {
      failedCount += 1;
    }
    batchProgress.value = {
      done: (batchProgress.value?.done ?? 0) + 1,
      total: files.length,
    };
  }

  if (failedCount) {
    uploadError.value = `${failedCount} 个文件提交失败，已在上传任务中标记失败原因。`;
  }

  batchProgress.value = null;
  isSubmitting.value = false;
}

async function retryUpload(record: UploadRecord) {
  if (!record.file) {
    selectedJobId.value = record.job_id;
    uploadError.value = '浏览器无法在刷新后保留本地文件，请重新选择同名文件后再上传。';
    return;
  }

  await submitFile(record.file, record.sheet_name || undefined, true);
}

async function submitFile(file: File, sheet?: string, isRetry = false, controlled = false) {
  const formData = new FormData();
  formData.append('file', file);

  const params = new URLSearchParams();
  if (sheet) {
    params.set('sheet_name', sheet);
  }
  const query = params.toString() ? `?${params.toString()}` : '';

  if (!controlled) {
    isSubmitting.value = true;
  }
  uploadError.value = '';
  detailError.value = '';

  try {
    const payload = await requestJson<JobStatus>(`/api/table-pipeline/upload${query}`, {
      method: 'POST',
      body: formData,
    });
    upsertUploadRecord({ ...payload, file });
    selectedJobId.value = payload.job_id;
    selectedTableId.value = '';
    if (isRetry) {
      selectedFiles.value = [file];
    }
    startPolling(payload.job_id);
    return true;
  } catch (err) {
    const message = errorMessage(err, '提交解析任务失败');
    const failedRecord = createFailedUploadRecord(file, sheet, message);
    upsertUploadRecord(failedRecord);
    selectedJobId.value = failedRecord.job_id;
    selectedTableId.value = '';
    if (!controlled) {
      uploadError.value = message;
    }
    return false;
  } finally {
    if (!controlled) {
      isSubmitting.value = false;
    }
  }
}

function startPolling(jobId: string) {
  if (pollTimers.has(jobId)) {
    return;
  }
  pollJob(jobId);
}

async function pollJob(jobId: string) {
  pollTimers.delete(jobId);

  try {
    const payload = await requestJson<JobStatus>(`/api/table-pipeline/jobs/${jobId}`);
    const previous = uploadRecords.value.find((item) => item.job_id === jobId);
    upsertUploadRecord({ ...payload, file: previous?.file });

    if (payload.status === 'completed') {
      if (payload.result) {
        tableDetail.value = resultToSummary(payload.result);
      }
      await loadUploadedTables();
      await selectTable(resultToSummary(payload.result), payload.table_id);
      return;
    }

    if (payload.status === 'failed') {
      selectedJobId.value = jobId;
      uploadError.value = payload.error || '解析任务失败';
      return;
    }

    const timer = window.setTimeout(() => pollJob(jobId), 1500);
    pollTimers.set(jobId, timer);
  } catch (err) {
    const previous = uploadRecords.value.find((item) => item.job_id === jobId);
    if (previous) {
      upsertUploadRecord({
        ...previous,
        status: 'failed',
        error: errorMessage(err, '查询任务状态失败'),
      });
    }
    uploadError.value = errorMessage(err, '查询任务状态失败');
  }
}

async function selectTable(summary: TableSummary | IngestResult | null, tableIdOverride?: string) {
  const tableId = tableIdOverride || summary?.table_id;
  if (!tableId) {
    return;
  }

  selectedTableId.value = tableId;
  selectedJobId.value = '';
  tableDetail.value = summary ? resultToSummary(summary) : null;
  treeResult.value = null;
  detailError.value = '';
  isLoadingDetail.value = true;

  try {
    const [detail, tree] = await Promise.all([
      requestJson<TableSummary>(`/api/table-pipeline/tables/${tableId}`),
      requestJson<TableTreeResponse>(`/api/table-pipeline/tables/${tableId}/tree`),
    ]);
    tableDetail.value = detail;
    treeResult.value = tree;
  } catch (err) {
    detailError.value = errorMessage(err, '加载表格详情失败');
  } finally {
    isLoadingDetail.value = false;
  }
}

function selectJob(record: UploadRecord) {
  selectedJobId.value = record.job_id;
  selectedTableId.value = record.status === 'completed' ? record.table_id : '';
  if (record.result) {
    tableDetail.value = resultToSummary(record.result);
  }
  if (record.status === 'completed') {
    selectTable(record.result ? resultToSummary(record.result) : null, record.table_id);
  }
}

async function askQuestion() {
  if (!question.value.trim()) {
    answerError.value = '请输入问题。';
    return;
  }

  isAsking.value = true;
  answerError.value = '';
  answerResult.value = null;

  try {
    answerResult.value = await requestJson<PipelineAnswerResponse>('/api/table-pipeline/answer', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        question: question.value.trim(),
        top_k: 3,
        evidence_limit: 12,
        use_llm: true,
      }),
    });
  } catch (err) {
    answerError.value = errorMessage(err, '问答失败');
  } finally {
    isAsking.value = false;
  }
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, init);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload?.detail === 'string' ? payload.detail : '';
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return payload as T;
}

function upsertUploadRecord(record: UploadRecord) {
  const index = uploadRecords.value.findIndex((item) => item.job_id === record.job_id);
  if (index >= 0) {
    uploadRecords.value[index] = { ...uploadRecords.value[index], ...record };
    return;
  }
  uploadRecords.value.unshift(record);
}

function createFailedUploadRecord(file: File, sheet: string | undefined, error: string): UploadRecord {
  const id = `local-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const now = new Date().toISOString();
  return {
    job_id: id,
    table_id: id,
    filename: file.name,
    sheet_name: sheet || null,
    status: 'failed',
    submitted_at: now,
    started_at: null,
    finished_at: now,
    error,
    result: null,
    queue_size: 0,
    file,
  };
}

function resultToSummary(result: IngestResult | TableSummary | null): TableSummary | null {
  if (!result) {
    return null;
  }

  return {
    table_id: result.table_id,
    filename: result.filename,
    normalized_filename: result.normalized_filename,
    source_extension: 'source_extension' in result ? result.source_extension : undefined,
    sheet_name: result.sheet_name,
    table_title: result.table_title,
    summary_text: result.summary_text,
    candidate_fields: result.candidate_fields,
    indexed: result.indexed,
    indexed_vector: 'indexed_vector' in result ? result.indexed_vector : undefined,
    minio_objects: result.minio_objects,
    created_at: 'created_at' in result ? result.created_at : undefined,
  };
}

function flattenTree(value: unknown, depth = 0, name = '根', path = 'root'): TreeRow[] {
  if (value === null || typeof value !== 'object') {
    return [{
      id: path,
      depth,
      name,
      value: formatTreeValue(value),
      type: 'leaf',
    }];
  }

  const entries = Array.isArray(value)
    ? value.map((item, index) => [String(index), item] as const)
    : Object.entries(value as Record<string, unknown>);
  const rows: TreeRow[] = [{
    id: path,
    depth,
    name,
    value: `${entries.length} 项`,
    type: 'branch',
  }];

  for (const [key, item] of entries) {
    rows.push(...flattenTree(item, depth + 1, key, `${path}.${key}`));
  }
  return rows;
}

function formatTreeValue(value: unknown) {
  if (value === null || value === undefined) {
    return '空';
  }
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return JSON.stringify(value);
}

function statusLabel(status: UploadRecord['status']) {
  const labels = {
    queued: '排队中',
    running: '解析中',
    completed: '已完成',
    failed: '失败',
  };
  return labels[status] || status;
}

function statusIcon(status: UploadRecord['status']) {
  if (status === 'completed') {
    return CircleCheck;
  }
  if (status === 'failed') {
    return CircleAlert;
  }
  return Clock3;
}

function isActiveStatus(status: string) {
  return status === 'queued' || status === 'running';
}

function formatDate(value?: string | null) {
  if (!value) {
    return '-';
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString('zh-CN', {
    hour12: false,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatFileSize(bytes: number) {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function errorMessage(err: unknown, fallback: string) {
  return err instanceof Error && err.message ? err.message : fallback;
}
</script>

<template>
  <main class="single-app">
    <section class="topbar">
      <div class="brand">
        <Database :size="28" />
        <div>
          <h1>表格上传工作台</h1>
          <p>上传、解析、入库、查看树和摘要集中在同一个界面。</p>
        </div>
      </div>
      <div class="status-strip">
        <span>已入库 {{ completedUploadCount }}</span>
        <span>处理中 {{ activeUploadCount }}</span>
        <span>失败 {{ failedUploadCount }}</span>
      </div>
    </section>

    <section class="workspace-grid">
      <div class="left-rail">
        <section
          class="upload-panel"
          @dragover.prevent
          @drop.prevent="onDrop"
        >
          <div class="panel-title">
            <span>上传文件</span>
            <button class="icon-button" title="刷新" :disabled="isLoadingTables || isLoadingJobs" @click="refreshWorkspace">
              <RefreshCw :class="{ spin: isLoadingTables || isLoadingJobs }" :size="17" />
            </button>
          </div>
          <button class="drop-zone" type="button" @click="openFilePicker">
            <UploadCloud :size="30" />
            <span>{{ selectedFileLabel }}</span>
            <small>{{ selectedFileMeta || 'csv / xls / xlsx / xlsm' }}</small>
          </button>
          <input
            ref="fileInput"
            class="file-input"
            accept=".csv,.xls,.xlsx,.xlsm"
            type="file"
            multiple
            @change="onFileChange"
          />
          <div v-if="selectedFilePreview.length" class="selected-files">
            <div v-for="file in selectedFilePreview" :key="`${file.name}-${file.size}-${file.lastModified}`">
              <FileText :size="14" />
              <span>{{ file.name }}</span>
              <small>{{ formatFileSize(file.size) }}</small>
            </div>
            <div v-if="selectedFileOverflow" class="more-files">
              还有 {{ selectedFileOverflow }} 个文件
            </div>
          </div>
          <div class="upload-controls">
            <input
              v-model="sheetName"
              class="sheet-input"
              placeholder="Sheet 名称"
            />
            <button class="button primary" :disabled="isSubmitting || !selectedFiles.length" @click="uploadTable">
              <Loader2 v-if="isSubmitting" class="spin" :size="18" />
              <FileSpreadsheet v-else :size="18" />
              {{ uploadButtonText }}
            </button>
          </div>
          <p v-if="uploadError" class="error-message compact-error">{{ uploadError }}</p>
        </section>

        <section class="panel">
          <div class="panel-title">
            <span>上传任务</span>
            <span class="muted-text">{{ uploadRecords.length }}</span>
          </div>
          <div v-if="uploadRecords.length" class="job-list">
            <button
              v-for="record in uploadRecords"
              :key="record.job_id"
              class="job-row"
              :class="{ selected: selectedJobId === record.job_id }"
              type="button"
              @click="selectJob(record)"
            >
              <component :is="statusIcon(record.status)" :size="18" />
              <span class="job-main">
                <strong>{{ record.filename || record.table_id }}</strong>
                <small>{{ statusLabel(record.status) }} · {{ record.step || record.celery_state || 'pipeline' }}</small>
              </span>
              <RotateCcw
                v-if="record.status === 'failed'"
                class="retry-inline"
                :size="17"
                @click.stop="retryUpload(record)"
              />
            </button>
          </div>
          <div v-else class="empty compact">暂无上传任务</div>
        </section>

        <section class="panel">
          <div class="panel-title">
            <span>已上传文件</span>
            <Loader2 v-if="isLoadingTables" class="spin muted-icon" :size="17" />
          </div>
          <p v-if="listError" class="error-message compact-error">{{ listError }}</p>
          <div v-if="uploadedTables.length" class="file-list">
            <button
              v-for="table in uploadedTables"
              :key="table.table_id"
              class="file-row"
              :class="{ selected: selectedTableId === table.table_id }"
              type="button"
              @click="selectTable(table)"
            >
              <FileText :size="18" />
              <span>
                <strong>{{ table.table_title || table.filename }}</strong>
                <small>{{ table.filename }} · {{ table.sheet_name || '-' }}</small>
              </span>
            </button>
          </div>
          <div v-else-if="!isLoadingTables" class="empty compact">暂无已入库文件</div>
        </section>
      </div>

      <div class="content-stack">
        <section v-if="selectedRecord && selectedRecord.status === 'failed'" class="failure-panel">
          <div class="failure-head">
            <CircleAlert :size="20" />
            <div>
              <strong>{{ selectedRecord.filename || selectedRecord.table_id }}</strong>
              <span>上传失败</span>
            </div>
          </div>
          <pre>{{ selectedRecord.error || '解析任务失败' }}</pre>
          <button class="button danger" :disabled="isSubmitting" @click="retryUpload(selectedRecord)">
            <RotateCcw :size="17" />
            重新上传
          </button>
        </section>

        <section v-if="selectedSummary || selectedRecord" class="summary-grid">
          <div class="panel">
            <div class="panel-title">
              <span>文件信息</span>
              <span v-if="selectedRecord" class="status-pill" :class="selectedRecord.status">
                {{ statusLabel(selectedRecord.status) }}
              </span>
            </div>
            <div class="status-row">
              <span>文件</span>
              <strong>{{ selectedSummary?.filename || selectedRecord?.filename || '-' }}</strong>
            </div>
            <div class="status-row">
              <span>Table ID</span>
              <strong>{{ currentTableId || '-' }}</strong>
            </div>
            <div class="status-row">
              <span>Sheet</span>
              <strong>{{ selectedSummary?.sheet_name || selectedRecord?.sheet_name || '-' }}</strong>
            </div>
            <div class="status-row" v-if="selectedSummary?.table_title">
              <span>标题</span>
              <strong>{{ selectedSummary.table_title }}</strong>
            </div>
            <div class="status-row">
              <span>创建时间</span>
              <strong>{{ formatDate(selectedSummary?.created_at || selectedRecord?.submitted_at) }}</strong>
            </div>
          </div>

          <div class="panel">
            <div class="panel-title">
              <span>结果文件</span>
              <Loader2 v-if="isLoadingDetail" class="spin muted-icon" :size="17" />
            </div>
            <div class="action-row">
              <a v-if="currentTableId" class="button secondary" :href="sourceUrl">
                <Download :size="16" />
                源文件
              </a>
              <a v-if="currentTableId" class="button secondary" :href="normalizedUrl">
                <Download :size="16" />
                xlsx
              </a>
              <a
                v-if="currentTableId"
                class="button secondary"
                :href="`${apiBaseUrl}/api/table-pipeline/tables/${currentTableId}/tree`"
                target="_blank"
              >
                <Eye :size="16" />
                JSON
              </a>
            </div>
            <pre v-if="selectedSummary?.minio_objects" class="object-snippet">{{ JSON.stringify(selectedSummary.minio_objects, null, 2) }}</pre>
          </div>
        </section>

        <p v-if="detailError" class="error-message">{{ detailError }}</p>

        <section v-if="selectedSummary?.summary_text" class="preview">
          <div class="section-title">
            <span><TableProperties :size="18" /> 表格摘要</span>
          </div>
          <pre class="summary-text">{{ selectedSummary.summary_text }}</pre>
        </section>

        <section v-if="treeResult" class="preview">
          <div class="section-title">
            <span><ListTree :size="18" /> 构建树</span>
            <span class="muted-text">{{ visibleTreeRows.length }} 节点</span>
          </div>
          <div class="tree-list">
            <div
              v-for="row in visibleTreeRows"
              :key="row.id"
              class="tree-row"
              :class="row.type"
              :style="{ paddingLeft: `${row.depth * 18 + 10}px` }"
            >
              <GitBranch v-if="row.type === 'branch'" :size="15" />
              <span class="tree-name">{{ row.name }}</span>
              <span class="tree-value">{{ row.value }}</span>
            </div>
          </div>
          <details class="json-details">
            <summary>原始 JSON</summary>
            <pre class="tree-json">{{ treeJson }}</pre>
          </details>
        </section>

        <section v-if="!selectedSummary && !selectedRecord" class="empty">
          选择左侧文件或上传任务
        </section>

        <section class="qa-panel">
          <div class="section-title">
            <span><Search :size="18" /> 全局表格问答</span>
          </div>
          <div class="question-row">
            <input
              v-model="question"
              placeholder="输入问题"
              @keyup.enter="askQuestion"
            />
            <button class="button primary" :disabled="isAsking" @click="askQuestion">
              <Loader2 v-if="isAsking" class="spin" :size="18" />
              <Search v-else :size="18" />
              检索
            </button>
          </div>
          <p v-if="answerError" class="error-message compact-error">{{ answerError }}</p>
          <div v-if="answerResult" class="answer">
            <div class="answer-main">{{ answerResult.answer }}</div>
            <div class="answer-meta">
              <span>模式：{{ answerResult.mode }}</span>
              <span>候选表：{{ answerResult.table_candidates.length }}</span>
            </div>
          </div>
        </section>

        <section v-if="answerResult" class="split">
          <div class="preview">
            <div class="section-title">ES 候选表</div>
            <pre class="tree-json">{{ JSON.stringify(answerResult.table_candidates, null, 2) }}</pre>
          </div>
          <div class="preview">
            <div class="section-title">各表树问答结果</div>
            <pre class="tree-json">{{ tableAnswersText }}</pre>
          </div>
        </section>
      </div>
    </section>
  </main>
</template>
