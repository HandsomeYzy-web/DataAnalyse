# 前端实际使用的后端路由说明

本文档只解释当前前端会发起请求的后端路由。

当前入口是 `frontend/src/main.ts`：

```ts
import App from './App.vue';
createApp(App).mount('#app');
```

因此主界面实际运行的是 `frontend/src/App.vue`。`frontend/src/parse.vue` 也包含一个接口调用，但当前没有被 `main.ts` 挂载，也没有配置 `vue-router`，所以它不是当前主应用实际运行路径的一部分。本文最后单独说明这个未挂载页面涉及的后端路由。

开发环境中，`frontend/vite.config.ts` 将 `/api` 代理到后端：

```ts
proxy: {
  '/api': process.env.VITE_DEV_PROXY_TARGET || 'http://localhost:8000'
}
```

后端统一在 `backend/app/main.py` 中给路由加上 `/api` 前缀：

```py
app.include_router(table_pipeline_router, prefix="/api")
```

所以 `backend/app/api/table_pipeline.py` 中声明的 `prefix="/table-pipeline"`，最终会变成前端访问的 `/api/table-pipeline/...`。

## 总览

`App.vue` 当前真正使用的后端路由如下：

| 前端行为 | 方法与路径 | 后端函数 |
| --- | --- | --- |
| 加载已上传文件列表 | `GET /api/table-pipeline/tables?limit=100` | `list_table_summaries` |
| 加载运行中/排队任务 | `GET /api/table-pipeline/jobs?limit=50` | `list_pipeline_jobs` |
| 上传文件并提交解析任务 | `POST /api/table-pipeline/upload?sheet_name=...` | `upload_table_to_pipeline` |
| 轮询单个任务状态 | `GET /api/table-pipeline/jobs/{job_id}` | `get_pipeline_job` |
| 加载表格摘要 | `GET /api/table-pipeline/tables/{table_id}` | `get_table_summary` |
| 加载表格语义树 | `GET /api/table-pipeline/tables/{table_id}/tree` | `get_table_tree` |
| 下载原始文件 | `GET /api/table-pipeline/tables/{table_id}/source` | `download_table_source` |
| 下载标准化 xlsx | `GET /api/table-pipeline/tables/{table_id}/normalized` | `download_table_normalized_xlsx` |
| 全局表格问答 | `POST /api/table-pipeline/answer` | `answer_from_pipeline` |

`table_pipeline.py` 中还有这些路由，但当前 `App.vue` 没有调用：

- `GET /api/table-pipeline/jobs/by-table/{table_id}`
- `GET /api/table-pipeline/tables/{table_id}/artifact`

其他 API 文件中的这些路由当前主前端也没有调用：

- `POST /api/table2tree/enhanced`
- `POST /api/table-qa/answer`
- `POST /api/table-parse-plan/parse`，仅 `parse.vue` 中写了调用，但当前未挂载

## 公共代码结构

### 路由层

文件：`backend/app/api/table_pipeline.py`

这个文件只负责 HTTP 层：

- 定义请求路径、方法、参数校验。
- 通过 `Depends(get_settings)` 注入配置。
- 创建服务类，例如 `TablePipelineService(settings)` 或 `TablePipelineQueue(settings)`。
- 捕获 Elasticsearch、MinIO、Celery 和通用异常，并转成前端能读懂的 HTTP 错误。

关键公共对象：

```py
router = APIRouter(prefix="/table-pipeline", tags=["table-pipeline"])
```

由于 `main.py` 额外加了 `/api`，最终完整前缀是 `/api/table-pipeline`。

```py
ELASTICSEARCH_ERRORS = (
    ApiError,
    ElasticsearchConnectionError,
    SerializationError,
    TransportError,
    UnsupportedProductError,
)
```

这些异常统一被转换成 `502`，表示后端服务本身可用，但下游 Elasticsearch 出问题。

### 队列层

文件：`backend/app/services/table_pipeline_queue.py`

上传文件不是同步解析，而是：

1. FastAPI 接收文件。
2. 先把原始文件写入 MinIO。
3. 提交 Celery 任务 `table_pipeline.ingest`。
4. 前端拿到 `job_id` 后轮询任务状态。

这样上传接口能快速返回 `202 Accepted`，避免大表解析时 HTTP 请求长时间阻塞。

### 任务层

文件：`backend/app/tasks/table_pipeline_tasks.py`

