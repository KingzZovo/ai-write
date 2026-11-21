# 服务器备份与迁移演练

日期2026-10-06；本机代码a46823c。生产未升级、未启动应用。

## 服务器实际状态

通过SSH重新读取141.148.185.96：/root/ai-write HEAD为72ee915，Alembic a1001920，872章。AI-WRITE PostgreSQL在运行，应用/Worker未运行。docker-compose.yml有作者修改，另有未跟踪文件，均未重置或清理。其他项目容器未触碰。

## 备份

服务器目录 `/root/aiwrite-backups/20261006-pre-repair-9932918`（创建时umask077）存放：

- database.dump：pg_dump -Fc，约668 MiB；SHA256 `052b875c438ec02953072b3376a328daf0dcce0a5301c9c0175a580b22c581e9`。
- worktree.tar.gz：完整/root/ai-write，约196 MiB；SHA256 `ce874557f1b72af0a8a362d92cad68801a87350229823144dbd5c2b6e9395e99`。
- SHA256SUMS：两文件重新校验通过。tar目录可读取，共27029条目，包含.git/HEAD、.env、docker-compose.yml；未将环境或密钥内容写入公开日志。

数据库副本传到本机 `/root/.aiwrite-backups/20261006-pre-repair-9932918/database.dump`，权限0600、父目录0700；传输后独立SHA256匹配。仓库归档随后传至本机并独立校验、解压成功：HEAD仍为72ee915，git状态中的Compose修改及未跟踪项一致；.env与Compose的SHA256逐项对比源服务器一致。PostgreSQL逻辑备份不包含Neo4j/Qdrant数据卷或Redis状态；Qdrant另用原生快照备份如下，旧图存储仍需盘点。

## 隔离数据库恢复与升级

在本机测试PG容器内新建aiwrite_restore_20261006，以pg_restore --no-owner --no-acl --exit-on-error恢复成功；没有覆盖原测试数据库或服务器原库。恢复后a1001920、872章与服务器核对一致。角色/ACL未还原，不能称作完整生产权限恢复。

升级前读取66张原有业务表、519852行数据，对原始列构造每行MD5、排序后以SHA256汇总，计入重复行数量；原文、端点密钥等行内容不导出到报告。指纹工具与私有逐表结果位于本机备份目录fingerprint.py、columns.json、before.json。

使用当前代码 Alembic upgrade head，a1001921至a1001928全部成功执行。随后按Base.metadata核对已注册模型：缺失表/列结果为空。仅验证表和列存在，不代表索引、约束、数据语义或所有运行功能均已验收。

首次升级后66表519852行的原始列指纹与升级前完全一致，872章保留。新增outbox、invalidations、extractions均为空，迁移未自动排入历史模型任务。随后在同一无新任务数据的隔离副本执行downgrade a1001920成功（872章），再upgrade head成功，第二次指纹比对仍为66表519852行完全一致。此演练不证明可以无损删除升级后新产生的任务/来源记录，生产回退应结合停写窗口及备份恢复。还需旧数据来源重建、任务与Prompt初始化、真实SSE/Worker/Beat/浏览器链路及文学质量；本报告不意味着可以直接宣称整体上线完成。

服务器后续存储盘点：本项目当前仅PG容器存在，Docker卷列有ai-write_postgres_data和ai-write_qdrant_data；已核实临时Qdrant挂载ai-write_qdrant_data（2.4 GiB），版本1.19.0，仅本机16333端口；Neo4j环境目标为内部主机neo4j，但当前无对应容器/命名卷，旧图存储状态仍需进一步确认。


## Qdrant原生快照

通过本机16333的POST /snapshots创建全存储快照，随后从GET /snapshots/{name}下载到同一服务器备份目录qdrant.snapshot，未停止tmp_qdrant或修改集合。文件2483176448字节；流式下载SHA256为 `825d6ee33033fa89631c092acade0c5f9b3300d85cfab3da6e705d35a42b43fd`，已追加SHA256SUMS，原始响应存qdrant-snapshot.json。随后独立sha256sum校验通过，并完成隔离恢复与全量验证，见下。

