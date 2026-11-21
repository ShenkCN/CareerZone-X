# CareerZone X

> 本地优先的个人 AI 求职工作台。它不替你“黑”招聘网站，而是把你**已经有权看到的职位页面**变成结构化数据，再用 DeepSeek 做初筛与两轮匹配分析。

## 现在能做什么

- 账号注册/审核/登录/忘记密码（管理员审批，Argon2id 哈希）
- 多用户隔离：职位库共享；A 清空后无副本用户也隐藏，B 自己采过的保留；管理员可按批次恢复
- 管理员账号管理：注册/最后登录、采集/收藏/浏览/活跃度、每批采集记录与恢复申请
- 管理员固定账号 + 密保问答二次验证
- 个人求职画像、PDF/Word 简历解析、历史版本回档
- 浏览器扩展**用户点击后**采集当前招聘页面；不做自动翻页、不绕验证码
- 职位板单页最多 1000 条、来源标记、城市/薪资/类型/经验/学历/行业/规模筛选；支持一键清空筛选
- AI 批量初筛（40 条/组、3 路并发、每页最多 300 条）+ 仅完整 JD 才允许的两轮深度分析
- 收藏、浏览历史、浏览次数、换一批低重复、采集重复率与按用户清空/恢复
- 管理员审批、Token/请求统计、反馈回复、CSV/ZIP 导出、系统文件与磁盘页
- 登录页 3 个本地小游戏：接鱼 20 秒、猜数字 10 次、贪吃蛇
- 预留官方 API 适配器与定时同步文档
- 测试、中文产品/技术/安全/部署/面试文档

## 10 分钟启动（Windows）

### 0. 当前默认：SQLite 兜底，MySQL 可切

当前 .env 使用 SQLite（storage/careerzone.db），原因是这台机器的 WSL MySQL
被环境里的某个进程每约 20 秒停止/重启一次，直接用会间歇性 500。
MySQL 代码路径已建库并跑通登录/采集/AI，连接串备份在 .env.mysql。

切回 MySQL（确认 WSL MySQL 稳定后）：

    wsl -u root -d Ubuntu-22.04 service mysql start
    python -m scripts.switch_db mysql
    python -m scripts.check_env

再切回 SQLite：

    python -m scripts.switch_db sqlite

### 1. 安装依赖

    cd "D:\CareerZone X"
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -U pip
    pip install -r requirements-dev.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

RAG 可选（Chroma；默认使用 hash-384，避免本机 DLL 拦截/模型下载卡住）。
需要 bge 中文语义模型时，再装 sentence-transformers，并把 .env 的 VECTOR_TRY_MODEL 改成 true：

    pip install -r requirements-rag.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

### 2. 建表 + 写入管理员

    python -m scripts.init_db

首次凭据在 storage/private/initial_credentials.txt。保存到密码管理器后删除该文件。

### 3. 启动

    .\start.ps1

- 用户端（日常只用这个）：http://127.0.0.1:8501
- 健康检查：http://127.0.0.1:8000/health
- 开发者 API 文档（日常不用）：http://127.0.0.1:8000/api/docs

## 浏览器采集（合规方式）

1. 本机不需要令牌：设置 → 浏览器采集 → 下载扩展 ZIP。
2. Edge 打开 edge://extensions，开启“开发人员模式”，加载解压后的 careerzone-extension 文件夹（旧版需重新加载 0.4.0）。
   - 可开启“浏览时自动补全当前可见 JD”；扩展不会自动滚动/翻页。
3. 扩展设置里填 API 地址 http://127.0.0.1:8000；令牌框已删除。
4. 你手动打开 BOSS/猎聘/智联/51JOB/鱼泡的搜索或详情页，点扩展“采集当前页”。

**边界**：扩展不会自动登录、自动翻页、破解验证码或高频请求；平台条款仍可能限制自动化采集。详见 docs/安全与合规.md。

## 文档

- [产品说明书](docs/产品说明书.md)
- [技术选型书](docs/技术选型书.md)
- [安全与合规](docs/安全与合规.md)
- [部署手册](docs/部署手册.md)
- [验收报告](docs/验收报告.md)
- [面试讲解稿](docs/面试讲解稿.md)
- [Demo 录制脚本](docs/Demo录制脚本.md)
- [技术架构书](docs/技术架构书.md)
- [官方 API 接入说明](docs/官方API接入说明.md)
- [华为云部署指南](docs/华为云部署指南.md)
- [从零部署到另一台电脑](docs/从零部署到另一台电脑.md)

## 目录

    backend/    FastAPI、SQLAlchemy、LLM、RAG、业务服务
    frontend/   Streamlit 用户端/管理员端
    extension/  Edge/Chrome 手动采集扩展
    scripts/    建库、体检、启动辅助
    tests/      接口与安全测试
    docs/       产品/技术/安全/部署/面试文档
    storage/    本机数据（已 gitignore）

## 安全提示

- 默认只监听 127.0.0.1，不要直接把 8000/8501 暴露公网。
- DeepSeek API Key 曾在本会话明文出现，建议立即到控制台吊销并换新，只写入 .env。
- 管理员密码固定不可改；当前是用户指定的弱密码，只可本机使用，不要公网。
- 项目暂停时已移除登录自启动；恢复自启用 `scripts/install_autostart.ps1`，移除用 `scripts/remove_autostart.ps1`。
- 换电脑打包：`scripts/package_release.ps1` 生成 `dist/CareerZoneX-src.zip`（不含 .env/数据库）。
- 个人自用项目，未授权商用。第三方招聘网站的数据版权归原平台；使用者需遵守平台条款与当地法律。