Celery worker 实际执行解析入库：

1. 根据 `source_object` 从 MinIO 读取原始文件。
2. 调用 `TablePipelineService.ingest_table(...)`。
3. 返回去掉大体积 `tree` 字段的解析结果。
4. 给结果补充几个链接，例如 summary、tree、source、normalized。

### 服务层

文件：`backend/app/services/table_pipeline.py`

这是核心业务逻辑：

- `ingest_table`：解析文件、构建语义树、保存 MinIO、写入 Elasticsearch。
- `list_table_summaries`：列出 Elasticsearch 中已入库的表。
- `get_table_summary`：读取某张表的摘要。
- `get_table_tree`：读取某张表的语义树。
- `get_table_file`：下载原始文件或标准化 xlsx。
- `answer_question`：跨表检索、表内问答、最终汇总。

### 存储层

文件：`backend/app/services/table_object_storage.py`

负责 MinIO：

- 保存原始文件。
- 保存标准化后的 xlsx。
- 保存解析产物 `tree.json`。
- 读取 JSON 或 bytes。

### 检索层

文件：`backend/app/services/table_search_index.py`

负责 Elasticsearch：

- `index_table`：写入表格摘要和树相关索引字段。
- `list_tables`：按创建时间列出表。
- `get_table_document`：按 `table_id` 读取索引文档。
- `search`：问答时先做向量检索，失败或无 embedding 时走文本检索。

## 1. GET /api/table-pipeline/tables

### 前端调用

位置：`frontend/src/App.vue` 的 `loadUploadedTables()`

```ts
requestJson<{ tables: TableSummary[] }>('/api/table-pipeline/tables?limit=100')
```

用途：

- 首次进入页面时加载已入库表格。
- 上传任务完成后刷新列表。
- 左侧“已上传文件”列表使用该结果。

### 后端入口

文件：`backend/app/api/table_pipeline.py`

```py
@router.get("/tables")
def list_table_summaries(
    limit: int = Query(default=50, ge=1, le=200),
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return {"tables": service.list_table_summaries(limit=limit)}
    except ELASTICSEARCH_ERRORS as exc:
        ...
```

参数：

- `limit`：查询数量，默认 50，最小 1，最大 200。前端传 100。

返回：

```json
{
  "tables": [
    {
      "table_id": "...",
      "filename": "...",
      "normalized_filename": "...",
      "source_extension": ".xlsx",
      "sheet_name": "...",
      "table_title": "...",
      "summary_text": "...",
      "candidate_fields": [],
      "tree_metric_names": [],
      "created_at": "...",
      "indexed": true,
      "minio_objects": {
        "source_object": "...",
        "xlsx_object": "...",
        "tree_object": "..."
      }
    }
  ]
}
```

### 内部逻辑

`TablePipelineService.list_table_summaries(limit)` 调用：

```py
for document in self.index.list_tables(limit=limit):
    tables.append({...})
```

`TableSearchIndex.list_tables(limit)` 做的事：

1. 检查 Elasticsearch index 是否存在。
2. 如果不存在，返回空列表。
3. 如果存在，执行 `match_all`。
4. 按 `created_at desc` 排序。
5. 排除 `summary_vector`，避免大向量返回给前端。
6. 转成普通字典列表。

### 错误处理

如果 Elasticsearch 连接失败、查询失败或版本不兼容，路由返回：

```json
{
  "detail": "Elasticsearch list failed: ..."
}
```

状态码是 `502`。

## 2. GET /api/table-pipeline/jobs

### 前端调用

位置：`frontend/src/App.vue` 的 `loadRunningJobs()`

```ts
requestJson<{ jobs: JobStatus[] }>('/api/table-pipeline/jobs?limit=50')
```

用途：

- 页面加载时恢复当前 Celery 中正在执行、排队或计划中的任务。
- 对 active 任务启动轮询。

### 后端入口

```py
@router.get("/jobs")
def list_pipeline_jobs(
    limit: int = Query(default=50, ge=1, le=200),
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    try:
        return {"jobs": queue.list_jobs(limit=limit)}
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
```

参数：

- `limit`：最多返回多少任务，默认 50。

返回：

```json
{
  "jobs": [
    {
      "job_id": "...",
      "table_id": "...",
      "filename": "...",
      "sheet_name": null,
      "status": "queued",
      "worker": "...",
      "submitted_at": null,
      "started_at": null,
      "finished_at": null,
      "error": null,
      "result": null,
      "queue_size": 0
    }
  ]
}
```