本批数据库演练无需模型或外网服务：原始数据指纹在PG内计算，逐表汇总留在受限备份目录；迁移日志同目录保存。代码应用、Worker/Beat与浏览器未启动到服务器。下一步优先完成向量恢复、确认旧图数据是否存在及生产Compose差异，再进行部署配置/任务种子和真实运行验收。


## Qdrant恢复验收（已完成）

在服务器新建aiwrite-qdrant-restore-20261006，使用原容器同一镜像摘要、只读挂载快照、独立新存储目录及本机16433端口；原16333实例及原数据卷不改。容器限制4GiB内存/2CPU并关闭遥测。使用二进制--storage-snapshot恢复成功，未使用强制覆盖参数。

逐个比较原实例与恢复实例的59个集合：配置相同，精确点数合计228201；分页scroll读取全部ID/payload/vector，对规范JSON作长度分隔SHA256累计，两侧完整数据指纹一致。别名均为0。45个非空集合各取一个向量执行exact=true前5检索，返回分数在1e-5容差内一致。耗时714.169秒，摘要见qdrant-restore-20261006.json，完整逐集合报告与校验脚本保留在受限服务器备份目录。验收不是仅比较集合数量，也不将检索检查等同文学质量或高并发SLA。

当前还保留恢复容器及副本，以便后续隔离应用验证。向量快照已传至本机受限备份目录，独立SHA256与服务器快照一致；数据库、代码目录、Qdrant快照三类异机副本均已校验。旧Neo4j盘点：/root及/opt/1panel搜索、全部Docker容器挂载/命名与匿名卷归属均未发现本项目图数据；本机aiwrite-volumes目录为空，.codex-tmp-aiwrite仅临时源码。不能据此证明所有磁盘或历史备份都不存在旧图，启动新图前需明确历史来源重建策略。


## 镜像构建与单一应用源码修复

实际在aarch64服务器构建bb24600后端/前端镜像成功。复现仓库AGENTS所述应用副本问题：旧镜像以/tmp为工作目录时，app.__file__为/usr/local/lib/python3.11/site-packages/app/__init__.py，有1份site-packages应用副本。

修复backend/Dockerfile：builder从pyproject.toml读取依赖并安装到/install，不再把项目本身安装成wheel；builder不复制应用源码，源码变更可复用依赖层；runtime只复制/app/app，并设置PYTHONPATH=/app。未改变项目声明的依赖列表或生产连接配置。

aarch64候选镜像aiwrite-backend:packaging-candidate真实构建成功（标注bb24600-packaging-candidate，不冒充最终提交）。在--network none、工作目录/tmp下导入app.main及app.tasks通过，app.__file__=/app/app/__init__.py，site-packages副本数0，34个Celery任务已注册且含entities.replay_pending。pip check返回No broken requirements found。前端生产镜像含.next/BUILD_ID，实际npm build完成。

本地按shadow-mind无可信只读隔离的回退复核：依赖完整来自project.dependencies，未引入新锁版本或删依赖；console scripts随/install保留；源码只有runtime一份，不再依靠操作者临时pip uninstall；Python heredoc AST和git diff --check通过。此次仅构建文件更改，不重跑全部业务测试，实际容器验证是本批针对性检查。尚未验证完整在线请求/SSE/任务消费；这些仍是部署前待办。

发布准备目录/root/ai-write-releases/bb24600与packaging-candidate独立于旧工作目录，复制原.env但不打印；Compose覆盖保留PG本机15433端口、去掉backend主机8000映射及restart设置，新增控制Worker/Beat沿用重启策略。Qdrant固定已验证镜像摘要，Neo4j候选固定5.26-community。compose config --quiet通过。未执行up、未迁移原库、未接管原Qdrant卷。最终发布目录及镜像需在提交后用准确版本重新标记，不能直接把候选目录当作上线完成。
