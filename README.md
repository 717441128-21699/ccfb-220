# 报关单 RPA 自动录入仿真系统

本地全仿真的「单一窗口」报关录入 RPA，**不接触任何真实网站**。Python + Playwright 实现，
包含：仿真申报网站、Excel/PDF 解析校验、录入机器人、管理后台、100 单测试数据生成与端到端核对。

## 架构

```
datagen/      测试单据生成：100 单（80 干净 + 20 脏数据，12 种脏数据类型），每单 Excel + 发票 PDF，
              脏单同时生成客户补正件 reissue/
simsite/      仿真单一窗口（Flask :5050）：登录+图形验证码、服务端会话超时、表体分页录入、
              字段联动服务端校验、维护公告弹窗、硬维护 503、提交幂等、申报数据回查 API
parser_service/ Excel/PDF 双源解析 -> 统一报关单结构 -> 交叉核对 + 归一化 + 代码映射 + 联动校验，
              缺字段/格式错误/两源不一致一律拦截（status=invalid）并开异常工单
common/       共享库：SQLite 数据层、代码表（币制/单位/HS/港口/贸易方式…）、验证码引擎（本地渲染+本地OCR）、
              两端共用的归一化与联动校验规则
rpa/          Playwright 机器人：验证码 OCR 登录、SQLite 原子抢锁、分页录入、草稿断点续录、
              掉线重登/维护等待、提交幂等、每步截图、提交后回查逐字段核对出报告
admin/        管理后台（Flask :5051）：任务列表 / 单据预览 / 运行监控 / 异常处理 / 设置 五个页面
run_demo.py   一键端到端编排，最终断言 100 单全部完成且核对差异为 0
```

## 快速开始

```powershell
py -m pip install -r requirements.txt
py -m playwright install chromium
py bootstrap.py --reset          # 建库、灌代码表、建 3 个机器人账号
py run_demo.py                   # 全流程演示（服务常驻，浏览器开 http://127.0.0.1:5051）
py run_demo.py --exit-when-done  # 跑完断言后自动退出（CI 模式）
```

分步手动运行：

```powershell
py simsite\app.py                 # 仿真网站   :5050
py admin\app.py                   # 管理后台   :5051（监控页可按账号启停机器人、注入故障）
py -m datagen.generator           # 生成 100 单
py -m tests.test_pipeline         # 解析/校验：应拦截 20 脏单、80 干净单入队
py run_worker.py --worker-id worker-rpa01 --account rpa01 --password Rpa@2026a --headless
```

## 关键设计如何对应需求

| 需求 | 实现 |
| --- | --- |
| Excel/PDF 解析成统一结构，缺字段/格式错先拦下 | `parser_service`：双源解析 + 交叉核对 + `common/validation.py` 联动校验，invalid 不进队列 |
| 商品编码/币制/计量单位代码表映射 | `common/codes.py`：代码直传校验、中文名称/别名归一化为代码 |
| 掉线/会话过期后断点续录、不重复申报 | 网站端草稿持久化 + `task_progress` 记录页级断点；重登后从下一页续录；提交按 ref_no 幂等，重复提交返回原回执 |
| 提交后回查逐字段核对、差异出报告 | `rpa/reconcile.py`：回查 API 拉网站数据与原单逐字段比对，报告写 `reconcile_reports` 和 `data/reports/final_report.json` |
| 多账号并发、同一单不被抢 | `rpa/lock.py`：单条 `UPDATE ... WHERE status='queued'` 原子领取，3 个 worker 并发零冲突 |
| 每步截图、失败单人工改完重跑 | 每步 PNG 入 `data/screenshots/` 并登记；后台预览页支持人工改单重校验入队、客户补正件一键重解析、失败单重新入队 |
| 图形验证码 | `common/captcha.py`：PIL 渲染（旋转/噪点/干扰线），机器人侧纯图像 OCR（中值滤波+连通域分割+numpy 模板匹配），不读答案 |
| 会话超时/维护公告 | 后台「设置」可调超时秒数；监控页可注入会话过期、软公告、硬维护，机器人自动恢复 |

## 脏数据类型（12 种）

必填缺失、信用代码格式错、日期非法/未来日期、币制不存在、计量单位与 HS 法定单位不一致、
总价联动错、表头总金额汇总错、总数量汇总错、港口国别联动错、PDF/Excel 金额冲突、
数值列文本、HS 编码不存在。

## 数据与账号

- SQLite：`data/rpa.db`；截图：`data/screenshots/`；日志：`data/logs/`；报告：`data/reports/`
- 机器人账号：`rpa01/Rpa@2026a`、`rpa02/Rpa@2026b`、`rpa03/Rpa@2026c`
- 所有服务仅监听 127.0.0.1，全部数据为本地模拟数据。