### 内部逻辑

`TablePipelineQueue.list_jobs(limit)` 会：

1. 获取 Celery app。
2. 通过 `celery_app.control.inspect(timeout=1)` 查询 worker。
3. 依次读取：
   - `active`：正在执行
   - `reserved`：已被 worker 预取但还没执行
   - `scheduled`：计划执行
4. 把 Celery 原始任务结构转换成前端 `JobStatus`。

转换函数是 `_inspect_task_to_job`。它会从 task 的 `kwargs` 中取：

- `table_id`
- `filename`
- `sheet_name`

并把状态映射成前端能展示的 `running`、`queued`、`scheduled` 等。

### 错误处理

如果 Celery 没安装或 Celery app 不可用，返回 `503`。前端这里刻意吞掉错误，不覆盖文件列表错误提示。

## 3. POST /api/table-pipeline/upload

### 前端调用

位置：`frontend/src/App.vue` 的 `submitFile()`

```ts
const formData = new FormData();
formData.append('file', file);

requestJson<JobStatus>(`/api/table-pipeline/upload${query}`, {
  method: 'POST',
  body: formData,
})
```

如果用户填写了 Sheet 名称，前端会拼：

```text
/api/table-pipeline/upload?sheet_name=Sheet1
```

用途：

- 上传一个文件。
- 创建一个异步解析任务。
- 前端拿到 `job_id` 后调用 `/jobs/{job_id}` 轮询。

### 后端入口

```py
@router.post("/upload", status_code=202)
async def upload_table_to_pipeline(
    file: UploadFile = File(...),
    sheet_name: str | None = Query(None),
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=400, detail="Uploaded file must have a filename.")

    try:
        content = await file.read()
        return queue.submit(filename=filename, content=content, sheet_name=sheet_name)
    ...
```

请求：

- `multipart/form-data`
- 字段 `file`
- 可选 query 参数 `sheet_name`

返回状态码：

- 成功是 `202 Accepted`，表示任务已提交，不代表解析完成。

返回：

```json
{
  "job_id": "...",
  "table_id": "...",
  "filename": "demo.xlsx",
  "sheet_name": null,
  "status": "queued",
  "submitted_at": "...",
  "started_at": null,
  "finished_at": null,
  "error": null,
  "result": null,
  "queue_size": 0,
  "source_object": "table-artifacts/.../source/demo.xlsx"
}
```

### 内部逻辑

`TablePipelineQueue.submit(...)` 做三件事：

1. 生成 `table_id`：

   ```py
   table_id = uuid4().hex
   ```

2. 把源文件先保存到 MinIO：

   ```py
   source_object = storage.store_source_file(table_id, filename, content)
   ```

3. 发送 Celery 任务：

   ```py
   task = celery_app.send_task(
       "table_pipeline.ingest",
       kwargs={
           "table_id": table_id,
           "filename": filename,
           "source_object": source_object,
           "sheet_name": sheet_name,
       },
       task_id=table_id,
   )
   ```

这里 `task_id=table_id`，所以前端看到的 `job_id` 和 `table_id` 通常相同。

### 后台任务如何真正解析

Celery 任务定义在 `backend/app/tasks/table_pipeline_tasks.py`：

```py
@celery_app.task(name="table_pipeline.ingest", bind=True)
def ingest_table_task(...):
    source_content = storage.get_bytes(source_object)
    service = TablePipelineService(settings)
    result = service.ingest_table(...)
    return _compact_ingest_result(result)
```

`TablePipelineService.ingest_table(...)` 的主要步骤：

1. `convert_table_file_to_xlsx` 标准化文件：
   - `.xlsx`：校验。
   - `.xlsm`：去掉 VBA 后保存成 xlsx。
   - `.csv`：用 pandas 读取并写成 xlsx。
   - `.xls`：用 xlrd 读取并写成 xlsx。

2. 用 openpyxl 打开 xlsx：

   ```py
   workbook = load_workbook(io.BytesIO(converted.xlsx_content), data_only=True)
   ```

3. 根据 `sheet_name` 选择 sheet，未传时用 active sheet。

