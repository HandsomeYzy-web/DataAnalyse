# ASTRA 表格问答工程化原型

项目目标：用户上传一张 Excel 表格后，系统使用 ASTRA 风格的复杂表格解析方法构建语义树，再基于该表格进行问答。

## 核心链路

```text
上传 Excel
  -> MinIO 保存原始文件
  -> openpyxl / pandas 解析 sheet
  -> ASTRA-style table-to-tree
  -> MySQL 保存结构化表、语义树、树节点
  -> Elasticsearch 索引表、字段、行块、树节点向量
  -> 用户基于当前 table_id 提问
  -> Elasticsearch 检索证据路径
  -> ASTRA 树导航
  -> 规则符号计算或配置的 LLM 生成最终答案
```

## 技术栈

- 后端：FastAPI
- 前端：Vue + Vite
- 数据库：MySQL
- 对象存储：MinIO
- 向量/关键词检索：Elasticsearch
- 模型平台：OpenAI-compatible LLM / Embedding API，可指向 OpenAI、DeepSeek、Qwen、vLLM、Xinference 等

## 启动

```bash
docker compose up --build
```

服务地址：

- 前端：http://localhost:5173
- 后端 API：http://localhost:8000/docs
- Elasticsearch：http://localhost:9200
- MySQL：localhost:3306
- MinIO 控制台：http://localhost:9001

## 使用流程

1. 打开前端。
2. 上传一个 `.xlsx` 或 `.xlsm` 文件。
3. 系统解析第一张可用表，生成 ASTRA 语义树。
4. 查看表格预览和树结构。
5. 在输入框中提问。

示例问题：

```text
这张表主要有哪些指标？
学生总数是多少？
哪个项目数量最多？
按相关路径找出校区对应的数据
```

## 模型平台配置

默认可以离线运行：

- LLM：关闭
- Embedding：hash fallback

如需启用模型平台，在 `.env` 中配置：

```env
ENABLE_LLM_ANSWER=true
ENABLE_LLM_TABLE_PARSE=true
ENABLE_SYMBOLIC_CODE_QA=true
ASTRA_PARSE_MODE=enhanced
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=https://api.example.com/v1
LLM_API_KEY=your-key
LLM_MODEL=your-chat-model

EMBEDDING_PROVIDER=openai_compatible
EMBEDDING_BASE_URL=https://api.example.com/v1
EMBEDDING_API_KEY=your-key
EMBEDDING_MODEL=your-embedding-model
EMBEDDING_DIMENSIONS=384
```

如果 embedding 模型实际维度和 `EMBEDDING_DIMENSIONS` 不一致，当前服务会截断或补零以适配 Elasticsearch mapping。生产环境建议保持二者一致并重建索引。

## ASTRA 工程化映射

- `backend/app/services/astra_tree.py`：ASTRA 风格表格转语义树。
- `backend/app/services/astra_parser.py`：ASTRA 三段式 prompt 表格解析，失败时规则兜底。
- `backend/app/services/astra_prompts.py`：ASTRA 解析/问答/符号代码 prompt。
- `backend/app/services/astra_qa.py`：树节点检索、树导航、符号计算、可选 LLM 回答。
- `backend/app/services/astra_symbolic.py`：ASTRA symbolic code QA 和安全执行。
- `backend/app/services/model_platform.py`：LLM 和 Embedding 模型平台。
- `backend/app/services/mysql_store.py`：MySQL 表、树、节点持久化。
- `backend/app/services/minio_store.py`：原始 Excel 对象存储。
- `backend/app/services/elasticsearch_store.py`：树节点向量和关键词检索。

## 镜像源

`python` 和 `node` 基础镜像默认已使用 AWS Public ECR 的 Docker Hub 同步源。如果仍然超时，可以通过环境变量换成你能访问的镜像源：

```powershell
$env:PYTHON_BASE_IMAGE="public.ecr.aws/docker/library/python:3.12-slim"
$env:NODE_BASE_IMAGE="public.ecr.aws/docker/library/node:22-alpine"
$env:MYSQL_IMAGE="docker.m.daocloud.io/library/mysql:8.4"
$env:MINIO_IMAGE="docker.m.daocloud.io/minio/minio:RELEASE.2025-04-22T22-12-26Z"
docker compose up --build
```

## 当前边界

当前是工程化原型，不是完整复现 ASTRA 论文效果：

- 表格转树目前使用确定性规则，后续可接入 LLM 生成 row/column hierarchy。
- 问答默认使用规则符号计算，启用 LLM 后会基于证据路径生成最终答案。
- 复杂 sheet 中一个 sheet 多张表、极复杂合并单元格还需要继续增强。
