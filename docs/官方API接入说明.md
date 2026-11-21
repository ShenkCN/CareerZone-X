# 官方岗位 API 接入说明（预留）

> 现状：没有平台官方授权，所以默认关闭。  
> 目标：未来拿到 BOSS/猎聘/智联/51JOB/鱼泡任一官方 API 后，替换适配器即可，不再改页面和 AI。

## 1. 已经写好的接口

### 配置（.env）

    OFFICIAL_API_ENABLED=false
    OFFICIAL_API_BASE_URL=
    OFFICIAL_API_KEY=
    OFFICIAL_API_TIMEOUT=30

### 管理员触发

    POST /api/admin/official/collect
    Authorization: Bearer <管理员会话>
    Content-Type: application/json

    {
      "query": "云计算",
      "city": "上海",
      "page": 1,
      "page_size": 20
    }

未配置时返回 503，并明确提示缺少哪几个变量。

## 2. 官方 API 需要返回的统一契约

适配器请求：

    GET <OFFICIAL_API_BASE_URL>/jobs?query=...&city=...&page=1&page_size=20
    Authorization: Bearer <OFFICIAL_API_KEY>

期望返回：

    {
      "items": [
        {
          "source_job_id": "平台职位ID",
          "url": "https://...",
          "title": "云计算工程师",
          "company": "示例公司",
          "city": "上海",
          "salary_text": "15-23K",
          "experience": "1-3年",
          "education": "大专",
          "description": "完整 JD 正文",
          "requirements": "任职要求正文",
          "tags": ["Linux", "Python"]
        }
      ]
    }

也兼容简单数组。

如果没有现成接口，只有导出文件，可以先写一个适配器：

1. 在 backend/services/official_source.py 里新增一个函数；
2. 读取对方的 CSV/JSON；
3. 返回同样字段的 list[dict]；
4. 保持管理员端点不变。

## 3. 未来自动化的正确接法

不要在 FastAPI 里写“自动登录 BOSS + 翻页”。正确做法：

1. 官方 API 提供授权和配额；
2. 写一个定时任务（Linux cron / systemd timer / APScheduler）；
3. 调用同一个适配器；
4. 写入 jobs + import_batches；
5. 触发 AI 初筛；
6. 记录每次 API 调用量，避免超过配额；
7. 失败重试 + 告警。

示例 cron：

    0 */6 * * * cd /opt/careerzone && .venv/bin/python -m scripts.official_sync >> storage/logs/official.log 2>&1

未来可补一个 scripts/official_sync.py 薄脚本，直接调用 fetch_official_jobs + upsert_job。

## 4. 接入后前端会怎样

- 职位来源会显示“官方/平台”；
- 用户可在职位板筛选来源；
- 现有 AI 初筛、两轮分析、收藏、历史全部复用；
- 不需要改 Streamlit 页面。

## 5. 安全

- Key 只放 .env，不写代码、不返回前端；
- 官方 API 有配额时，定时任务要限速；
- 只存公开职位字段，不存候选人隐私；
- 管理员端点仍要求管理员会话。

## 6. 现实提醒

官方 API 通常需要企业资质/合作，个人不一定能申请。  
在拿到授权前，系统仍只做浏览器扩展的“用户点击/浏览采集”，不做服务端自动爬取。