4. 调用 `LangChainEnhancedTableParser` 解析表格，生成：
   - `table_title`
   - `summary_text`
   - `normalized_headers`
   - `hierarchy_definition`
   - `tree`
   - `tree_with_cell_refs`

5. 给树附加表格名：

   ```py
   tree_with_name = _attach_table_name(parse_result.tree, table_name)
   ```

6. 保存完整 artifact 到 MinIO：
   - 原始文件
   - 标准化 xlsx
   - `tree.json`

7. 调用 `build_table_summary(...)` 构建 Elasticsearch 文档字段。

8. 调用 `self.index.index_table(index_document)` 写入 Elasticsearch。

9. 返回前端需要的摘要信息。

### 错误处理

- 文件名为空：`400`
- Celery 不可用：`503`
- 其他异常：`500`

前端提交失败时会本地创建一个 failed record，用于在上传任务列表里展示失败原因。

## 4. GET /api/table-pipeline/jobs/{job_id}

### 前端调用

位置：`frontend/src/App.vue` 的 `pollJob(jobId)`

```ts
requestJson<JobStatus>(`/api/table-pipeline/jobs/${jobId}`)
```

用途：

- 上传成功后每 1.5 秒轮询任务状态。
- 如果任务完成，前端刷新表列表并自动选中结果表。
- 如果失败，前端展示错误并允许重试。

### 后端入口

```py
@router.get("/jobs/{job_id}")
def get_pipeline_job(
    job_id: str,
    queue: TablePipelineQueue = Depends(get_pipeline_queue),
):
    try:
        return queue.get_job(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}") from exc
```

### 内部逻辑

`TablePipelineQueue.get_job(job_id)`：

1. 通过 Celery 读取任务：

   ```py
   async_result = celery_app.AsyncResult(job_id)
   ```

2. 读取 Celery 状态：

   ```py
   state = async_result.state
   ```

3. 如果任务成功并且结果是字典，取出 `result`。

4. 用 `_map_celery_state` 把 Celery 状态转成前端状态：

   | Celery state | 前端 status |
   | --- | --- |
   | `SUCCESS` | `completed` |
   | `FAILURE`, `REVOKED` | `failed` |
   | `STARTED`, `PROGRESS`, `RETRY` | `running` |
   | 其他 | `queued` |

5. 如果任务失败，用 `_format_error` 转成字符串。

返回结构：

```json
{
  "job_id": "...",
  "table_id": "...",
  "filename": "...",
  "sheet_name": null,
  "status": "completed",
  "celery_state": "SUCCESS",
  "step": null,
  "submitted_at": null,
  "started_at": null,
  "finished_at": "...",
  "error": null,
  "result": {
    "table_id": "...",
    "filename": "...",
    "summary_text": "...",
    "candidate_fields": [],
    "minio_objects": {},
    "links": {}
  },
  "queue_size": 0
}
```

### 前端如何消费

前端看到：

- `completed`：把 `result` 转成 summary，刷新 `/tables`，再请求 `/tables/{table_id}` 和 `/tree`。
- `failed`：展示 `error`。
- 其他状态：继续 `setTimeout` 轮询。

## 5. GET /api/table-pipeline/tables/{table_id}

### 前端调用

位置：`frontend/src/App.vue` 的 `selectTable(...)`

```ts
requestJson<TableSummary>(`/api/table-pipeline/tables/${tableId}`)
```

用途：

- 用户点击左侧已上传文件时加载详情。
- 上传任务完成后自动加载详情。
- 中间“文件信息”和“表格摘要”区域使用该结果。

### 后端入口

```py
@router.get("/tables/{table_id}")
def get_table_summary(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.get_table_summary(table_id)
    except S3Error as exc:
        _raise_minio_http(exc)
    except ELASTICSEARCH_ERRORS as exc:
        ...
```

### 内部逻辑

`TablePipelineService.get_table_summary(table_id)`：

1. 从 MinIO 读取完整 artifact：

   ```py
   artifact = self.get_table_artifact(table_id)
   ```

   `get_table_artifact` 实际读取：

   ```py
   self.storage.get_json(self.storage.tree_object_name(table_id))
   ```

2. 从 Elasticsearch 读取索引文档：

   ```py
   document = self.index.get_table_document(table_id)
   ```

3. 合并 MinIO 和 Elasticsearch 的信息返回。

返回：

```json
{
  "table_id": "...",
  "filename": "...",
  "normalized_filename": "...",
  "source_extension": ".xlsx",
  "sheet_name": "...",
  "table_title": "...",
  "minio_objects": {
    "source_object": "...",
    "xlsx_object": "...",
    "tree_object": "..."
  },
  "summary_text": "...",
  "candidate_fields": [],
  "tree_metric_names": [],
  "indexed": true
}
```

### 错误处理

- MinIO 找不到对象：`404`
- MinIO 其他错误：`502`
- Elasticsearch 错误：`502`

注意：如果 MinIO artifact 存在但 Elasticsearch 文档不存在，接口仍可返回，只是：

```json
{
  "indexed": false,
  "summary_text": null,
  "candidate_fields": []
}
```

## 6. GET /api/table-pipeline/tables/{table_id}/tree

### 前端调用

位置一：`frontend/src/App.vue` 的 `selectTable(...)`

```ts
requestJson<TableTreeResponse>(`/api/table-pipeline/tables/${tableId}/tree`)
```

位置二：页面中“JSON”按钮直接打开同一个 URL：

```vue
:href="`${apiBaseUrl}/api/table-pipeline/tables/${currentTableId}/tree`"
```

用途：

- 加载语义树。
- 渲染“构建树”列表。
- 展示原始 JSON。
- 允许用户新窗口打开 tree JSON。

### 后端入口

```py
@router.get("/tables/{table_id}/tree")
def get_table_tree(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.get_table_tree(table_id)
    except S3Error as exc:
        _raise_minio_http(exc)
```

### 内部逻辑

`TablePipelineService.get_table_tree(table_id)`：

1. 读取 MinIO 中的 `tree.json`。
2. 用 `table_title`、`sheet_name`、`filename` 推断展示用表名。
3. 调用 `_attach_table_name(...)` 确保返回的树里有 `"表格名"` 节点。
4. 返回：

```json
{
  "table_id": "...",
  "filename": "...",
  "sheet_name": "...",
  "table_title": "...",
  "tree": {},
  "tree_with_cell_refs": {}
}
```

`tree` 是值树，叶子节点已经是单元格值。`tree_with_cell_refs` 是引用树，叶子节点可能是 `A10` 这种单元格坐标，用于调试和追踪来源。

## 7. GET /api/table-pipeline/tables/{table_id}/source

### 前端调用

位置：`frontend/src/App.vue` 的 `sourceUrl`

```ts
const sourceUrl = computed(() =>
  currentTableId.value
    ? `${apiBaseUrl}/api/table-pipeline/tables/${currentTableId.value}/source`
    : ''
);
```

用途：

- “源文件”按钮下载用户最初上传的文件。

### 后端入口

```py
@router.get("/tables/{table_id}/source")
def download_table_source(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    return _download_table_file(table_id=table_id, kind="source", settings=settings)
```

### 内部逻辑

`_download_table_file(...)` 调用：

```py
filename, media_type, content = service.get_table_file(table_id, kind=kind)
```

当 `kind == "source"` 时，`get_table_file`：

1. 读取 artifact 中的 `minio_objects.source_object`。
2. 如果 artifact 里没有，则从 Elasticsearch 文档查 `source_object`。
3. 文件名使用 artifact 中的 `filename`，兜底为 `{table_id}-source`。
4. media type 使用 `application/octet-stream`。
5. 从 MinIO 读取 bytes 并返回。

路由层会设置下载响应头：

```py
"Content-Disposition": f"attachment; filename*=UTF-8''{quoted_filename}"
```

这样浏览器会下载文件，并尽量保留中文文件名。

## 8. GET /api/table-pipeline/tables/{table_id}/normalized

### 前端调用

位置：`frontend/src/App.vue` 的 `normalizedUrl`

```ts
const normalizedUrl = computed(() =>
  currentTableId.value
    ? `${apiBaseUrl}/api/table-pipeline/tables/${currentTableId.value}/normalized`
    : ''
);
```

用途：

- “xlsx”按钮下载标准化后的 xlsx 文件。

### 后端入口

```py
@router.get("/tables/{table_id}/normalized")
def download_table_normalized_xlsx(
    table_id: str,
    settings: Settings = Depends(get_settings),
):
    return _download_table_file(table_id=table_id, kind="normalized", settings=settings)
```

### 内部逻辑

和 source 下载共用 `_download_table_file(...)`，区别是 `kind == "normalized"`：

1. 优先读取 artifact 中的 `minio_objects.xlsx_object`。
2. 如果 artifact 里没有，则从 Elasticsearch 文档查 `xlsx_object`。
3. 文件名使用 `normalized_filename`，兜底为 `{table_id}.xlsx`。
4. media type 是：

```text
application/vnd.openxmlformats-officedocument.spreadsheetml.sheet
```

这个文件来自上传解析阶段的 `convert_table_file_to_xlsx(...)`：

- csv 会被 pandas 转成 xlsx。
- xls 会被 pandas/xlrd 转成 xlsx。
- xlsm 会去 VBA 后保存成 xlsx。
- xlsx 会被校验后原样保存。

## 9. POST /api/table-pipeline/answer

### 前端调用

位置：`frontend/src/App.vue` 的 `askQuestion()`

```ts
requestJson<PipelineAnswerResponse>('/api/table-pipeline/answer', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({
    question: question.value.trim(),
    top_k: 3,
    evidence_limit: 12,
    use_llm: true,
  }),
})
```

用途：

- 用户在“全局表格问答”输入问题后，跨所有已索引表格检索并回答。
- 前端展示：
  - `answer`
  - `mode`
  - `table_candidates`
  - `table_answers`

### 请求模型

后端使用 Pydantic 模型：

```py
class PipelineQuestionRequest(BaseModel):
    question: str = Field(min_length=1)
    top_k: int = Field(default=3, ge=1, le=10)
    evidence_limit: int = Field(default=12, ge=1, le=50)
    use_llm: bool = True
```

字段含义：

- `question`：用户问题，不能为空。
- `top_k`：从 Elasticsearch 取多少张候选表，前端传 3。
- `evidence_limit`：每张表内部最多保留多少证据路径，前端传 12。
- `use_llm`：是否使用 LLM 做重排、回答和最终汇总，前端传 true。

### 后端入口

```py
@router.post("/answer")
def answer_from_pipeline(
    request: PipelineQuestionRequest,
    settings: Settings = Depends(get_settings),
):
    try:
        service = TablePipelineService(settings)
        return service.answer_question(
            question=request.question,
            top_k=request.top_k,
            evidence_limit=request.evidence_limit,
            use_llm=request.use_llm,
        )
    ...
```

### 内部逻辑

核心在 `TablePipelineService.answer_question(...)`。

#### 1. 构造 QA 服务和 LLM

```py
qa_service = TableQAService(self.settings)
llm = qa_service.llm if use_llm else None
```

`TableQAService` 会根据配置创建：

- ChatOpenAI，用于候选路径重排和答案生成。
- OpenAI embedding client，用于语义检索。

#### 2. Query 拆分改写

```py
query_plan = _build_query_plan(question, llm)
retrieval_questions = query_plan["sub_questions"]
```

如果有 LLM，会调用 `_rewrite_queries_with_llm(...)`，把复合问题拆成多个完整子问题。例如：

```text
湖南师范大学的博士研究生，普通本科生，硕士研究生有多少
```

会拆成：

```json
[
  "湖南师范大学的博士研究生有多少",
  "湖南师范大学的普通本科生有多少",
  "湖南师范大学的硕士研究生有多少"
]
```

如果 LLM 不可用或返回异常，会用 `_split_question_heuristically(...)` 做规则兜底，支持逗号、顿号、分号以及常见的“和/及/与”并列结构。

#### 3. 并行执行子问题检索

```py
query_results = self._answer_retrieval_questions(...)
```

如果只有一个子问题，直接执行。

如果有多个子问题，使用线程池并行：

```py
with ThreadPoolExecutor(max_workers=worker_count) as executor:
    ...
```

最大并行数是：

```py
MAX_PARALLEL_SUB_QUERIES = 4
```

每个子问题进入 `_answer_single_retrieval_question(...)`。

#### 4. 每个子问题先检索候选表

```py
candidates = self.index.search(retrieval_question, top_k=top_k)
```

`TableSearchIndex.search(...)` 的策略：

1. 如果 Elasticsearch index 不存在，返回空。
2. 如果配置了 embedding，先将 query 转向量，走 KNN 向量检索。
3. 如果向量检索失败或没有 embedding，走 `multi_match` 文本检索。

文本检索字段带权重：

- `tree_metric_names^6`
- `tree_path_text^5`
- `table_title^4`
- `summary_text^3`
- `tree_leaf_text^3`
- `filename^2`
- `sheet_name^2`
- `candidate_fields^2`
- `normalized_headers`
- `hierarchy_definition`

#### 5. 每张候选表读取树并做表内问答

对每张候选表：

```py
artifact = self.storage.get_json(candidate["tree_object"])
qa_result = service.answer(
    question=retrieval_question,
    tree=artifact.get("tree") or {},
    metadata=metadata,
    limit=evidence_limit,
    use_llm=use_llm,
)
```

`TableQAService.answer(...)` 做：

1. `flatten_tree(...)` 把语义树展开成很多 `TreeRecord`。
2. 检索相关路径：
   - 有 embedding：语义向量检索。
   - 无 embedding 但有 LLM：让 LLM 从候选路径中选 ID。
   - 都没有：规则词法检索。
3. 如果有 LLM，再做 rerank。
4. 最后生成答案：
   - 有 LLM：根据 evidence_paths 生成自然语言答案。
   - 无 LLM：返回“找到以下相关路径”。

#### 6. 去重候选表

多个子问题可能命中同一张表，所以会调用：

```py
table_candidates = _dedupe_query_candidates(query_results)
```

去重键优先用：

1. `table_id`
2. `tree_object`
3. `question:index`

如果同一张表被多个子问题命中，会保留更高 score，并在 `matched_queries` 里记录命中过哪些子问题。

#### 7. 汇总最终答案

```py
answer = _synthesize_pipeline_answer(
    question=question,
    table_answers=table_answers,
    llm=llm,
)
```

汇总前会先压缩表内答案：

- 有 evidence_paths 且答案不是“当前证据不足”的结果会被保留。
- 如果某个子问题没有答案，也会保留一个“未回答子问题”的占位，避免最终答案漏项。

如果有 LLM，最终 prompt 要求：

- 只根据 `table_answers` 和 `evidence_paths`。
- 不使用外部知识。
- 多表互补时合并回答。
- 子问题存在时逐个覆盖，再汇总原始问题。

如果没有 LLM，则按列表返回每张表/子问题的结果。

### 返回结构

当前服务返回字段包含：

```json
{
  "answer": "...",
  "mode": "es_minio_tree_qa",
  "original_question": "...",
  "retrieval_question": "...",
  "retrieval_questions": ["..."],
  "query_plan": {
    "original_question": "...",
    "sub_questions": ["..."],
    "rewritten": true,
    "source": "llm"
  },
  "query_results": [
    {
      "question": "...",
      "table_candidates": [],
      "table_answer_count": 1,
      "answerable_count": 1
    }
  ],
  "table_candidates": [
    {
      "score": 1.23,
      "table_id": "...",
      "filename": "...",
      "sheet_name": "...",
      "table_title": "...",
      "tree_object": "...",
      "tree_metric_names": [],
      "matched_queries": []
    }
  ],
  "table_answers": []
}
```

前端当前 TypeScript 类型只声明了旧字段：

```ts
answer
mode
table_candidates
table_answers
```

新增的 `query_plan`、`retrieval_questions`、`query_results` 不会影响前端，因为 TypeScript 的结构类型允许响应对象包含额外字段。

### 错误处理

- Elasticsearch 检索失败：`502`
- MinIO 读取失败：`502`
- 其他问答异常：`500`

如果没有检索到候选表，业务上返回成功响应：

```json
{
  "answer": "当前未检索到相关表格，无法回答问题。",
  "mode": "pipeline_no_table_candidates",
  "table_candidates": [],
  "table_answers": []
}
```

## 当前未挂载页面：POST /api/table-parse-plan/parse

`frontend/src/parse.vue` 中有这个调用：

```ts
fetch(`${apiBaseUrl}/api/table-parse-plan/parse${query}`, {
  method: 'POST',
  body: formData,
})
```

但当前 `frontend/src/main.ts` 没有挂载 `parse.vue`，项目也没有配置 `vue-router`，所以主应用运行时不会触发这个接口。除非开发者临时把入口换成 `parse.vue`，或者后续加路由页面。

### 后端入口

文件：`backend/app/api/table_parse_plan.py`

```py
@router.post("/parse")
async def parse_table_with_plan(
    file: UploadFile = File(...),
    sheet_name: str | None = Query(None),
    settings: Settings = Depends(get_settings),
):
    ...
```

最终路径是：

```text
POST /api/table-parse-plan/parse
```

请求：

- `multipart/form-data`
- 字段 `file`
- 可选 query 参数 `sheet_name`

文件限制：

```py
if not filename.lower().endswith((".xlsx", ".xlsm")):
    raise HTTPException(
        status_code=400,
        detail="Plan-based table parsing currently supports .xlsx and .xlsm files.",
    )
```

这个接口只支持 `.xlsx` 和 `.xlsm`，不同于主上传接口支持 `.csv`、`.xls`、`.xlsx`、`.xlsm`。

### 内部逻辑

1. 读取上传文件。
2. 用 openpyxl 打开 workbook。
3. 选择指定 sheet 或 active sheet。
4. 创建 `PlanBasedTableParser(settings)`。
5. 调用 `parser.parse_sheet(sheet)`。

`PlanBasedTableParser` 的逻辑在 `backend/app/services/table_parse_plan.py`：

1. `extract_sheet_grid(sheet)` 把工作表转成包含单元格坐标、值、合并信息、样式线索的 JSON。
2. `generate_parse_plan(grid)` 调用 LLM，让模型只输出 `TableParsePlan`，不直接生成最终树。
3. `parse_table_parse_plan(raw_plan_output)` 从 LLM 输出中抽取 JSON 并用 Pydantic 校验。
4. `normalize_parse_plan(...)` 对明显缺失的层级列做修复。
5. `validate_parse_plan(...)` 检查范围、列、row_paths 等是否越界或缺失。
6. `build_tree_from_plan(...)` 按坐标确定性构建：
   - `tree_with_cell_refs`
   - `tree`
   - `coverage`

返回：

```json
{
  "filename": "...",
  "sheet_name": "...",
  "mode": "plan",
  "raw_plan_output": "...",
  "parse_plan": {},
  "validation_warnings": [],
  "coverage": {
    "data_rows": 0,
    "value_columns": 0,
    "expected_cells": 0,
    "covered_cells": 0,
    "missing_cells": [],
    "skipped_rows": [],
    "is_complete": true
  },
  "tree_with_cell_refs": {},
  "tree": {}
}
```

### 错误处理

- 非 `.xlsx`/`.xlsm`：`400`
- sheet 不存在：`404`
- LLM 超时：`504`
- LLM 返回非法 JSON 或不符合 `TableParsePlan`：`422`
- 其他解析错误：`500`

## 前端没有使用的后端路由

以下路由虽然存在，但当前主前端不会发请求：

### GET /api/table-pipeline/jobs/by-table/{table_id}

按 `table_id` 查任务状态。当前前端轮询用的是 `job_id`，而当前实现里 `job_id` 通常等于 `table_id`，所以没用这个路由。

### GET /api/table-pipeline/tables/{table_id}/artifact

返回 MinIO 中完整 artifact。当前前端只需要摘要和树，所以分别调用：

- `/tables/{table_id}`
- `/tables/{table_id}/tree`

### POST /api/table2tree/enhanced

旧的或独立的表格转树接口，当前主前端没有调用。

### POST /api/table-qa/answer

独立单表问答接口，当前主前端使用的是 pipeline 全局问答：

- `/api/table-pipeline/answer`

### GET /health

健康检查接口，当前前端没有调用。

## 当前主链路总结

主工作台的后端链路可以概括为：

```text
页面加载
  -> GET /api/table-pipeline/tables
  -> GET /api/table-pipeline/jobs

上传文件
  -> POST /api/table-pipeline/upload
  -> MinIO 保存源文件
  -> Celery table_pipeline.ingest
  -> TablePipelineService.ingest_table
  -> 转 xlsx / 解析语义树 / 保存 MinIO / 写 Elasticsearch
  -> 前端轮询 GET /api/table-pipeline/jobs/{job_id}

查看表格
  -> GET /api/table-pipeline/tables/{table_id}
  -> GET /api/table-pipeline/tables/{table_id}/tree
  -> GET /api/table-pipeline/tables/{table_id}/source
  -> GET /api/table-pipeline/tables/{table_id}/normalized

问答
  -> POST /api/table-pipeline/answer
  -> query 拆分改写
  -> 并行 Elasticsearch 检索候选表
  -> MinIO 读取候选表语义树
  -> TableQAService 表内证据检索与回答
  -> 跨表汇总最终答案
```
